"""配置向导：把「读文档 → 找环境变量名 → 手改 .env」压缩成几个选择。

设计目标（针对个人自用场景）：
  · **不要求用户编辑任何文件**。选编号、粘密钥，剩下的写进本地私有配置。
  · **密钥与非密钥分离**。密钥进 `.env`，其余进 `config.local.yaml`，
    两个文件都在 .gitignore 里，git pull 永远不冲突。
  · **当场验证**。配完立刻探一次，让用户马上知道对不对，
    而不是等到几小时后发现「怎么没数据」。
  · **可反复运行**。改主意了再跑一次即可，不会产生重复条目。

非交互环境（管道、CI）下自动跳过提问，只打印指引，不会卡住。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import (
    Config,
    LLMSettings,
    local_config_path,
    write_local_config,
)
from .llm import LOCAL_PRESETS, PRESETS, LLMClient, LLMConfig

# ── 终端交互 ───────────────────────────────────────────────

def _interactive() -> bool:
    """是否具备交互条件。管道/重定向下不提问，避免脚本卡死。"""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(130)
    return value or default


def _ask_choice(prompt: str, options: list[tuple[str, str]],
                default: str) -> str:
    """让用户按编号选择。options = [(值, 说明)]。"""
    print(f"\n  {prompt}")
    for index, (value, label) in enumerate(options, start=1):
        mark = " (默认)" if value == default else ""
        print(f"    {index}. {label}{mark}")
    raw = _ask("输入编号", "").strip()
    if not raw:
        return default
    try:
        picked = int(raw)
    except ValueError:
        print(f"    无法识别「{raw}」，用默认值 {default}")
        return default
    if 1 <= picked <= len(options):
        return options[picked - 1][0]
    print(f"    编号超范围，用默认值 {default}")
    return default


def _confirm(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = _ask(f"{prompt} ({hint})", "").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "是", "1", "true")


# ── .env 读写 ──────────────────────────────────────────────

def env_file_path(config_path: Path | None = None) -> Path:
    base = config_path or Path("config.yaml")
    return base.parent / ".env"


def update_env(path: Path, updates: dict[str, str]) -> list[str]:
    """更新 .env 里的键值，保持其余内容与注释不变。

    已有的键就地替换（不追加重复项），新键追加到末尾。
    返回实际变更的键名。
    """
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    changed: list[str] = []
    remaining = dict(updates)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
            changed.append(key)

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# --- 由 facet setup 写入 ---")
        for key, value in remaining.items():
            lines.append(f"{key}={value}")
            changed.append(key)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)      # 尽力而为：Windows 上无效果，Unix 上限制权限
    except OSError:
        pass
    return changed


def mask(secret: str, keep: int = 4) -> str:
    """脱敏展示：只留头尾几个字符，供确认用。"""
    if not secret:
        return "(空)"
    if len(secret) <= keep * 2:
        return "*" * len(secret)
    return f"{secret[:keep]}{'*' * 6}{secret[-keep:]}"


# ── LLM 向导 ───────────────────────────────────────────────

#: 预设展示顺序与说明（按国内可用性排序，本地模型放最后）
PRESET_MENU: list[tuple[str, str]] = [
    ("deepseek", "DeepSeek      —— 便宜、中文好，CS 场景够用（推荐）"),
    ("moonshot", "Kimi 月之暗面  —— 长上下文"),
    ("dashscope", "通义千问       —— 阿里云"),
    ("zhipu", "智谱 GLM       —— 有免费额度"),
    ("siliconflow", "硅基流动     —— 聚合多模型"),
    ("openai", "OpenAI        —— 需要海外网络"),
    ("anthropic", "Claude        —— 需要海外网络"),
    ("ollama", "Ollama（本机）—— 完全离线，无需密钥"),
    ("lmstudio", "LM Studio（本机）"),
    ("vllm", "vLLM（本机）"),
]

#: 每个预设对应的密钥环境变量名（写进 .env）
PRESET_KEY_ENV: dict[str, str] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "zhipu": "ZHIPUAI_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "FACET_LLM_API_KEY",
}

PRESET_SIGNUP: dict[str, str] = {
    "deepseek": "https://platform.deepseek.com/api_keys",
    "moonshot": "https://platform.moonshot.cn/console/api-keys",
    "dashscope": "https://bailian.console.aliyun.com/",
    "zhipu": "https://open.bigmodel.cn/usercenter/apikeys",
    "siliconflow": "https://cloud.siliconflow.cn/account/ak",
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
}


def llm_wizard(config: Config, interactive: bool | None = None,
               preset: str | None = None, api_key: str | None = None,
               model: str | None = None, verify: bool = True) -> dict[str, Any]:
    """配置 LLM 接入。返回结果摘要。"""
    use_tty = _interactive() if interactive is None else interactive
    result: dict[str, Any] = {"preset": "", "changed": [], "verified": None}

    if not use_tty and not preset:
        return {
            "skipped": True,
            "hint": ("非交互环境。请手动设定：\n"
                     "  在 config.local.yaml 写：llm: {preset: deepseek}\n"
                     "  在 .env 写：DEEPSEEK_API_KEY=sk-xxx\n"
                     "  预设列表见 python -m facet advice presets"),
        }

    # 1) 选预设
    if preset:
        chosen = preset
    else:
        chosen = _ask_choice("用哪家模型？", PRESET_MENU,
                             default=config.llm.preset or "deepseek")
    if chosen not in PRESETS:
        print(f"  未知预设「{chosen}」，有效值：{', '.join(PRESETS)}")
        return {"error": f"unknown preset: {chosen}"}
    result["preset"] = chosen

    spec = PRESETS[chosen]
    is_local = chosen in LOCAL_PRESETS

    # 2) 取密钥（本机模型跳过）
    key_value = api_key or ""
    if not is_local:
        key_env_name = PRESET_KEY_ENV.get(chosen, "FACET_LLM_API_KEY")
        existing = os.environ.get(key_env_name) or os.environ.get("FACET_LLM_API_KEY")
        if existing:
            print(f"\n  检测到已配置的密钥（{key_env_name} = {mask(existing)}）")
            if use_tty and not _confirm("要替换吗？", default=False):
                key_value = existing
        if not key_value and use_tty:
            signup = PRESET_SIGNUP.get(chosen)
            if signup:
                print(f"\n  获取密钥：{signup}")
            key_value = _ask(f"粘贴 API Key（回车跳过）", "")
        if not key_value:
            print("  未提供密钥，LLM 功能将保持不可用。")
    else:
        print(f"\n  本机模型无需密钥。请确保 {spec['base_url']} 已在运行。")
        print(f"  启动方式（示例）：ollama serve && ollama pull {spec['model']}")

    # 3) 写配置：非密钥进本地配置，密钥进 .env
    local_updates: dict[str, Any] = {"llm": {"preset": chosen, "enabled": True}}
    if model:
        local_updates["llm"]["model"] = model
    local_file = write_local_config(local_updates, local_config_path(config.config_path))
    print(f"\n  已写入本地配置：{local_file}")

    if key_value and not is_local:
        key_env_name = PRESET_KEY_ENV.get(chosen, "FACET_LLM_API_KEY")
        env_path = env_file_path(config.config_path)
        changed = update_env(env_path, {key_env_name: key_value})
        os.environ[key_env_name] = key_value        # 让本次校验立刻生效
        result["changed"] = changed
        print(f"  已写入密钥：{env_path}  ({key_env_name} = {mask(key_value)})")

    # 4) 当场验证
    if verify:
        print("\n  正在验证连通性…")
        client = LLMClient(LLMConfig.from_settings(
            LLMSettings(preset=chosen, model=model or "")))
        try:
            probe = client.probe()
        finally:
            client.close()
        result["verified"] = probe.get("ok", False)
        if probe.get("ok"):
            print(f"  ✓ 可用（{probe['latency_ms']}ms，模型 {probe['config']['model']}）")
        else:
            print(f"  ✗ {probe.get('error')}")
            print("    排查：密钥是否正确 / 账户是否有余额 / 本机模型是否已启动")
    return result


# ── BUFF Cookie 向导 ───────────────────────────────────────

BUFF_COOKIE_STEPS = """\
  获取 BUFF Cookie 的步骤（约 1 分钟）：
    1. 浏览器登录 https://buff.163.com
    2. 按 F12 打开开发者工具 → Network（网络）标签
    3. 刷新页面，随便点一条 api 请求
    4. 在 Request Headers 里找到 Cookie 一行，整行复制
       （关键字段是 session；整行复制最省事）

  配置后可解锁（实测确认）：
    · 按名称搜索饰品 —— 不用再扫描 goods_id 空间，也就不容易触发风控
    · 按 paintseed / 磨损区间筛选挂单 —— 档位级在售价
    · 成交记录
