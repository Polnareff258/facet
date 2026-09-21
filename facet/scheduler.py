"""采集调度器：一轮 = 取清单 → 多源并发采集 → 落库 → 告警 → 通知。

并发策略：不同源之间并发（互不干扰），同一源内部串行（限速闸门在源内共享）。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Sequence

from .alerts import AlertEngine
from .config import Config
from .mapping import MappingService
from .models import AlertEvent, FetchReport, ItemRef, SourceQuote, iso, utcnow
from .names import NameResolver, learn_from_quotes
from .notify import Notifier
from .ratelimit import GateRegistry
from .sources import SourceAdapter, build_sources
from .store import Store

logger = logging.getLogger(__name__)


@dataclass
class CycleResult:
    """一轮采集的完整结果，供 CLI / 看板展示。"""

    started_at: str
    finished_at: str | None = None
    watched: int = 0
    quotes: int = 0
    reports: list[FetchReport] = field(default_factory=list)
    alerts: list[AlertEvent] = field(default_factory=list)
    notify_results: list[tuple[AlertEvent, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        if not self.finished_at:
            return 0.0
        from datetime import datetime
        return (datetime.fromisoformat(self.finished_at)
                - datetime.fromisoformat(self.started_at)).total_seconds()

    def summary(self) -> str:
        parts = [f"监控 {self.watched} 个饰品", f"采到 {self.quotes} 条报价",
                 f"告警 {len(self.alerts)} 条", f"耗时 {self.duration:.1f}s"]
        failed = [r for r in self.reports if r.failed]
        if failed:
            parts.append("失败源: " + ", ".join(
                f"{r.source}({r.failed})" for r in failed))
        return " | ".join(parts)


class Monitor:
    """编排采集全流程。"""

    def __init__(self, config: Config, store: Store | None = None) -> None:
        self.config = config
        self.store = store or Store(config.database)
        self.gates = GateRegistry()
        self.mapping = MappingService(self.store)
        self.names = NameResolver(self.store)
        self.engine = AlertEngine(self.store)
        self.notifier = Notifier(config.notify)
        self._sources: list[SourceAdapter] | None = None
        self._stop = threading.Event()
        self._extreme: object | None = None

    # ── 装配 ───────────────────────────────────────────────

    @property
    def sources(self) -> list[SourceAdapter]:
        if self._sources is None:
            self._sources = build_sources(self.config, self.gates)
            for adapter in self._sources:
                logger.info("已装配源：%s（平台 %s，在售=%s 求购=%s）",
                            adapter.name, ",".join(adapter.platforms),
                            adapter.provides_sell, adapter.provides_bid)
        return self._sources

    def seed_watchlist(self) -> int:
        """把 YAML 里的清单灌进 DB（幂等，不覆盖已有规则）。"""
        existing = {r.market_hash_name for r in self.store.list_watch()}
        added = 0
        for rule in self.config.watchlist:
            if rule.market_hash_name in existing:
                continue
            self.store.upsert_watch(rule, source="yaml")
            self.store.upsert_item(ItemRef(market_hash_name=rule.market_hash_name))
            added += 1
        if added:
            logger.info("从配置导入 %d 条监控规则", added)
        return added

    # ── 一轮 ───────────────────────────────────────────────

    def run_once(self, only_sources: set[str] | None = None) -> CycleResult:
        started = utcnow()
        result = CycleResult(started_at=iso(started))

        rules = self.store.list_watch()
        result.watched = len(rules)
        if not rules:
            result.notes.append("监控清单为空：先用 `python -m facet watch add <名称>` 添加")
            result.finished_at = iso()
            return result

        names = [r.market_hash_name for r in rules]
        refs = self.mapping.to_item_refs(names)

        missing = self.mapping.unresolved(names)
        if missing["buff_goods_id"]:
            result.notes.append(
                f"{len(missing['buff_goods_id'])} 个饰品缺 BUFF goods_id"
                f"（buff_direct 会跳过；用 `python -m facet index resolve` 补齐）")
        if missing["youpin_template_id"]:
            result.notes.append(
                f"{len(missing['youpin_template_id'])} 个饰品缺悠悠有品 templateId"
                f"（youpin_direct 会跳过）")

        adapters = [s for s in self.sources
                    if not only_sources or s.name in only_sources]
        all_quotes: list[SourceQuote] = []

        if adapters:
            with ThreadPoolExecutor(max_workers=min(4, len(adapters)),
                                    thread_name_prefix="facet-fetch") as pool:
                futures = {pool.submit(a.fetch_report, refs): a for a in adapters}
                for future in as_completed(futures):
                    adapter = futures[future]
                    try:
                        quotes, report = future.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("[%s] 采集线程崩溃", adapter.name)
                        report = FetchReport(source=adapter.name,
                                             requested=len(refs), failed=len(refs),
                                             errors=[str(exc)]).finish()
                        quotes = []
                    result.reports.append(report)
                    all_quotes.extend(quotes)
                    self.store.log_fetch(
                        source=report.source, requested=report.requested,
                        succeeded=report.succeeded, failed=report.failed,
                        quotes=report.quotes, started_at=iso(report.started_at),
                        finished_at=iso(report.finished_at) if report.finished_at else None,
                        errors=report.errors)

        if all_quotes:
            result.quotes = self.store.insert_quotes(all_quotes)
            # 顺手回收中文名：CSQAQ 与 BUFF 的响应里都带中文名，采集时已经
            # 落在 SourceQuote.raw 里。这样攒中文名不需要额外请求，
            # 也就不会额外消耗限额或触发风控。
            try:
                learned = learn_from_quotes(self.names, all_quotes)
                if learned:
                    logger.info("本轮收录 %d 条中文名", learned)
            except Exception:  # noqa: BLE001 — 取名失败不能影响采集
                logger.exception("中文名收录失败（不影响采集）")

        events = self.engine.evaluate(all_quotes, rules)
        if events:
            saved = self.engine.persist(events)
            result.alerts = [ev for _, ev in saved]
            logger.info("产生 %d 条告警", len(saved))

            if self.config.notify.enabled:
                for alert_id, event in saved:
                    outcome = self.notifier.send([event])[0][1]
                    self.store.mark_alert_notified(alert_id, outcome)
                    result.notify_results.append((event, outcome))
            else:
                for alert_id, _ in saved:
                    self.store.mark_alert_notified(alert_id, "notify_disabled")

        result.finished_at = iso()
        logger.info("本轮完成：%s", result.summary())
        return result

    # ── 常驻循环 ───────────────────────────────────────────

    def start_extreme(self) -> int:
        """启动极致追踪线程（若配置了任务）。返回任务数。"""
        from .extreme import ExtremeTracker

        tasks = self.store.list_extreme(enabled_only=True)
        if not tasks:
            return 0
        tracker = ExtremeTracker(self.store, self.sources, self.engine,
                                 self.notifier, self.gates)
        tracker.start()
        self._extreme = tracker
        return len(tasks)

    def stop_extreme(self) -> None:
        tracker = self._extreme
        if tracker is not None:
            tracker.stop()          # type: ignore[attr-defined]

    @property
    def extreme_snapshot(self) -> dict[str, Any]:
        """极致追踪状态。

        关键点：追踪器未运行时也要如实列出**已配置**的任务。
        否则用 serve 模式（只起看板）或采集还没进入常驻循环时，
        看板会显示「没有追踪任务」，而用户明明配了——这种「界面撒谎」
        比功能缺失更难排查。
        """
        tracker = self._extreme
        if tracker is not None:
            snapshot = tracker.snapshot()   # type: ignore[attr-defined]
            if snapshot.get("task_count"):
                return snapshot

        configured = self.store.list_extreme(enabled_only=False)
        return {
            "running": bool(tracker is not None and getattr(
                tracker, "_thread", None) and tracker._thread.is_alive()),  # noqa: SLF001
            "task_count": len(configured),
            "stats": {},
            "tasks": [{
                "market_hash_name": r["market_hash_name"],
                "platform": r["platform"],
                "interval_seconds": r["interval_seconds"],
                "current_interval": r["interval_seconds"],
                "ticks": 0,
                "last_error": "",
                "last_price": None,
                "last_count": None,
                "quiet": False,
            } for r in configured],
        }

    def run_forever(self, interval: int | None = None) -> None:
        period = interval or self.config.poll_interval
        logger.info("启动常驻监控，间隔 %d 秒（Ctrl+C 退出）", period)
        started = self.start_extreme()
        if started:
            logger.info("极致追踪：%d 个任务已并行启动", started)
        while not self._stop.is_set():
            began = time.monotonic()
            try:
                self.run_once()
            except Exception:  # noqa: BLE001
                logger.exception("本轮采集异常，继续下一轮")
            elapsed = time.monotonic() - began
            sleep_for = max(5.0, period - elapsed)
            logger.info("休眠 %.0f 秒", sleep_for)
            self._stop.wait(sleep_for)

    def stop(self) -> None:
        self._stop.set()

    # ── 资源 ───────────────────────────────────────────────

    def close(self) -> None:
        self.stop_extreme()
        for adapter in self._sources or []:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass
        self.notifier.close()
        self.store.close()

    def __enter__(self) -> Monitor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
