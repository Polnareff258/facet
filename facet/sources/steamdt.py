"""SteamDT 开放平台适配器（补充源）。

特点：批量接口一次可查 100 个饰品，且同时返回在售价 + 求购价 + 双方数量，
是唯一在官方层面同时给出「求购价」的授权源；但批量限额为 1 次/分钟，
因此定位是低频兜底与求购价来源，而不是高频轮询主力。

实测/文档依据：
  - POST https://open.steamdt.com/open/cs2/v1/price/batch
    Header: Authorization: Bearer <api_key>
    Body:   {"marketHashNames": [...]}（1..100）
    响应:   data[] = {marketHashName, dataList[] = {platform, platformItemId,
                     sellPrice, sellCount, biddingPrice, biddingCount, updateTime}}
    updateTime 是 epoch 毫秒
  - 限额：批量 1 次/分钟；单件 60 次/分钟
  - 平台覆盖以返回的 platform 枚举为准（实测含 BUFF / YOUPIN / STEAM）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence

import requests

from ..models import PLATFORM_BUFF, PLATFORM_STEAM, PLATFORM_YOUPIN, ItemRef, SourceQuote
from ..ratelimit import RateLimitExceeded
from .base import SourceAdapter, SourceUnavailable

logger = logging.getLogger(__name__)

API_BASE = "https://open.steamdt.com"
ENDPOINT_BATCH = "/open/cs2/v1/price/batch"
ENDPOINT_SINGLE = "/open/cs2/v1/price/single"

# SteamDT 的 platform 枚举 -> 本项目平台标识
PLATFORM_MAP = {
    "BUFF": PLATFORM_BUFF,
    "YOUPIN": PLATFORM_YOUPIN,
    "UUYP": PLATFORM_YOUPIN,      # 不同接口对悠悠有品的拼写不一致，统一归一
    "STEAM": PLATFORM_STEAM,
}


class SteamDtAdapter(SourceAdapter):
    name = "steamdt"
    platforms = (PLATFORM_BUFF, PLATFORM_YOUPIN, PLATFORM_STEAM)
    provides_sell = True
    provides_bid = True
    provides_count = True
    required_credential = "api_key"
    credential_env = "STEAMDT_API_KEY"
    degraded_note = ("无 API Key 则取不到价；在 https://steamdt.com "
                     "个人中心-API管理 申请（限时免费）")

    def __init__(self, config, gates=None) -> None:
        super().__init__(config, gates)
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def preflight(self) -> None:
        if not self.config.api_key:
            raise SourceUnavailable(
                "缺少 STEAMDT_API_KEY。在 https://steamdt.com 个人中心-API管理 申请后写入 .env")

    def fetch(self, items: Sequence[ItemRef]) -> list[SourceQuote]:
        quotes: list[SourceQuote] = []
        for batch in self.chunked(items, min(self.config.batch_size, 100)):
            quotes.extend(self._fetch_batch(batch))
        return quotes

    def _fetch_batch(self, batch: Sequence[ItemRef]) -> list[SourceQuote]:
        gate = self.gates.gate(self.name, "price_batch", self.config.min_interval)
        try:
            gate.acquire()
        except RateLimitExceeded as exc:
            logger.info("[steamdt] 跳过一批：%s", exc)
            return []

        payload = {"marketHashNames": [i.market_hash_name for i in batch]}
        try:
            resp = self._session.post(
                API_BASE + ENDPOINT_BATCH,
                json=payload,
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                timeout=self.config.timeout,
            )
        except requests.RequestException as exc:
            gate.report_transient_failure(str(exc), cooldown=30.0)
            logger.warning("[steamdt] 网络错误: %s", exc)
            return []

        if resp.status_code == 429:
            cooldown = gate.report_rate_limit("HTTP 429", _retry_after(resp))
            logger.warning("[steamdt] 限流，冷却 %.0fs", cooldown)
            return []
        if resp.status_code in (401, 403):
            gate.report_success()
            logger.error("[steamdt] 鉴权失败 HTTP %s：请检查 API Key", resp.status_code)
            return []
        if resp.status_code >= 500:
            gate.report_transient_failure(f"HTTP {resp.status_code}", cooldown=20.0)
            return []

        gate.report_success()
        try:
            body = resp.json()
        except ValueError:
            return []

        if not body.get("success", False):
            code = body.get("errorCode")
            msg = body.get("errorMsg")
            # 业务限流码走冷却，其余只记录
            if code in (4029,) or "限流" in str(msg) or "频繁" in str(msg):
                cooldown = gate.report_rate_limit(str(msg))
                logger.warning("[steamdt] 业务限流，冷却 %.0fs", cooldown)
            else:
                logger.warning("[steamdt] 业务错误 code=%s msg=%s", code, msg)
            return []

        return self._parse(body.get("data") or [])

    def _parse(self, data: Any) -> list[SourceQuote]:
        if not isinstance(data, list):
            return []
        quotes: list[SourceQuote] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            mhn = entry.get("marketHashName")
            if not mhn:
                continue
            for platform_info in (entry.get("dataList") or []):
                if not isinstance(platform_info, dict):
                    continue
                raw_platform = str(platform_info.get("platform") or "").upper()
                platform = PLATFORM_MAP.get(raw_platform)
                if platform is None:
                    continue  # 未纳入监控范围的第三方平台
                updated = _from_epoch_ms(platform_info.get("updateTime"))
                quotes.append(SourceQuote(
                    market_hash_name=mhn,
                    platform=platform,
                    source=self.name,
                    sell_price=_positive(platform_info.get("sellPrice")),
                    sell_count=_positive_int(platform_info.get("sellCount")),
                    bid_price=_positive(platform_info.get("biddingPrice")),
                    bid_count=_positive_int(platform_info.get("biddingCount")),
                    source_updated_at=updated,
                    raw={"platform_item_id": platform_info.get("platformItemId")},
                ))
        return quotes

    def close(self) -> None:
        self._session.close()


def _retry_after(resp: requests.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(1.0, float(raw))
    except ValueError:
        return None


def _from_epoch_ms(value: Any) -> datetime | None:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    # 兼容秒级时间戳
    if ms < 10_000_000_000:
        ms *= 1000
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _positive(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _positive_int(value: Any) -> int | None:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None
