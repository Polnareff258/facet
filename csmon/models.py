"""数据模型：跨源统一的报价与告警结构。

所有适配器都必须把平台私有字段翻译成这里的 SourceQuote，
这样告警引擎、存储层、看板都只依赖一套字段语义。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# 平台标识（统一大写；BUFF / YOUPIN 是两个真正的目标平台）
PLATFORM_BUFF = "BUFF"
PLATFORM_YOUPIN = "YOUPIN"
PLATFORM_STEAM = "STEAM"
KNOWN_PLATFORMS = (PLATFORM_BUFF, PLATFORM_YOUPIN, PLATFORM_STEAM)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class SourceQuote:
    """一条「某平台某饰品」的报价快照。

    价格语义（务必区分，否则告警会误判）：
      sell_price  在售价 —— 当前最低挂售价，即「现在买入要花多少」
      sell_count  在售量 —— 该平台当前挂单数量，衡量流动性
      bid_price   求购价 —— 当前最高求购出价，即「现在卖出能拿多少」
      bid_count   求购量
    """

    market_hash_name: str
    platform: str
    source: str
    sell_price: float | None = None
    sell_count: int | None = None
    bid_price: float | None = None
    bid_count: int | None = None
    currency: str = "CNY"
    observed_at: datetime = field(default_factory=utcnow)
    # 源侧自带的更新时间（部分源提供），用于判断数据新鲜度
    source_updated_at: datetime | None = None
    #: 档位标签（红宝石 / P2 / T1 / 渐变 99-100 …）。
    #:
    #: 同一个 market_hash_name 下会并存多个档位，且价差可达数倍（多普勒红宝石 vs P2），
    #: 所以「一条报价属于哪个档位」是必须落库的一等信息，不能塞进 raw 里当附注。
    #: 值为 None 表示该报价未细分档位（大多数源如此）。
    variant_label: str | None = None
    raw: dict[str, Any] | None = None

    def is_empty(self) -> bool:
        return self.sell_price is None and self.bid_price is None

    def to_row(self) -> dict[str, Any]:
        return {
            "market_hash_name": self.market_hash_name,
            "platform": self.platform,
            "source": self.source,
            "sell_price": self.sell_price,
            "sell_count": self.sell_count,
            "bid_price": self.bid_price,
            "bid_count": self.bid_count,
            "currency": self.currency,
            "observed_at": iso(self.observed_at),
            "source_updated_at": iso(self.source_updated_at) if self.source_updated_at else None,
            "variant_label": self.variant_label,
        }


@dataclass(slots=True)
class ItemRef:
    """饰品的跨平台身份。

    market_hash_name 是唯一的跨平台主键（Steam 官方命名）；
    buff_goods_id / youpin_template_id 是各平台私有 ID，需要另行解析并缓存。
    """

    market_hash_name: str
    display_name: str | None = None
    buff_goods_id: int | None = None
    youpin_template_id: int | None = None
    steam_nameid: str | None = None
    updated_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class AlertEvent:
    """一条待落库/待推送的告警。"""

    market_hash_name: str
    platform: str
    rule: str
    message: str
    severity: str = "info"
    current_price: float | None = None
    baseline_price: float | None = None
    change_percent: float | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class WatchRule:
    """单条监控规则。空值表示该维度不启用。

    规则语义：
      below            在售价 <= below 时告警（捡漏）
      above            在售价 >= above 时告警（出货）
      drop_percent     相对基准价下跌 >= drop_percent% 时告警
      rise_percent     相对基准价上涨 >= rise_percent% 时告警
      baseline_window  基准价取最近 N 小时的中位数（默认 168h = 7 天）
      cooldown_minutes 同一 (饰品,方向) 的最短告警间隔
      platforms        限定平台；空 = 全部已启用源
    """

    market_hash_name: str
    below: float | None = None
    above: float | None = None
    drop_percent: float | None = None
    rise_percent: float | None = None
    baseline_window_hours: int = 168
    cooldown_minutes: int = 240
    platforms: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "below": self.below, "above": self.above,
            "drop_percent": self.drop_percent, "rise_percent": self.rise_percent,
            "baseline_window_hours": self.baseline_window_hours,
            "cooldown_minutes": self.cooldown_minutes,
            "platforms": self.platforms, "note": self.note,
        }

    @classmethod
    def from_dict(cls, market_hash_name: str, data: dict[str, Any]) -> WatchRule:
        return cls(
            market_hash_name=market_hash_name,
            below=data.get("below"), above=data.get("above"),
            drop_percent=data.get("drop_percent"), rise_percent=data.get("rise_percent"),
            baseline_window_hours=int(data.get("baseline_window_hours", 168)),
            cooldown_minutes=int(data.get("cooldown_minutes", 240)),
            platforms=list(data.get("platforms") or []),
            note=data.get("note", ""),
        )


@dataclass(slots=True)
class FetchReport:
    """一次采集的统计，用于看板健康度与自检。"""

    source: str
    requested: int = 0
    succeeded: int = 0
    empty: int = 0
    failed: int = 0
    quotes: int = 0
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    errors: list[str] = field(default_factory=list)

    def finish(self) -> FetchReport:
        self.finished_at = utcnow()
        return self
