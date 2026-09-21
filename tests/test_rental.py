"""租赁收益分析测试。

夹具直接用 CSQAQ 官方文档给出的真实响应样本（M9 刺刀多普勒），
这样测试验证的是「解析真实数据能否得出正确结论」，而不是我自己编的数字。
"""
from __future__ import annotations

import pytest

from facet import rental as R
from facet.rental import (
    MODE_LONG,
    MODE_SHORT,
    VERDICT_GOOD,
    VERDICT_MARGINAL,
    VERDICT_POOR,
    VERDICT_UNKNOWN,
)
from facet.store import Store

# ── 真实样本（CSQAQ docs /api-187131780 的返回示例，节选关键字段）──
M9_DOPPLER = {
    "id": 7310,
    "turnover_number": 12,
    "turnover_avg_price": 1293.65,
    "buff_id": 43091,
    "yyyp_id": 754,
    "name": "M9 刺刀（★） | 多普勒 (崭新出厂)",
    "market_hash_name": "★ M9 Bayonet | Doppler (Factory New)",
    "buff_sell_price": 6750.0,
    "buff_buy_price": 6550.0,
    "buff_sell_num": 1308,
    "buff_buy_num": 38,
    "yyyp_sell_price": 6669.5,
    "yyyp_lease_num": 106,
    "yyyp_transfer_price": 7500.0,
    "yyyp_lease_price": 4.14,
    "yyyp_long_lease_price": 3.55,
    "yyyp_lease_annual": 11.92,
    "yyyp_long_lease_annual": 14.05,
    "yyyp_sell_num": 1463,
    "steam_sell_price": 10049.0,
    "yyyp_buy_num": 77,
    "yyyp_buy_price": 57000.0,
    "sell_price_rate_1": -2.0,
    "sell_price_rate_7": -2.17,
    "sell_price_rate_15": -5.66,
    "sell_price_rate_30": -8.13,
    "sell_price_rate_90": -4.92,
    "sell_price_rate_180": 22.73,
    "sell_price_rate_365": -58.33,
    "statistic": 29346,
    "min_float": 0.0,
    "max_float": 0.07,
    "_dpl": [
        {"key": 146, "label": "Phase1", "value": "Phase1", "paint_index": 418,
         "buff_sell_price": 6799.0, "buff_buy_price": 6550.0},
        {"key": 147, "label": "Phase2", "value": "Phase2", "paint_index": 419,
         "buff_sell_price": 9249.5, "buff_buy_price": 8850.0},
        {"key": 150, "label": "黑珍珠", "value": "Black Pearl", "paint_index": 417,
         "buff_sell_price": 63500.0, "buff_buy_price": 54000.0},
        {"key": 151, "label": "红宝石", "value": "Ruby", "paint_index": 415,
         "buff_sell_price": 60000.0, "buff_buy_price": 49000.0},
        {"key": 152, "label": "蓝宝石", "value": "Sapphire", "paint_index": 416,
         "buff_sell_price": 34498.0, "buff_buy_price": 29500.0},
    ],
}


def _snapshot(**overrides) -> R.RentSnapshot:
    data = dict(M9_DOPPLER)
    data.update(overrides)
    return R.parse_detail(data)


# ── 解析 ───────────────────────────────────────────────────

def test_parse_detail_maps_rent_fields() -> None:
    snap = _snapshot()
    assert snap.market_hash_name == "★ M9 Bayonet | Doppler (Factory New)"
    assert snap.display_name == "M9 刺刀（★） | 多普勒 (崭新出厂)"
    assert snap.short_daily_rent == 4.14
    assert snap.long_daily_rent == 3.55
    assert snap.short_annual_pct == 11.92
    assert snap.long_annual_pct == 14.05
    assert snap.lease_listings == 106
    assert snap.transfer_price == 7500.0
    assert snap.supply == 29346
    assert snap.turnover_number == 12


def test_parse_detail_price_changes() -> None:
    snap = _snapshot()
    assert snap.price_change_pct[7] == -2.17
    assert snap.price_change_pct[90] == -4.92
    assert snap.price_change_pct[180] == 22.73
    assert snap.price_change_pct[365] == -58.33


def test_parse_detail_phases_carry_paint_index() -> None:
    """dpl 数组给出相位 ↔ paint_index 映射 —— 这是权威的档位对照来源。"""
    snap = _snapshot()
    by_label = {p.label: p for p in snap.phases}
    assert by_label["红宝石"].paint_index == 415
    assert by_label["蓝宝石"].paint_index == 416
    assert by_label["黑珍珠"].paint_index == 417
    assert by_label["Phase1"].paint_index == 418
    assert by_label["Phase2"].buff_sell_price == 9249.5


