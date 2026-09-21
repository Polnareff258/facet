"""用 CSQAQ 官方文档的真实样本数据演示租赁分析（不需要 Token）。

跑法： .venv\\Scripts\\python.exe tools/demo_rental.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from facet import rental as R              # noqa: E402
from facet.cli import _print_rent_report, _snapshot_from_row  # noqa: E402
from facet.store import Store              # noqa: E402

# CSQAQ 文档 /api-187131780 返回示例（真实数据）
SAMPLES = [
    {
        "id": 7310,
        "name": "M9 刺刀（★） | 多普勒 (崭新出厂)",
        "market_hash_name": "★ M9 Bayonet | Doppler (Factory New)",
        "buff_sell_price": 6750.0, "buff_buy_price": 6550.0,
        "buff_sell_num": 1308, "buff_buy_num": 38,
        "yyyp_sell_price": 6669.5, "yyyp_sell_num": 1463,
        "yyyp_lease_price": 4.14, "yyyp_long_lease_price": 3.55,
        "yyyp_lease_annual": 11.92, "yyyp_long_lease_annual": 14.05,
        "yyyp_lease_num": 106, "yyyp_transfer_price": 7500.0,
        "steam_sell_price": 10049.0,
        "turnover_number": 12, "turnover_avg_price": 1293.65,
        "statistic": 29346, "min_float": 0.0, "max_float": 0.07,
        "sell_price_rate_1": -2.0, "sell_price_rate_7": -2.17,
        "sell_price_rate_15": -5.66, "sell_price_rate_30": -8.13,
        "sell_price_rate_90": -4.92, "sell_price_rate_180": 22.73,
        "sell_price_rate_365": -58.33,
        "_dpl": [
            {"label": "Phase1", "value": "Phase1", "paint_index": 418,
             "buff_sell_price": 6799.0, "buff_buy_price": 6550.0},
            {"label": "Phase2", "value": "Phase2", "paint_index": 419,
             "buff_sell_price": 9249.5, "buff_buy_price": 8850.0},
            {"label": "Phase3", "value": "Phase3", "paint_index": 420,
             "buff_sell_price": 6850.0, "buff_buy_price": 6560.0},
            {"label": "Phase4", "value": "Phase4", "paint_index": 421,
             "buff_sell_price": 7599.0, "buff_buy_price": 6870.0},
            {"label": "黑珍珠", "value": "Black Pearl", "paint_index": 417,
             "buff_sell_price": 63500.0, "buff_buy_price": 54000.0},
            {"label": "红宝石", "value": "Ruby", "paint_index": 415,
             "buff_sell_price": 60000.0, "buff_buy_price": 49000.0},
            {"label": "蓝宝石", "value": "Sapphire", "paint_index": 416,
             "buff_sell_price": 34498.0, "buff_buy_price": 29500.0},
        ],
    },
    # 一个「价格在涨 + 租金高」的构造样本，用来看结论如何翻转
    {
        "id": 9999,
        "name": "运动手套（★） | 迈阿密风云 (略有磨损)",
        "market_hash_name": "★ Sport Gloves | Vice (Minimal Wear)",
        "buff_sell_price": 12800.0, "yyyp_sell_price": 12600.0,
        "buff_sell_num": 420, "yyyp_sell_num": 380,
        "yyyp_lease_price": 16.5, "yyyp_long_lease_price": 13.2,
        "yyyp_lease_annual": 0, "yyyp_long_lease_annual": 0,
        "yyyp_lease_num": 28, "buff_buy_price": 12100.0,
        "steam_sell_price": 19800.0,
        "turnover_number": 46, "turnover_avg_price": 12400.0,
        "statistic": 8600,
        "sell_price_rate_7": 3.2, "sell_price_rate_30": 8.5,
        "sell_price_rate_90": 18.4, "sell_price_rate_180": 26.1,
        "sell_price_rate_365": 41.0,
        "_dpl": [],
    },
]


def main() -> int:
    store = Store("data/rental_demo.db")
    try:
        for goods in SAMPLES:
            snapshot = R.parse_detail(goods)
            store.insert_rent_snapshot(snapshot)
            print(f"已写入租赁快照：{snapshot.display_name}")

        print("\n" + "=" * 70)
        print("  从库里读回并重新分析（验证存库→读回→分析链路）")
        print("=" * 70)

        rows = store.latest_rent_all()
        pairs = []
        for row in rows:
            snapshot = _snapshot_from_row(row)
            if snapshot is None:
                continue
            # 补回中文名（真实运行时由 NameResolver 提供）
            for goods in SAMPLES:
                if goods["market_hash_name"] == snapshot.market_hash_name:
                    snapshot.display_name = goods["name"]
            yields = R.analyze_all(snapshot)
            pairs.append((snapshot, R.judge(snapshot, yields)))

        # 完整报告看第一个
        if pairs:
            _print_rent_report(pairs[0][0], R)

        # 排行
        board = R.rank(pairs, limit=10)
        print("=" * 70)
        print("  租赁收益排行")
        print("=" * 70)
        print(f"\n{'年化':>8} {'风险调整':>9} {'模式':<5} {'天数':>4} {'日租':>7} "
              f"{'出租率':>7} {'流动性':>6} 饰品")
        for row in board:
            ra = (f"{row['risk_adjusted_pct']:.2f}"
                  if row["risk_adjusted_pct"] is not None else "—")
            liq = (f"{row['liquidity_score']:.0f}"
                   if row["liquidity_score"] is not None else "—")
            print(f"{row['annualized_pct']:>7.1f}% {ra:>9} {row['mode_cn']:<5} "
                  f"{row['horizon_days']:>4} {row['daily_rent']:>7.2f} "
                  f"{row['occupancy']*100:>6.0f}% {liq:>6} "
                  f"{(row['display_name'] or '')[:30]}")
        print()
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
