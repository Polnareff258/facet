"""悠悠有品直连适配器（免登录，只覆盖求购价）。

实测结论（docs/DATA_SOURCES.md 有完整矩阵证据）：
  可用  POST /api/youpin/bff/trade/purchase/order/getTemplatePurchaseOrderPageList
        body {pageIndex, pageSize, showMaxPriceFlag, templateId}
        -> data.responseList[].purchasePrice / surplusQuantity / templateId
        完全免凭据，code=0。

  不可用（对匿名请求返回风控/登录话术，且与 App-Version 取值无关）：
        POST /api/homepage/v3/detail/commodity/list/sell     -> 85100 当前app版本过低
        POST /api/homepage/pc/goods/market/queryOnSaleCommodityList -> 85100
        POST /api/homepage/v3/detail/commodity/list/lease    -> 84103 请登录

因此本适配器只声明 provides_bid=True、provides_sell=False —— 它是「求购价」
的来源，不是「在售价」的来源；悠悠有品的在售价请走 CSQAQ / SteamDT。

请求头按参考实现 YouPinMobileApiClient.ApplyHeaders 的形状要求构造：
  DeviceToken/DeviceId 24 位、requestTag 32 位大写十六进制、deviceUk 65 位；
  形状不符会被风控直接拦掉（这是实测踩过的坑）。
"""
from __future__ import annotations

import json
import logging
import random
import string
from typing import Any, Sequence

import requests

from ..models import PLATFORM_YOUPIN, ItemRef, SourceQuote
from ..ratelimit import RateLimitExceeded
from .base import SourceAdapter

logger = logging.getLogger(__name__)

YOUPIN_BASE = "https://api.youpin898.com"
ENDPOINT_PURCHASE_PAGE = "/api/youpin/bff/trade/purchase/order/getTemplatePurchaseOrderPageList"

APP_VERSION = "5.45.4"          # 与参考实现对齐的移动端版本
ANDROID_VERSION = "15"
ALNUM = string.ascii_letters + string.digits
HEXDIGITS = "0123456789abcdef"

PAGE_SIZE = 20                  # 求购榜单首页足够取到最高价，避免多翻页触发风控


