"""本地私有配置、LLM 预设、BUFF 数据源能力的测试。

这组测试保护「用户能不能少改文件就跑起来」这条主线。
"""
from __future__ import annotations

import os

from pathlib import Path

import pytest

from facet.config import (
    LOCAL_CONFIG_NAME,
    Config,
    LLMSettings,
    SourceConfig,
    _deep_merge,
    load_config,
    local_config_path,
    write_local_config,
)
from facet.llm import LLMConfig, PRESETS
from facet.ratelimit import GateRegistry
from facet.setup_wizard import (
    PRESET_KEY_ENV,
    env_file_path,
    mask,
    update_env,
)
from facet.sources import ALIASES, REGISTRY, build_sources, describe_registry
from facet.sources.buff_direct import BuffDirectAdapter
from facet.config import SOURCE_BUFF, SOURCE_BUFF_DIRECT


# ── 配置分层 ───────────────────────────────────────────────

def test_deep_merge_merges_dicts_and_replaces_lists() -> None:
    base = {"a": 1, "nested": {"x": 1, "y": 2}, "list": [1, 2, 3]}
    overlay = {"a": 9, "nested": {"y": 20, "z": 30}, "list": [4]}
    merged = _deep_merge(base, overlay)
    assert merged["a"] == 9
    assert merged["nested"] == {"x": 1, "y": 20, "z": 30}   # 逐键合并
    assert merged["list"] == [4]                            # 列表整体替换


def test_local_config_overrides_base(tmp_path: Path) -> None:
    """个人改动写在 config.local.yaml，不污染主配置。"""
    (tmp_path / "config.yaml").write_text(
        "poll_interval: 1800\n"
        "sources:\n"
        "  buff:\n"
        "    enabled: false\n"
        "    min_interval: 5.0\n",
        encoding="utf-8")
    (tmp_path / LOCAL_CONFIG_NAME).write_text(
        "poll_interval: 600\n"
        "sources:\n"
        "  buff:\n"
        "    enabled: true\n",
        encoding="utf-8")

    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.poll_interval == 600                 # 被覆盖
    assert cfg.source("buff").enabled is True       # 被覆盖
    assert cfg.source("buff").min_interval == 5.0   # 未提到的项保留
    assert cfg.local_path == tmp_path / LOCAL_CONFIG_NAME


