"""存储层测试：身份映射、报价时序、基准价、告警冷却、游标。"""
from __future__ import annotations

from datetime import timedelta

from csmon.models import AlertEvent, ItemRef, SourceQuote, WatchRule, utcnow
from csmon.store import Store


def test_upsert_item_merges_partial_ids(store: Store) -> None:
    """先有悠悠 ID，后补 BUFF ID，两个 ID 都要保留（不能互相覆盖成 NULL）。"""
    store.upsert_item(ItemRef(market_hash_name="AWP | Asiimov (Field-Tested)",
                              youpin_template_id=822))
    store.upsert_item(ItemRef(market_hash_name="AWP | Asiimov (Field-Tested)",
                              buff_goods_id=43076))

    row = store.get_item("AWP | Asiimov (Field-Tested)")
    assert row is not None
    assert row["youpin_template_id"] == 822
    assert row["buff_goods_id"] == 43076


def test_reverse_lookup_by_platform_id(store: Store) -> None:
    store.upsert_item(ItemRef(market_hash_name="X", buff_goods_id=11, youpin_template_id=22))
    assert store.find_item_by_buff_id(11)["market_hash_name"] == "X"
    assert store.find_item_by_youpin_id(22)["market_hash_name"] == "X"
    assert store.find_item_by_buff_id(999) is None


def test_upsert_items_batch(store: Store) -> None:
    n = store.upsert_items([
        ItemRef(market_hash_name=f"item-{i}", buff_goods_id=1000 + i) for i in range(25)
    ])
    assert n == 25
    assert store.count_items() == 25


def test_latest_quote_and_snapshot(store: Store) -> None:
    base = utcnow()
    store.insert_quotes([
        SourceQuote(market_hash_name="X", platform="BUFF", source="csqaq",
                    sell_price=100.0, observed_at=base - timedelta(hours=2)),
        SourceQuote(market_hash_name="X", platform="BUFF", source="csqaq",
                    sell_price=95.0, observed_at=base),
    ])
    latest = store.latest_quote("X", "BUFF")
    assert latest is not None and latest["sell_price"] == 95.0

    snap = store.latest_snapshot_all()
    assert len(snap) == 1 and snap[0]["sell_price"] == 95.0


def test_baseline_median_ignores_outlier_and_last_sample(store: Store) -> None:
    """基准价用中位数：一个 1 元钓鱼单不该把基准拉下来。"""
    base = utcnow()
    quotes = [100.0, 102.0, 98.0, 1.0, 101.0]
    store.insert_quotes([
        SourceQuote(market_hash_name="X", platform="BUFF", source="s",
                    sell_price=p, observed_at=base - timedelta(hours=5 - i))
        for i, p in enumerate(quotes)
    ])
    # 再写入本次采样（应被 exclude_last 排除）
    store.insert_quotes([SourceQuote(market_hash_name="X", platform="BUFF", source="s",
                                     sell_price=50.0, observed_at=base)])

    baseline = store.baseline_median("X", "BUFF", hours=24, exclude_last=1)
    # 排除本次采样 50 后，剩余 [101, 1, 98, 100, 102] 的中位数为 100
    assert baseline == 100.0


def test_baseline_requires_history(store: Store) -> None:
    store.insert_quotes([SourceQuote(market_hash_name="Y", platform="BUFF", source="s",
                                     sell_price=10.0)])
    assert store.baseline_median("Y", "BUFF") is None    # 样本不足


def test_watchlist_roundtrip(store: Store) -> None:
    rule = WatchRule(market_hash_name="AK-47 | Redline (Field-Tested)",
                     below=100.0, drop_percent=8.0, platforms=["BUFF"], note="捡漏")
    store.upsert_watch(rule)
    loaded = store.list_watch()
    assert len(loaded) == 1
    assert loaded[0].below == 100.0
    assert loaded[0].drop_percent == 8.0
    assert loaded[0].platforms == ["BUFF"]
    assert store.watch_count() == 1

    assert store.remove_watch(rule.market_hash_name) is True
    assert store.watch_count() == 0


def test_alert_cooldown_lookup(store: Store) -> None:
    store.insert_alert(AlertEvent(market_hash_name="X", platform="BUFF",
                                  rule="below", message="m"))
    assert store.last_alert_at("X", "BUFF", "below") is not None
    assert store.last_alert_at("X", "BUFF", "above") is None
    assert store.last_alert_at("X", "YOUPIN", "below") is None
    assert store.alert_count() == 1


def test_mark_alert_notified(store: Store) -> None:
    alert_id = store.insert_alert(AlertEvent(market_hash_name="X", platform="BUFF",
                                             rule="below", message="m"))
    store.mark_alert_notified(alert_id, "webhook:200")
    row = store.recent_alerts(1)[0]
    assert row["notified"] == 1
    assert row["notify_result"] == "webhook:200"


def test_index_cursor_persistence(store: Store) -> None:
    assert store.get_cursor("buff")["cursor"] == 0
    store.set_cursor("buff", 1234, hits=7, scanned=1200)
    saved = store.get_cursor("buff")
    assert saved == {"cursor": 1234, "hits": 7, "scanned": 1200}


def test_source_health_keeps_latest(store: Store) -> None:
    store.log_fetch("csqaq", 10, 9, 1, 18, "2026-01-01T00:00:00+00:00", None, ["boom"])
    store.log_fetch("csqaq", 10, 10, 0, 20, "2026-01-01T01:00:00+00:00", None, [])
    health = store.source_health()
    assert len(health) == 1
    assert health[0]["succeeded"] == 10


def test_stats(store: Store) -> None:
    store.upsert_item(ItemRef(market_hash_name="X"))
    store.insert_quotes([SourceQuote(market_hash_name="X", platform="BUFF", source="s",
                                     sell_price=1.0)])
    stats = store.stats()
    assert stats["items"] == 1 and stats["quotes"] == 1