"""


def buff_wizard(config: Config, interactive: bool | None = None,
                cookie: str | None = None, verify: bool = True) -> dict[str, Any]:
    """配置 BUFF Cookie（可选增强；不配也能跑匿名模式）。"""
    use_tty = _interactive() if interactive is None else interactive
    result: dict[str, Any] = {"configured": False, "verified": None}

    print("\n  BUFF 数据源有两种模式：")
    print("    匿名   —— 可在售价 + 求购价（够用，但会被用量触发的风控关闭）")
    print("    带 Cookie —— 额外解锁：按名称搜索、按档位筛选、成交记录")
    print(BUFF_COOKIE_STEPS)

    value = cookie or ""
    if not value and use_tty:
        if not _confirm("现在就配置 Cookie 吗？", default=False):
            print("  跳过。之后可随时执行 python -m facet setup buff")
            return result
        value = _ask("粘贴 Cookie（回车跳过）", "")
    if not value:
        if not use_tty:
            result["hint"] = "非交互环境：请手动在 .env 写 BUFF_COOKIE=<值>"
        return result

    env_path = env_file_path(config.config_path)
    update_env(env_path, {"BUFF_COOKIE": value})
    os.environ["BUFF_COOKIE"] = value
    result["configured"] = True
    print(f"\n  已写入 {env_path}  (BUFF_COOKIE = {mask(value, 6)})")

    if verify:
        print("  正在验证…")
        from .sources.buff_direct import BuffDirectAdapter

        adapter = BuffDirectAdapter(config.source("buff"))
        try:
            check = adapter.check_session()
        finally:
            adapter.close()
        result["verified"] = check.get("session_valid")
        if check.get("session_valid"):
            print(f"  ✓ Cookie 有效（搜索返回示例：{check.get('sample')}）")
            print("  已解锁：按名称搜索、按档位筛选、成交记录")
        else:
            print(f"  ✗ {check.get('hint')}")
            print("    多半是 Cookie 不完整或已过期，重新复制一次（要含 session 字段）")
    return result


# ── CSQAQ 向导 ─────────────────────────────────────────────

def csqaq_wizard(config: Config, interactive: bool | None = None,
                 token: str | None = None, verify: bool = True) -> dict[str, Any]:
    """配置 CSQAQ Token（主力源）。"""
    use_tty = _interactive() if interactive is None else interactive
    result: dict[str, Any] = {"configured": False, "verified": None}

    print("\n  CSQAQ 是主力源：一次调用同时返回 BUFF + 悠悠有品 + Steam 的")
    print("  在售价与在售量。注册免费，但**必须在官网绑定本机公网 IP**。")
    print("    注册与取 Token： https://csqaq.com")

    value = token or os.environ.get("CSQAQ_TOKEN", "")
    if not value and use_tty:
        value = _ask("粘贴 ApiToken（回车跳过）", "")
    if not value:
        if not use_tty:
            result["hint"] = "非交互环境：请手动在 .env 写 CSQAQ_TOKEN=<值>"
        return result

    env_path = env_file_path(config.config_path)
    update_env(env_path, {"CSQAQ_TOKEN": value})
    os.environ["CSQAQ_TOKEN"] = value
    result["configured"] = True
    print(f"\n  已写入 {env_path}  (CSQAQ_TOKEN = {mask(value)})")

    if verify:
        print("  正在验证…")
        from .sources.csqaq import CsqaqAdapter
        from .models import ItemRef

        source_cfg = config.source("csqaq")
        source_cfg.api_token = value
        adapter = CsqaqAdapter(source_cfg)
        try:
            quotes, report = adapter.fetch_report(
                [ItemRef(market_hash_name="AK-47 | Redline (Field-Tested)")])
        finally:
            adapter.close()

        if quotes:
            platforms = sorted({q.platform for q in quotes})
            result["verified"] = True
            print(f"  ✓ 有效，返回平台：{', '.join(platforms)}")
        else:
            result["verified"] = False
            print("  ✗ 未取到数据")
            print("    最常见原因：**没有在官网绑定本机白名单 IP**")
            print("    其次：Token 复制不完整")
            for err in (report.errors or [])[:2]:
                print(f"    {err}")
    return result


# ── 一键总向导 ─────────────────────────────────────────────

def run_setup(config: Config, targets: list[str] | None = None,
              interactive: bool | None = None, verify: bool = True) -> dict[str, Any]:
    """按需跑各个向导。targets 为空时自动判断缺什么。"""
    use_tty = _interactive() if interactive is None else interactive
    wanted = set(targets or [])

    if not wanted:
        # 自动探测：缺什么配什么
        if config.source("csqaq").api_token:
            pass
        else:
            wanted.add("csqaq")
        if not config.source("buff").cookie:
            wanted.add("buff")
        if not config.llm.preset:
            wanted.add("llm")

    if not use_tty:
        print("\n  非交互环境，只输出待办清单：")
        for name in sorted(wanted):
            if name == "llm":
                print("    · LLM：config.local.yaml 写 llm.preset，.env 写对应 API Key")
            elif name == "buff":
                print("    · BUFF：.env 写 BUFF_COOKIE=<浏览器 Cookie>")
            elif name == "csqaq":
                print("    · CSQAQ：.env 写 CSQAQ_TOKEN=<ApiToken>（需绑定白名单 IP）")
        return {"interactive": False, "pending": sorted(wanted)}

    print("\n" + "=" * 62)
    print("  配置向导 —— 只为缺失的部分提问，已有配置会跳过")
    print("=" * 62)

    results: dict[str, Any] = {}
    if "csqaq" in wanted:
        if config.source("csqaq").api_token and not _confirm(
                "\n  CSQAQ Token 已配置，要重新设置吗？", default=False):
            results["csqaq"] = {"configured": True, "skipped": True}
        else:
            results["csqaq"] = csqaq_wizard(config, interactive, verify=verify)

    if "buff" in wanted:
        results["buff"] = buff_wizard(config, interactive, verify=verify)

    if "llm" in wanted:
        results["llm"] = llm_wizard(config, interactive, verify=verify)

    print("\n" + "=" * 62)
    print("  配置完成。接下来：")
    print("    python -m facet doctor          # 复查环境")
    print("    python bootstrap.py             # 启动")
    print("=" * 62)
    return results
