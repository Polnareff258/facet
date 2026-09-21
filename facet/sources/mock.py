"""离线回放源：让整条链路（采集→存储→告警→看板）不依赖外网也能跑通与自测。

两种用法：
  1) 确定性随机游走（seed 固定则结果可复现），用于测试与演示
  2) 场景脚本：显式指定「第 N 次采样时把某饰品打到某价」，用于验证告警触发
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from ..models import PLATFORM_BUFF, PLATFORM_YOUPIN, ItemRef, SourceQuote
from .base import SourceAdapter

# 基准价表：没有给定时用它生成合理的起始价，避免出现 1 元、99999 元这类噪声
DEFAULT_BASE_PRICE = 100.0


class MockAdapter(SourceAdapter):
    """确定性伪随机行情源。"""

    name = "mock"
    platforms = (PLATFORM_BUFF, PLATFORM_YOUPIN)
    provides_sell = True
    provides_bid = True
    provides_count = True

    def __init__(self, config, gates=None, seed: int = 20260101,
                 base_prices: dict[str, float] | None = None) -> None:
        super().__init__(config, gates)
        self._rng = random.Random(seed)
        self._base_prices = dict(base_prices or {})
        self._call_count = 0
        #: 场景脚本：{market_hash_name: {轮次: (sell_price, sell_count)}}
        self.scenarios: dict[str, dict[int, tuple[float | None, int | None]]] = {}

    def script(self, market_hash_name: str, turn: int,
               sell_price: float | None, sell_count: int | None = None) -> None:
        self.scenarios.setdefault(market_hash_name, {})[turn] = (sell_price, sell_count)

    def fetch(self, items: Sequence[ItemRef]) -> list[SourceQuote]:
        self._call_count += 1
        quotes: list[SourceQuote] = []
        for item in items:
            base = self._base_prices.get(item.market_hash_name, DEFAULT_BASE_PRICE)
            for platform in self.platforms:
                quotes.append(self._quote_for(item, platform, base))
        return quotes

    def _quote_for(self, item: ItemRef, platform: str, base: float) -> SourceQuote:
        planned = self.scenarios.get(item.market_hash_name, {}).get(self._call_count)
        if planned is not None:
            sell_price, sell_count = planned
        else:
            drift = self._rng.uniform(-0.03, 0.03)
            sell_price = round(base * (1 + drift), 2)
            sell_count = self._rng.randint(5, 300)

        # 悠悠有品的求购价通常略低于在售价，BUFF 略低一些
        spread = 0.94 if platform == PLATFORM_YOUPIN else 0.97
        return SourceQuote(
            market_hash_name=item.market_hash_name,
            platform=platform,
            source=self.name,
            sell_price=sell_price,
            sell_count=sell_count,
            bid_price=round(sell_price * spread, 2) if sell_price else None,
            bid_count=self._rng.randint(1, 50),
            observed_at=datetime.now(timezone.utc),
            raw={"mock": True, "turn": self._call_count},
        )


class ReplayAdapter(SourceAdapter):
    """从录制好的报价记录回放（用于回归测试真实响应结构）。"""

    name = "replay"
    platforms = (PLATFORM_BUFF, PLATFORM_YOUPIN)
    provides_sell = True
    provides_bid = True
    provides_count = True

    def __init__(self, config, gates=None,
                 records: list[dict[str, Any]] | None = None) -> None:
        super().__init__(config, gates)
        self.records = list(records or [])
        self._cursor = 0

    def fetch(self, items: Sequence[ItemRef]) -> list[SourceQuote]:
        wanted = {i.market_hash_name for i in items}
        out: list[SourceQuote] = []
        for rec in self.records:
            if rec.get("market_hash_name") not in wanted:
                continue
            out.append(SourceQuote(
                market_hash_name=rec["market_hash_name"],
                platform=rec["platform"],
                source=rec.get("source", self.name),
                sell_price=rec.get("sell_price"),
                sell_count=rec.get("sell_count"),
                bid_price=rec.get("bid_price"),
                bid_count=rec.get("bid_count"),
                source_updated_at=(datetime.fromisoformat(rec["source_updated_at"])
                                   if rec.get("source_updated_at") else None),
                raw=rec.get("raw"),
            ))
        self._cursor += 1
        return out
