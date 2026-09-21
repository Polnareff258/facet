"""分析层：日线 OHLC、K 线束、跨平台价差、涨跌排行。

把「存储的原始报价」变成「看板和告警能直接用的结论」。
所有函数只读数据库，不做网络请求；这样分析永远可用，即使某个数据源挂了。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from . import indicators
from .models import PLATFORM_BUFF, PLATFORM_YOUPIN, utcnow
from .store import Store

logger = logging.getLogger(__name__)

# ── 手续费模型 ─────────────────────────────────────────────
#
# 套利算的是「净收益」，所以必须扣手续费。费率随会员等级/活动变动，
# 这里的默认值只是常见档位，请按自己的实际情况在 config.yaml 的
# arbitrage.fees 里覆盖——费率填错会让套利雷达给出错误结论。
DEFAULT_FEES: dict[str, dict[str, float]] = {
    PLATFORM_BUFF: {"sell": 0.025, "withdraw": 0.01},
    PLATFORM_YOUPIN: {"sell": 0.020, "withdraw": 0.01},
}


@dataclass(slots=True)
class Bar:
    """一根 K 线。用报价序列聚合成日线。"""

    date: str                 # YYYY-MM-DD（UTC）
    open: float
    high: float
    low: float
    close: float
    count: int                # 该日采样点数（衡量数据可信度）
    avg_count: float | None = None   # 该日在售量均值，用于量能

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "open": self.open, "high": self.high,
                "low": self.low, "close": self.close, "count": self.count,
                "avg_count": self.avg_count}


# ── 日线 OHLC ──────────────────────────────────────────────

def daily_ohlc(store: Store, market_hash_name: str, platform: str,
               days: int = 90) -> list[Bar]:
    """把报价序列聚合成日线。

    注意：这里的「日」按 UTC 划分。跨时区的用户在日线边界上会看到
    与本地日期不一致的分组，我们选择 UTC 以保持与存储一致、避免夏令时歧义。
    """
    hours = max(24, days * 24 + 24)
    rows = store.price_history(market_hash_name, platform, hours=hours, limit=20000)
    if not rows:
        return []

    grouped: dict[str, list[tuple[str, float, int | None]]] = {}
    for row in rows:
        price = row.get("sell_price")
        if price is None or price <= 0:
            continue
        observed = row["observed_at"][:10]        # ISO 字符串前 10 位即日期
        grouped.setdefault(observed, []).append(
            (f"{row['observed_at']}#{row.get('id', 0):012d}",
             float(price), row.get("sell_count")))

    bars: list[Bar] = []
    for date in sorted(grouped):
        # 排序键带上 id：同一时间戳的并列记录若不定序，
        # open/close 会在每次查询间漂移，K 线看起来像在抖
        points = sorted(grouped[date], key=lambda p: p[0])
        prices = [p[1] for p in points]
        counts = [p[2] for p in points if p[2] is not None]
        bars.append(Bar(
            date=date,
            open=prices[0],
            high=max(prices),
            low=min(prices),
            close=prices[-1],
            count=len(prices),
            avg_count=(sum(counts) / len(counts)) if counts else None,
        ))

    return bars[-days:] if days else bars


def backfill_ohlc(store: Store, days: int = 365) -> dict[str, int]:
    """为所有已采集过的 (饰品, 平台) 预计算日线缓存。

    日常查询可以直接现算（几千行很快），但当报价表累积到百万行时，
    现算会变慢——这个函数把结果落到 ohlc_cache 表，看板优先读缓存。
    """
    pairs = store.distinct_quote_pairs()
    written = 0
    for mhn, platform in pairs:
        bars = daily_ohlc(store, mhn, platform, days=days)
        if bars:
            written += store.upsert_ohlc(mhn, platform, [b.to_dict() for b in bars])
    logger.info("[analytics] OHLC 回填完成：%d 个 (饰品,平台) 组合，%d 根 K 线",
                len(pairs), written)
    return {"pairs": len(pairs), "bars": written}


# ── K 线束（给看板直接用）────────────────────────────────────

def kline_bundle(store: Store, market_hash_name: str, platform: str,
                 days: int = 90, prefer_cache: bool = True) -> dict[str, Any]:
    """OHLC + 指标 + 文字解读 + 最新报价，一次给全。"""
    bars: list[Bar] = []
    if prefer_cache:
        cached = store.get_ohlc(market_hash_name, platform, days=days)
        bars = [Bar(date=c["date"], open=c["open"], high=c["high"],
                    low=c["low"], close=c["close"], count=c["count"],
                    avg_count=c.get("avg_count")) for c in cached]
    if not bars:
        bars = daily_ohlc(store, market_hash_name, platform, days=days)

    closes = [b.close for b in bars]
    snap = indicators.snapshot(closes) if closes else None
    latest = store.latest_quote(market_hash_name, platform)
    baseline = store.baseline_median(market_hash_name, platform, hours=168)

    return {
        "market_hash_name": market_hash_name,
        "platform": platform,
        "bars": [b.to_dict() for b in bars],
        "latest": latest,
        "baseline_median_7d": baseline,
        "indicators": snap.to_dict() if snap else None,
        "readings": indicators.interpret(snap) if snap else [],
        "data_points": len(bars),
        "source": "cache" if (prefer_cache and bars and
                              store.get_ohlc(market_hash_name, platform, days=1)) else "live",
    }


# ── 跨平台价差 / 套利雷达 ───────────────────────────────────

@dataclass(slots=True)
class Spread:
    """一个饰品在两个平台之间的价差机会。"""

    market_hash_name: str
    buy_platform: str        # 在 A 平台买入
    sell_platform: str       # 在 B 平台卖出
    buy_price: float         # A 的最低价（在售价）
    sell_price: float        # B 的参考卖出价
    sell_is_bid: bool        # True=用 B 的求购价（可即时成交）；False=需挂单等
    gross_profit: float
    net_profit: float
    net_percent: float
    fee_note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_hash_name": self.market_hash_name,
            "buy_platform": self.buy_platform,
            "sell_platform": self.sell_platform,
            "buy_price": round(self.buy_price, 2),
            "sell_price": round(self.sell_price, 2),
            "sell_is_bid": self.sell_is_bid,
            "gross_profit": round(self.gross_profit, 2),
            "net_profit": round(self.net_profit, 2),
            "net_percent": round(self.net_percent, 4),
            "executable": self.sell_is_bid,
            "fee_note": self.fee_note,
        }


def compute_spread(market_hash_name: str, buy_platform: str, sell_platform: str,
                   buy_price: float, sell_price: float, sell_is_bid: bool,
                   fees: dict[str, dict[str, float]] | None = None) -> Spread | None:
    """计算一次跨平台搬砖的净收益。

    保守算法：
      买入成本 = 买入价（买家通常不付平台手续费）
      卖出收入 = 卖出价 * (1 - 卖方手续费) * (1 - 提现费)
    费率来源见 DEFAULT_FEES，可被配置覆盖。
    """
    if buy_price <= 0 or sell_price <= 0:
        return None
    table = fees or DEFAULT_FEES
    sell_fee = table.get(sell_platform, {}).get("sell", 0.025)
    withdraw = table.get(sell_platform, {}).get("withdraw", 0.01)
    buy_fee = table.get(buy_platform, {}).get("sell", 0.0)   # 买不需要，仅为口径完整

    buy_cost = buy_price * (1 + buy_fee)
    net_revenue = sell_price * (1 - sell_fee) * (1 - withdraw)
    net_profit = net_revenue - buy_cost
    return Spread(
        market_hash_name=market_hash_name,
        buy_platform=buy_platform,
        sell_platform=sell_platform,
        buy_price=buy_price,
        sell_price=sell_price,
        sell_is_bid=sell_is_bid,
        gross_profit=sell_price - buy_price,
        net_profit=net_profit,
        net_percent=net_profit / buy_cost if buy_cost else 0.0,
        fee_note=(f"{sell_platform} 卖出手续费 {sell_fee:.1%} + 提现 {withdraw:.1%}"),
    )


def spread_radar(store: Store, min_net_percent: float = 0.03,
                 min_net_profit: float = 1.0,
                 fees: dict[str, dict[str, float]] | None = None,
                 limit: int = 100) -> list[Spread]:
    """跨平台价差雷达。

    两个口径，结果里用 executable 字段区分——这个区分很重要：
      · 可即时成交（executable=True）：对手平台有求购价，买入后可立刻卖给求购单
      · 需挂单等待（executable=False）：只是两边挂售价有差，卖出要等买家
    把这两者混在一起会让人误以为「到处是套利机会」。

    数据来源：库里每个 (饰品, 平台) 的最新一条报价。
    """
    snapshot_rows = store.latest_snapshot_all(limit=5000)
    by_item: dict[str, dict[str, dict[str, Any]]] = {}
    for row in snapshot_rows:
        by_item.setdefault(row["market_hash_name"], {})[row["platform"]] = row

    results: list[Spread] = []
    for mhn, platforms in by_item.items():
        priced = {p: r for p, r in platforms.items()
                  if r.get("sell_price") and r["sell_price"] > 0}
        if len(priced) < 2:
            continue
        for buy_platform, buy_row in priced.items():
            for sell_platform, sell_row in priced.items():
                if buy_platform == sell_platform:
                    continue
                buy_price = float(buy_row["sell_price"])

                # 优先用对手平台的求购价（可即时成交），没有则退回挂售价
                bid = sell_row.get("bid_price")
                if bid and float(bid) > 0:
                    sell_price, is_bid = float(bid), True
                else:
                    sell_price, is_bid = float(sell_row["sell_price"]), False

                spread = compute_spread(mhn, buy_platform, sell_platform,
                                        buy_price, sell_price, is_bid, fees)
                if spread is None:
                    continue
                if (spread.net_percent >= min_net_percent
                        and spread.net_profit >= min_net_profit):
                    results.append(spread)

    # 每个饰品只保留最优的一条，避免同一机会在列表里刷屏
    best: dict[str, Spread] = {}
    for spread in results:
        current = best.get(spread.market_hash_name)
        if current is None or spread.net_profit > current.net_profit:
            best[spread.market_hash_name] = spread

    ranked = sorted(best.values(), key=lambda s: s.net_percent, reverse=True)
    return ranked[:limit]


# ── 涨跌排行 ───────────────────────────────────────────────

@dataclass(slots=True)
class Mover:
    market_hash_name: str
    platform: str
    current: float
    baseline: float
    change_percent: float
    sell_count: int | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_hash_name": self.market_hash_name, "platform": self.platform,
            "current": round(self.current, 2), "baseline": round(self.baseline, 2),
            "change_percent": round(self.change_percent, 4),
            "sell_count": self.sell_count, "source": self.source,
        }


def movers(store: Store, hours: int = 168, limit: int = 20,
           min_baseline_points: int = 3) -> dict[str, list[dict[str, Any]]]:
    """涨幅榜与跌幅榜（相对近 N 小时中位价）。"""
    gained: list[Mover] = []
    lost: list[Mover] = []
    for row in store.latest_snapshot_all(limit=5000):
        mhn, platform = row["market_hash_name"], row["platform"]
        current = row.get("sell_price")
        if not current or current <= 0:
            continue
        history = store.price_history(mhn, platform, hours=hours, limit=500)
        prices = [h["sell_price"] for h in history if h.get("sell_price")]
        if len(prices) < min_baseline_points:
            continue
        import statistics as _st
        baseline = _st.median(prices)
        if not baseline:
            continue
        change = (float(current) - baseline) / baseline
        mover = Mover(market_hash_name=mhn, platform=platform,
                      current=float(current), baseline=baseline,
                      change_percent=change, sell_count=row.get("sell_count"),
                      source=row.get("source", ""))
        (gained if change >= 0 else lost).append(mover)

    gained.sort(key=lambda m: m.change_percent, reverse=True)
    lost.sort(key=lambda m: m.change_percent)
    return {
        "gained": [m.to_dict() for m in gained[:limit]],
        "lost": [m.to_dict() for m in lost[:limit]],
        "window_hours": hours,
    }


# ── 流动性 ─────────────────────────────────────────────────

def liquidity_board(store: Store, limit: int = 30) -> list[dict[str, Any]]:
    """在售量榜：量大的饰品更容易成交，也更能承受套利搬砖。"""
    rows = store.latest_snapshot_all(limit=5000)
    items = []
    for row in rows:
        count = row.get("sell_count")
        if count is None:
            continue
        items.append({
            "market_hash_name": row["market_hash_name"],
            "platform": row["platform"],
            "sell_count": count,
            "sell_price": row.get("sell_price"),
            "bid_price": row.get("bid_price"),
        })
    items.sort(key=lambda r: r["sell_count"], reverse=True)
    return items[:limit]