def test_load_config_without_local_file(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("poll_interval: 900\n", encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.poll_interval == 900
    assert cfg.local_path is None


def test_write_local_config_merges_not_replaces(tmp_path: Path) -> None:
    """多次写入应累积，而不是互相覆盖 —— 向导可能分多次运行。"""
    target = tmp_path / LOCAL_CONFIG_NAME
    write_local_config({"llm": {"preset": "deepseek"}}, target)
    write_local_config({"sources": {"buff": {"enabled": True}}}, target)

    cfg = load_config(tmp_path / "config.yaml")  # 主配置不存在，用默认
    import yaml
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["llm"]["preset"] == "deepseek"
    assert data["sources"]["buff"]["enabled"] is True
    assert cfg is not None


def test_write_local_config_has_no_secret_hint(tmp_path: Path) -> None:
    """写出的文件必须有「密钥不写这里」的提示，避免用户误放。"""
    target = tmp_path / LOCAL_CONFIG_NAME
    write_local_config({"llm": {"preset": "openai"}}, target)
    text = target.read_text(encoding="utf-8")
    assert "密钥" in text
    assert "不入库" in text


def test_local_config_path_uses_base_dir(tmp_path: Path) -> None:
    assert local_config_path(tmp_path / "config.yaml") == tmp_path / LOCAL_CONFIG_NAME


# ── .env 读写 ──────────────────────────────────────────────

def test_update_env_replaces_in_place(tmp_path: Path) -> None:
    """已存在的键就地替换，不追加重复项。"""
    env = tmp_path / ".env"
    env.write_text(
        "# 注释保留\nCSQAQ_TOKEN=old\nOTHER=keep\n", encoding="utf-8")
    changed = update_env(env, {"CSQAQ_TOKEN": "new"})
    text = env.read_text(encoding="utf-8")

    assert changed == ["CSQAQ_TOKEN"]
    assert "CSQAQ_TOKEN=new" in text
    assert text.count("CSQAQ_TOKEN=") == 1        # 没有重复
    assert "# 注释保留" in text                    # 注释与其它键不动
    assert "OTHER=keep" in text


def test_update_env_appends_new_keys(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("EXISTING=1\n", encoding="utf-8")
    update_env(env, {"DEEPSEEK_API_KEY": "sk-x"})
    text = env.read_text(encoding="utf-8")
    assert "EXISTING=1" in text
    assert "DEEPSEEK_API_KEY=sk-x" in text


def test_update_env_creates_missing_file(tmp_path: Path) -> None:
    env = tmp_path / "sub" / ".env"
    update_env(env, {"A": "1"})
    assert env.exists() and "A=1" in env.read_text(encoding="utf-8")


def test_env_file_path_follows_config(tmp_path: Path) -> None:
    assert env_file_path(tmp_path / "config.yaml") == tmp_path / ".env"


# ── 改名兼容（youyoumonitor/csmon → facet）──────────────────

def test_legacy_env_prefix_migrated(monkeypatch) -> None:
    """旧的 CSMON_* 环境变量必须被自动搬到 FACET_*。

    改名不能顺带把别人已配置好的环境弄失效 —— Token 可能写在系统环境变量
    或 CI secret 里，改个前缀就要求重配是不合理的。
    """
    from facet import _bootstrap_env
    from facet.config import _migrate_legacy_env

    monkeypatch.delenv("FACET_LLM_API_KEY", raising=False)
    monkeypatch.setenv("CSMON_LLM_API_KEY", "sk-legacy")
    monkeypatch.setenv("CSMON_POLL_INTERVAL", "600")

    moved = _migrate_legacy_env()
    assert moved >= 2
    assert os.environ["FACET_LLM_API_KEY"] == "sk-legacy"
    assert os.environ["FACET_POLL_INTERVAL"] == "600"
    assert callable(_bootstrap_env)


def test_new_env_prefix_wins_over_legacy(monkeypatch) -> None:
    """新名字优先，不能被旧值覆盖。"""
    from facet.config import _migrate_legacy_env

    monkeypatch.setenv("FACET_LLM_API_KEY", "sk-new")
    monkeypatch.setenv("CSMON_LLM_API_KEY", "sk-old")
    _migrate_legacy_env()
    assert os.environ["FACET_LLM_API_KEY"] == "sk-new"


def test_legacy_llm_key_still_works(monkeypatch) -> None:
    """端到端：只设旧变量名也能正常构造 LLM 客户端。"""
    from facet import _bootstrap_env
    from facet.llm import LLMConfig

    monkeypatch.delenv("FACET_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("CSMON_LLM_API_KEY", "sk-legacy")
    _bootstrap_env()

    cfg = LLMConfig.from_settings(LLMSettings(preset="deepseek"))
    assert cfg.api_key == "sk-legacy"
    assert cfg.is_configured() is True


def test_package_name_is_facet() -> None:
    """命名一致性：包名、版本号、CLI 程序名。"""
    import facet
    from facet.cli import build_parser

    assert facet.__version__ >= "0.2.0"
    assert build_parser().prog == "facet"


def test_mask_never_reveals_short_secrets() -> None:
    assert "secret" not in mask("secret")
    assert mask("") == "(空)"
    long_secret = "sk-1234567890abcdef"
    shown = mask(long_secret)
    assert long_secret not in shown
    assert shown.startswith("sk-1")


# ── LLM 预设 ───────────────────────────────────────────────

def test_all_presets_have_base_url_and_model() -> None:
    for name, spec in PRESETS.items():
        assert spec.get("base_url"), name
        assert spec.get("model"), name


def test_every_preset_used_in_wizard_has_key_env() -> None:
    """向导菜单里出现的预设，必须能查到该写哪个密钥变量。"""
    from facet.llm import LOCAL_PRESETS
    from facet.setup_wizard import PRESET_MENU

    for name, _label in PRESET_MENU:
        assert name in PRESETS, f"菜单里的 {name} 不是有效预设"
        if name not in LOCAL_PRESETS:
            assert name in PRESET_KEY_ENV, f"{name} 缺少密钥变量名"


def test_llm_settings_from_config_takes_preset(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("llm:\n  preset: deepseek\n", encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    resolved = LLMConfig.from_settings(cfg.llm)
    assert resolved.preset == "deepseek"
    assert "deepseek.com" in resolved.resolved_base_url()
    assert resolved.resolved_model() == "deepseek-chat"


def test_env_overrides_config_preset(tmp_path: Path, monkeypatch) -> None:
    """环境变量优先于配置文件，便于临时切换。"""
    monkeypatch.setenv("FACET_LLM_PRESET", "moonshot")
    cfg = LLMConfig.from_settings(LLMSettings(preset="deepseek"))
    assert cfg.preset == "moonshot"
    assert "moonshot" in cfg.resolved_base_url()


def test_all_local_presets_need_no_key() -> None:
    """标为本机预设的，必须真的不需要密钥就能用。"""
    from facet.llm import LOCAL_PRESETS

    for name in LOCAL_PRESETS:
        cfg = LLMConfig.from_settings(LLMSettings(preset=name))
        assert cfg.is_configured() is True, name


def test_cloud_preset_without_key_not_configured() -> None:
    for key in ("FACET_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        import os
        os.environ.pop(key, None)
    cfg = LLMConfig.from_settings(LLMSettings(preset="deepseek"))
    assert cfg.is_configured() is False


def test_llm_settings_describe_is_safe() -> None:
    described = LLMSettings(preset="deepseek").describe()
    assert described["preset"] == "deepseek"
    assert "key" not in " ".join(described).lower()


# ── BUFF 数据源 ────────────────────────────────────────────

def test_buff_registered_with_legacy_alias() -> None:
    assert REGISTRY[SOURCE_BUFF] is BuffDirectAdapter
    assert REGISTRY[SOURCE_BUFF_DIRECT] is BuffDirectAdapter
    assert ALIASES[SOURCE_BUFF_DIRECT] == SOURCE_BUFF


def test_registry_description_dedupes_alias() -> None:
    """别名不该让同一个适配器在列表里出现两次。"""
    rows = describe_registry()
    names = [r["name"] for r in rows]
    assert names.count(SOURCE_BUFF) == 1
    assert SOURCE_BUFF_DIRECT not in names
    buff_row = next(r for r in rows if r["name"] == SOURCE_BUFF)
    assert SOURCE_BUFF_DIRECT in buff_row["aliases"]


def test_buff_declares_bid_capability() -> None:
    """buy_order 匿名可用已实测确认，因此要如实声明提供求购价。"""
    assert BuffDirectAdapter.provides_bid is True
    assert BuffDirectAdapter.provides_sell is True


def test_buff_anonymous_capabilities() -> None:
    adapter = BuffDirectAdapter(SourceConfig(name=SOURCE_BUFF, min_interval=0.0),
                                GateRegistry())
    caps = adapter.capabilities()
    assert caps["sell_order"] is True
    assert caps["buy_order"] is True          # 实测匿名可用
    assert caps["goods_info"] is True
    assert caps["search_by_name"] is False    # 需要 Cookie
    assert caps["paintseed_filter"] is False


def test_buff_authed_capabilities() -> None:
    adapter = BuffDirectAdapter(
        SourceConfig(name=SOURCE_BUFF, min_interval=0.0, cookie="session=abc"),
        GateRegistry())
    caps = adapter.capabilities()
    assert adapter.authed is True
    assert caps["search_by_name"] is True
    assert caps["paintseed_filter"] is True
    assert caps["bill_order"] is True


def test_buff_fetch_bid_toggle() -> None:
    on = BuffDirectAdapter(SourceConfig(name=SOURCE_BUFF, min_interval=0.0),
                           GateRegistry())
    off = BuffDirectAdapter(
        SourceConfig(name=SOURCE_BUFF, min_interval=0.0, extra={"fetch_bid": False}),
        GateRegistry())
    assert on.capabilities()["buy_order"] is True
    assert off.capabilities()["buy_order"] is False


def test_buff_search_requires_cookie() -> None:
    from facet.ratelimit import RateLimitExceeded

    adapter = BuffDirectAdapter(SourceConfig(name=SOURCE_BUFF, min_interval=0.0),
                                GateRegistry())
    with pytest.raises(RateLimitExceeded, match="BUFF_COOKIE"):
        adapter.search_goods("AK-47")


def test_buff_resolve_by_name_without_cookie_returns_none() -> None:
    adapter = BuffDirectAdapter(SourceConfig(name=SOURCE_BUFF, min_interval=0.0),
                                GateRegistry())
    assert adapter.resolve_by_name("AK-47 | Redline (Field-Tested)") is None


def test_buff_status_shape() -> None:
    adapter = BuffDirectAdapter(SourceConfig(name=SOURCE_BUFF, min_interval=0.0),
                                GateRegistry())
    status = adapter.describe_status()
    assert status["source"] == SOURCE_BUFF
    assert status["authed"] is False
    assert isinstance(status["capabilities"], dict)
    assert status["ip_blocked"] is False


def test_buff_env_cookie_applies_to_both_names(tmp_path: Path, monkeypatch) -> None:
    """BUFF_COOKIE 要同时作用于 buff 与旧名 buff_direct。"""
    monkeypatch.setenv("BUFF_COOKIE", "session=xyz")
    (tmp_path / "config.yaml").write_text(
        "sources:\n"
        "  buff:\n    enabled: true\n"
        "  buff_direct:\n    enabled: true\n",
        encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.source("buff").cookie == "session=xyz"
    assert cfg.source("buff_direct").cookie == "session=xyz"


def test_build_sources_uses_config_name_for_alias(tmp_path: Path) -> None:
    """用旧名配置时，适配器实例的名字也应是旧名（限速闸门要按名字区分）。"""
    (tmp_path / "config.yaml").write_text(
        "sources:\n"
        "  csqaq:\n    enabled: false\n"
        "  youpin_direct:\n    enabled: false\n"
        "  buff_direct:\n    enabled: true\n",
        encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    built = build_sources(cfg)
    names = [s.name for s in built]
    assert names == [SOURCE_BUFF_DIRECT], names
    assert isinstance(built[0], BuffDirectAdapter)


# ── doctor 集成 ────────────────────────────────────────────

def test_doctor_reports_buff_mode_and_llm(tmp_path: Path) -> None:
    from facet.doctor import run_checks

    (tmp_path / "config.yaml").write_text(
        "sources:\n  buff:\n    enabled: true\n",
        encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    cfg.database = str(tmp_path / "d.db")
    report = run_checks(cfg, network=False)
    names = [c.name for c in report.checks]
    assert "BUFF 模式" in names
    assert "LLM 接入" in names


def test_doctor_buff_anonymous_is_warning_not_error(tmp_path: Path) -> None:
    """匿名模式是能力受限，不是错误 —— 不该阻断启动。"""
    from facet.doctor import LEVEL_OK, LEVEL_WARN, run_checks

    cfg = Config(database=str(tmp_path / "d.db"),
                 sources={SOURCE_BUFF: SourceConfig(name=SOURCE_BUFF, enabled=True)})
    report = run_checks(cfg, network=False)
    check = next(c for c in report.checks if c.name == "BUFF 模式")
    assert check.level == LEVEL_WARN
    assert "匿名" in check.detail