def test_parse_detail_missing_fields_stay_none() -> None:
    """缺字段必须是 None，不能填 0 —— 「没有数据」与「数据是 0」在收益计算里不同。"""
    snap = R.parse_detail({"market_hash_name": "X", "buff_sell_price": None,
                           "yyyp_lease_price": ""})
    assert snap.buff_sell_price is None
    assert snap.short_daily_rent is None
    assert snap.market_price is None
    assert snap.price_change_pct == {}


def test_market_price_uses_lowest_sell_not_buy() -> None:
    """买入价取各平台最低**在售价**。

    这条很重要：样本里 yyyp_buy_price=57000 明显异常（是卖出价的 8 倍，
    疑似押金或总额口径），若参与取价会把收益率分母放大 8 倍，
    让一个亏钱的标的看着像微利。
    """
    snap = _snapshot()
    assert snap.market_price == 6669.5          # min(6750, 6669.5, 10049)
    assert snap.sell_platform == "YOUPIN"        # 最便宜的平台，决定手续费口径


# ── 年化与出租率 ───────────────────────────────────────────

def test_theoretical_annual_is_full_occupancy_ceiling() -> None:
    """理论年化 = 日租金 × 365 ÷ 买入价 —— 满租上限，任何平台都达不到。"""
    snap = _snapshot()
    yields = R.analyze_all(snap, horizons=(90,))
    short = next(y for y in yields if y.mode == MODE_SHORT)
    assert short.theoretical_annual_pct == pytest.approx(22.66, abs=0.05)
    # 平台口径必须低于满租理论值
    assert short.platform_annual_pct == pytest.approx(11.92)
    assert short.platform_annual_pct < short.theoretical_annual_pct


def test_implied_occupancy_derived_from_platform_annual() -> None:
    """出租率由「平台年化 ÷ 理论年化」推算，且标注为推算值。"""
    snap = _snapshot()
    short = R.implied_occupancy(11.92, 22.66)
    long_ = R.implied_occupancy(14.05, 19.43)
    assert short == pytest.approx(0.526, abs=0.005)
    assert long_ == pytest.approx(0.723, abs=0.005)


def test_implied_occupancy_refuses_impossible_ratio() -> None:
    """平台年化高于满租理论值时，说明口径不同 —— 宁可不给数字也不猜。"""
    assert R.implied_occupancy(30.0, 20.0) is None
    assert R.implied_occupancy(None, 20.0) is None
    assert R.implied_occupancy(10.0, 0.0) is None
    assert R.implied_occupancy(10.0, None) is None


def test_long_lease_beats_short_despite_lower_daily_rent() -> None:
    """核心洞察：长租日租金更低（3.55 < 4.14），但折算年化更高（14.05 > 11.92）。

    原因在空置率：长租租期连续、等待期短。只看日租金高低会选错方案，
    这条测试把这个结论钉住。
    """
    snap = _snapshot()
    assert snap.long_daily_rent < snap.short_daily_rent
    assert snap.long_annual_pct > snap.short_annual_pct

    yields = R.analyze_all(snap, horizons=(90,))
    long_y = next(y for y in yields if y.mode == MODE_LONG)
    short_y = next(y for y in yields if y.mode == MODE_SHORT)
    assert long_y.occupancy > short_y.occupancy      # 长租空置率更低
    assert long_y.gross_rent > short_y.gross_rent     # 按出租率折算后长租反而多


# ── 收益分解 ───────────────────────────────────────────────

def test_analyze_decomposes_return() -> None:
    snap = _snapshot()
    result = R.analyze(snap, horizon_days=90, mode=MODE_LONG)
    assert result is not None

    # 毛租金 = 日租 × 天数 × 出租率
    assert result.gross_rent == pytest.approx(3.55 * 90 * result.occupancy, rel=1e-6)
    # 净租金扣了租赁抽成
    assert result.net_rent < result.gross_rent
    # 90 天用实测涨跌 -4.92%
    assert result.price_move == pytest.approx(6669.5 * -0.0492, rel=1e-3)
    # 卖出成本 = |卖价| × (平台抽成 + 提现)
    assert result.exit_cost == pytest.approx(
        abs(6669.5 + result.price_move) * (0.020 + 0.01), rel=1e-3)
    # 总收益 = 净租金 + 价格变动 − 卖出成本
    assert result.total_return == pytest.approx(
        result.net_rent + result.price_move - result.exit_cost, rel=1e-9)


