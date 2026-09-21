"""极致追踪：单件高频轮询，捕捉普通轮询看不到的瞬时变动。

与普通监控的区别：
  · 普通监控是「一批饰品每 30 分钟看一次」——看趋势
  · 极致追踪是「一个饰品每 10-60 秒看一次」——抓瞬时机会（如突然被扫货）

从 cs-monitor 的极致追踪模式移植，并保留了它最值得抄的一点：
**命中限流自我降频**。高频轮询型任务最容易在数据源限流时把对方惹毛，
所以这里把「退避」做成任务级状态（间隔翻倍、上限 1 小时、成功后逐步恢复），
而不是简单重试。

顺带的取舍：极致追踪的采样写入独立的 extreme_samples 表，不污染主报价表
——否则高频采样的密度会把普通监控的基准价计算带偏。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .alerts import AlertEngine
from .models import AlertEvent, ItemRef, utcnow
from .notify import Notifier
from .ratelimit import GateRegistry, RateLimitExceeded
from .sources import SourceAdapter
from .store import Store

logger = logging.getLogger(__name__)

MODE_ANY = "any"
MODE_PERCENT = "percent"

#: 命中限流后间隔最多放大到多少秒（1 小时）
MAX_INTERVAL = 3600
#: 连续成功多少次后，把放大过的间隔收回一档
RECOVER_AFTER_SUCCESS = 10


@dataclass
class ExtremeTask:
    """一个极致追踪任务及其运行时状态。"""

    market_hash_name: str
    platform: str
    interval_seconds: int = 60
    price_mode: str = MODE_PERCENT
    price_threshold: float = 1.0
    quantity_mode: str = MODE_PERCENT
    quantity_threshold: float = 10.0
    cooldown_seconds: int = 300
    quiet_start: int | None = None
    quiet_end: int | None = None

    # ── 运行时状态（不落库）──
    current_interval: float = field(default=0.0)
    next_run_at: float = 0.0
    consecutive_ok: int = 0
    tick_count: int = 0
    last_error: str = ""
    last_price: float | None = None
    last_count: int | None = None

    def __post_init__(self) -> None:
        if not self.current_interval:
            self.current_interval = float(self.interval_seconds)

    @property
    def key(self) -> str:
        return f"{self.market_hash_name}@{self.platform}"

    def in_quiet_hours(self, when: datetime | None = None) -> bool:
        if self.quiet_start is None or self.quiet_end is None:
            return False
        hour = (when or datetime.now().astimezone()).hour
        if self.quiet_start == self.quiet_end:
            return False
        if self.quiet_start < self.quiet_end:
            return self.quiet_start <= hour < self.quiet_end
        return hour >= self.quiet_start or hour < self.quiet_end

    def to_dict(self) -> dict[str, object]:
        return {
            "market_hash_name": self.market_hash_name, "platform": self.platform,
            "interval_seconds": self.interval_seconds,
            "current_interval": round(self.current_interval, 1),
            "price_mode": self.price_mode, "price_threshold": self.price_threshold,
            "quantity_mode": self.quantity_mode,
            "quantity_threshold": self.quantity_threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "ticks": self.tick_count, "last_error": self.last_error,
            "last_price": self.last_price, "last_count": self.last_count,
            "quiet": self.in_quiet_hours(),
        }

    @classmethod
    def from_row(cls, row: dict) -> ExtremeTask:
        return cls(
            market_hash_name=row["market_hash_name"], platform=row["platform"],
            interval_seconds=int(row.get("interval_seconds") or 60),
            price_mode=row.get("price_mode") or MODE_PERCENT,
            price_threshold=float(row.get("price_threshold") or 1.0),
            quantity_mode=row.get("quantity_mode") or MODE_PERCENT,
            quantity_threshold=float(row.get("quantity_threshold") or 10.0),
            cooldown_seconds=int(row.get("cooldown_seconds") or 300),
            quiet_start=row.get("quiet_start"), quiet_end=row.get("quiet_end"),
        )


class ExtremeTracker:
    """跑在后台线程里的极致追踪器。"""

    def __init__(self, store: Store, sources: list[SourceAdapter],
                 engine: AlertEngine, notifier: Notifier,
                 gates: GateRegistry | None = None) -> None:
        self.store = store
        self.sources = {s.name: s for s in sources}
        self.engine = engine
        self.notifier = notifier
        self.gates = gates or GateRegistry()
        self._tasks: dict[str, ExtremeTask] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = {"ticks": 0, "changes": 0, "errors": 0, "backoffs": 0}

    # ── 任务装载 ───────────────────────────────────────────

    def load_tasks(self) -> int:
        rows = self.store.list_extreme(enabled_only=True)
        with self._lock:
            self._tasks = {f"{r['market_hash_name']}@{r['platform']}":
                           ExtremeTask.from_row(r) for r in rows}
            for task in self._tasks.values():
                task.next_run_at = time.monotonic()
        return len(self._tasks)

    # ── 主循环 ─────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        count = self.load_tasks()
        if not count:
            logger.info("[extreme] 没有启用的追踪任务")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="facet-extreme",
                                        daemon=True)
        self._thread.start()
        logger.info("[extreme] 已启动 %d 个追踪任务", count)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self._tick()
            except Exception:  # noqa: BLE001 — 追踪线程绝不因单次异常退出
                logger.exception("[extreme] tick 异常")
                wait = 5.0
            self._stop.wait(max(0.5, min(wait, 30.0)))

    def _tick(self) -> float:
        """执行所有到点的任务，返回下次该醒来的等待秒数。"""
        now = time.monotonic()
        with self._lock:
            due = [t for t in self._tasks.values() if t.next_run_at <= now]

        for task in due:
            if self._stop.is_set():
                break
            self._run_task(task)

        with self._lock:
            if not self._tasks:
                return 30.0
            soonest = min(t.next_run_at for t in self._tasks.values())
        return max(0.5, soonest - time.monotonic())

    def _run_task(self, task: ExtremeTask) -> None:
        """执行一次采样并检测变动。"""
        task.next_run_at = time.monotonic() + task.current_interval

        if task.in_quiet_hours():
            # 静默时段仍然采样（保持数据连续），只是不推送
            pass

        adapter = self._pick_adapter(task.platform)
        if adapter is None:
            task.last_error = f"没有可用于 {task.platform} 的源"
            self.stats["errors"] += 1
            return

        ref = self._build_ref(task)
        try:
            quotes, report = adapter.fetch_report([ref])
        except Exception as exc:  # noqa: BLE001
            task.last_error = f"{type(exc).__name__}: {exc}"
            self.stats["errors"] += 1
            self._backoff(task)
            return

        if report.errors:
            task.last_error = report.errors[0][:200]
            self.stats["errors"] += 1
            if any("限流" in e or "冷却" in e or "风控" in e for e in report.errors):
                self._backoff(task)
            return

        quote = next((q for q in quotes if q.platform == task.platform), None)
        if quote is None:
            task.last_error = "该平台本次无数据"
            return

        task.last_error = ""
        task.tick_count += 1
        self.stats["ticks"] += 1
        self._recover(task)

        # 顺序很重要：必须先取「上一条」再写入本次采样，
        # 否则拿到的是刚插入的自己，变动检测永远为 0。
        previous = self.store.latest_extreme_sample(
            task.market_hash_name, task.platform)

        self.store.insert_extreme_samples([{
            "market_hash_name": task.market_hash_name, "platform": task.platform,
            "sell_price": quote.sell_price, "sell_count": quote.sell_count,
            "bid_price": quote.bid_price, "observed_at": quote.observed_at.isoformat(),
        }])

        self._detect(task, previous, quote)

        task.last_price = quote.sell_price
        task.last_count = quote.sell_count

    def _build_ref(self, task: ExtremeTask) -> ItemRef:
        row = self.store.get_item(task.market_hash_name)
        return ItemRef(
            market_hash_name=task.market_hash_name,
            display_name=(row or {}).get("display_name"),
            buff_goods_id=(row or {}).get("buff_goods_id"),
            youpin_template_id=(row or {}).get("youpin_template_id"),
        )

    def _pick_adapter(self, platform: str) -> SourceAdapter | None:
        """挑一个能提供该平台在售价的、优先级最高的源。"""
        for adapter in self.sources.values():
            if platform in adapter.platforms and adapter.provides_sell:
                return adapter
        return None

    # ── 变动检测 ───────────────────────────────────────────

    def _detect(self, task: ExtremeTask, previous: dict | None, quote) -> None:
        if not previous:
            return

        events: list[AlertEvent] = []

        prev_price = previous.get("sell_price")
        if (prev_price and quote.sell_price
                and self._changed(prev_price, quote.sell_price,
                                  task.price_mode, task.price_threshold)):
            change = (quote.sell_price - prev_price) / prev_price * 100
            direction = "上涨" if change > 0 else "下跌"
            events.append(AlertEvent(
                market_hash_name=task.market_hash_name, platform=task.platform,
                rule="extreme_price",
                severity="warning" if abs(change) >= 2 * task.price_threshold else "info",
                message=(f"[极致追踪] {task.market_hash_name} 在 {task.platform} "
                         f"价格{direction} {abs(change):.2f}%："
                         f"{prev_price:.2f} → {quote.sell_price:.2f}"),
                current_price=quote.sell_price, baseline_price=prev_price,
                change_percent=change,
            ))

        prev_count = previous.get("sell_count")
        if (prev_count and quote.sell_count is not None
                and self._changed(prev_count, quote.sell_count,
                                  task.quantity_mode, task.quantity_threshold)):
            change = (quote.sell_count - prev_count) / prev_count * 100
            events.append(AlertEvent(
                market_hash_name=task.market_hash_name, platform=task.platform,
                rule="extreme_quantity", severity="warning",
                message=(f"[极致追踪] {task.market_hash_name} 在 {task.platform} "
                         f"在售量变动 {change:+.1f}%："
                         f"{prev_count} → {quote.sell_count}"
                         + ("（疑似被扫货）" if change < 0 else "")),
                current_price=quote.sell_price,
                baseline_price=float(prev_count),
                change_percent=change,
            ))

        if not events:
            return

        # 冷却：同一 (饰品,平台,规则) 在冷却期内不重复轰炸
        if task.cooldown_seconds > 0:
            def _cooled(event: AlertEvent) -> bool:
                last = self.store.last_alert_at(task.market_hash_name, task.platform,
                                                event.rule)
                return bool(last and utcnow() - last
                            < timedelta(seconds=task.cooldown_seconds))

            events = [e for e in events if not _cooled(e)]
        if not events:
            return

        saved = self.engine.persist(events)
        self.stats["changes"] += len(saved)

        if task.in_quiet_hours():
            for alert_id, _ in saved:
                self.store.mark_alert_notified(alert_id, "quiet_hours_suppressed")
            logger.info("[extreme] %s 有变动但处于静默时段，仅落库", task.key)
            return

        for alert_id, event in saved:
            outcome = self.notifier.send([event])[0][1]
            self.store.mark_alert_notified(alert_id, outcome)
            logger.info("[extreme] %s", event.message)

    @staticmethod
    def _changed(previous: float, current: float, mode: str, threshold: float) -> bool:
        if mode == MODE_ANY:
            return current != previous
        if not previous:
            return False
        return abs((current - previous) / previous * 100) >= abs(threshold)

    # ── 自适应间隔 ─────────────────────────────────────────

    def _backoff(self, task: ExtremeTask) -> None:
        """命中限流：间隔翻倍（上限 1 小时）。"""
        task.current_interval = min(MAX_INTERVAL, task.current_interval * 2)
        task.consecutive_ok = 0
        self.stats["backoffs"] += 1
        logger.warning("[extreme] %s 触发降频，新间隔 %.0fs", task.key,
                       task.current_interval)

    def _recover(self, task: ExtremeTask) -> None:
        """连续成功后逐步收回间隔，最终回到配置值。"""
        task.consecutive_ok += 1
        if (task.consecutive_ok >= RECOVER_AFTER_SUCCESS
                and task.current_interval > task.interval_seconds):
            task.current_interval = max(
                float(task.interval_seconds), task.current_interval / 2)
            task.consecutive_ok = 0
            logger.info("[extreme] %s 恢复中，间隔 %.0fs", task.key,
                        task.current_interval)

    # ── 状态 ───────────────────────────────────────────────

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            tasks = [t.to_dict() for t in self._tasks.values()]
        return {"running": bool(self._thread and self._thread.is_alive()),
                "task_count": len(tasks), "stats": dict(self.stats),
                "tasks": tasks}
