"""LLM 接口与建议引擎测试（全部离线：用假的 HTTP 会话，不真调模型）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from csmon.advice import (
    ACTION_CN,
    build_context,
    advice_is_stale,
    render_user_prompt,
    request_advice,
    _digest,
)
from csmon.llm import (
    LLMClient,
    LLMConfig,
    LLMError,
    PROVIDER_ANTHROPIC,
    PROVIDER_OLLAMA,
    PROVIDER_OPENAI,
    extract_json,
)
from csmon.models import SourceQuote, utcnow
from csmon.store import Store


# ── 配置 ───────────────────────────────────────────────────

def test_config_from_env_local_endpoint_needs_no_key(monkeypatch) -> None:
    """本机端点（Ollama）不该因为没密钥被判定为未配置。"""
    monkeypatch.setenv("CSMON_LLM_PRESET", "ollama")
    for key in ("CSMON_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    cfg = LLMConfig.from_env()
    assert cfg.provider == PROVIDER_OLLAMA
    assert cfg.is_configured() is True
    assert "11434" in cfg.resolved_base_url()


def test_config_cloud_requires_key(monkeypatch) -> None:
    monkeypatch.setenv("CSMON_LLM_PRESET", "deepseek")
    for key in ("CSMON_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    cfg = LLMConfig.from_env()
    assert cfg.is_configured() is False

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    cfg2 = LLMConfig.from_env()
    assert cfg2.is_configured() is True


def test_config_describe_never_leaks_key() -> None:
    cfg = LLMConfig(api_key="sk-super-secret-value")
    described = json.dumps(cfg.describe())
    assert "sk-super-secret-value" not in described
    assert cfg.describe()["api_key_set"] is True


def test_config_preset_resolution() -> None:
    cfg = LLMConfig(preset="deepseek")
    assert "deepseek.com" in cfg.resolved_base_url()
    assert cfg.resolved_model() == "deepseek-chat"


def test_config_explicit_overrides_preset() -> None:
    cfg = LLMConfig(preset="deepseek", base_url="http://my.endpoint/v1",
                    model="my-model")
    assert cfg.resolved_base_url() == "http://my.endpoint/v1"
    assert cfg.resolved_model() == "my-model"


# ── JSON 抽取 ──────────────────────────────────────────────

def test_extract_json_plain() -> None:
    assert extract_json('{"action": "buy"}') == {"action": "buy"}


def test_extract_json_from_code_fence() -> None:
    text = '分析如下：\n```json\n{"action": "sell", "confidence": 0.6}\n```\n完毕'
    assert extract_json(text) == {"action": "sell", "confidence": 0.6}


def test_extract_json_from_prose() -> None:
    text = '我的判断是 {"action": "hold"} ，仅供参考。'
    assert extract_json(text) == {"action": "hold"}


def test_extract_json_nested_braces_in_string() -> None:
    text = '{"reasoning": "包含 { 花括号 } 的字符串", "action": "buy"}'
    assert extract_json(text) == {"reasoning": "包含 { 花括号 } 的字符串",
                                  "action": "buy"}


def test_extract_json_returns_none_on_garbage() -> None:
    assert extract_json("完全不是 JSON") is None
    assert extract_json("") is None
    assert extract_json("[1,2,3]") is None      # 顶层数组不接受


# ── 客户端（假会话）────────────────────────────────────────

class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            raise AssertionError("响应已耗尽")
        return self._responses.pop(0)

    def close(self):
        pass


def _openai_reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_client_openai_happy_path() -> None:
    client = LLMClient(LLMConfig(provider=PROVIDER_OPENAI, api_key="k",
                                 base_url="https://x/v1", model="m"))
    client._session = FakeSession([FakeResponse(_openai_reply('{"ok":1}'))])
    assert client.complete("s", "u") == '{"ok":1}'
    sent = client._session.calls[0]
    assert sent["url"].endswith("/chat/completions")
    assert sent["headers"]["Authorization"] == "Bearer k"
    assert sent["json"]["response_format"]["type"] == "json_object"


def test_client_401_raises_clear_error() -> None:
    client = LLMClient(LLMConfig(provider=PROVIDER_OPENAI, api_key="bad",
                                 base_url="https://x/v1", model="m"))
    client._session = FakeSession([FakeResponse({"error": "nope"}, status_code=401)])
    with pytest.raises(LLMError, match="鉴权失败"):
        client.complete("s", "u")


def test_client_404_mentions_base_url() -> None:
    client = LLMClient(LLMConfig(provider=PROVIDER_OPENAI, api_key="k",
                                 base_url="https://wrong/v1", model="m"))
    client._session = FakeSession([FakeResponse({"e": 1}, status_code=404)])
    with pytest.raises(LLMError, match="base_url"):
        client.complete("s", "u")


def test_client_not_configured_raises() -> None:
    client = LLMClient(LLMConfig(provider=PROVIDER_OPENAI, api_key="",
                                 base_url="https://api.openai.com/v1"))
    with pytest.raises(LLMError, match="未配置"):
        client.complete("s", "u")


def test_client_anthropic_shape() -> None:
    client = LLMClient(LLMConfig(provider=PROVIDER_ANTHROPIC, api_key="k",
                                 base_url="https://api.anthropic.com/v1", model="m"))
    client._session = FakeSession([FakeResponse(
        {"content": [{"type": "text", "text": '{"a":1}'}]})])
    assert client.complete("s", "u") == '{"a":1}'
    call = client._session.calls[0]
    assert call["url"].endswith("/messages")
    assert call["headers"]["x-api-key"] == "k"


def test_client_ollama_shape() -> None:
    client = LLMClient(LLMConfig(provider=PROVIDER_OLLAMA,
                                 base_url="http://127.0.0.1:11434", model="qwen"))
    client._session = FakeSession([FakeResponse({"message": {"content": "你好"}})])
    assert client.complete("s", "u", json_mode=False) == "你好"
    assert client._session.calls[0]["url"].endswith("/api/chat")


def test_client_malformed_response_raises() -> None:
    client = LLMClient(LLMConfig(provider=PROVIDER_OPENAI, api_key="k",
                                 base_url="https://x/v1", model="m"))
    client._session = FakeSession([FakeResponse({"unexpected": True})])
    with pytest.raises(LLMError, match="结构异常"):
        client.complete("s", "u")


def test_client_redacts_key_from_error() -> None:
    """错误信息里绝不能出现密钥原文。"""
    client = LLMClient(LLMConfig(provider=PROVIDER_OPENAI,
                                 api_key="sk-abcdef1234567890",
                                 base_url="https://x/v1", model="m"))
    client._session = FakeSession([
        FakeResponse("bad request: key sk-abcdef1234567890 rejected", status_code=400)])
    with pytest.raises(LLMError) as exc:
        client.complete("s", "u")
    assert "sk-abcdef1234567890" not in str(exc.value)


def test_complete_json_rejects_non_json() -> None:
    client = LLMClient(LLMConfig(provider=PROVIDER_OPENAI, api_key="k",
                                 base_url="https://x/v1", model="m"))
    client._session = FakeSession([FakeResponse(_openai_reply("我想了想，还是不说了"))])
    with pytest.raises(LLMError, match="JSON"):
        client.complete_json("s", "u")


# ── 上下文构建 ─────────────────────────────────────────────

def _seed(store: Store, name: str = "AK-47 | Redline (Field-Tested)") -> None:
    from datetime import timedelta

    now = utcnow()
    store.insert_quotes([
        SourceQuote(market_hash_name=name, platform="BUFF", source="csqaq",
                    sell_price=100.0 + (i % 7), sell_count=50 - i, bid_price=95.0,
                    observed_at=now - timedelta(days=30 - i),
                    raw={"name": "AK-47 | 红线 (久经沙场)"})
        for i in range(30)
    ])
    store.upsert_item(__import__("csmon.models", fromlist=["ItemRef"]).ItemRef(
        market_hash_name=name, buff_goods_id=1))


def test_build_context_has_required_sections(store: Store) -> None:
    _seed(store)
    ctx = build_context(store, "AK-47 | Redline (Field-Tested)")
    payload = ctx.payload
    for key in ("item", "current", "indicators", "wear_ladder",
                "cross_platform_spreads", "my_focus", "recent_alerts"):
        assert key in payload, key
    assert payload["item"]["wear_cn"] == "久经沙场"
    assert payload["item"]["weapon"] == "AK-47"
    assert ctx.digest and len(ctx.digest) == 16


def test_build_context_includes_focus_intent(store: Store) -> None:
    from csmon.focus import add_focus

    _seed(store)
    add_focus(store, "AK-47 | Redline (Field-Tested)", intent="buy", target_price=95.0)
    ctx = build_context(store, "AK-47 | Redline (Field-Tested)")
    assert ctx.payload["my_focus"]["intent"] == "buy"
    assert ctx.payload["my_focus"]["target_price"] == 95.0


def test_context_digest_changes_when_price_changes(store: Store) -> None:
    _seed(store)
    first = build_context(store, "AK-47 | Redline (Field-Tested)")
    store.insert_quotes([SourceQuote(
        market_hash_name="AK-47 | Redline (Field-Tested)", platform="BUFF",
        source="csqaq", sell_price=888.0, observed_at=utcnow())])
    second = build_context(store, "AK-47 | Redline (Field-Tested)")
    assert first.digest != second.digest


def test_render_user_prompt_reflects_intent(store: Store) -> None:
    from csmon.focus import add_focus

    _seed(store)
    add_focus(store, "AK-47 | Redline (Field-Tested)", intent="sell")
    prompt = render_user_prompt(build_context(store, "AK-47 | Redline (Field-Tested)"))
    assert "准备卖出" in prompt
    assert "```json" in prompt


def test_advice_is_stale_detects_change(store: Store) -> None:
    _seed(store)
    ctx = build_context(store, "AK-47 | Redline (Field-Tested)")
    fresh = advice_is_stale(store, "AK-47 | Redline (Field-Tested)", ctx.digest)
    assert fresh["stale"] is False

    store.insert_quotes([SourceQuote(
        market_hash_name="AK-47 | Redline (Field-Tested)", platform="BUFF",
        source="csqaq", sell_price=999.0, observed_at=utcnow())])
    changed = advice_is_stale(store, "AK-47 | Redline (Field-Tested)", ctx.digest)
    assert changed["stale"] is True


def test_digest_is_order_independent() -> None:
    assert _digest({"a": 1, "b": 2}) == _digest({"b": 2, "a": 1})


# ── 建议生成 ───────────────────────────────────────────────

VALID_ADVICE = {
    "action": "buy",
    "confidence": 0.72,
    "target_buy": 95.0,
    "target_sell": 130.0,
    "stop_loss": 80.0,
    "horizon_days": 60,
    "reasoning": "现价 100 低于 7 日中位 103，RSI 41 未超买，在售量回落。",
    "counter_evidence": "全平台在售量仍在增加，可能还有下跌空间。",
    "risks": "赛事结束后的需求回落风险；流动性差，急售可能折价。",
    "data_gaps": ["缺少贴纸溢价数据", "缺少近期成交量"],
}


def test_request_advice_happy_path(store: Store) -> None:
    _seed(store)
    client = LLMClient(LLMConfig(provider=PROVIDER_OPENAI, api_key="k",
                                 base_url="https://x/v1", model="m"))
    client._session = FakeSession([FakeResponse(
        _openai_reply(json.dumps(VALID_ADVICE, ensure_ascii=False)))])

    result = request_advice(store, "AK-47 | Redline (Field-Tested)", client=client)
    assert result.action == "buy"
    assert result.confidence == pytest.approx(0.72)
    assert result.target_buy == 95.0
    assert result.counter_evidence
    assert result.data_gaps
    assert result.advice_id is not None

    saved = store.recent_advice(1)[0]
    assert saved["action"] == "buy"
    assert "反面证据" in saved["reasoning"]     # 落库时把反面证据并进正文
    assert saved["context_digest"] == result.context_digest


def test_request_advice_percent_confidence_normalised(store: Store) -> None:
    """模型有时把把握写成 72 而不是 0.72，要归一。"""
    _seed(store)
    payload = dict(VALID_ADVICE, confidence=72)
    client = LLMClient(LLMConfig(provider=PROVIDER_OPENAI, api_key="k",
                                 base_url="https://x/v1", model="m"))
    client._session = FakeSession([FakeResponse(
        _openai_reply(json.dumps(payload, ensure_ascii=False)))])
    result = request_advice(store, "AK-47 | Redline (Field-Tested)", client=client)
    assert result.confidence == pytest.approx(0.72)


def test_request_advice_unknown_action_coerced(store: Store) -> None:
    _seed(store)
    payload = dict(VALID_ADVICE, action="强烈建议梭哈")
    client = LLMClient(LLMConfig(provider=PROVIDER_OPENAI, api_key="k",
                                 base_url="https://x/v1", model="m"))
    client._session = FakeSession([FakeResponse(
        _openai_reply(json.dumps(payload, ensure_ascii=False)))])
    result = request_advice(store, "AK-47 | Redline (Field-Tested)", client=client)
    assert result.action in ACTION_CN


def test_request_advice_data_gaps_string_becomes_list(store: Store) -> None:
    _seed(store)
    payload = dict(VALID_ADVICE, data_gaps="缺少成交量")
    client = LLMClient(LLMConfig(provider=PROVIDER_OPENAI, api_key="k",
                                 base_url="https://x/v1", model="m"))
    client._session = FakeSession([FakeResponse(
        _openai_reply(json.dumps(payload, ensure_ascii=False)))])
    result = request_advice(store, "AK-47 | Redline (Field-Tested)", client=client)
    assert result.data_gaps == ["缺少成交量"]


def test_request_advice_does_not_persist_when_asked(store: Store) -> None:
    _seed(store)
    client = LLMClient(LLMConfig(provider=PROVIDER_OPENAI, api_key="k",
                                 base_url="https://x/v1", model="m"))
    client._session = FakeSession([FakeResponse(
        _openai_reply(json.dumps(VALID_ADVICE, ensure_ascii=False)))])
    request_advice(store, "AK-47 | Redline (Field-Tested)", client=client,
                   persist=False)
    assert store.recent_advice() == []


def test_advice_cli_help_present() -> None:
    """advice 命令必须可达（命令行是主要使用方式）。"""
    from csmon.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["advice", "config"])
    assert args.action == "config"
    args = parser.parse_args(["focus", "add", "AK-47 | Redline",
                              "--intent", "buy", "--target", "95"])
    assert args.intent == "buy" and args.target == 95.0
    args = parser.parse_args(["patterns", "learn", "X", "--pages", "3"])
    assert args.pages == 3
