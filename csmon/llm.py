"""LLM 接口：把行情数据交给模型做二次分析。

**先说清楚定位**：这里的 LLM 输出是**第二个视角**，不是投资建议，也不是决策依据。
CS 饰品市场流动性差、单件差异大、受赛事与版本更新影响剧烈，任何模型都无法
可靠预测价格。本项目把 LLM 用在它真正擅长的地方：

  · 把散落的指标（RSI / 布林 / 在售量 / 跨平台价差 / 磨损分布）串成一个连贯叙事
  · 指出数据里的矛盾（例如「价格跌了但在售量也在降」这种不是单纯利空的情形）
  · 帮你把「为什么想买」写清楚，从而发现自己逻辑里的漏洞

因此接口设计上有两条硬性约束：
  1. **必须回结构化 JSON**，字段固定（action/confidence/target/reasoning/risks）。
     自由文本没法进数据库、没法回看、没法统计「这个模型有多准」。
  2. **必须显式给出风险与不利证据**，不允许只输出看多理由。

支持任何 OpenAI 兼容端点（OpenAI / DeepSeek / Kimi / 通义 / 本地 vLLM / LM Studio），
另有 Anthropic 与 Ollama 原生适配。密钥只从环境变量读，不写进配置、不进日志。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

PROVIDER_OPENAI = "openai"          # 任何 OpenAI 兼容端点
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OLLAMA = "ollama"

#: 常见端点预设（base_url 与默认模型）
PRESETS: dict[str, dict[str, str]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "anthropic": {"base_url": "https://api.anthropic.com/v1",
                  "model": "claude-3-5-sonnet-latest"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "dashscope": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "model": "qwen-plus"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "siliconflow": {"base_url": "https://api.siliconflow.cn/v1",
                    "model": "Qwen/Qwen2.5-7B-Instruct"},
    "ollama": {"base_url": "http://127.0.0.1:11434", "model": "qwen2.5:7b"},
    "lmstudio": {"base_url": "http://127.0.0.1:1234/v1", "model": "local-model"},
    "vllm": {"base_url": "http://127.0.0.1:8000/v1", "model": "local-model"},
}

#: 走本机、不需要密钥的预设
LOCAL_PRESETS: frozenset[str] = frozenset({"ollama", "lmstudio", "vllm"})


class LLMError(RuntimeError):
    """LLM 调用失败。调用方应把它当作「暂时没有第二意见」，而不是阻断主流程。"""


@dataclass
class LLMConfig:
    """LLM 端点配置。密钥字段永不参与日志与落盘。"""

    provider: str = PROVIDER_OPENAI
    preset: str = ""                 # 预设名（deepseek / ollama ...），用于取默认 base_url/model
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    api_key_env: str = "CSMON_LLM_API_KEY"
    timeout: float = 90.0
    max_tokens: int = 2000
    temperature: float = 0.2
    max_retries: int = 2
    enabled: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        preset = PRESETS.get(self.preset or "")
        if preset:
            return preset["base_url"].rstrip("/")
        return PRESETS["openai"]["base_url"]

    def resolved_model(self) -> str:
        if self.model:
            return self.model
        preset = PRESETS.get(self.preset or "")
        return preset["model"] if preset else PRESETS["openai"]["model"]

    def is_configured(self) -> bool:
        """是否具备调用条件。

        本机端点（Ollama / LM Studio / vLLM）不需要密钥，所以不能只看密钥是否为空。
        """
        if not self.enabled:
            return False
        base = self.resolved_base_url()
        local = any(h in base for h in ("127.0.0.1", "localhost", "0.0.0.0", "::1"))
        if self.provider == PROVIDER_OLLAMA or local:
            return True
        return bool(self.api_key)

    def describe(self) -> dict[str, Any]:
        """可安全展示/落盘的描述（绝不含密钥）。"""
        return {
            "provider": self.provider,
            "preset": self.preset,
            "base_url": self.resolved_base_url(),
            "model": self.resolved_model(),
            "configured": self.is_configured(),
            "api_key_set": bool(self.api_key),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

    @classmethod
    def from_env(cls) -> LLMConfig:
        """仅从环境变量构造（保留给不加载应用的场景）。"""
        return cls._from(settings=None)

    @classmethod
    def from_settings(cls, settings: Any) -> LLMConfig:
        """从应用的 llm 配置段 + 环境变量构造。

        优先级：环境变量 > config.local.yaml > 预设默认。
        这样「在 config.local.yaml 里选预设」和「用环境变量临时覆盖」
        两条路都能走，且密钥始终只来自环境变量。
        """
        return cls._from(settings=settings)

    @classmethod
    def _from(cls, settings: Any = None) -> LLMConfig:
        # ── 密钥（只来自环境变量）──
        key_env = "CSMON_LLM_API_KEY"
        api_key = os.environ.get(key_env, "").strip()
        if not api_key:
            # 兼容各家约定的变量名，降低接入摩擦
            for fallback in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "MOONSHOT_API_KEY",
                             "DASHSCOPE_API_KEY", "ZHIPUAI_API_KEY",
                             "SILICONFLOW_API_KEY"):
                if os.environ.get(fallback):
                    api_key = os.environ[fallback].strip()
                    key_env = fallback
                    break

        # ── 预设与模型（环境变量优先于配置文件）──
        def pick(env_name: str, attr: str) -> str:
            return ((os.environ.get(env_name) or "").strip()
                    or (getattr(settings, attr, "") or "").strip())

        preset = pick("CSMON_LLM_PRESET", "preset").lower()
        provider = pick("CSMON_LLM_PROVIDER", "provider").lower()
        base_url = pick("CSMON_LLM_BASE_URL", "base_url")
        model = pick("CSMON_LLM_MODEL", "model")

        if not provider:
            if preset == PROVIDER_OLLAMA:
                provider = PROVIDER_OLLAMA
            elif preset == PROVIDER_ANTHROPIC:
                provider = PROVIDER_ANTHROPIC
            else:
                provider = PROVIDER_OPENAI

        enabled_raw = (os.environ.get("CSMON_LLM_ENABLED") or "").strip().lower()
        if enabled_raw:
            enabled = enabled_raw not in ("0", "false", "no", "off")
        else:
            enabled = bool(getattr(settings, "enabled", True))

        def pick_float(env_name: str, attr: str, default: float) -> float:
            raw = os.environ.get(env_name)
            if raw:
                try:
                    return float(raw)
                except ValueError:
                    pass
            return float(getattr(settings, attr, default) or default)

        def pick_int(env_name: str, attr: str, default: int) -> int:
            raw = os.environ.get(env_name)
            if raw:
                try:
                    return int(raw)
                except ValueError:
                    pass
            return int(getattr(settings, attr, default) or default)

        return cls(
            provider=provider,
            preset=preset,
            base_url=base_url,
            model=model,
            api_key=api_key,
            api_key_env=key_env,
            timeout=pick_float("CSMON_LLM_TIMEOUT", "timeout", 90.0),
            max_tokens=pick_int("CSMON_LLM_MAX_TOKENS", "max_tokens", 2000),
            temperature=pick_float("CSMON_LLM_TEMPERATURE", "temperature", 0.2),
            enabled=enabled,
        )


class LLMClient:
    """最小可用的 LLM 客户端，覆盖三种协议。"""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig.from_env()
        self._session = requests.Session()

    # ── 对外入口 ───────────────────────────────────────────

    def complete(self, system: str, user: str,
                 json_mode: bool = True) -> str:
        """发起一次对话补全，返回模型原文。"""
        if not self.config.is_configured():
            raise LLMError(
                f"LLM 未配置：请设置 {self.config.api_key_env}"
                "（本机端点如 Ollama 则设置 CSMON_LLM_PRESET=ollama）")

        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 2):
            try:
                return self._dispatch(system, user, json_mode)
            except LLMError:
                raise
            except requests.RequestException as exc:
                last_error = exc
                if attempt <= self.config.max_retries:
                    backoff = 2 ** attempt
                    logger.warning("[llm] 网络错误（第 %d 次）：%s，%ds 后重试",
                                   attempt, _safe(str(exc)), backoff)
                    time.sleep(backoff)
        raise LLMError(f"LLM 调用失败：{_safe(str(last_error))}")

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        """要求模型返回 JSON 并解析。解析失败时抛 LLMError（不静默返回空字典）。"""
        text = self.complete(system, user, json_mode=True)
        payload = extract_json(text)
        if payload is None:
            raise LLMError(f"模型未返回可解析的 JSON：{_safe(text[:200])}")
        return payload

    def probe(self) -> dict[str, Any]:
        """连通性自检：发一个极小请求，确认端点、密钥、模型都对。"""
        started = time.monotonic()
        try:
            reply = self.complete("你是测试助手。", "只回复两个字：正常", json_mode=False)
        except LLMError as exc:
            return {"ok": False, "error": str(exc),
                    "config": self.config.describe()}
        return {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000),
                "reply": _safe(reply.strip()[:80]),
                "config": self.config.describe()}

    def close(self) -> None:
        self._session.close()

    # ── 协议分发 ───────────────────────────────────────────

    def _dispatch(self, system: str, user: str, json_mode: bool) -> str:
        if self.config.provider == PROVIDER_ANTHROPIC:
            return self._call_anthropic(system, user, json_mode)
        if self.config.provider == PROVIDER_OLLAMA:
            return self._call_ollama(system, user, json_mode)
        return self._call_openai(system, user, json_mode)

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.extra_headers)
        return headers

    def _call_openai(self, system: str, user: str, json_mode: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.config.resolved_model(),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if json_mode:
            # 多数兼容端点支持；不支持的会忽略该字段，不影响后续解析
            payload["response_format"] = {"type": "json_object"}

        resp = self._session.post(
            f"{self.config.resolved_base_url()}/chat/completions",
            json=payload, headers=self._auth_headers(), timeout=self.config.timeout)

        if resp.status_code == 401:
            raise LLMError("LLM 鉴权失败（401）：请检查 API Key")
        if resp.status_code == 404:
            raise LLMError(
                f"LLM 端点 404：请检查 base_url（当前 {self.config.resolved_base_url()}）"
                " 与模型名")
        if resp.status_code == 429:
            raise LLMError("LLM 限流（429）：请降低调用频率或更换配额")
        if resp.status_code >= 400:
            raise LLMError(f"LLM 返回 HTTP {resp.status_code}：{_safe(resp.text[:200])}")

        body = resp.json()
        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应结构异常：{_safe(json.dumps(body)[:200])}") from exc

    def _call_anthropic(self, system: str, user: str, json_mode: bool) -> str:
        prompt = user
        if json_mode:
            prompt += "\n\n只输出一个 JSON 对象，不要任何其他文字。"
        payload = {
            "model": self.config.resolved_model(),
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
        }
        resp = self._session.post(
            f"{self.config.resolved_base_url()}/messages",
            json=payload, headers=headers, timeout=self.config.timeout)
        if resp.status_code == 401:
            raise LLMError("Anthropic 鉴权失败（401）：请检查 API Key")
        if resp.status_code >= 400:
            raise LLMError(f"Anthropic 返回 HTTP {resp.status_code}：{_safe(resp.text[:200])}")
        body = resp.json()
        blocks = body.get("content") or []
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))

    def _call_ollama(self, system: str, user: str, json_mode: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.config.resolved_model(),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": {"temperature": self.config.temperature},
        }
        if json_mode:
            payload["format"] = "json"
        resp = self._session.post(
            f"{self.config.resolved_base_url()}/api/chat",
            json=payload, timeout=self.config.timeout)
        if resp.status_code >= 400:
            raise LLMError(f"Ollama 返回 HTTP {resp.status_code}：{_safe(resp.text[:200])}")
        body = resp.json()
        return (body.get("message") or {}).get("content", "")


# ── 工具 ───────────────────────────────────────────────────

def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象。

    模型经常把 JSON 包在 ```json 代码块里、或在前后加一句解释，
    所以直接 json.loads 是不够的。这里按「先整段、再去代码块、
    最后括号配对扫描」的顺序尝试。
    """
    if not text:
        return None
    text = text.strip()

    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except ValueError:
        pass

    if "```" in text:
        chunks = text.split("```")
        for chunk in chunks:
            candidate = chunk.strip()
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                try:
                    payload = json.loads(candidate)
                    if isinstance(payload, dict):
                        return payload
                except ValueError:
                    continue

    start = text.find("{")
    if start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            char = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        payload = json.loads(text[start:idx + 1])
                        return payload if isinstance(payload, dict) else None
                    except ValueError:
                        return None
    return None


_SENSITIVE = ("sk-", "api_key", "apikey", "authorization", "bearer ")


def _safe(text: str) -> str:
    """脱敏：把可能混进错误信息里的密钥抹掉。"""
    out = text
    lowered = out.lower()
    for marker in _SENSITIVE:
        idx = lowered.find(marker)
        while idx >= 0:
            end = min(len(out), idx + len(marker) + 24)
            out = out[:idx + len(marker)] + "***" + out[end:]
            lowered = out.lower()
            idx = lowered.find(marker, idx + len(marker) + 3)
    return out
