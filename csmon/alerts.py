"""告警规则引擎。

设计取舍（都是踩过的坑）：
  1. 「最低价」不等于「可信价」。同一个 (饰品, 平台) 可能被多个源同时覆盖，
     若两源报价相差过大，说明有一方是脏数据（错挂、单位错误、已下架缓存），
     此时不出告警并记录异常，比发出错误告警更负责。
  2. 基准价用中位数而非均值，避免 1 元钓鱼单把基准拉偏。
  3. 冷却按 (饰品, 平台, 规则方向) 计算，避免涨/跌两个方向互相压制。
  4. 在售量骤降是「被扫货」的先行信号，单列一条 restock 规则。
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Sequence

from .models import AlertEvent, SourceQuote, WatchRule, utcnow
from .store import Store

logger = logging.getLogger(__name__)

#: 同一平台内两个源报价的最大可容忍偏差（超过则判定为数据冲突，不出告警）
DEFAULT_CONFLICT_TOLERANCE = 0.25
#: 在售量相对基准下降多少算「被扫货」
DEFAULT_STOCK_DROP_PERCENT = 60.0


@dataclass(slots=True)
class Consolidated:
    """某 (饰品, 平台) 的合并视图。"""

    market_hash_name: str
    platform: str
    sell_price: float | None
    sell_count: int | None
    bid_price: float | None
    source: str
    sources: list[str]
    conflict: bool = False


class AlertEngine:
    def __init__(self, store: Store, conflict_tolerance: float = DEFAULT_CONFLICT_TOLERANCE,
                 stock_drop_percent: float = DEFAULT_STOCK_DROP_PERCENT) -> None:
        self.store = store
        self.conflict_tolerance = conflict_tolerance
        self.stock_drop_percent = stock_drop_percent

    # ── 合并 ───────────────────────────────────────────────

    def consolidate(self, quotes: Iterable[SourceQuote]) -> tuple[list[Consolidated], list[str]]:
        """把多源报价合并为每个 (饰品, 平台) 一条视图，并报告冲突。"""
        grouped: dict[tuple[str, str], list[SourceQuote]] = defaultdict(list)
        for q in quotes:
            grouped[(q.market_hash_name, q.platform)].append(q)

        out: list[Consolidated] = []
        conflicts: list[str] = []
        for (mhn, platform), group in grouped.items():
            priced = [q for q in group if q.sell_price]
            bid_priced = [q for q in group if q.bid_price]

            if priced:
                prices = sorted(q.sell_price for q in priced if q.sell_price)
                low, high = prices[0], prices[-1]
                conflict = False
                if low > 0 and (high - low) / low > self.conflict_tolerance:
                    conflict = True
                    conflicts.append(
                        f"{mhn}@{platform} 多源价差 {(high - low) / low:.0%} "
                        f"(低 {low} 高 {high}，来源 {[q.source for q in priced]})")
                # 取最低价；同时保留该报价的源名，便于追溯
                chosen = min(priced, key=lambda q: q.sell_price or float("inf"))
                sell_price = chosen.sell_price
                sell_count = chosen.sell_count
                source = chosen.source
            else:
                conflict = False
                sell_price = None
                sell_count = None
                source = group[0].source if group else "unknown"

            bid_price = max((q.bid_price for q in bid_priced), default=None)
            out.append(Consolidated(
                market_hash_name=mhn, platform=platform,
                sell_price=sell_price, sell_count=sell_count,
                bid_price=bid_price, source=source,
                sources=sorted({q.source for q in group}),
                conflict=conflict,
            ))
        return out, conflicts

    # ── 评估 ───────────────────────────────────────────────

    def evaluate(self, quotes: Sequence[SourceQuote],
                 rules: Sequence[WatchRule] | None = None) -> list[AlertEvent]:
        """对一批新报价跑全部规则，返回应当发出的告警。"""
        watch_rules = list(rules if rules is not None else self.store.list_watch())
        if not watch_rules:
            return []

        consolidated, conflicts = self.consolidate(quotes)
        for c in conflicts:
            logger.warning("[alerts] 数据冲突，已抑制告警：%s", c)

        by_key: dict[tuple[str, str], Consolidated] = {
            (c.market_hash_name, c.platform): c for c in consolidated
        }

        events: list[AlertEvent] = []
        for rule in watch_rules:
            platforms = rule.platforms or None
            for (mhn, platform), view in by_key.items():
                if mhn != rule.market_hash_name:
                    continue
                if platforms and platform not in platforms:
                    continue
                if view.conflict:
                    continue
                events.extend(self._evaluate_one(rule, view))
        return events

    def _evaluate_one(self, rule: WatchRule, view: Consolidated) -> list[AlertEvent]:
        # _make 在冷却期内返回 None，因此这里先收 None 再统一过滤
        events: list[AlertEvent | None] = []
        price = view.sell_price

        # 规则 1/2：绝对价格阈值 —— 不依赖历史，数据一到即可判定
        if price is not None and rule.below is not None and price <= rule.below:
            depth = (rule.below - price) / rule.below if rule.below else 0.0
            events.append(self._make(
                rule, view, rule_name="below",
                severity="critical" if depth >= 0.1 else "warning",
                message=(f"{view.market_hash_name} 在 {view.platform} 最低在售价 "
                         f"{price:.2f}，已跌破设定价 {rule.below:.2f}"
                         f"（低 {depth:.1%}，在售 {view.sell_count}）"),
                current=price, baseline=rule.below, change_percent=-depth * 100,
            ))

        if price is not None and rule.above is not None and price >= rule.above:
            excess = (price - rule.above) / rule.above if rule.above else 0.0
            events.append(self._make(
                rule, view, rule_name="above",
                severity="info",
                message=(f"{view.market_hash_name} 在 {view.platform} 最低在售价 "
                         f"{price:.2f}，已超过设定价 {rule.above:.2f}"
                         f"（高 {excess:.1%}，在售 {view.sell_count}）"),
                current=price, baseline=rule.above, change_percent=excess * 100,
            ))

        # 规则 3/4：相对基准的波动 —— 需要先有历史
        if price is not None and (rule.drop_percent or rule.rise_percent):
            baseline = self.store.baseline_median(
                view.market_hash_name, view.platform,
                hours=rule.baseline_window_hours, exclude_last=1)
            if baseline:
                change = (price - baseline) / baseline * 100
                if rule.drop_percent and change <= -abs(rule.drop_percent):
                    events.append(self._make(
                        rule, view, rule_name="drop_percent",
                        severity="critical" if change <= -2 * abs(rule.drop_percent) else "warning",
                        message=(f"{view.market_hash_name} 在 {view.platform} 跌至 "
                                 f"{price:.2f}，较 {rule.baseline_window_hours}h 基准 "
                                 f"{baseline:.2f} 下跌 {abs(change):.1f}%"
                                 f"（在售 {view.sell_count}）"),
                        current=price, baseline=baseline, change_percent=change,
                    ))
                elif rule.rise_percent and change >= abs(rule.rise_percent):
                    events.append(self._make(
                        rule, view, rule_name="rise_percent",
                        severity="info",
                        message=(f"{view.market_hash_name} 在 {view.platform} 涨至 "
                                 f"{price:.2f}，较 {rule.baseline_window_hours}h 基准 "
                                 f"{baseline:.2f} 上涨 {change:.1f}%"
                                 f"（在售 {view.sell_count}）"),
                        current=price, baseline=baseline, change_percent=change,
                    ))

        # 规则 5：在售量骤降（被扫货的先行信号）
        if view.sell_count is not None:
            stock_baseline = self._stock_baseline(view)
            if stock_baseline and view.sell_count <= stock_baseline * (1 - self.stock_drop_percent / 100):
                events.append(self._make(
                    rule, view, rule_name="stock_drop",
                    severity="warning",
                    message=(f"{view.market_hash_name} 在 {view.platform} 在售量 "
                             f"{view.sell_count}，较近期均值 {stock_baseline:.0f} "
                             f"下降 {(1 - view.sell_count / stock_baseline):.0%}，疑似被扫货"),
                    current=price, baseline=stock_baseline, change_percent=None,
                ))

        return [e for e in events if e is not None]

    def _stock_baseline(self, view: Consolidated) -> float | None:
        history = self.store.price_history(
            view.market_hash_name, view.platform, hours=168, limit=200)
        counts = [h["sell_count"] for h in history
                  if h.get("sell_count") is not None and h["sell_count"] > 0]
        if len(counts) < 4:
            return None
        return float(statistics.median(counts[1:]))

    # ── 冷却 ───────────────────────────────────────────────

    def _make(self, rule: WatchRule, view: Consolidated, rule_name: str,
              severity: str, message: str, current: float | None,
              baseline: float | None, change_percent: float | None) -> AlertEvent | None:
        last = self.store.last_alert_at(view.market_hash_name, view.platform, rule_name)
        if last is not None:
            if utcnow() - last < timedelta(minutes=rule.cooldown_minutes):
                logger.debug("[alerts] %s@%s/%s 处于冷却期，抑制",
                             view.market_hash_name, view.platform, rule_name)
                return None
        return AlertEvent(
            market_hash_name=view.market_hash_name,
            platform=view.platform,
            rule=rule_name,
            severity=severity,
            message=message,
            current_price=current,
            baseline_price=baseline,
            change_percent=change_percent,
        )

    # ── 落库 ───────────────────────────────────────────────

    def persist(self, events: Sequence[AlertEvent]) -> list[tuple[int, AlertEvent]]:
        """写入告警表，返回 (id, event) 便于后续标记推送结果。"""
        saved: list[tuple[int, AlertEvent]] = []
        for ev in events:
            alert_id = self.store.insert_alert(ev)
            saved.append((alert_id, ev))
        return saved


def filter_by_severity(events: Sequence[AlertEvent], min_severity: str) -> list[AlertEvent]:
    """按最低等级过滤（用于通知渠道）。"""
    order = {"info": 0, "warning": 1, "critical": 2}
    threshold = order.get(min_severity, 1)
    return [e for e in events if order.get(e.severity, 0) >= threshold]
