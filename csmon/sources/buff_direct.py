"""BUFF 直连适配器。

**两种运行模式，能力差异很大**（均由 `probe/probe_buff_capability.py` 实测确认）：

| 接口 | 匿名 | 带 Cookie |
| --- | --- | --- |
| `sell_order`（在售挂单） | ✅ | ✅ |
| `buy_order`（求购挂单） | ✅ | ✅ |
| `goods/info`（ID→名称） | ✅ | ✅ |
| `sell_order?paintseed=N`（档位筛选） | ❌ Login Required | ✅ |
| `sell_order?sort_by=...` | ❌ Login Required | ✅ |
| `goods`（列表 / 按名称搜索） | ❌ Login Required | ✅ |
| `bill_order`（成交记录） | ❌ Login Required | ✅ |

由此得出两个关键结论：

1. **匿名也能拿到求购价**。`buy_order` 返回求购挂单且按价格降序，
   `items[0].price` 即最高求购价。这让 BUFF 侧首次具备「能立刻卖出多少钱」
   的信息 —— 此前只有悠悠有品有求购价。

2. **带 Cookie 的最大价值是「按名称解析 goods_id」**。
   没有 Cookie 时只能靠扫描 ID 空间碰运气（那正是之前触发 IP 级风控的操作）；
   有了搜索接口，加一个监控只需一次精确查询。

⚠ 匿名额度的硬边界：BUFF 的匿名读会被**用量触发的 IP 级风控**整体关闭。
实测在扫描 ID 空间后，所有匿名请求转为 `Login Required`，与请求头无关，
等待 150 秒不恢复。因此本适配器内置 IP 级熔断，且默认关闭。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Sequence

import requests

from ..models import PLATFORM_BUFF, ItemRef, SourceQuote
from ..ratelimit import RateLimitExceeded
from .base import SourceAdapter

logger = logging.getLogger(__name__)

BUFF_BASE = "https://buff.163.com"
ENDPOINT_SELL_ORDER = "/api/market/goods/sell_order"
ENDPOINT_BUY_ORDER = "/api/market/goods/buy_order"
ENDPOINT_GOODS_INFO = "/api/market/goods/info"
ENDPOINT_GOODS_LIST = "/api/market/goods"
ENDPOINT_BILL_ORDER = "/api/market/goods/bill_order"
ENDPOINT_USER_INFO = "/api/market/user/info"

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://buff.163.com/market/csgo",
    "X-Requested-With": "XMLHttpRequest",
}


class BuffDirectAdapter(SourceAdapter):
    """BUFF 数据源：匿名可读在售 + 求购；配置 Cookie 后可搜索与按档位筛选。"""

    name = "buff_direct"
    platforms = (PLATFORM_BUFF,)
    provides_sell = True
    provides_bid = True          # buy_order 匿名可用，已实测确认
    provides_count = True
    required_credential = None   # 免凭据可运行；Cookie 是可选增强
    degraded_note = ("未配置 BUFF_COOKIE：只能用 goods_id 查价，"
                     "无法按名称搜索，也无法按档位筛选")

    #: 命中一次 Login Required 后，整个适配器停摆多久（秒）。默认 6 小时。
    IP_BLOCK_COOLDOWN = 6 * 3600

    def __init__(self, config, gates=None) -> None:
        super().__init__(config, gates)
        self._session = requests.Session()
        self._session.headers.update(BROWSER_HEADERS)
        self.cookie = (config.cookie or "").strip()
        if self.cookie:
            self._session.headers["Cookie"] = self.cookie
        # 是否同时取求购价：多一次请求，换「能立刻卖出多少钱」的信息
        self.fetch_bid = bool(config.extra.get("fetch_bid", True))
        # 档位筛选（需 Cookie）：命中时按 paintseed 精确查价
        self._ip_blocked_until = 0.0
        self._ip_block_reason = ""

    # ── 能力声明 ───────────────────────────────────────────

    @property
    def authed(self) -> bool:
        """是否配置了 Cookie（决定能否用搜索与档位筛选）。"""
        return bool(self.cookie)

    def capabilities(self) -> dict[str, bool]:
        """当前实际可用的能力，供 CLI / 看板如实展示。"""
        return {
            "sell_order": True,
            "buy_order": self.fetch_bid,
            "goods_info": True,
            "search_by_name": self.authed,
            "paintseed_filter": self.authed,
            "sort_by": self.authed,
            "bill_order": self.authed,
            "session_valid": self.authed and not self._ip_blocked(),
        }

    # ── IP 级熔断 ──────────────────────────────────────────

    def _ip_blocked(self) -> bool:
        return time.monotonic() < self._ip_blocked_until

    def _trip_ip_block(self, reason: str, cooldown: float | None = None) -> None:
        self._ip_blocked_until = time.monotonic() + (cooldown or self.IP_BLOCK_COOLDOWN)
        self._ip_block_reason = reason
        logger.error(
            "[buff_direct] 已触发熔断（%s），本适配器停摆 %.0f 分钟。"
            "配置 BUFF_COOKIE 可显著降低触发概率（搜索替代 ID 扫描）。",
            reason, (cooldown or self.IP_BLOCK_COOLDOWN) / 60)

    @property
    def ip_block_remaining(self) -> float:
        return max(0.0, self._ip_blocked_until - time.monotonic())

    def describe_status(self) -> dict[str, Any]:
        return {
            "source": self.name,
            "authed": self.authed,
            "capabilities": self.capabilities(),
            "ip_blocked": self._ip_blocked(),
            "ip_block_remaining_s": round(self.ip_block_remaining, 1),
            "ip_block_reason": self._ip_block_reason,
        }

    # ── 通用请求 ───────────────────────────────────────────

    def _get(self, endpoint: str, params: dict[str, Any],
             gate_name: str) -> dict[str, Any] | None:
        """带闸门与熔断的统一 GET。失败返回 None，绝不抛出到调用方。"""
        if self._ip_blocked():
            return None
        gate = self.gates.gate(self.name, gate_name, self.config.min_interval)
        try:
            gate.acquire()
        except RateLimitExceeded as exc:
            logger.info("[buff_direct] %s 冷却中：%s", gate_name, exc)
            return None

        try:
            resp = self._session.get(BUFF_BASE + endpoint, params=params,
                                     timeout=self.config.timeout)
        except requests.RequestException as exc:
            gate.report_transient_failure(str(exc), cooldown=30.0)
            logger.warning("[buff_direct] %s 网络错误：%s", endpoint, exc)
            return None

        if resp.status_code == 429:
            gate.report_rate_limit("HTTP 429")
            return None
        if resp.status_code >= 500:
            gate.report_transient_failure(f"HTTP {resp.status_code}", cooldown=20.0)
            return None
        try:
            body = resp.json()
        except ValueError:
            gate.report_transient_failure("响应非 JSON", cooldown=15.0)
            return None

        code = body.get("code")
        if code == "Login Required":
            # 匿名额度被关闭，或该接口需要 Cookie
            if self.authed:
                logger.warning("[buff_direct] %s 返回 Login Required："
                               "Cookie 可能已失效，请重新获取", endpoint)
            else:
                self._trip_ip_block(f"{endpoint} 返回 Login Required")
            return None
        gate.report_success()
        return body

    # ── 采集 ───────────────────────────────────────────────

    def fetch(self, items: Sequence[ItemRef]) -> list[SourceQuote]:
        quotes: list[SourceQuote] = []
        for item in items:
            if self._ip_blocked():
                break
            if not item.buff_goods_id:
                logger.debug("[buff_direct] 跳过未解析 goods_id 的饰品: %s",
                             item.market_hash_name)
                continue
            quote = self.fetch_one(item.market_hash_name, int(item.buff_goods_id))
            if quote:
                quotes.append(quote)
        return quotes

    def fetch_one(self, market_hash_name: str, goods_id: int,
                  paintseed: int | None = None) -> SourceQuote | None:
        """查单个饰品的在售价、在售量、求购价、求购量。

        `paintseed` 非空时按图案编号筛选（需要 Cookie），用于档位级在售价。
        """
        params: dict[str, Any] = {"game": "csgo", "goods_id": goods_id, "page_num": 1}
        # 只有真的把 paintseed 发出去了，才算档位级报价。
        # 若因缺少 Cookie 而忽略筛选，必须让 variant_label 保持 None ——
        # 否则一条普通报价会伪装成档位报价，污染下游的档位分析与套利判定。
        applied_paintseed: int | None = None
        if paintseed is not None:
            if not self.authed:
                logger.info("[buff_direct] 档位筛选需要 BUFF_COOKIE，已忽略 paintseed"
                            "（本次按整品口径取价）")
            else:
                params["paintseed"] = paintseed
                applied_paintseed = paintseed

        body = self._get(ENDPOINT_SELL_ORDER, params, "sell_order")
        if body is None or body.get("code") != "OK":
            if body is not None:
                logger.info("[buff_direct] %s 返回 code=%s err=%s",
                            market_hash_name, body.get("code"), body.get("error"))
            return None

        data = body.get("data") or {}
        items = data.get("items") or []
        # 默认排序即价格升序，但仍按数值取最小，不依赖排序假设
        prices = [p for p in (_to_float(it.get("price")) for it in items) if p]
        lowest = min(prices) if prices else None
        total = _to_int(data.get("total_count"))
        goods_infos = data.get("goods_infos") or {}
        info = goods_infos.get(str(goods_id)) or goods_infos.get(goods_id) or {}

        bid_price: float | None = None
        bid_count: int | None = None
        if self.fetch_bid:
            bid_price, bid_count = self._fetch_best_bid(goods_id)

        return SourceQuote(
            market_hash_name=info.get("market_hash_name") or market_hash_name,
            platform=PLATFORM_BUFF,
            source=self.name,
            sell_price=lowest,
            sell_count=total,
            bid_price=bid_price,
            bid_count=bid_count,
            variant_label=(f"seed#{applied_paintseed}"
                           if applied_paintseed is not None else None),
            raw={"goods_id": goods_id, "page_size": len(items),
                 "name": info.get("name"), "authed": self.authed,
                 "paintseed_applied": applied_paintseed},
        )

    def _fetch_best_bid(self, goods_id: int) -> tuple[float | None, int | None]:
        """取最高求购价与求购挂单数。

        实测：buy_order 返回的挂单**按价格降序**（1970 > 1960 > 1900），
        所以首条即最高。仍按数值取 max，不依赖排序假设。
        """
        body = self._get(ENDPOINT_BUY_ORDER,
                         {"game": "csgo", "goods_id": goods_id, "page_num": 1},
                         "buy_order")
        if body is None or body.get("code") != "OK":
            return None, None
        data = body.get("data") or {}
        items = data.get("items") or []
        prices = [p for p in (_to_float(it.get("price")) for it in items) if p]
        return (max(prices) if prices else None, _to_int(data.get("total_count")))

    # ── 身份解析 ───────────────────────────────────────────

    def resolve_goods_id(self, goods_id: int) -> dict[str, Any] | None:
        """由 goods_id 反查身份信息（建索引时用）。"""
        body = self._get(ENDPOINT_GOODS_INFO,
                         {"game": "csgo", "goods_id": goods_id}, "goods_info")
        if body is None or body.get("code") != "OK":
            return None
        data = body.get("data") or {}
        mhn = data.get("market_hash_name")
        if not mhn:
            return None
        return {
            "goods_id": int(data.get("id") or goods_id),
            "market_hash_name": mhn,
            "name": data.get("name"),
            "appid": data.get("appid"),
            "game": data.get("game"),
            "icon_url": (data.get("goods_info") or {}).get("icon_url"),
            "steam_price": (data.get("goods_info") or {}).get("steam_price"),
        }

    def search_goods(self, keyword: str, page_size: int = 20,
                     exact_only: bool = False) -> list[dict[str, Any]]:
        """按名称搜索饰品（**需要 Cookie**）。

        这是配置 Cookie 后最大的收益：不用再扫描 goods_id 空间，
        加一个监控只需一次精确查询，也就不容易触发 IP 级风控。
        """
        if not self.authed:
            raise RateLimitExceeded("按名称搜索需要 BUFF_COOKIE", 0)

        body = self._get(ENDPOINT_GOODS_LIST,
                         {"game": "csgo", "page_num": 1, "page_size": page_size,
                          "search": keyword}, "goods_list")
        if body is None or body.get("code") != "OK":
            return []

        data = body.get("data") or {}
        rows = []
        for item in (data.get("items") or []):
            mhn = item.get("market_hash_name")
            gid = item.get("id")
            if not mhn or gid is None:
                continue
            rows.append({
                "goods_id": int(gid),
                "market_hash_name": mhn,
                "name": item.get("name"),
                "sell_num": item.get("sell_num"),
                "buy_num": item.get("buy_num"),
                "quick_price": item.get("quick_price"),
                "sell_min_price": item.get("sell_min_price"),
                "icon_url": (item.get("goods_info") or {}).get("icon_url"),
            })

        if exact_only:
            lowered = keyword.strip().lower()
            exact = [r for r in rows if r["market_hash_name"].lower() == lowered]
            if exact:
                return exact
        return rows

    def resolve_by_name(self, market_hash_name: str) -> dict[str, Any] | None:
        """由市场名精确解析 goods_id（需要 Cookie）。"""
        if not self.authed:
            return None
        rows = self.search_goods(market_hash_name, page_size=20, exact_only=True)
        for row in rows:
            if row["market_hash_name"] == market_hash_name:
                return row
        return rows[0] if rows else None

    def check_session(self) -> dict[str, Any]:
        """自检：Cookie 是否有效、解锁了哪些能力。

        用「搜索一个必然存在的词」来判定 —— 比调 user/info 可靠，
        因为后者路径可能变动，而搜索是后续真正要用的能力。
        """
        result: dict[str, Any] = {"authed": self.authed,
                                  "capabilities": self.capabilities()}
        if not self.authed:
            result["session_valid"] = False
            result["hint"] = "未配置 BUFF_COOKIE"
            return result

        if self._ip_blocked():
            result["session_valid"] = False
            result["hint"] = f"熔断中：{self._ip_block_reason}"
            return result

        try:
            rows = self.search_goods("AK-47", page_size=1)
        except RateLimitExceeded as exc:
            result["session_valid"] = False
            result["hint"] = str(exc)
            return result

        if rows:
            result["session_valid"] = True
            result["sample"] = rows[0]["market_hash_name"]
        else:
            result["session_valid"] = False
            result["hint"] = ("搜索无结果或 Cookie 已失效；"
                              "请从浏览器重新复制 Cookie（含 session 字段）")
        return result

    # ── 实例明细（档位/变体学习用）─────────────────────────

    def fetch_listings(self, goods_id: int, pages: int = 2,
                       page_size: int = 80) -> list[dict[str, Any]]:
        """抓取在售挂单明细，含**变体指纹**：paintseed / paintindex / paintwear。

        配置 Cookie 后可用 `paintseed` 参数按档位精确抓取。
        注意：本方法会消耗匿名额度，学习一次「同一饰品 + 1-2 页」就够用。
        """
        rows: list[dict[str, Any]] = []
        for page in range(1, max(1, pages) + 1):
            body = self._get(ENDPOINT_SELL_ORDER,
                             {"game": "csgo", "goods_id": goods_id, "page_num": page},
                             "sell_order")
            if body is None or body.get("code") != "OK":
                break
            data = body.get("data") or {}
            items = data.get("items") or []
            if not items:
                break
            for item in items:
                asset = item.get("asset_info") or {}
                info = asset.get("info") or {}
                rows.append({
                    "goods_id": goods_id,
                    "price": _to_float(item.get("price")),
                    "paint_wear": _to_float(asset.get("paintwear")),
                    "paint_seed": _to_int(info.get("paintseed")),
                    "paint_index": _to_int(info.get("paintindex")),
                    "listing_id": item.get("id"),
                })
            total = _to_int(data.get("total_count")) or 0
            if len(items) < page_size and total <= len(rows):
                break

        logger.info("[buff_direct] goods_id=%s 抓取到 %d 条挂单明细",
                    goods_id, len(rows))
        return rows

    def close(self) -> None:
        self._session.close()


def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _to_int(value: Any) -> int | None:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None