def test_falling_price_overwhelms_rent() -> None:
    """这个真实标的 90 天跌 4.92%，租金覆盖不了 —— 结论必须是不建议。

    这正是租赁分析的价值：单看「日租 3.55、年化 14%」很像好生意，
    把价格波动和手续费算进去却是负收益。
    """
    snap = _snapshot()
    yields = R.analyze_all(snap, horizons=(90,))
    best = max(yields, key=lambda y: y.annualized_pct)
    assert best.annualized_pct < 0            # 真实样本在此周期是亏的

    verdict = R.judge(snap, yields)
    assert verdict.verdict == VERDICT_POOR
    assert "覆盖不了" in verdict.headline or "不建议" in verdict.headline


def test_positive_trend_gives_positive_verdict() -> None:
    """价格若上涨，同样的租金就变成好生意 —— 验证结论随数据变化。"""
    snap = _snapshot(sell_price_rate_90=30.0, sell_price_rate_30=12.0)
    yields = R.analyze_all(snap, horizons=(90,))
    best = max(yields, key=lambda y: y.annualized_pct)
    assert best.annualized_pct > 100          # 30% 价格涨幅主导了结果
    verdict = R.judge(snap, yields)
    assert verdict.verdict == VERDICT_GOOD


def test_fees_materially_reduce_return() -> None:
    """手续费必须真的被扣掉，且可覆盖。

    日租金常常只有价格的 0.05–0.2%，而一次卖出抽成 2.5% ——
    相当于吃掉十几天的租金。费率填错会让结论反向。
    """
    snap = _snapshot()
    hi_fee = R.analyze(snap, horizon_days=90, mode=MODE_LONG,
                       sell_fee={"YOUPIN": 0.10}, withdraw_fee=0.02)
    lo_fee = R.analyze(snap, horizon_days=90, mode=MODE_LONG,
                       sell_fee={"YOUPIN": 0.00}, withdraw_fee=0.0)
    assert hi_fee is not None and lo_fee is not None
    assert hi_fee.exit_cost > lo_fee.exit_cost
    assert hi_fee.total_return < lo_fee.total_return


def test_rent_fee_applied() -> None:
    snap = _snapshot()
    free = R.analyze(snap, horizon_days=90, mode=MODE_LONG,
                     rent_fee={"YOUPIN": 0.0})
    taxed = R.analyze(snap, horizon_days=90, mode=MODE_LONG,
                      rent_fee={"YOUPIN": 0.5})
    assert free is not None and taxed is not None
    assert taxed.net_rent == pytest.approx(free.net_rent * 0.5, rel=1e-6)


def test_analyze_returns_none_without_price_or_rent() -> None:
    assert R.analyze(_snapshot(buff_sell_price=None, yyyp_sell_price=None,
                               steam_sell_price=None)) is None
    assert R.analyze(_snapshot(yyyp_lease_price=None,
                               yyyp_long_lease_price=None)) is None


def test_price_move_extrapolation_is_flagged() -> None:
    """没有正对窗口时用线性外推，且说明里必须标明是外推。"""
    snap = _snapshot()
    rate, note = R.estimate_price_move(snap, 45)
    assert 45 not in snap.price_change_pct            # 确实是外推
    assert "外推" in note
    # 45 天距离 30 天更近
    assert rate == pytest.approx(snap.price_change_pct[30] * 45 / 30 / 100, rel=1e-6)


def test_price_move_uses_exact_window_when_available() -> None:
    snap = _snapshot()
    rate, note = R.estimate_price_move(snap, 30)
    assert rate == pytest.approx(-0.0813)
    assert "实测" in note


# ── 风险与流动性 ───────────────────────────────────────────

def test_volatility_estimate_is_positive() -> None:
    snap = _snapshot()
    vol = R.estimate_volatility(snap)
    assert vol is not None and vol > 0


def test_volatility_needs_enough_windows() -> None:
    snap = _snapshot(sell_price_rate_1=-2.0, sell_price_rate_7=-2.17)
    # 只剩一个 >=7 天的窗口
    for key in (15, 30, 90, 180, 365):
        snap.price_change_pct.pop(key, None)
    assert R.estimate_volatility(snap) is None


def test_liquidity_score_from_supply_sellnum_turnover() -> None:
    snap = _snapshot()
    score = R.liquidity_score(snap)
    assert score is not None and 0 < score <= 100


def test_liquidity_none_when_too_few_dimensions() -> None:
    """缺维度就不给分 —— 缺数据的「高分」会误导决策。"""
    snap = _snapshot(statistic=None, sell_num=None, turnover_number=None)
    assert R.liquidity_score(snap) is None


def test_risk_adjusted_none_without_volatility() -> None:
    snap = _snapshot()
    for key in list(snap.price_change_pct):
        snap.price_change_pct.pop(key)
    result = R.analyze(snap, horizon_days=90, mode=MODE_LONG)
    assert result is not None
    assert result.risk_adjusted_pct is None


# ── 结论 ───────────────────────────────────────────────────

