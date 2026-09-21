"""CSQAQ 数据开放 API 适配器（主力源）。

为什么它是主力：
  一次 POST 即同时返回 BUFF、悠悠有品、Steam 三个平台的在售价与在售量，
  覆盖了本项目两个目标平台，无需分别对接。

实测/文档依据（docs/DATA_SOURCES.md 有完整证据）：
  - POST https://api.csqaq.com/api/v1/goods/getPriceByMarketHashName
  - Header: ApiToken: <token>；Body: {"marketHashNameList": [...]}（≤50）
  - 响应: data.success 为 mhn -> 价格字典；data.error 为未能解析的名称列表
  - 限额: 不限次，单 IP 1 次/秒
  - Token 注册即得，但必须在官网绑定本机白名单 IP，否则返回 401/400
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

import requests

from ..models import PLATFORM_BUFF, PLATFORM_STEAM, PLATFORM_YOUPIN, ItemRef, SourceQuote
from ..ratelimit import RateLimitExceeded
from .base import SourceAdapter, SourceUnavailable

logger = logging.getLogger(__name__)

API_BASE = "https://api.csqaq.com/api/v1"
ENDPOINT_PRICE_BY_MHN = "/goods/getPriceByMarketHashName"
ENDPOINT_GOODS_ID = "/goods/getGoodsIdByKeyWord"
ENDPOINT_GOOD_DETAIL = "/info/good"

# CSQAQ 的价格字段 -> 本项目的平台标识
SELL_FIELD = {
    PLATFORM_BUFF: ("buffSellPrice", "buffSellNum"),
    PLATFORM_YOUPIN: ("yyypSellPrice", "yyypSellNum"),
    PLATFORM_STEAM: ("steamSellPrice", "steamSellNum"),
}


class CsqaqAdapter(SourceAdapter):
    name = "csqaq"
    platforms = (PLATFORM_BUFF, PLATFORM_YOUPIN, PLATFORM_STEAM)
    provides_sell = True
    provides_bid = False
    provides_count = True
    required_credential = "api_token"
    credential_env = "CSQAQ_TOKEN"
    degraded_note = ("无 Token 则完全取不到价；注册 https://csqaq.com 取 Token "
                     "并在官网绑定本机白名单 IP")

    def __init__(self, config, gates=None) -> None:
        super().__init__(config, gates)
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "facet/0.1 (+local monitor)",
        })

    def preflight(self) -> None:
        if not self.config.api_token:
            raise SourceUnavailable(
                "缺少 CSQAQ_TOKEN。注册 https://csqaq.com 后取 Token，"
                "在官网绑定本机白名单 IP，再写入 .env")

    # ── 采集 ───────────────────────────────────────────────

    def fetch(self, items: Sequence[ItemRef]) -> list[SourceQuote]:
        quotes: list[SourceQuote] = []
        for batch in self.chunked(items, min(self.config.batch_size, 50)):
            quotes.extend(self._fetch_batch(batch))
        return quotes

    def _fetch_batch(self, batch: Sequence[ItemRef]) -> list[SourceQuote]:
        gate = self.gates.gate(self.name, "price", self.config.min_interval)
        try:
            gate.acquire()
        except RateLimitExceeded as exc:
            logger.info("[csqaq] 跳过一批：%s", exc)
            return []

        payload = {"marketHashNameList": [i.market_hash_name for i in batch]}
        try:
            resp = self._session.post(
                API_BASE + ENDPOINT_PRICE_BY_MHN,
                json=payload,
                headers={"ApiToken": self.config.api_token or ""},
                timeout=self.config.timeout,
            )
        except requests.RequestException as exc:
            gate.report_transient_failure(str(exc), cooldown=30.0)
            logger.warning("[csqaq] 网络错误: %s", exc)
            return []

        if resp.status_code == 429 or resp.status_code == 503:
            cooldown = gate.report_rate_limit(f"HTTP {resp.status_code}")
            logger.warning("[csqaq] 限流，冷却 %.0fs", cooldown)
            return []
        if resp.status_code in (401, 400):
            gate.report_success()
            logger.error("[csqaq] 鉴权失败 HTTP %s：请检查 Token 与白名单 IP 绑定", resp.status_code)
            return []
        if resp.status_code >= 500:
            gate.report_transient_failure(f"HTTP {resp.status_code}", cooldown=20.0)
            return []

        gate.report_success()
        try:
            body = resp.json()
        except ValueError:
            logger.warning("[csqaq] 响应非 JSON: %s", resp.text[:160])
            return []

        if body.get("code") not in (200, 0):
            logger.warning("[csqaq] 业务错误 code=%s msg=%s", body.get("code"), body.get("msg"))
            return []

        return self._parse(body.get("data") or {})

    def _parse(self, data: dict[str, Any]) -> list[SourceQuote]:
        success = data.get("success") or {}
        error = data.get("error") or []
        if error:
            logger.info("[csqaq] %d 个名称未被识别（示例: %s）", len(error), error[:3])

        quotes: list[SourceQuote] = []
        for mhn, info in success.items():
            if not isinstance(info, dict):
                continue
            for platform, (price_field, count_field) in SELL_FIELD.items():
                price = _to_float(info.get(price_field))
                count = _to_int(info.get(count_field))
                if price is None and count is None:
                    continue
                quotes.append(SourceQuote(
                    market_hash_name=info.get("marketHashName") or mhn,
                    platform=platform,
                    source=self.name,
                    sell_price=price,
                    sell_count=count,
                    raw={"goodId": info.get("goodId"), "name": info.get("name")},
                ))
        return quotes

    # ── 名称解析（把中文关键词变成 market_hash_name）────────

    def resolve_keyword(self, keyword: str) -> list[dict[str, Any]]:
        """联想查询饰品 ID 信息，用于 CLI 里按关键词加监控。"""
        gate = self.gates.gate(self.name, "goods_id", self.config.min_interval)
        gate.acquire()
        try:
            resp = self._session.get(
                API_BASE + ENDPOINT_GOODS_ID,
                params={"keyWords": keyword},
                headers={"ApiToken": self.config.api_token or ""},
                timeout=self.config.timeout,
            )
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("[csqaq] 联想查询失败: %s", exc)
            return []
        finally:
            gate.report_success()

        data = body.get("data") or []
        if isinstance(data, dict):
            data = data.get("list") or data.get("data") or []
        return [d for d in data if isinstance(d, dict)]

    # ── 单件详情（含租赁数据）───────────────────────────────

    def fetch_good_detail(self, good_id: int) -> dict[str, Any] | None:
        """取单件饰品详情。

        这是**唯一**一个能一次拿到下列全部数据的接口（授权 API，单次请求）：

          · 租赁日租金：`yyyp_lease_price`（短租）/ `yyyp_long_lease_price`（长租）
          · 租赁年化率：`yyyp_lease_annual` / `yyyp_long_lease_annual`
          · 出租挂单数：`yyyp_lease_num`（衡量出租竞争与流动性）
          · 多平台在售价：buff / yyyp / steam / c5 / igxe / r8 / eco
          · 涨跌：`sell_price_rate_{1,7,15,30,90,180,365}`（百分比）与绝对值
          · 成交：`turnover_number` / `turnover_avg_price`
          · 存世量：`statistic`
          · 相位映射：`dpl[]` 直接给出 label ↔ paint_index ↔ 各相位价格

        只读，不写库。解析交给 facet.rental。
        """
        gate = self.gates.gate(self.name, "good_detail", self.config.min_interval)
        try:
            gate.acquire()
        except RateLimitExceeded as exc:
            logger.info("[csqaq] 详情查询跳过：%s", exc)
            return None

        try:
            resp = self._session.get(
                API_BASE + ENDPOINT_GOOD_DETAIL,
                params={"id": int(good_id)},
                headers={"ApiToken": self.config.api_token or ""},
                timeout=self.config.timeout,
            )
        except requests.RequestException as exc:
            gate.report_transient_failure(str(exc), cooldown=30.0)
            logger.warning("[csqaq] 详情查询网络错误: %s", exc)
            return None

        if resp.status_code in (401, 400):
            gate.report_success()
            logger.error("[csqaq] 详情查询鉴权失败 HTTP %s：检查 Token 与白名单 IP",
                         resp.status_code)
            return None
        if resp.status_code == 429:
            gate.report_rate_limit("HTTP 429")
            return None
        if resp.status_code >= 500:
            gate.report_transient_failure(f"HTTP {resp.status_code}", cooldown=20.0)
            return None

        gate.report_success()
        try:
            body = resp.json()
        except ValueError:
            return None
        if body.get("code") not in (200, 0):
            logger.warning("[csqaq] 详情业务错误 code=%s msg=%s",
                           body.get("code"), body.get("msg"))
            return None

        data = body.get("data") or {}
        goods = data.get("goods_info") or {}
        if not goods:
            return None

        # 原始载荷整包带回：字段很多且会变，解析层按需取用，避免这里写死
        goods["_dpl"] = data.get("dpl") or []
        goods["_button_list"] = data.get("button_list") or []
        goods["_statistic_list"] = data.get("statistic_list") or []
        goods["_container"] = data.get("container") or []
        return goods

    def find_good_id(self, market_hash_name: str) -> int | None:
        """按名称精确找 CSQAQ 的 good_id（详情接口需要它）。"""
        rows = self.resolve_keyword(market_hash_name)
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("marketHashName") or row.get("market_hash_name")
            gid = row.get("id") or row.get("goodId") or row.get("good_id")
            if name == market_hash_name and gid:
                try:
                    return int(gid)
                except (TypeError, ValueError):
                    continue
        # 退而求其次：用第一条
        for row in rows:
            if isinstance(row, dict):
                gid = row.get("id") or row.get("goodId") or row.get("good_id")
                if gid:
                    try:
                        return int(gid)
                    except (TypeError, ValueError):
                        continue
        return None

    def close(self) -> None:
        self._session.close()


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
