"""告警引擎测试：阈值、波动、跨源冲突抑制、冷却、在售量骤降。"""
from __future__ import annotations

from datetime import timedelta

from csmon.alerts import AlertEngine, filter_by_severity
from csmon.models import AlertEvent, SourceQuote, WatchRule, utcnow
from csmon.store import Store


def _q(mhn: str, platform: str, sell=None, count=None, bid=None, source="csqaq") -> SourceQuote:
    return SourceQuote(market_hash_name=mhn, platform=platform, source=source,
                       sell_price=sell, sell_count=count, bid_price=bid)


def test_below_threshold_fires_with_severity(store: Store) -> None:
    engine = AlertEngine(store)
    rule = WatchRule(market_hash_name="AK", below=100.0, cooldown_minutes=0)
    events = engine.evaluate([_q("AK", "BUFF", sell=92.0, count=10)], [rule])
    assert len(events) == 1
    assert events[0].rule == "below"
    # 跌破 8% 应升级为 critical（阈值 10% 时 warning）
    assert events[0].severity == "warning"
    assert events[0].change_percent is not None


def test_below_threshold_deep_discount_is_critical(store: Store) -> None:
    engine = AlertEngine(store)
    rule = WatchRule(market_hash_name="AK", below=100.0, cooldown_minutes=0)
    events = engine.evaluate([_q("AK", "BUFF", sell=80.0, count=10)], [rule])
    assert events[0].severity == "critical"     # 低 20%


def test_no_alert_when_price_above_threshold(store: Store) -> None:
    engine = AlertEngine(store)
    rule = WatchRule(market_hash_name="AK", below=100.0, cooldown_minutes=0)
    assert engine.evaluate([_q("AK", "BUFF", sell=150.0, count=10)], [rule]) == []


def test_above_threshold(store: Store) -> None:
    engine = AlertEngine(store)
    rule = WatchRule(market_hash_name="AK", above=200.0, cooldown_minutes=0)
    events = engine.evaluate([_q("AK", "BUFF", sell=250.0, count=3)], [rule])
    assert len(events) == 1 and events[0].rule == "above" and events[0].severity == "info"


def test_platform_filter(store: Store) -> None:
    engine = AlertEngine(store)
    rule = WatchRule(market_hash_name="AK", below=100.0, cooldown_minutes=0,
                     platforms=["BUFF"])
    quotes = [_q("AK", "BUFF", sell=90.0), _q("AK", "YOUPIN", sell=90.0)]
    events = engine.evaluate(quotes, [rule])
    assert len(events) == 1 and events[0].platform == "BUFF"


def test_drop_percent_uses_baseline_median(store: Store) -> None:
    base = utcnow()
    store.insert_quotes([
        SourceQuote(market_hash_name="AK", platform="BUFF", source="csqaq",
                    sell_price=p, observed_at=base - timedelta(hours=10 - i))
        for i, p in enumerate([100.0, 101.0, 99.0, 100.0, 102.0])
    ])
    engine = AlertEngine(store)
    rule = WatchRule(market_hash_name="AK", drop_percent=7.0,
                     baseline_window_hours=24, cooldown_minutes=0)
    # 现价 85 相对中位 100 跌 15%；阈值 7% 的两倍是 14%，15% ≥ 14% -> 升级 critical
    events = engine.evaluate([_q("AK", "BUFF", sell=85.0)], [rule])
    assert len(events) == 1
    assert events[0].rule == "drop_percent"
    assert events[0].severity == "critical"
    assert round(events[0].change_percent or 0, 1) == -15.0


def test_drop_percent_just_over_threshold_is_warning(store: Store) -> None:
    """仅略微越过阈值时应是 warning，不该升级为 critical。"""
    base = utcnow()
    store.insert_quotes([
        SourceQuote(market_hash_name="AK", platform="BUFF", source="csqaq",
                    sell_price=100.0, observed_at=base - timedelta(hours=10 - i))
        for i in range(5)
    ])
    engine = AlertEngine(store)
    rule = WatchRule(market_hash_name="AK", drop_percent=8.0,
                     baseline_window_hours=24, cooldown_minutes=0)
    events = engine.evaluate([_q("AK", "BUFF", sell=91.0)], [rule])
    assert len(events) == 1 and events[0].severity == "warning"


