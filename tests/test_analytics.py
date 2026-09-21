"""指标与分析层测试：手工可验算的数值 + 边界行为。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from facet import analytics, indicators
from facet.models import SourceQuote, utcnow
from facet.store import Store


# ── 均线 ───────────────────────────────────────────────────

def test_sma_basic() -> None:
    assert indicators.sma([1, 2, 3, 4, 5], 5) == 3.0
    assert indicators.sma([1, 2, 3, 4, 5], 2) == 4.5


def test_sma_returns_none_when_insufficient() -> None:
    """数据不足必须返回 None —— 返回 0 会让下游把「算不出」当成「价格是 0」。"""
    assert indicators.sma([1, 2], 5) is None


def test_sma_ignores_invalid_prices() -> None:
    assert indicators.sma([0, None, 10, 20], 2) == 15.0


def test_ema_reacts_faster_than_sma() -> None:
    rising = list(range(1, 21))
    ema_val = indicators.ema(rising, 5)
    sma_val = indicators.sma(rising, 5)
    assert ema_val is not None and sma_val is not None
    assert ema_val > sma_val     # 上涨序列下 EMA 更贴近现值


# ── RSI ────────────────────────────────────────────────────

def test_rsi_all_gains_is_100() -> None:
    assert indicators.rsi(list(range(1, 20)), 14) == 100.0


def test_rsi_all_losses_is_zero() -> None:
    assert indicators.rsi(list(range(20, 1, -1)), 14) == pytest.approx(0.0, abs=1e-6)


def test_rsi_flat_series_is_neutral() -> None:
    """价格完全不动时 RSI 应为 50（无涨无跌），而不是 0 或 100。"""
    assert indicators.rsi([100.0] * 20, 14) == 50.0


def test_rsi_needs_enough_data() -> None:
    assert indicators.rsi([1, 2, 3], 14) is None


def test_rsi_known_value() -> None:
    """用一组可手算的序列核对 Wilder RSI 落在合理区间。"""
    prices = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
              45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    value = indicators.rsi(prices, 14)
    assert value is not None
    assert 69.0 < value < 71.0      # 经典示例序列 RSI 约 70.46


# ── 布林带 ─────────────────────────────────────────────────

def test_bollinger_bands_ordering() -> None:
    prices = [100 + (i % 7) for i in range(40)]
    bands = indicators.bollinger(prices, 20, 2.0)
    assert bands is not None
    assert bands.lower < bands.middle < bands.upper
    assert bands.width > 0


def test_bollinger_position_maps_to_channel() -> None:
    prices = [100 + (i % 7) for i in range(40)]
    bands = indicators.bollinger(prices, 20, 2.0)
    assert bands is not None
    assert bands.position(bands.lower) == pytest.approx(0.0)
    assert bands.position(bands.upper) == pytest.approx(1.0)
    assert bands.position(bands.middle) == pytest.approx(0.5)


def test_bollinger_flat_series_width_zero() -> None:
    bands = indicators.bollinger([100.0] * 25, 20, 2.0)
    assert bands is not None
    assert bands.width == 0.0
    assert bands.position(100.0) is None      # 通道宽度为 0 时位置无意义


# ── 波动 / 收益 ────────────────────────────────────────────

def test_volatility_zero_for_flat_prices() -> None:
    assert indicators.volatility([100.0] * 40, 30) == pytest.approx(0.0)


def test_annualized_volatility_scales() -> None:
    prices = [100 * (1.01 ** i) for i in range(40)]
    daily = indicators.volatility(prices, 30)
    annual = indicators.annualized_volatility(prices, 30)
    assert daily is not None and annual is not None
    assert annual > daily


def test_max_drawdown() -> None:
    assert indicators.max_drawdown([100, 120, 60, 80]) == pytest.approx(-0.5)


def test_max_drawdown_monotonic_rise_is_zero() -> None:
    assert indicators.max_drawdown([1, 2, 3, 4]) == 0.0


def test_annualized_return_exact_double() -> None:
    """持有 365 个周期、净值翻倍 → 年化正好 +100%。"""
    prices = [100.0] + [100.0] * 364 + [200.0]
    value = indicators.annualized_return(prices, periods_per_year=365)
    assert value == pytest.approx(1.0)


def test_annualized_return_short_span_extrapolates() -> None:
    """只涨 1% 但只隔了 1 个周期 → 年化会被放大，这正是年化指标要谨慎解读的原因。"""
    prices = [100.0, 101.0]
    value = indicators.annualized_return(prices, periods_per_year=365)
    assert value is not None and value > 10


def test_momentum() -> None:
    assert indicators.momentum([100, 100, 100, 110], 3) == pytest.approx(0.1)


def test_zscore_of_flat_series_is_none() -> None:
    assert indicators.zscore([100.0] * 30, 30) is None


def test_zscore_detects_outlier() -> None:
    prices = [100.0] * 29 + [200.0]
    value = indicators.zscore(prices, 30)
    assert value is not None and value > 5


# ── 综合快照 ───────────────────────────────────────────────

def test_snapshot_and_interpret() -> None:
    prices = [100 + i * 0.5 for i in range(120)]
    snap = indicators.snapshot(prices)
    assert snap.count == 120
    assert snap.ma["ma7"] is not None
    assert snap.rsi14 is not None and snap.rsi14 > 90     # 一路上涨必然超买
    notes = indicators.interpret(snap)
    assert any("超买" in n for n in notes)
    assert any("均线多头" in n for n in notes)


def test_snapshot_empty_input_is_safe() -> None:
    snap = indicators.snapshot([])
    assert snap.count == 0 and snap.last is None
    assert indicators.interpret(snap) == []


def test_interpret_bollinger_squeeze() -> None:
    prices = [100.0] * 25
    snap = indicators.snapshot(prices)
    notes = indicators.interpret(snap)
    assert any("收敛" in n for n in notes)


# ── 日线 OHLC ──────────────────────────────────────────────

def _seed_quotes(store: Store, name: str, platform: str,
                 series: list[tuple[int, float, int | None]]) -> None:
    """series: [(天数前, 价格, 在售量)]，天数前 0 = 现在。"""
    now = utcnow()
    store.insert_quotes([
        SourceQuote(market_hash_name=name, platform=platform, source="test",
                    sell_price=price, sell_count=count,
                    observed_at=now - timedelta(days=days, hours=1))
        for days, price, count in series
    ])


def test_daily_ohlc_groups_by_day(store: Store) -> None:
    """同一天内按时间顺序聚合；三条同时间戳的记录也必须有确定顺序。

    这里三条记录的 observed_at 完全相同，是有意为之的回归用例：
    修复前它们会按索引内 rowid 逆序返回，导致 open/close 在两次查询间漂移。
    正确行为是按写入顺序（id 升序）定序。
    """
    # 第 2 天：100 → 110 → 95（开 100 高 110 低 95 收 95）
    _seed_quotes(store, "X", "BUFF", [
        (2, 100.0, 10), (2, 110.0, 20), (2, 95.0, 30),
        (1, 120.0, 5), (0, 130.0, 1),
    ])
    bars = analytics.daily_ohlc(store, "X", "BUFF", days=10)
    assert len(bars) == 3

    # 重复查询必须给出完全相同的结果（定序稳定）
    assert analytics.daily_ohlc(store, "X", "BUFF", days=10) == bars

    first = bars[0]
    assert first.open == 100.0 and first.high == 110.0
    assert first.low == 95.0 and first.close == 95.0
    assert first.count == 3
    assert first.avg_count == pytest.approx(20.0)


def test_daily_ohlc_ignores_invalid_prices(store: Store) -> None:
    _seed_quotes(store, "X", "BUFF", [(1, 100.0, 1)])
    store.insert_quotes([SourceQuote(market_hash_name="X", platform="BUFF",
                                     source="test", sell_price=0.0)])
    bars = analytics.daily_ohlc(store, "X", "BUFF", days=10)
    assert all(b.low > 0 for b in bars)


def test_daily_ohlc_empty(store: Store) -> None:
    assert analytics.daily_ohlc(store, "none", "BUFF") == []


def test_ohlc_cache_roundtrip(store: Store) -> None:
    written = store.upsert_ohlc("X", "BUFF", [
        {"date": "2026-01-02", "open": 1.0, "high": 3.0, "low": 0.5,
         "close": 2.0, "count": 5, "avg_count": 7.0},
        {"date": "2026-01-01", "open": 1.0, "high": 1.0, "low": 1.0,
         "close": 1.0, "count": 1, "avg_count": None},
    ])
    assert written == 2
    rows = store.get_ohlc("X", "BUFF", days=10)
    assert [r["date"] for r in rows] == ["2026-01-01", "2026-01-02"]  # 升序


# ── 套利 ───────────────────────────────────────────────────

def test_compute_spread_deducts_fees() -> None:
    spread = analytics.compute_spread(
        "X", "BUFF", "YOUPIN", buy_price=100.0, sell_price=110.0,
        sell_is_bid=True,
        fees={"BUFF": {"sell": 0.0, "withdraw": 0.0},
              "YOUPIN": {"sell": 0.10, "withdraw": 0.0}})
    assert spread is not None
    assert spread.gross_profit == pytest.approx(10.0)
    assert spread.net_profit == pytest.approx(-1.0)     # 110 - 10% 手续费 = 99
    assert spread.net_percent < 0


def test_compute_spread_rejects_bad_prices() -> None:
    assert analytics.compute_spread("X", "BUFF", "YOUPIN", 0, 10, True) is None


def test_spread_radar_uses_bid_when_available(store: Store) -> None:
    """对手平台有求购价时应标记为可即时成交。"""
    now = utcnow()
    store.insert_quotes([
        SourceQuote(market_hash_name="X", platform="BUFF", source="csqaq",
                    sell_price=100.0, observed_at=now),
        SourceQuote(market_hash_name="X", platform="YOUPIN", source="csqaq",
                    sell_price=130.0, bid_price=125.0, observed_at=now),
    ])
    rows = analytics.spread_radar(
        store, min_net_percent=0.01, min_net_profit=0.5,
        fees={"BUFF": {"sell": 0.0, "withdraw": 0.0},
              "YOUPIN": {"sell": 0.0, "withdraw": 0.0}})
    assert rows, "应检出 BUFF 买入 → 悠悠有品卖出的价差"
    best = rows[0]
    assert best.buy_platform == "BUFF" and best.sell_platform == "YOUPIN"
    assert best.sell_is_bid is True
    assert best.net_profit == pytest.approx(25.0)


def test_spread_radar_marks_non_executable(store: Store) -> None:
    """没有求购价时退回挂售价，必须标成不可即时成交。"""
    now = utcnow()
    store.insert_quotes([
        SourceQuote(market_hash_name="X", platform="BUFF", source="csqaq",
                    sell_price=100.0, observed_at=now),
        SourceQuote(market_hash_name="X", platform="YOUPIN", source="csqaq",
                    sell_price=130.0, observed_at=now),
    ])
    rows = analytics.spread_radar(
        store, min_net_percent=0.01, min_net_profit=0.5,
        fees={"BUFF": {"sell": 0.0, "withdraw": 0.0},
              "YOUPIN": {"sell": 0.0, "withdraw": 0.0}})
    assert rows and rows[0].sell_is_bid is False


def test_spread_radar_respects_threshold(store: Store) -> None:
    now = utcnow()
    store.insert_quotes([
        SourceQuote(market_hash_name="X", platform="BUFF", source="csqaq",
                    sell_price=100.0, observed_at=now),
        SourceQuote(market_hash_name="X", platform="YOUPIN", source="csqaq",
                    sell_price=101.0, observed_at=now),
    ])
    # 价差仅 1%，阈值要 3% → 不该命中
    assert analytics.spread_radar(store, min_net_percent=0.03, min_net_profit=0.0) == []


def test_spread_radar_keeps_best_per_item(store: Store) -> None:
    """同一饰品只保留最优机会，避免列表刷屏。"""
    now = utcnow()
    store.insert_quotes([
        SourceQuote(market_hash_name="X", platform="BUFF", source="s",
                    sell_price=100.0, observed_at=now),
        SourceQuote(market_hash_name="X", platform="YOUPIN", source="s",
                    sell_price=150.0, bid_price=140.0, observed_at=now),
        SourceQuote(market_hash_name="X", platform="STEAM", source="s",
                    sell_price=120.0, observed_at=now),
    ])
    rows = analytics.spread_radar(
        store, min_net_percent=0.01, min_net_profit=0.0,
        fees={p: {"sell": 0.0, "withdraw": 0.0}
              for p in ("BUFF", "YOUPIN", "STEAM")})
    assert len(rows) == 1
    assert rows[0].net_profit == pytest.approx(40.0)


# ── 涨跌 / 流动性 ──────────────────────────────────────────

def test_movers_classifies_direction(store: Store) -> None:
    _seed_quotes(store, "UP", "BUFF", [(4, 100.0, 1), (3, 100.0, 1),
                                       (2, 100.0, 1), (0, 150.0, 1)])
    _seed_quotes(store, "DOWN", "BUFF", [(4, 100.0, 1), (3, 100.0, 1),
                                         (2, 100.0, 1), (0, 60.0, 1)])
    board = analytics.movers(store, hours=168, limit=10)
    gained = {r["market_hash_name"] for r in board["gained"]}
    lost = {r["market_hash_name"] for r in board["lost"]}
    assert "UP" in gained and "DOWN" in lost


def test_movers_needs_history(store: Store) -> None:
    _seed_quotes(store, "NEW", "BUFF", [(0, 100.0, 1)])
    board = analytics.movers(store, hours=168)
    assert board["gained"] == [] and board["lost"] == []


def test_liquidity_board_sorted(store: Store) -> None:
    now = utcnow()
    store.insert_quotes([
        SourceQuote(market_hash_name="A", platform="BUFF", source="s",
                    sell_price=1.0, sell_count=10, observed_at=now),
        SourceQuote(market_hash_name="B", platform="BUFF", source="s",
                    sell_price=1.0, sell_count=99, observed_at=now),
    ])
    rows = analytics.liquidity_board(store, limit=5)
    assert [r["market_hash_name"] for r in rows] == ["B", "A"]


# ── K 线束 ─────────────────────────────────────────────────

def test_kline_bundle_shape(store: Store) -> None:
    series = [(d, 100.0 + d, 5) for d in range(40, 0, -1)]
    _seed_quotes(store, "X", "BUFF", series)
    bundle = analytics.kline_bundle(store, "X", "BUFF", days=90)
    assert bundle["market_hash_name"] == "X"
    assert bundle["platform"] == "BUFF"
    assert len(bundle["bars"]) == 40
    assert bundle["indicators"] is not None
    assert bundle["indicators"]["count"] == 40
    assert isinstance(bundle["readings"], list)


def test_kline_bundle_empty_is_safe(store: Store) -> None:
    bundle = analytics.kline_bundle(store, "none", "BUFF")
    assert bundle["bars"] == []
    assert bundle["indicators"] is None
    assert bundle["readings"] == []