def test_judge_reports_no_data_gracefully() -> None:
    verdict = R.judge(_snapshot(), [])
    assert verdict.verdict == VERDICT_UNKNOWN
    assert verdict.best is None


def test_judge_mentions_lease_competition() -> None:
    """出租挂单相对在售量很高时，空置风险要提示出来。"""
    snap = _snapshot(yyyp_lease_num=900, yyyp_sell_num=1463,
                     sell_price_rate_90=25.0)
    yields = R.analyze_all(snap, horizons=(90,))
    verdict = R.judge(snap, yields)
    assert any("竞争" in c for c in verdict.caveats)


def test_judge_always_warns_about_inferred_occupancy() -> None:
    """出租率是推算值，任何结论都必须带上这条提醒。"""
    snap = _snapshot(sell_price_rate_90=20.0)
    verdict = R.judge(snap, R.analyze_all(snap, horizons=(90,)))
    assert any("推算" in c for c in verdict.caveats)
    assert any("外推" in c or "外部事件" in c for c in verdict.caveats)


def test_judge_flags_low_liquidity() -> None:
    # 注意用**原始字段名**（yyyp_sell_num），而不是解析后的字段名
    snap = _snapshot(statistic=50, yyyp_sell_num=3, turnover_number=1,
                     sell_price_rate_90=25.0)
    yields = R.analyze_all(snap, horizons=(90,))
    verdict = R.judge(snap, yields)
    assert any("流动性" in c for c in verdict.caveats)


def test_verdict_thresholds() -> None:
    """年化分档：>=25 值得考虑，>=10 勉强，<10 不建议。"""
    good = R.judge(_snapshot(sell_price_rate_90=25.0),
                   R.analyze_all(_snapshot(sell_price_rate_90=25.0), horizons=(90,)))
    assert good.verdict in (VERDICT_GOOD, VERDICT_MARGINAL)

    poor = R.judge(_snapshot(), R.analyze_all(_snapshot(), horizons=(90,)))
    assert poor.verdict == VERDICT_POOR


def test_rank_sorts_by_annualized() -> None:
    a = _snapshot(sell_price_rate_90=40.0)
    b = _snapshot(sell_price_rate_90=5.0)
    pairs = [(a, R.judge(a, R.analyze_all(a, horizons=(90,)))),
             (b, R.judge(b, R.analyze_all(b, horizons=(90,))))]
    rows = R.rank(pairs, limit=10)
    assert len(rows) == 2
    assert rows[0]["annualized_pct"] >= rows[1]["annualized_pct"]
    assert {"mode_cn", "daily_rent", "occupancy", "liquidity_score"} <= set(rows[0])


def test_rank_filters_by_liquidity() -> None:
    snap = _snapshot(sell_price_rate_90=40.0, statistic=10, sell_num=1,
                     turnover_number=0)
    pairs = [(snap, R.judge(snap, R.analyze_all(snap, horizons=(90,))))]
    assert R.rank(pairs, min_liquidity=90.0) == []


# ── 存储 ───────────────────────────────────────────────────

def test_rent_snapshot_roundtrip(store: Store) -> None:
    snap = _snapshot()
    snapshot_id = store.insert_rent_snapshot(snap)
    assert snapshot_id > 0

    row = store.latest_rent_snapshot(snap.market_hash_name)
    assert row is not None
    assert row["short_daily_rent"] == 4.14
    assert row["long_annual_pct"] == 14.05
    assert row["market_price"] == 6669.5
    assert row["lease_listings"] == 106


def test_rent_stats(store: Store) -> None:
    store.insert_rent_snapshot(_snapshot())
    store.insert_rent_snapshot(_snapshot(market_hash_name="X",
                                         yyyp_lease_price=None,
                                         yyyp_long_lease_price=None))
    stats = store.rent_stats()
    assert stats["snapshots"] == 2
    assert stats["items"] == 2
    assert stats["items_with_rent"] == 1     # 第二个没有租价


def test_store_stats_includes_rent(store: Store) -> None:
    store.insert_rent_snapshot(_snapshot())
    stats = store.stats()
    assert stats["rent_snapshots"] == 1
    assert stats["rent_items"] == 1


def test_snapshot_from_row_keeps_market_price(store: Store) -> None:
    """存库再读回时，收益率分母的口径必须与采集时一致。"""
    from facet.cli import _snapshot_from_row

    snap = _snapshot()
    store.insert_rent_snapshot(snap)
    row = store.latest_rent_snapshot(snap.market_hash_name)
    restored = _snapshot_from_row(row)
    assert restored is not None
    assert restored.market_price == pytest.approx(snap.market_price)
    assert restored.price_change_pct[90] == -4.92
    assert len(restored.phases) == len(snap.phases)
