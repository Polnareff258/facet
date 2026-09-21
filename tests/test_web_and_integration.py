"""看板 API 与调度器集成测试（全部离线，用 mock 源）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from facet.config import Config, NotifyConfig, SourceConfig, WebConfig
from facet.mapping import MappingService
from facet.models import ItemRef, WatchRule
from facet.scheduler import Monitor
from facet.store import Store

fastapi_testclient = pytest.importorskip("fastapi.testclient")


def _config(tmp_path: Path, **kw) -> Config:
    cfg = Config(
        database=str(tmp_path / "web.db"),
        poll_interval=60,
        baseline_window_hours=24,
        sources={
            "mock": SourceConfig(name="mock", enabled=True, priority=1, min_interval=0.0),
        },
        notify=NotifyConfig(enabled=False, dry_run=True),
        web=WebConfig(host="127.0.0.1", port=0),
    )
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture()
def client(tmp_path: Path):
    from facet.web import create_app
    cfg = _config(tmp_path)
    app = create_app(cfg, store=Store(cfg.database))
    with fastapi_testclient.TestClient(app) as c:
        yield c, app
    app.state.store.close()


# ── 调度器 ─────────────────────────────────────────────────

def test_monitor_seeds_watchlist_and_runs(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.watchlist = [
        WatchRule(market_hash_name="AK-47 | Redline (Field-Tested)", below=200.0),
        WatchRule(market_hash_name="AWP | Asiimov (Field-Tested)"),
    ]
    with Monitor(cfg) as monitor:
        assert monitor.seed_watchlist() == 2
        # 幂等：再跑一次不应重复导入
        assert monitor.seed_watchlist() == 0

        result = monitor.run_once()
        assert result.watched == 2
        assert result.quotes == 4                       # 2 饰品 x 2 平台
        assert len(result.reports) == 1
        assert result.reports[0].source == "mock"
        assert result.reports[0].succeeded == 2
        # AK 基准价 100，阈值 200 -> 必定触发 below
        assert any(a.rule == "below" for a in result.alerts)
        assert monitor.store.quote_count() == 4


def test_monitor_empty_watchlist_gives_hint(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with Monitor(cfg) as monitor:
        result = monitor.run_once()
        assert result.watched == 0
        assert result.notes and "监控清单为空" in result.notes[0]


def test_monitor_notes_missing_platform_ids(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.watchlist = [WatchRule(market_hash_name="AK-47 | Redline (Field-Tested)")]
    with Monitor(cfg) as monitor:
        monitor.seed_watchlist()
        result = monitor.run_once()
        joined = " ".join(result.notes)
        assert "BUFF goods_id" in joined
        assert "templateId" in joined


def test_monitor_respects_source_filter(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.watchlist = [WatchRule(market_hash_name="AK-47 | Redline (Field-Tested)")]
    with Monitor(cfg) as monitor:
        monitor.seed_watchlist()
        result = monitor.run_once(only_sources={"nonexistent"})
        assert result.reports == []
        assert result.quotes == 0


# ── 看板 API ───────────────────────────────────────────────

def test_dashboard_page_served(client) -> None:
    c, _ = client
    resp = c.get("/")
    assert resp.status_code == 200
    assert "facet" in resp.text
    # 新看板是多标签页 + 原生 Canvas K 线（无 CDN 依赖）
    assert "跨平台价差雷达" in resp.text
    assert "canvas" in resp.text
    assert "cdn" not in resp.text.lower().replace("cdn.apifox", "")


def test_dashboard_has_no_external_assets(client) -> None:
    """看板必须能离线打开 —— 树莓派常部署在无外网环境，
    引入任何 CDN 都会让「数据源全挂时看历史」这个兜底失效。"""
    c, _ = client
    html = c.get("/").text
    for marker in ("http://cdn", "https://cdn", "unpkg.com", "jsdelivr",
                   "googleapis", "echarts"):
        assert marker not in html.lower(), f"看板不应依赖外部资源：{marker}"


def test_kline_endpoint(client) -> None:
    c, app = client
    app.state.store.upsert_watch(WatchRule(market_hash_name="AK", below=200.0))
    c.post("/api/run")
    body = c.get("/api/kline/AK?platform=BUFF&days=30").json()
    assert body["market_hash_name"] == "AK"
    assert body["platform"] == "BUFF"
    assert "bars" in body and "indicators" in body and "readings" in body


def test_spread_endpoint(client) -> None:
    c, app = client
    # mock 源给 BUFF/YOUPIN 两个平台，价差结构完整
    app.state.store.upsert_watch(WatchRule(market_hash_name="AK", below=200.0))
    c.post("/api/run")
    body = c.get("/api/spread?min_percent=0&min_profit=0").json()
    assert "count" in body and "executable_count" in body
    assert isinstance(body["items"], list)
    if body["items"]:
        row = body["items"][0]
        assert {"buy_platform", "sell_platform", "net_profit",
                "net_percent", "executable"} <= set(row)


def test_movers_and_liquidity_endpoints(client) -> None:
    c, app = client
    app.state.store.upsert_watch(WatchRule(market_hash_name="AK", below=200.0))
    c.post("/api/run")
    movers = c.get("/api/movers?hours=168").json()
    assert "gained" in movers and "lost" in movers
    assert isinstance(c.get("/api/liquidity").json(), list)


def test_search_endpoint(client) -> None:
    c, app = client
    app.state.mapping.set_ids("AK-47 | Redline (Field-Tested)", buff_goods_id=43076)
    body = c.get("/api/search?q=AK-47").json()
    assert body["keyword"] == "AK-47"
    assert isinstance(body["fts"], bool)
    assert any("AK-47" in i["market_hash_name"] for i in body["items"])


def test_extreme_and_archive_endpoints(client) -> None:
    c, app = client
    app.state.store.upsert_extreme("AK", "BUFF", interval_seconds=15)
    ext = c.get("/api/extreme").json()
    assert ext["task_count"] == 1
    assert ext["tasks"][0]["interval_seconds"] == 15

    arch = c.get("/api/archive").json()
    assert "archived_days" in arch


def test_platform_endpoint(client) -> None:
    c, _ = client
    body = c.get("/api/platform").json()
    assert body["os"] in ("windows", "linux", "darwin")
    assert "max_fetch_workers" in body
    assert isinstance(body["notes"], list)


def test_health_and_stats(client) -> None:
    c, _ = client
    assert c.get("/api/health").json()["ok"] is True
    stats = c.get("/api/stats").json()
    assert set(stats) >= {"items", "quotes", "alerts", "watching", "database"}


def test_add_list_delete_watch(client) -> None:
    c, app = client
    payload = {"market_hash_name": "AK-47 | Redline (Field-Tested)",
               "below": 100.0, "platforms": ["BUFF"]}
    assert c.post("/api/watch", json=payload).json()["ok"] is True

    rows = c.get("/api/watch").json()
    assert len(rows) == 1
    assert rows[0]["market_hash_name"] == "AK-47 | Redline (Field-Tested)"
    assert rows[0]["below"] == 100.0

    assert c.delete("/api/watch/AK-47 | Redline (Field-Tested)").json()["ok"] is True
    assert c.get("/api/watch").json() == []
    assert c.delete("/api/watch/nope").status_code == 404


def test_manual_run_endpoint(client) -> None:
    c, app = client
    app.state.store.upsert_watch(WatchRule(market_hash_name="AK", below=200.0))
    body = c.post("/api/run").json()
    assert body["watched"] == 1
    assert body["quotes"] == 2
    assert body["sources"][0]["source"] == "mock"


def test_quotes_and_alerts_endpoints(client) -> None:
    c, app = client
    app.state.store.upsert_watch(WatchRule(market_hash_name="AK", below=200.0))
    c.post("/api/run")

    quotes = c.get("/api/quotes").json()
    assert len(quotes) == 2
    assert {q["platform"] for q in quotes} == {"BUFF", "YOUPIN"}

    alerts = c.get("/api/alerts").json()
    assert alerts and alerts[0]["rule"] == "below"


def test_history_endpoint(client) -> None:
    c, app = client
    app.state.store.upsert_watch(WatchRule(market_hash_name="AK", below=200.0))
    c.post("/api/run")
    body = c.get("/api/history/AK?platform=BUFF&hours=24").json()
    assert body["market_hash_name"] == "AK"
    assert len(body["points"]) >= 1
    assert "baseline_median" in body


def test_sources_and_catalog_endpoints(client) -> None:
    c, app = client
    # 先添加监控项再跑，否则清单为空、不会调用任何源，也就没有健康记录
    app.state.store.upsert_watch(WatchRule(market_hash_name="AK", below=200.0))
    c.post("/api/run")
    health = c.get("/api/sources").json()
    assert health and health[0]["source"] == "mock"

    catalog = c.get("/api/source-catalog").json()
    names = {row["name"] for row in catalog}
    # BUFF 源的规范名是 buff；buff_direct 是旧名，只在 aliases 里出现
    assert {"csqaq", "steamdt", "buff", "youpin_direct", "mock"} <= names
    buff_row = next(r for r in catalog if r["name"] == "buff")
    assert "buff_direct" in buff_row["aliases"]


def test_mapping_coverage_endpoint(client) -> None:
    c, app = client
    MappingService(app.state.store).set_ids("AK", buff_goods_id=43076,
                                            youpin_template_id=822)
    cov = c.get("/api/mapping-coverage").json()
    assert cov["items"] == 1
    assert cov["with_buff_goods_id"] == 1
    assert cov["with_youpin_template_id"] == 1


# ── 映射服务 ───────────────────────────────────────────────

def test_seed_from_youpin_mapping(tmp_path: Path) -> None:
    db = Store(tmp_path / "m.db")
    try:
        payload = ('{"success": true, "data": {'
                   '"\'The Doctor\' Romanov | Sabre": '
                   '{"steam_hash_name": "\'The Doctor\' Romanov | Sabre", "yyyp_id": 822},'
                   '"AK-47 | Redline (Field-Tested)": '
                   '{"steam_hash_name": "AK-47 | Redline (Field-Tested)", "yyyp_id": 490}'
                   '}}')
        path = tmp_path / "map.json"
        path.write_text(payload, encoding="utf-8")

        service = MappingService(db)
        assert service.seed_from_youpin_mapping(path) == 2
        row = db.get_item("'The Doctor' Romanov | Sabre")
        assert row["youpin_template_id"] == 822
    finally:
        db.close()


def test_to_item_refs_returns_placeholder_for_unknown(tmp_path: Path) -> None:
    db = Store(tmp_path / "m2.db")
    try:
        service = MappingService(db)
        service.set_ids("known", buff_goods_id=1)
        refs = service.to_item_refs(["known", "unknown"])
        assert refs[0].buff_goods_id == 1
        assert refs[1].buff_goods_id is None    # 未知项也返回，由适配器决定跳过
    finally:
        db.close()


def test_notify_dry_run_does_not_send(tmp_path: Path) -> None:
    from facet.models import AlertEvent
    from facet.notify import Notifier

    cfg = _config(tmp_path)
    cfg.notify = NotifyConfig(enabled=True, dry_run=True, channels=[
        {"type": "webhook", "url": "http://127.0.0.1:1/never"}])
    notifier = Notifier(cfg.notify)
    try:
        results = notifier.send([AlertEvent(market_hash_name="X", platform="BUFF",
                                            rule="below", message="m")])
        assert results[0][1] == "dry_run"
    finally:
        notifier.close()


def test_quiet_hours_logic() -> None:
    from datetime import datetime

    from facet.notify import Notifier

    notifier = Notifier(NotifyConfig(enabled=True, dry_run=True, quiet_hours=(23, 8)))
    try:
        # 静默判断按本地时区进行，因此这里显式构造本地时区的时间
        local_tz = datetime.now().astimezone().tzinfo
        assert notifier._in_quiet_hours(datetime(2026, 1, 1, 2, 0, tzinfo=local_tz)) is True
        assert notifier._in_quiet_hours(datetime(2026, 1, 1, 23, 30, tzinfo=local_tz)) is True
        assert notifier._in_quiet_hours(datetime(2026, 1, 1, 12, 0, tzinfo=local_tz)) is False
        assert notifier._in_quiet_hours(datetime(2026, 1, 1, 8, 0, tzinfo=local_tz)) is False
    finally:
        notifier.close()


def test_notify_disabled_skips_send() -> None:
    from facet.models import AlertEvent
    from facet.notify import Notifier

    notifier = Notifier(NotifyConfig(enabled=False))
    try:
        results = notifier.send([AlertEvent(market_hash_name="X", platform="BUFF",
                                            rule="below", message="m")])
        assert results[0][1] == "disabled"
    finally:
        notifier.close()