def test_rise_percent(store: Store) -> None:
    base = utcnow()
    store.insert_quotes([
        SourceQuote(market_hash_name="AK", platform="BUFF", source="csqaq",
                    sell_price=100.0, observed_at=base - timedelta(hours=i))
        for i in range(1, 5)
    ])
    engine = AlertEngine(store)
    rule = WatchRule(market_hash_name="AK", rise_percent=10.0,
                     baseline_window_hours=24, cooldown_minutes=0)
    events = engine.evaluate([_q("AK", "BUFF", sell=130.0)], [rule])
    assert len(events) == 1 and events[0].rule == "rise_percent"


def test_conflict_suppresses_alert(store: Store) -> None:
    """两个源报价相差超过容忍度时不告警 —— 脏数据不该触发误报。"""
    engine = AlertEngine(store, conflict_tolerance=0.25)
    rule = WatchRule(market_hash_name="AK", below=100.0, cooldown_minutes=0)
    quotes = [
        _q("AK", "BUFF", sell=50.0, source="csqaq"),
        _q("AK", "BUFF", sell=100.0, source="buff_direct"),
    ]
    assert engine.evaluate(quotes, [rule]) == []


def test_consolidate_picks_lowest_price(store: Store) -> None:
    engine = AlertEngine(store)
    quotes = [
        _q("AK", "BUFF", sell=105.0, source="csqaq"),
        _q("AK", "BUFF", sell=101.0, source="buff_direct"),
    ]
    views, conflicts = engine.consolidate(quotes)
    assert not conflicts
    assert views[0].sell_price == 101.0
    assert views[0].source == "buff_direct"
    assert views[0].sources == ["buff_direct", "csqaq"]


def test_cooldown_suppresses_repeat(store: Store) -> None:
    engine = AlertEngine(store)
    rule = WatchRule(market_hash_name="AK", below=100.0, cooldown_minutes=240)
    first = engine.evaluate([_q("AK", "BUFF", sell=90.0)], [rule])
    assert len(first) == 1
    engine.persist(first)

    second = engine.evaluate([_q("AK", "BUFF", sell=95.0)], [rule])
    assert second == []


def test_stock_drop_detected(store: Store) -> None:
    """在售量骤降是被扫货的先行信号。"""
    base = utcnow()
    store.insert_quotes([
        SourceQuote(market_hash_name="AK", platform="BUFF", source="csqaq",
                    sell_price=100.0, sell_count=c,
                    observed_at=base - timedelta(hours=6 - i))
        for i, c in enumerate([200, 210, 190, 200, 205])
    ])
    engine = AlertEngine(store, stock_drop_percent=60.0)
    rule = WatchRule(market_hash_name="AK", cooldown_minutes=0)
    events = engine.evaluate([_q("AK", "BUFF", sell=100.0, count=20)], [rule])
    assert any(e.rule == "stock_drop" for e in events)


def test_empty_quote_ignored(store: Store) -> None:
    engine = AlertEngine(store)
    rule = WatchRule(market_hash_name="AK", below=100.0, cooldown_minutes=0)
    assert engine.evaluate([_q("AK", "BUFF", sell=None)], [rule]) == []


def test_other_items_not_alerted(store: Store) -> None:
    engine = AlertEngine(store)
    rule = WatchRule(market_hash_name="AK", below=100.0, cooldown_minutes=0)
    assert engine.evaluate([_q("M4", "BUFF", sell=10.0)], [rule]) == []


def test_persist_returns_ids(store: Store) -> None:
    engine = AlertEngine(store)
    event = AlertEvent(market_hash_name="AK", platform="BUFF", rule="below", message="m")
    saved = engine.persist([event])
    assert len(saved) == 1 and saved[0][0] > 0


def test_filter_by_severity() -> None:
    events = [
        AlertEvent(market_hash_name="a", platform="BUFF", rule="r", message="m", severity="info"),
        AlertEvent(market_hash_name="b", platform="BUFF", rule="r", message="m", severity="warning"),
        AlertEvent(market_hash_name="c", platform="BUFF", rule="r", message="m", severity="critical"),
    ]
    assert len(filter_by_severity(events, "warning")) == 2
    assert len(filter_by_severity(events, "critical")) == 1
    assert len(filter_by_severity(events, "info")) == 3