class YouPinDirectAdapter(SourceAdapter):
    """免登录读取悠悠有品求购价（bid）。"""

    name = "youpin_direct"
    platforms = (PLATFORM_YOUPIN,)
    provides_sell = False        # 在售通道被风控，明确不声明
    provides_bid = True
    provides_count = True
    required_credential = None   # 求购接口免凭据
    degraded_note = "只提供求购价；悠悠有品在售价请走 csqaq / steamdt"

    def __init__(self, config, gates=None) -> None:
        super().__init__(config, gates)
        self._session = requests.Session()
        self._device = _make_device_profile()
        if config.cookie:                 # 形如 "deviceUk=..." 的可选覆盖
            self._device["device_uk"] = config.cookie
        # 档位模式：开启后按档位分别产出求购价（多花几页请求，换取档位级精度）
        # 对多普勒/渐变/淬火这类「同名不同档、价差数倍」的品类值得开
        self.tier_mode = bool(config.extra.get("tier_mode", False))
        self.tier_pages = int(config.extra.get("tier_pages", 2))

    def fetch(self, items: Sequence[ItemRef]) -> list[SourceQuote]:
        quotes: list[SourceQuote] = []
        for item in items:
            if not item.youpin_template_id:
                logger.debug("[youpin_direct] 跳过未解析 templateId 的饰品: %s",
                             item.market_hash_name)
                continue
            template_id = int(item.youpin_template_id)

            if self.tier_mode:
                tier_quotes = self.fetch_tier_quotes(item.market_hash_name, template_id,
                                                     pages=self.tier_pages)
                if tier_quotes:
                    # 档位报价之外，再补一条整品报价，便于和其他平台对齐比较
                    aggregate = self._aggregate_from_tiers(item.market_hash_name,
                                                           tier_quotes)
                    quotes.extend(tier_quotes)
                    if aggregate:
                        quotes.append(aggregate)
                    continue

            quote = self.fetch_one(item.market_hash_name, template_id)
            if quote:
                quotes.append(quote)
        return quotes

    @staticmethod
    def _aggregate_from_tiers(market_hash_name: str,
                              tier_quotes: Sequence[SourceQuote]) -> SourceQuote | None:
        """把档位报价汇总成一条「整品」报价（variant_label=None）。

        为什么要汇总：其他平台（CSQAQ/SteamDT）给的是整品口径的钱，
        没有这条就没法和它们对齐做跨平台比价。
        注意汇总取的是**档位中的最高求购价**，它代表「有买家愿意为最好的档位出多少」，
        不等于「整品的普遍求购价」—— 所以它只用于横向对比，不用于判断单品价值。
        """
        priced = [q for q in tier_quotes if q.bid_price]
        if not priced:
            return None
        best = max(priced, key=lambda q: q.bid_price or 0)
        return SourceQuote(
            market_hash_name=market_hash_name,
            platform=PLATFORM_YOUPIN,
            source="youpin_direct",
            sell_price=None,
            bid_price=best.bid_price,
            bid_count=sum(q.bid_count or 0 for q in tier_quotes),
            variant_label=None,
            raw={"aggregated_from_tiers": len(tier_quotes),
                 "best_tier": best.variant_label},
        )

    def fetch_one(self, market_hash_name: str, template_id: int) -> SourceQuote | None:
        gate = self.gates.gate(self.name, "purchase_page", self.config.min_interval)
        try:
            gate.acquire()
        except RateLimitExceeded as exc:
            logger.info("[youpin_direct] 跳过 %s：%s", market_hash_name, exc)
            return None

        payload = {
            "pageIndex": 1,
            "pageSize": PAGE_SIZE,
            "showMaxPriceFlag": False,
            "templateId": template_id,
        }
        try:
            resp = self._session.post(
                YOUPIN_BASE + ENDPOINT_PURCHASE_PAGE,
                json=payload,
                headers=self._headers(),
                timeout=self.config.timeout,
            )
        except requests.RequestException as exc:
            gate.report_transient_failure(str(exc), cooldown=30.0)
            logger.warning("[youpin_direct] 网络错误 %s: %s", market_hash_name, exc)
            return None

        if resp.status_code == 429:
            cooldown = gate.report_rate_limit("HTTP 429", _retry_after(resp))
            logger.warning("[youpin_direct] 限流，冷却 %.0fs", cooldown)
            return None
        if resp.status_code >= 500:
            gate.report_transient_failure(f"HTTP {resp.status_code}", cooldown=20.0)
            return None

        try:
            body = resp.json()
        except ValueError:
            gate.report_transient_failure("响应非 JSON", cooldown=15.0)
            return None

        code = body.get("code", body.get("Code"))
        if code == 84103:
            cooldown = gate.report_rate_limit("求购接口要求登录", retry_after=1800)
            logger.warning("[youpin_direct] 求购接口提示需登录，冷却 %.0fs", cooldown)
            return None
        if code == 85100:
            gate.report_rate_limit(str(body.get("msg")), retry_after=900)
            logger.warning("[youpin_direct] 风控：%s", body.get("msg"))
            return None
        if code not in (0, None):
            msg = body.get("msg", body.get("Msg"))
            if any(k in str(msg) for k in ("频繁", "过快", "限流", "风控")):
                cooldown = gate.report_rate_limit(str(msg))
                logger.warning("[youpin_direct] 风控，冷却 %.0fs", cooldown)
            else:
                logger.info("[youpin_direct] %s code=%s msg=%s", market_hash_name, code, msg)
            return None

        gate.report_success()
        data = body.get("data") or body.get("Data") or {}
        rows = data.get("responseList") or []
        prices = [p for p in (_to_float(r.get("purchasePrice")) for r in rows
                              if isinstance(r, dict)) if p]
        if not prices:
            # 该模板当前无求购挂单，是正常状态（不是错误）
            return SourceQuote(
                market_hash_name=market_hash_name,
                platform=PLATFORM_YOUPIN,
                source=self.name,
                sell_price=None,
                bid_price=None,
                bid_count=0,
                raw={"template_id": template_id, "rows": 0},
            )

        # responseList 按平台榜单顺序返回；取页面内最高价作为「最高求购出价」
        top_bid = max(prices)
        quantities = [_to_int(r.get("surplusQuantity")) or 0 for r in rows
                      if isinstance(r, dict)]
        return SourceQuote(
            market_hash_name=market_hash_name,
            platform=PLATFORM_YOUPIN,
            source=self.name,
            sell_price=None,
            bid_price=top_bid,
            bid_count=len(rows),
            raw={"template_id": template_id, "rows": len(rows),
                 "top_quantity": max(quantities) if quantities else None},
        )

    # ── 档位词表采集 ───────────────────────────────────────

    def fetch_purchase_rows(self, template_id: int, pages: int = 3,
                            page_size: int = 100) -> list[dict[str, Any]]:
        """分页抓取求购挂单**原始行**，用于采集档位词表。

        为什么值得单独做这件事：这些行里带 `specialStyle`（红宝石/蓝宝石/P1-P4/
        T1-T2/绿宝石）、`fadeText`（渐变百分比区间）、`abradeText`（精细磨损区间），
        是平台自己分类好的**中文档位标签**。有了它就完全不需要从 paint_seed
        反推档位 —— 那种反推是刀型相关的，抄错表就会给出错误的价格预期。

        多页是必要的：实测刺刀多普勒第一页 41 条里没有 P3 相位，
        只拉一页会漏掉冷门档位。
        """
        rows: list[dict[str, Any]] = []
        for page in range(1, max(1, pages) + 1):
            gate = self.gates.gate(self.name, "purchase_page", self.config.min_interval)
            try:
                gate.acquire()
            except RateLimitExceeded:
                break

            payload = {"pageIndex": page, "pageSize": page_size,
                       "showMaxPriceFlag": False, "templateId": template_id}
            try:
                resp = self._session.post(
                    YOUPIN_BASE + ENDPOINT_PURCHASE_PAGE, json=payload,
                    headers=self._headers(), timeout=self.config.timeout)
            except requests.RequestException as exc:
                gate.report_transient_failure(str(exc), cooldown=20.0)
                logger.warning("[youpin_direct] 词表采集网络错误：%s", exc)
                break

            if resp.status_code >= 500 or resp.status_code == 429:
                gate.report_transient_failure(f"HTTP {resp.status_code}", cooldown=30.0)
                break

            try:
                body = resp.json()
            except ValueError:
                # 空响应偶发（实测遇到过），跳过该页而不是整体失败
                gate.report_transient_failure("空响应", cooldown=5.0)
                logger.info("[youpin_direct] 第 %d 页返回空/非 JSON，跳过", page)
                continue

            code = body.get("code", body.get("Code"))
            if code == 84103:
                gate.report_rate_limit("求购接口要求登录", retry_after=1800)
                break
            if code not in (0, None):
                logger.info("[youpin_direct] 词表采集 code=%s msg=%s",
                            code, body.get("msg"))
                break

            gate.report_success()
            batch = (body.get("data") or body.get("Data") or {}).get("responseList") or []
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < page_size:
                break

        logger.info("[youpin_direct] templateId=%s 采集 %d 行求购样本",
                    template_id, len(rows))
        return rows

    def fetch_tier_quotes(self, market_hash_name: str, template_id: int,
                          pages: int = 2) -> list[SourceQuote]:
        """按**档位**分别产出求购价。

        这是档位维度最有价值的应用：同一把刀的红宝石与 P2 价差常达数倍，
        把它们合成一个「整品最高求购价」等于把两件不同的商品当一件定价。

        兜底策略：若该模板没有任何档位标注（如普通枪皮），返回空列表，
        调用方 `fetch()` 会自动退回单条整品报价 —— 这样既不会重复计数，
        也不会把「无档位」伪装成一个叫 None 的档位。
        """
        rows = self.fetch_purchase_rows(template_id, pages=pages)
        if not rows:
            return []

        # 档位键：优先 specialStyle（宝石/相位/Tier），其次 fadeText（渐变区间）
        grouped: dict[str | None, list[dict[str, Any]]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = _tier_label(row)
            if label is None:
                continue          # 「不限」不是档位，不进任何分组
            grouped.setdefault(label, []).append(row)

        if not grouped:
            return []             # 该品类无档位维度，交给调用方走整品口径

        quotes: list[SourceQuote] = []
        for label, group in grouped.items():
            prices = [p for p in (_to_float(r.get("purchasePrice")) for r in group) if p]
            if not prices:
                continue
            quotes.append(SourceQuote(
                market_hash_name=market_hash_name,
                platform=PLATFORM_YOUPIN,
                source=self.name,
                sell_price=None,
                bid_price=max(prices),
                bid_count=len(group),
                variant_label=label,
                raw={"template_id": template_id, "rows": len(group),
                     "tier": label},
            ))
        logger.info("[youpin_direct] %s：%d 行样本 → %d 个档位报价",
                    market_hash_name, len(rows), len(quotes))
        return quotes

    # ── 请求头 ─────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        dev = self._device
        return {
            "authorization": "Bearer ",
            "uk": dev["uk"],
            "user-agent": (f"Android/{ANDROID_VERSION} official com.uu898.uuhavequality/"
                           f"{APP_VERSION} okhttp/4.9.3"),
            "App-Version": APP_VERSION,
            "AppType": "4",
            "deviceType": "1",
            "package-type": "uuyp",
            "DeviceToken": dev["device_id"],
            "DeviceId": dev["device_id"],
            "deviceUk": dev["device_uk"],
            "platform": "android",
            "Gameid": "730",
            "requestTag": dev["request_tag"],
            "deviceBrand": "xiaomi",
            "systemVersion": ANDROID_VERSION,
            "App-Source": "h5",
            "traceId": dev["trace_id"],
            "currentTheme": "Dark",
            "Accept-Language": "zh-CN,zh;q=0.8",
            "content-type": "application/json; charset=utf-8",
            "Device-Info": json.dumps({
                "deviceType": dev["device_id"],
                "systemName ": "Android",
                "hasSteamApp": 1,
                "deviceId": dev["device_id"],
                "requestTag": dev["request_tag"],
                "systemVersion": ANDROID_VERSION,
            }, ensure_ascii=False),
        }

    def close(self) -> None:
        self._session.close()


def _make_device_profile() -> dict[str, str]:
    """生成形状合法的设备标识（形状不合会被风控直接拦掉）。"""
    device_id = "".join(random.choices(ALNUM, k=24))
    return {
        "device_id": device_id,
        "device_token": device_id,
        "request_tag": "".join(random.choices("0123456789ABCDEF", k=32)),
        "device_uk": "".join(random.choices(ALNUM, k=65)),
        "uk": "".join(random.choices(ALNUM, k=65)),
        "trace_id": "".join(random.choices(HEXDIGITS, k=32)),
    }


def _tier_label(row: dict[str, Any]) -> str | None:
    """从求购行里取档位标签。

    平台把档位放在两个字段里，语义不同：
      specialStyle —— 离散档位（红宝石 / 蓝宝石 / 黑珍珠 / P1-P4 / T1-T2 / 绿宝石）
      fadeText     —— 连续区间（渐变百分比，如 "99-100"）
    两者都是「买家指定的收货条件」，所以是这个档位真实在流通的证据。

    "不限" / "0-100" 表示买家没有限定档位，属于占位值，不能当成一个档位。
    """
    style = row.get("specialStyle")
    if style is not None:
        text = str(style).strip()
        if text and text not in ("不限", "无", "None", "null", "-1"):
            return text

    fade = row.get("fadeText")
    if fade is not None:
        text = str(fade).strip()
        if text and text not in ("不限", "无", "None", "null", "0-100"):
            return f"渐变 {text}"
    return None


def _retry_after(resp: requests.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(1.0, float(raw))
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
