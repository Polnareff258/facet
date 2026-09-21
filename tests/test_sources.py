"""适配器测试：用真实响应结构的样本验证解析，不联网。

样本取自各平台文档与实测响应（docs/DATA_SOURCES.md 有出处）。
"""
from __future__ import annotations

from typing import Any

import pytest

from csmon.config import SourceConfig
from csmon.models import ItemRef
from csmon.ratelimit import GateRegistry, RateLimitExceeded
from csmon.sources.buff_direct import BuffDirectAdapter
from csmon.sources.csqaq import CsqaqAdapter
from csmon.sources.mock import MockAdapter
from csmon.sources.steamdt import SteamDtAdapter
from csmon.sources.youpin_direct import YouPinDirectAdapter

# ── 伪造 HTTP 会话 ─────────────────────────────────────────

class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200, headers: dict | None = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = str(payload)

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """按顺序返回预设响应；记录调用以便断言参数。"""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}

    def _next(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeSession 响应已耗尽")
        return self._responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            raise AssertionError("FakeSession 响应已耗尽")
        return self._responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.post(url, **kwargs)

    def close(self) -> None:
        pass


def _cfg(name: str, **kw: Any) -> SourceConfig:
    return SourceConfig(name=name, min_interval=0.0, **kw)


# ── CSQAQ ─────────────────────────────────────────────────

CSQAQ_PAYLOAD = {
    "code": 200,
    "msg": "Success",
    "data": {
        "success": {
            "★ Bowie Knife": {
                "goodId": 6733, "name": "鲍伊猎刀（★）", "marketHashName": "★ Bowie Knife",
                "buffSellPrice": 1340.0, "buffSellNum": 43,
                "yyypSellPrice": 1309.0, "yyypSellNum": 35,
                "steamSellPrice": 1947.77, "steamSellNum": 11,
            },
            "AWP | Snake Camo (Factory New)": {
                "goodId": 301, "name": "AWP | 蝮蛇迷彩 (崭新出厂)",
                "marketHashName": "AWP | Snake Camo (Factory New)",
                "buffSellPrice": 1479.0, "buffSellNum": 8,
                "yyypSellPrice": 1484.0, "yyypSellNum": 5,
                "steamSellPrice": 2474.25, "steamSellNum": 4,
            },
        },
        "error": ["test_name"],
    },
}


def test_csqaq_parses_three_platforms() -> None:
    adapter = CsqaqAdapter(_cfg("csqaq", api_token="t"), GateRegistry())
    quotes = adapter._parse(CSQAQ_PAYLOAD["data"])
    # 2 个饰品 x 3 个平台
    assert len(quotes) == 6

    bowie = {q.platform: q for q in quotes if q.market_hash_name == "★ Bowie Knife"}
    assert bowie["BUFF"].sell_price == 1340.0 and bowie["BUFF"].sell_count == 43
    assert bowie["YOUPIN"].sell_price == 1309.0
    assert bowie["STEAM"].sell_price == 1947.77
    assert bowie["BUFF"].source == "csqaq"


def test_csqaq_preflight_requires_token() -> None:
    from csmon.sources.base import SourceUnavailable
    adapter = CsqaqAdapter(_cfg("csqaq", api_token=None), GateRegistry())
    with pytest.raises(SourceUnavailable):
        adapter.preflight()


def test_csqaq_rate_limit_returns_empty_not_crash() -> None:
    adapter = CsqaqAdapter(_cfg("csqaq", api_token="t"), GateRegistry())
    adapter._session = FakeSession([FakeResponse({"code": 429}, status_code=429)])
    assert adapter._fetch_batch([ItemRef(market_hash_name="X")]) == []


def test_csqaq_auth_failure_returns_empty() -> None:
    adapter = CsqaqAdapter(_cfg("csqaq", api_token="bad"), GateRegistry())
    adapter._session = FakeSession([FakeResponse({"code": 401}, status_code=401)])
    assert adapter._fetch_batch([ItemRef(market_hash_name="X")]) == []


# ── SteamDT ────────────────────────────────────────────────

STEAMDT_DATA = [
    {
        "marketHashName": "AK-47 | Redline (Field-Tested)",
        "dataList": [
            {"platform": "BUFF", "platformItemId": "1", "sellPrice": 125.0,
             "sellCount": 42, "biddingPrice": 118.0, "biddingCount": 30,
             "updateTime": 1700000000000},
            {"platform": "YOUPIN", "platformItemId": "2", "sellPrice": 124.5,
             "sellCount": 38, "biddingPrice": 117.0, "biddingCount": 25,
             "updateTime": 1700000000000},
            {"platform": "UUYP", "platformItemId": "3", "sellPrice": 124.0,
             "sellCount": 5, "biddingPrice": 116.0, "biddingCount": 1,
             "updateTime": 1700000000000},
            {"platform": "C5", "platformItemId": "4", "sellPrice": 130.0,
             "sellCount": 9, "biddingPrice": 1.0, "biddingCount": 1,
             "updateTime": 1700000000000},
            {"platform": "STEAM", "platformItemId": "5", "sellPrice": 200.0,
             "sellCount": 3, "biddingPrice": 190.0, "biddingCount": 2,
             "updateTime": 1700000000000},
        ],
    }
]


def test_steamdt_parses_and_maps_platforms() -> None:
    adapter = SteamDtAdapter(_cfg("steamdt", api_key="k"), GateRegistry())
    quotes = adapter._parse(STEAMDT_DATA)
    by_platform = {q.platform: q for q in quotes}
    # UUYP 应归一为 YOUPIN；未纳入范围的 C5 应被丢弃
    assert set(by_platform) == {"BUFF", "YOUPIN", "STEAM"}
    assert by_platform["BUFF"].sell_price == 125.0
    assert by_platform["BUFF"].bid_price == 118.0
    assert by_platform["BUFF"].sell_count == 42
    assert by_platform["YOUPIN"].source_updated_at is not None


def test_steamdt_preflight_requires_key() -> None:
    from csmon.sources.base import SourceUnavailable
    with pytest.raises(SourceUnavailable):
        SteamDtAdapter(_cfg("steamdt", api_key=None), GateRegistry()).preflight()


def test_steamdt_business_rate_limit_sets_cooldown() -> None:
    adapter = SteamDtAdapter(_cfg("steamdt", api_key="k"), GateRegistry())
    adapter._session = FakeSession([
        FakeResponse({"success": False, "errorCode": 4029, "errorMsg": "请求过于频繁"})
    ])
    assert adapter._fetch_batch([ItemRef(market_hash_name="X")]) == []
    gate = adapter.gates.gate("steamdt", "price_batch")
    assert gate.cooldown_remaining > 0


def test_steamdt_epoch_seconds_and_ms_both_ok() -> None:
    adapter = SteamDtAdapter(_cfg("steamdt", api_key="k"), GateRegistry())
    quotes = adapter._parse([{
        "marketHashName": "X",
        "dataList": [
            {"platform": "BUFF", "sellPrice": 1.0, "updateTime": 1700000000},
            {"platform": "STEAM", "sellPrice": 2.0, "updateTime": 1700000000000},
        ],
    }])
    assert all(q.source_updated_at is not None for q in quotes)


# ── BUFF 直连 ──────────────────────────────────────────────

BUFF_OK = {
    "code": "OK",
    "data": {
        "total_count": 3,
        "items": [{"price": "2180"}, {"price": "2230"}, {"price": "2400"}],
        "goods_infos": {
            "43076": {
                "market_hash_name": "★ M9 Bayonet | Bright Water (Well-Worn)",
                "name": "M9 刺刀（★） | 澄澈之水 (破损不堪)",
            }
        },
    },
}


BUFF_BUY_OK = {
    "code": "OK",
    "data": {
        "total_count": 3,
        "items": [{"price": "1970"}, {"price": "1960"}, {"price": "1900"}],
    },
}


def test_buff_direct_lowest_price_and_count() -> None:
    """在售价取最低、在售量取总数，名称以响应回传的官方名为准。"""
    adapter = BuffDirectAdapter(_cfg("buff_direct"), GateRegistry())
    adapter._session = FakeSession([FakeResponse(BUFF_OK), FakeResponse(BUFF_BUY_OK)])
    quote = adapter.fetch_one("fallback-name", 43076)
    assert quote is not None
    assert quote.platform == "BUFF"
    assert quote.sell_price == 2180.0        # 三档挂单里取最低
    assert quote.sell_count == 3
    # 应采用响应里回传的官方 market_hash_name，而非入参回退名
    assert quote.market_hash_name == "★ M9 Bayonet | Bright Water (Well-Worn)"


def test_buff_direct_reads_bid_from_buy_order() -> None:
    """buy_order 匿名可用（实测），应据此提供求购价 —— 此前只有悠悠有求购价。"""
    adapter = BuffDirectAdapter(_cfg("buff_direct"), GateRegistry())
    adapter._session = FakeSession([FakeResponse(BUFF_OK), FakeResponse(BUFF_BUY_OK)])
    quote = adapter.fetch_one("X", 43076)
    assert quote is not None
    assert quote.bid_price == 1970.0         # 按数值取最大，不依赖排序
    assert quote.bid_count == 3


def test_buff_direct_can_skip_bid_fetch() -> None:
    """关掉 fetch_bid 时不该多发一次请求（匿名额度紧张）。"""
    cfg = _cfg("buff_direct")
    cfg.extra["fetch_bid"] = False
    adapter = BuffDirectAdapter(cfg, GateRegistry())
    adapter._session = FakeSession([FakeResponse(BUFF_OK)])   # 只给一个响应
    quote = adapter.fetch_one("X", 43076)
    assert quote is not None
    assert quote.sell_price == 2180.0
    assert quote.bid_price is None


def test_buff_direct_paintseed_ignored_without_cookie() -> None:
    """档位筛选需要 Cookie；没配时应忽略而不是发一个注定失败的请求。"""
    adapter = BuffDirectAdapter(_cfg("buff_direct"), GateRegistry())
    adapter._session = FakeSession([FakeResponse(BUFF_OK), FakeResponse(BUFF_BUY_OK)])
    quote = adapter.fetch_one("X", 43076, paintseed=755)
    assert quote is not None
    assert quote.variant_label is None                    # 没筛选就不带档位标签
    sent = adapter._session.calls[-2]                     # sell_order 那次调用
    assert "paintseed" not in (sent.get("params") or {})


def test_buff_direct_paintseed_applied_with_cookie() -> None:
    cfg = _cfg("buff_direct", cookie="session=abc")
    adapter = BuffDirectAdapter(cfg, GateRegistry())
    adapter._session = FakeSession([FakeResponse(BUFF_OK), FakeResponse(BUFF_BUY_OK)])
    quote = adapter.fetch_one("X", 43076, paintseed=755)
    assert quote is not None
    assert quote.variant_label == "seed#755"
    sent = adapter._session.calls[-2]
    assert (sent.get("params") or {}).get("paintseed") == 755


def test_buff_direct_login_required_trips_ip_circuit_breaker() -> None:
    """命中 Login Required 必须熔断整个适配器，而不是只冷却当前端点。"""
    adapter = BuffDirectAdapter(_cfg("buff_direct"), GateRegistry())
    adapter._session = FakeSession([FakeResponse({"code": "Login Required"})])
    assert adapter.fetch_one("X", 1) is None
    assert adapter.ip_block_remaining > 0
    assert adapter.describe_status()["ip_blocked"] is True

    # 熔断后不再发请求：给一个会抛异常的会话，证明它没被碰到
    adapter._session = FakeSession([])
    assert adapter.fetch([ItemRef(market_hash_name="X", buff_goods_id=1)]) == []


def test_buff_direct_skips_items_without_goods_id() -> None:
    adapter = BuffDirectAdapter(_cfg("buff_direct"), GateRegistry())
    adapter._session = FakeSession([])
    assert adapter.fetch([ItemRef(market_hash_name="no-id")]) == []


# ── 悠悠有品直连 ───────────────────────────────────────────

YOUPIN_OK = {
    "code": 0, "msg": "成功",
    "data": {
        "responseList": [
            {"purchasePrice": 55.0, "surplusQuantity": 3, "templateId": 822,
             "commodityName": "a"},
            {"purchasePrice": 60.0, "surplusQuantity": 1, "templateId": 822,
             "commodityName": "b"},
            {"purchasePrice": 50.0, "surplusQuantity": 9, "templateId": 822,
             "commodityName": "c"},
        ]
    },
}


def test_youpin_direct_takes_highest_bid() -> None:
    adapter = YouPinDirectAdapter(_cfg("youpin_direct"), GateRegistry())
    adapter._session = FakeSession([FakeResponse(YOUPIN_OK)])
    quote = adapter.fetch_one("'The Doctor' Romanov | Sabre", 822)
    assert quote is not None
    assert quote.platform == "YOUPIN"
    assert quote.bid_price == 60.0           # 页面内最高求购出价
    assert quote.sell_price is None          # 该源不提供在售价
    assert quote.bid_count == 3


def test_youpin_direct_declares_no_sell_capability() -> None:
    """在售通道对匿名请求有风控，适配器必须如实声明不提供在售价。"""
    assert YouPinDirectAdapter.provides_sell is False
    assert YouPinDirectAdapter.provides_bid is True


def test_youpin_direct_login_gate_sets_cooldown() -> None:
    adapter = YouPinDirectAdapter(_cfg("youpin_direct"), GateRegistry())
    adapter._session = FakeSession([
        FakeResponse({"Code": 84103, "Msg": "登录悠悠有品，解锁更多功能"})
    ])
    assert adapter.fetch_one("X", 822) is None
    assert adapter.gates.gate("youpin_direct", "purchase_page").cooldown_remaining > 0


def test_youpin_direct_risk_control_code() -> None:
    adapter = YouPinDirectAdapter(_cfg("youpin_direct"), GateRegistry())
    adapter._session = FakeSession([FakeResponse({"code": 85100, "msg": "当前app版本过低"})])
    assert adapter.fetch_one("X", 822) is None
    assert adapter.gates.gate("youpin_direct", "purchase_page").cooldown_remaining > 0


def test_youpin_direct_empty_page_is_not_error() -> None:
    """无求购挂单是正常状态，应返回一个 bid_count=0 的报价而不是 None。"""
    adapter = YouPinDirectAdapter(_cfg("youpin_direct"), GateRegistry())
    adapter._session = FakeSession([FakeResponse({"code": 0, "data": {"responseList": []}})])
    quote = adapter.fetch_one("X", 822)
    assert quote is not None and quote.bid_price is None and quote.bid_count == 0


def test_youpin_headers_have_required_shapes() -> None:
    """请求头形状不合会被风控直接拦掉，这里把它钉住。"""
    adapter = YouPinDirectAdapter(_cfg("youpin_direct"), GateRegistry())
    headers = adapter._headers()
    assert len(headers["DeviceId"]) == 24
    assert len(headers["DeviceToken"]) == 24
    assert len(headers["requestTag"]) == 32
    assert len(headers["deviceUk"]) == 65
    assert len(headers["uk"]) == 65
    assert headers["App-Version"] == "5.45.4"
    assert headers["Gameid"] == "730"
    assert headers["package-type"] == "uuyp"


# ── Mock / 回放 ────────────────────────────────────────────

def test_mock_is_deterministic_with_same_seed() -> None:
    a = MockAdapter(_cfg("mock"), GateRegistry(), seed=42)
    b = MockAdapter(_cfg("mock"), GateRegistry(), seed=42)
    items = [ItemRef(market_hash_name="AK")]
    left = a.fetch(items)
    right = b.fetch(items)
    assert [q.sell_price for q in left] == [q.sell_price for q in right]


def test_mock_scenario_script_overrides_price() -> None:
    a = MockAdapter(_cfg("mock"), GateRegistry(), seed=1)
    a.script("AK", turn=2, sell_price=50.0, sell_count=1)
    items = [ItemRef(market_hash_name="AK")]
    a.fetch(items)                       # turn 1
    second = a.fetch(items)              # turn 2 -> 命中脚本
    buff = next(q for q in second if q.platform == "BUFF")
    assert buff.sell_price == 50.0 and buff.sell_count == 1


# ── 限速闸门 ───────────────────────────────────────────────

def test_gate_rate_limit_grows_cooldown() -> None:
    reg = GateRegistry()
    gate = reg.gate("s", "e", min_interval=0.0, base_cooldown=10.0, max_cooldown=1000.0)
    first = gate.report_rate_limit("a")
    second = gate.report_rate_limit("b")
    assert second > first                    # 指数放大
    assert gate.cooldown_remaining > 0
    with pytest.raises(RateLimitExceeded):
        gate.acquire(blocking=False)


def test_gate_success_clears_cooldown() -> None:
    reg = GateRegistry()
    gate = reg.gate("s", "e2", min_interval=0.0)
    gate.report_transient_failure("boom", cooldown=60)
    assert gate.cooldown_remaining > 0
    gate.report_success()
    assert gate.cooldown_remaining == 0
    assert gate.snapshot()["failures"] == 0


# ── 能力声明（如实标注，避免误导选型）───────────────────────

def test_credential_state_reports_missing_token() -> None:
    """csqaq 缺 Token 时必须显式报「不可用」，而不是含糊显示「无需凭证」。"""
    state = CsqaqAdapter(_cfg("csqaq", api_token=None), GateRegistry()).credential_state()
    assert state["required"] is True
    assert state["ok"] is False
    assert state["env"] == "CSQAQ_TOKEN"


def test_credential_state_ok_when_token_present() -> None:
    state = CsqaqAdapter(_cfg("csqaq", api_token="t"), GateRegistry()).credential_state()
    assert state["ok"] is True


def test_credential_free_sources_declare_no_requirement() -> None:
    for adapter in (BuffDirectAdapter(_cfg("buff_direct"), GateRegistry()),
                    YouPinDirectAdapter(_cfg("youpin_direct"), GateRegistry())):
        state = adapter.credential_state()
        assert state["required"] is False and state["ok"] is True


def test_registry_describes_capabilities_and_credentials() -> None:
    from csmon.sources import describe_registry

    rows = {row["name"]: row for row in describe_registry()}
    assert rows["csqaq"]["credential_env"] == "CSQAQ_TOKEN"
    assert rows["steamdt"]["credential_env"] == "STEAMDT_API_KEY"
    assert rows["csqaq"]["sell"] is True
    assert rows["youpin_direct"]["sell"] is False     # 如实声明不提供在售价
    assert rows["youpin_direct"]["bid"] is True


def test_buff_direct_status_snapshot_shape() -> None:
    adapter = BuffDirectAdapter(_cfg("buff_direct"), GateRegistry())
    status = adapter.describe_status()
    assert status["source"] == "buff_direct"
    assert status["ip_blocked"] is False
    assert status["ip_block_remaining_s"] == 0.0
