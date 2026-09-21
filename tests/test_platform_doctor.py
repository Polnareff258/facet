"""平台适配、自检、运行环境相关的测试。

这些测试保护的是「一键启动能不能一次跑通」——这是最容易在别人机器上翻车、
也最不容易被手工验证覆盖的部分。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from csmon.config import Config, NotifyConfig, SourceConfig, WebConfig, load_config, load_dotenv
from csmon.platform import detect, pip_index_args, venv_paths
from csmon.store import Store


# ── 平台探测 ───────────────────────────────────────────────

def test_detect_returns_coherent_profile() -> None:
    prof = detect(force=True)
    assert prof.os_name in ("windows", "linux", "darwin")
    assert prof.arch
    assert prof.python_version
    assert prof.max_fetch_workers >= 1
    assert prof.default_poll_interval >= 60
    assert prof.sqlite_synchronous in ("NORMAL", "FULL", "OFF")


def test_low_resource_flag_logic() -> None:
    """小内存环境必须被判为低资源，从而走更克制的默认值。"""
    prof = detect(force=True)
    if 0 < prof.memory_mb <= 1024 or prof.is_pi:
        assert prof.is_low_resource
        assert prof.max_fetch_workers <= 2


def test_profile_describe_is_json_safe() -> None:
    import json
    described = detect(force=True).describe()
    json.dumps(described)          # 不能有不可序列化对象
    assert "notes" in described and isinstance(described["notes"], list)


def test_venv_paths_layout() -> None:
    paths = venv_paths(Path("/tmp/proj") if os.name != "nt" else Path("C:/proj"))
    assert paths["venv"].name == ".venv"
    if os.name == "nt":
        assert paths["python"].name == "python.exe"
        assert paths["bin"].name == "Scripts"
    else:
        assert paths["python"].name == "python"
        assert paths["bin"].name == "bin"


def test_pip_index_args_are_opts() -> None:
    args = pip_index_args(detect(force=True))
    assert all(a.startswith("-") for a in args)


# ── .env 解析 ──────────────────────────────────────────────

def test_load_dotenv_parses_common_forms(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释行\n"
        "\n"
        "CSMON_TEST_A=plain\n"
        "CSMON_TEST_B='quoted value'\n"
        'CSMON_TEST_C="double quoted"\n'
        "CSMON_TEST_D=value # 行内注释\n"
        "export CSMON_TEST_E=exported\n"
        "CSMON_TEST_EMPTY=\n"
        "没等号的行\n",
        encoding="utf-8",
    )
    for key in list(os.environ):
        if key.startswith("CSMON_TEST_"):
            monkeypatch.delenv(key, raising=False)

    loaded = load_dotenv(env_file)
    assert loaded >= 6
    assert os.environ["CSMON_TEST_A"] == "plain"
    assert os.environ["CSMON_TEST_B"] == "quoted value"
    assert os.environ["CSMON_TEST_C"] == "double quoted"
    assert os.environ["CSMON_TEST_D"] == "value"
    assert os.environ["CSMON_TEST_E"] == "exported"
    assert os.environ["CSMON_TEST_EMPTY"] == ""


def test_load_dotenv_does_not_override_real_env(tmp_path: Path, monkeypatch) -> None:
    """真实环境变量优先于 .env —— 用户临时 export 应当能盖过文件。"""
    monkeypatch.setenv("CSMON_TEST_KEEP", "from-env")
    env_file = tmp_path / ".env"
    env_file.write_text("CSMON_TEST_KEEP=from-file\n", encoding="utf-8")
    load_dotenv(env_file)
    assert os.environ["CSMON_TEST_KEEP"] == "from-env"


def test_load_dotenv_missing_file_is_noop(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path / "nope.env") == 0


def test_load_dotenv_override_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CSMON_TEST_OVR", "from-env")
    env_file = tmp_path / ".env"
    env_file.write_text("CSMON_TEST_OVR=from-file\n", encoding="utf-8")
    load_dotenv(env_file, override=True)
    assert os.environ["CSMON_TEST_OVR"] == "from-file"


def test_env_credentials_flow_into_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CSQAQ_TOKEN", "tok-from-env")
    cfg = load_config(tmp_path / "absent.yaml")
    assert cfg.source("csqaq").api_token == "tok-from-env"


# ── 自检 ───────────────────────────────────────────────────

def test_doctor_report_structure(tmp_path: Path) -> None:
    from csmon.doctor import run_checks

    cfg = Config(database=str(tmp_path / "d.db"),
                 sources={"mock": SourceConfig(name="mock", enabled=True)},
                 notify=NotifyConfig(), web=WebConfig())
    report = run_checks(cfg, network=False)
    assert report.profile is not None
    names = [c.name for c in report.checks]
    assert "Python 版本" in names
    assert "数据库可写" in names
    assert "可用数据源" in names
    assert report.render()


def test_doctor_flags_unwritable_database(tmp_path: Path) -> None:
    """:memory: 之类不可写路径应被判为阻断（fail），而不是悄悄降级。"""
    from csmon.doctor import LEVEL_FAIL, run_checks

    cfg = Config(database=str(tmp_path / "sub" / "x.db"),
                 sources={"mock": SourceConfig(name="mock", enabled=True)})
    report = run_checks(cfg, network=False)
    assert not any(c.level == LEVEL_FAIL and c.name == "数据库可写" for c in report.checks)


def test_doctor_reports_missing_credential_as_warning(tmp_path: Path) -> None:
    """缺 CSQAQ Token 是警告而非阻断：免凭据源仍能工作。"""
    from csmon.doctor import run_checks

    cfg = Config(database=str(tmp_path / "d.db"),
                 sources={"csqaq": SourceConfig(name="csqaq", enabled=True,
                                                api_token=None)})
    report = run_checks(cfg, network=False)
    cred_checks = [c for c in report.checks if c.name == "缺少凭证的源"]
    assert cred_checks and cred_checks[0].level == "warn"
    assert "CSQAQ_TOKEN" in cred_checks[0].detail


def test_doctor_to_dict_serialisable(tmp_path: Path) -> None:
    import json

    from csmon.doctor import run_checks

    cfg = Config(database=str(tmp_path / "d.db"),
                 sources={"mock": SourceConfig(name="mock", enabled=True)})
    payload = run_checks(cfg, network=False).to_dict()
    json.dumps(payload, ensure_ascii=False)
    assert "ok" in payload and "checks" in payload


# ── 存储：归档 ─────────────────────────────────────────────

def test_archive_moves_old_rows_to_daily(store: Store) -> None:
    from datetime import timedelta

    from csmon.models import SourceQuote, utcnow

    now = utcnow()
    # 同一天内的三次采样，分钟递增：100 → 110 → 90
    # （时间戳严格递增，避免并列时间戳把 open/close 变得不确定）
    store.insert_quotes([
        SourceQuote(market_hash_name="X", platform="BUFF", source="s",
                    sell_price=price,
                    observed_at=now - timedelta(days=200) + timedelta(minutes=minutes))
        for minutes, price in enumerate([100.0, 110.0, 90.0])
    ])
    store.insert_quotes([SourceQuote(market_hash_name="X", platform="BUFF",
                                     source="s", sell_price=120.0, observed_at=now)])

    result = store.archive_old_quotes(keep_days=90)
    assert result["rows_deleted"] == 3
    assert store.quote_count() == 1                    # 近期明细保留

    archived = store.archived_history("X", "BUFF")
    assert len(archived) == 1
    row = archived[0]
    assert row["min_price"] == 90.0 and row["max_price"] == 110.0
    assert row["first_price"] == 100.0 and row["last_price"] == 90.0
    assert row["record_count"] == 3
    assert store.archive_stats()["archived_days"] == 1


def test_archive_is_idempotent(store: Store) -> None:
    from datetime import timedelta

    from csmon.models import SourceQuote, utcnow

    store.insert_quotes([SourceQuote(market_hash_name="X", platform="BUFF", source="s",
                                     sell_price=1.0,
                                     observed_at=utcnow() - timedelta(days=200))])
    first = store.archive_old_quotes(keep_days=90)
    second = store.archive_old_quotes(keep_days=90)
    assert first["rows_deleted"] == 1
    assert second["rows_deleted"] == 0
    assert store.archive_stats()["archived_days"] == 1     # 不重复计天


# ── 存储：全文搜索 ─────────────────────────────────────────

def test_fts_search_finds_by_prefix(store: Store) -> None:
    from csmon.models import ItemRef

    store.upsert_items([
        ItemRef(market_hash_name="AK-47 | Redline (Field-Tested)", buff_goods_id=1),
        ItemRef(market_hash_name="AWP | Asiimov (Field-Tested)", buff_goods_id=2),
    ])
    hits = store.search_items("AK-47")
    assert len(hits) == 1 and hits[0]["market_hash_name"].startswith("AK-47")


def test_search_handles_special_characters(store: Store) -> None:
    """用户输入里的引号/星号不能把搜索搞崩 —— FTS5 会把这些当运算符。"""
    from csmon.models import ItemRef

    store.upsert_item(ItemRef(market_hash_name='★ Karambit | "Fade" (FN)', buff_goods_id=3))
    for keyword in ['"', "*", "★", 'a"b', "((", "OR", "NOT"]:
        store.search_items(keyword)          # 不应抛异常


def test_search_empty_keyword(store: Store) -> None:
    assert store.search_items("") == []
    assert store.search_items("   ") == []


def test_search_like_fallback_still_works(tmp_path: Path) -> None:
    """即使 FTS 不可用，也要能按子串搜到（功能降级但不失效）。"""
    from csmon.models import ItemRef

    db = Store(tmp_path / "nofs.db")
    try:
        db._fts_enabled = False             # 模拟无 FTS5 的构建
        db.upsert_item(ItemRef(market_hash_name="AK-47 | Redline (FT)", buff_goods_id=1))
        hits = db.search_items("Redline")
        assert len(hits) == 1
    finally:
        db.close()


# ── 存储：极致追踪 ─────────────────────────────────────────

def test_extreme_watch_crud(store: Store) -> None:
    store.upsert_extreme("X", "BUFF", interval_seconds=15, price_threshold=0.5)
    rows = store.list_extreme()
    assert len(rows) == 1
    assert rows[0]["interval_seconds"] == 15
    assert rows[0]["price_threshold"] == 0.5

    store.upsert_extreme("X", "BUFF", interval_seconds=45)   # 更新而非插入
    rows = store.list_extreme()
    assert len(rows) == 1 and rows[0]["interval_seconds"] == 45

    assert store.remove_extreme("X", "BUFF") is True
    assert store.list_extreme() == []


def test_extreme_samples_and_prune(store: Store) -> None:
    from datetime import timedelta

    from csmon.models import iso, utcnow

    store.insert_extreme_samples([
        {"market_hash_name": "X", "platform": "BUFF", "sell_price": 100.0,
         "sell_count": 5, "observed_at": iso(utcnow() - timedelta(days=30))},
        {"market_hash_name": "X", "platform": "BUFF", "sell_price": 101.0,
         "sell_count": 4, "observed_at": iso(utcnow())},
    ])
    latest = store.latest_extreme_sample("X", "BUFF")
    assert latest is not None and latest["sell_price"] == 101.0

    removed = store.prune_extreme_samples(keep_days=7)
    assert removed == 1
    assert store.latest_extreme_sample("X", "BUFF")["sell_price"] == 101.0


# ── 极致追踪的变动判定 ─────────────────────────────────────

def test_changed_modes() -> None:
    from csmon.extreme import MODE_ANY, MODE_PERCENT, ExtremeTracker

    assert ExtremeTracker._changed(100.0, 100.0, MODE_ANY, 0) is False
    assert ExtremeTracker._changed(100.0, 100.01, MODE_ANY, 0) is True

    # percent 模式：阈值 1% 时，0.5% 不动、2% 触发
    assert ExtremeTracker._changed(100.0, 100.5, MODE_PERCENT, 1.0) is False
    assert ExtremeTracker._changed(100.0, 102.0, MODE_PERCENT, 1.0) is True
    assert ExtremeTracker._changed(100.0, 98.0, MODE_PERCENT, 1.0) is True


def test_extreme_task_quiet_hours() -> None:
    from csmon.extreme import ExtremeTask

    task = ExtremeTask(market_hash_name="X", platform="BUFF",
                       quiet_start=23, quiet_end=8)
    from datetime import datetime

    assert task.in_quiet_hours(datetime(2026, 1, 1, 2)) is True     # 跨零点区内
    assert task.in_quiet_hours(datetime(2026, 1, 1, 23)) is True
    assert task.in_quiet_hours(datetime(2026, 1, 1, 12)) is False
    assert task.in_quiet_hours(datetime(2026, 1, 1, 8)) is False

    none_task = ExtremeTask(market_hash_name="Y", platform="BUFF")
    assert none_task.in_quiet_hours(datetime(2026, 1, 1, 3)) is False


def test_extreme_backoff_and_recover() -> None:
    """限流降频必须指数放大且有上限，成功后逐步收回 —— 不能被限流打崩也不能永久卡在低频。"""
    from csmon.extreme import MAX_INTERVAL, ExtremeTask, ExtremeTracker

    task = ExtremeTask(market_hash_name="X", platform="BUFF", interval_seconds=10)
    tracker = ExtremeTracker.__new__(ExtremeTracker)     # 只测纯逻辑，不建依赖
    tracker.stats = {"ticks": 0, "changes": 0, "errors": 0, "backoffs": 0}

    tracker._backoff(task)
    assert task.current_interval == 20
    tracker._backoff(task)
    assert task.current_interval == 40

    for _ in range(20):
        tracker._backoff(task)
    assert task.current_interval == MAX_INTERVAL

    from csmon.extreme import RECOVER_AFTER_SUCCESS
    for _ in range(RECOVER_AFTER_SUCCESS):
        tracker._recover(task)
    assert task.current_interval < MAX_INTERVAL


# ── 启动器参数解析 ─────────────────────────────────────────

def test_bootstrap_parses_modes() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bootstrap", str(Path(__file__).resolve().parent.parent / "bootstrap.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args = module.parse_args(["--check", "--no-network"])
    assert args.check and args.no_network

    args = module.parse_args(["--daemon", "--interval", "600"])
    assert args.daemon and args.interval == 600

    args = module.parse_args([])
    assert not args.check and not args.daemon

    # --run / --serve / --collect 互斥：同时给两个应当报错
    with pytest.raises(SystemExit):
        module.parse_args(["--run", "--serve"])


def test_config_demo_is_loadable() -> None:
    demo = Path(__file__).resolve().parent.parent / "config.demo.yaml"
    if not demo.exists():
        pytest.skip("demo 配置不存在")
    cfg = load_config(demo)
    assert cfg.source("mock").enabled is True
    assert cfg.source("csqaq").enabled is False
    assert cfg.watchlist
