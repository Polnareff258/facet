"""为演示/验证灌入多日历史数据，让 K 线与指标有足够样本。

仅用于本地演示与人工验证，不影响真实采集。
用法： python tools/seed_demo_history.py [--db data/demo.db] [--days 180]
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from facet.models import PLATFORM_BUFF, PLATFORM_YOUPIN, SourceQuote, iso, utcnow  # noqa: E402
from facet.store import Store  # noqa: E402

# 有确定性趋势 + 随机波动的合成价格，便于肉眼判断指标是否正确
SERIES = [
    ("AK-47 | Redline (Field-Tested)", 100.0, 0.15, 0.02),
    ("AWP | Asiimov (Field-Tested)", 420.0, -0.22, 0.025),
    ("★ M9 Bayonet | Bright Water (Well-Worn)", 2180.0, 0.05, 0.03),
]


def synth(base: float, drift: float, vol: float, days: int,
          rng: random.Random) -> list[float]:
    """几何随机游走 + 线性趋势。"""
    prices: list[float] = []
    price = base
    for day in range(days):
        trend = 1 + drift / days
        shock = 1 + rng.gauss(0, vol)
        # 加一点周期性，让 K 线有可辨认的波动形态
        wave = 1 + 0.012 * math.sin(day / 6.0)
        price = max(1.0, price * trend * shock * wave)
        prices.append(round(price, 2))
    return prices


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/demo.db")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--samples-per-day", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--reset", action="store_true",
                        help="先清空该库的报价与 K 线缓存")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    store = Store(args.db)
    try:
        if args.reset:
            store._conn.execute("DELETE FROM quotes WHERE source = 'demo'")
            store._conn.execute("DELETE FROM ohlc_cache")
            store._conn.commit()
            print("已清空旧的演示数据")

        total = 0
        for name, base, drift, vol in SERIES:
            for platform in (PLATFORM_BUFF, PLATFORM_YOUPIN):
                # 两个平台保持一个稳定的价差，便于验证套利雷达
                platform_factor = 0.97 if platform == PLATFORM_YOUPIN else 1.0
                daily = synth(base * platform_factor, drift, vol, args.days, rng)
                quotes: list[SourceQuote] = []
                for day, close in enumerate(daily):
                    # 每天若干次采样，围绕当日收盘价小幅抖动
                    for k in range(args.samples_per_day):
                        hours_back = (args.days - day) * 24 - k * (24 / args.samples_per_day)
                        price = round(close * (1 + rng.gauss(0, 0.004)), 2)
                        count = max(1, int(rng.gauss(120, 40)))
                        quotes.append(SourceQuote(
                            market_hash_name=name, platform=platform, source="demo",
                            sell_price=max(0.5, price), sell_count=count,
                            bid_price=round(max(0.5, price) * 0.94, 2),
                            bid_count=max(1, count // 3),
                            observed_at=utcnow() - timedelta(hours=hours_back),
                        ))
                total += store.insert_quotes(quotes)
                print(f"  {name[:44]:<44} {platform:<7} {len(quotes)} 条")

        # 回填日线缓存
        from facet.analytics import backfill_ohlc
        result = backfill_ohlc(store, days=args.days + 5)
        print(f"\n共写入 {total} 条报价，日线缓存 {result['bars']} 根"
              f"（{result['pairs']} 个组合）")
        print(f"库：{args.db}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
