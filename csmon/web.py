"""Web 看板：本地只读为主 + 少量写操作（加/删监控、手动触发一轮采集）。

安全默认值：只绑 127.0.0.1。如需公网访问，请自行加反向代理与鉴权，
并注意看板会展示你的持仓意向（监控清单本身就是敏感信息）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import Config
from .mapping import MappingService
from .models import WatchRule
from .names import NameResolver
from .patterns import PatternTable
from .scheduler import Monitor
from .sources import describe_registry
from .store import Store

logger = logging.getLogger(__name__)

# 看板前端在独立模块里（纯 HTML/CSS/JS，无构建步骤也无 CDN 依赖）
from .dashboard import DASHBOARD_HTML  # noqa: E402



class WatchPayload(BaseModel):
    market_hash_name: str
    below: float | None = None
    above: float | None = None
    drop_percent: float | None = None
    rise_percent: float | None = None
    cooldown_minutes: int = Field(default=240, ge=0)
    baseline_window_hours: int = Field(default=168, ge=1)
    platforms: list[str] = Field(default_factory=list)
    note: str = ""


def create_app(config: Config, store: Store | None = None,
               monitor: Monitor | None = None) -> FastAPI:
    """构造 FastAPI 应用。store/monitor 可注入，便于测试。"""
    app = FastAPI(title="youyoumonitor", version="0.1.0",
                  description="CS 饰品多源行情监控（BUFF / 悠悠有品）")
    db = store or Store(config.database)
    mon = monitor or Monitor(config, db)

    app.state.store = db
    app.state.monitor = mon
    app.state.mapping = MappingService(db)
    # 中文名解析器与图案规则表：看板各处都要用，构造一次共享
    app.state.names = NameResolver(db)
    app.state.patterns = PatternTable.load(getattr(config, "patterns_file", None)
                                           or "patterns.yaml")

    # ── 页面 ───────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return DASHBOARD_HTML

    # ── 只读 API ───────────────────────────────────────────

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "stats": db.stats()}

    @app.get("/api/stats")
    def stats() -> dict[str, Any]:
        return db.stats()

    @app.get("/api/quotes")
    def quotes(limit: int = Query(200, ge=1, le=2000)) -> list[dict[str, Any]]:
        return db.latest_snapshot_all(limit=limit)

    @app.get("/api/alerts")
    def alerts(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return db.recent_alerts(limit=limit)

    @app.get("/api/sources")
    def sources() -> list[dict[str, Any]]:
        return db.source_health()

    @app.get("/api/source-catalog")
    def source_catalog() -> list[dict[str, Any]]:
        return describe_registry()

    @app.get("/api/history/{market_hash_name}")
    def history(market_hash_name: str, platform: str = "BUFF",
                hours: int = Query(720, ge=1, le=8760)) -> dict[str, Any]:
        """K 线数据源：返回按时间升序的报价点。"""
        rows = db.price_history(market_hash_name, platform, hours=hours)
        baseline = db.baseline_median(market_hash_name, platform, hours=hours)
        return {
            "market_hash_name": market_hash_name,
            "platform": platform,
            "baseline_median": baseline,
            "points": [{"t": r["observed_at"], "sell": r["sell_price"],
                        "count": r["sell_count"], "bid": r["bid_price"],
                        "source": r["source"]} for r in rows],
        }

    @app.get("/api/items")
    def items(keyword: str = "", limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
        return db.search_items(keyword, limit=limit) if keyword else \
            db.items_with_buff_id()[:limit] + db.items_with_youpin_id()[:limit]

    @app.get("/api/mapping-coverage")
    def mapping_coverage() -> dict[str, int]:
        return app.state.mapping.coverage()

    # ── 分析 API ───────────────────────────────────────────

    @app.get("/api/kline/{market_hash_name}")
    def kline(market_hash_name: str, platform: str = "BUFF",
              days: int = Query(90, ge=1, le=2000)) -> dict[str, Any]:
        """K 线束：日线 OHLC + 技术指标 + 文字解读。"""
        from .analytics import kline_bundle
        return kline_bundle(db, market_hash_name, platform, days=days)

    @app.get("/api/spread")
    def spread_api(min_percent: float = 0.03, min_profit: float = 1.0,
                   limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        """跨平台价差雷达。executable=True 表示对手平台有求购价、可即时成交。"""
        from .analytics import spread_radar
        rows = spread_radar(db, min_net_percent=min_percent,
                            min_net_profit=min_profit, limit=limit)
        return {
            "min_percent": min_percent, "min_profit": min_profit,
            "count": len(rows),
            "executable_count": sum(1 for r in rows if r.sell_is_bid),
            "items": [r.to_dict() for r in rows],
        }

    @app.get("/api/movers")
    def movers_api(hours: int = Query(168, ge=1, le=8760),
                   limit: int = Query(15, ge=1, le=200)) -> dict[str, Any]:
        from .analytics import movers
        return movers(db, hours=hours, limit=limit)

    @app.get("/api/liquidity")
    def liquidity(limit: int = Query(30, ge=1, le=500)) -> list[dict[str, Any]]:
        from .analytics import liquidity_board
        return liquidity_board(db, limit=limit)

    @app.get("/api/extreme")
    def extreme_status() -> dict[str, Any]:
        return mon.extreme_snapshot

    @app.get("/api/archive")
    def archive_status() -> dict[str, Any]:
        return db.archive_stats()

    # ── 租赁收益 ───────────────────────────────────────────

    @app.get("/api/rent")
    def rent_rank(limit: int = Query(50, ge=1, le=500),
                  min_liquidity: float = 0.0) -> dict[str, Any]:
        """租赁收益排行：日租金 + 价格波动 + 手续费 → 年化与结论。"""
        from . import rental as rental_mod
        from .cli import _snapshot_from_row

        rows = db.latest_rent_all(limit=2000)
        pairs = []
        for row in rows:
            snapshot = _snapshot_from_row(row)
            if snapshot is None:
                continue
            snapshot.display_name = app.state.names.resolve(row["market_hash_name"])
            yields = rental_mod.analyze_all(snapshot)
            pairs.append((snapshot, rental_mod.judge(snapshot, yields)))

        board = rental_mod.rank(pairs, limit=limit, min_liquidity=min_liquidity)
        return {"count": len(board), "items": board,
                "stats": db.rent_stats(),
                "assumptions": {
                    "rent_fee": rental_mod.DEFAULT_RENT_FEE,
                    "sell_fee": rental_mod.DEFAULT_SELL_FEE,
                    "withdraw_fee": rental_mod.DEFAULT_WITHDRAW_FEE,
                    "occupancy_fallback": rental_mod.OCCUPANCY_FALLBACK,
                }}

    @app.get("/api/rent/{market_hash_name}")
    def rent_detail(market_hash_name: str) -> dict[str, Any]:
        """单个饰品的租赁全量分析（含各周期方案与相位价格）。"""
        from . import rental as rental_mod
        from .cli import _snapshot_from_row

        row = db.latest_rent_snapshot(market_hash_name)
        if not row:
            raise HTTPException(status_code=404,
                                detail="本地没有该饰品的租赁数据，先跑 rent scan")
        snapshot = _snapshot_from_row(row)
        if snapshot is None:
            raise HTTPException(status_code=500, detail="快照解析失败")
        snapshot.display_name = app.state.names.resolve(market_hash_name)
        yields = rental_mod.analyze_all(snapshot)
        verdict = rental_mod.judge(snapshot, yields)
        return {
            "snapshot": snapshot.to_dict(),
            "display_name": snapshot.display_name,
            "scenarios": [y.to_dict() for y in yields],
            "verdict": verdict.to_dict(),
            "observed_at": row.get("observed_at"),
        }

    @app.get("/api/platform")
    def platform_info() -> dict[str, Any]:
        """运行平台画像（看板页脚展示，便于确认树莓派上的实际调优值）。"""
        from .platform import detect
        return detect().describe()

    @app.get("/api/search")
    def search(q: str = "", limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
        rows = db.search_items(q, limit=limit)
        return {
            "keyword": q, "fts": db.fts_enabled,
            "items": [{"market_hash_name": r["market_hash_name"],
                       "display_name": app.state.names.resolve(r["market_hash_name"]),
                       "buff_goods_id": r.get("buff_goods_id"),
                       "youpin_template_id": r.get("youpin_template_id")}
                      for r in rows],
        }

    # ── 关注清单 / 中文名 / 图案档位 / 建议 ────────────────

    @app.get("/api/focus")
    def focus_board() -> dict[str, Any]:
        """关注清单：带交易意图与目标价的标的（已展开到具体变体）。"""
        from .focus import build_board
        return build_board(db, patterns=app.state.patterns,
                           name_resolver=app.state.names).to_dict()

    @app.get("/api/name-coverage")
    def name_coverage() -> dict[str, Any]:
        return db.cn_name_stats()

    @app.get("/api/patterns/{market_hash_name}")
    def pattern_info(market_hash_name: str) -> dict[str, Any]:
        """图案档位判定 + 已从实盘学到的溢价种子。"""
        table = app.state.patterns
        match = table.classify(market_hash_name)
        premium = table.premium_seeds(market_hash_name)
        return {
            "market_hash_name": market_hash_name,
            "display_name": app.state.names.resolve(market_hash_name),
            "match": match.to_dict(),
            "premium_seeds": [{"seed": s, "ratio": round(r, 4)}
                              for s, r in premium.items()],
        }

    @app.get("/api/variants/{base}")
    def variants(base: str, limit: int = Query(60, ge=1, le=300)) -> dict[str, Any]:
        """把一个基础皮肤展开成全部变体，供 UI 折叠展示。"""
        from .focus import expand_focus, grouped_view
        rows = db.find_items_by_base(base, limit=limit)
        names = [r["market_hash_name"] for r in rows]
        if not names:
            # 库里没有就用解析规则现推，保证 UI 永远有内容可看
            names = expand_focus(base)
        return {"base": base, "groups": grouped_view(names, resolver=app.state.names)}

    @app.get("/api/variant-vocab")
    def variant_vocab_list(limit: int = Query(50, ge=1, le=300)) -> dict[str, Any]:
        """档位词表总览：哪些饰品有相位/档位/渐变维度。"""
        from . import variants as variants_mod
        stats = db.variant_vocab_stats()
        items: list[dict[str, Any]] = []
        names = sorted({r["market_hash_name"] for r in db.latest_snapshot_all(limit=5000)})
        for name in names:
            if len(items) >= limit:
                break
            vocab = variants_mod.load_vocab(db, name)
            if vocab.has_variants:
                items.append({**vocab.to_dict(),
                              "display_name": app.state.names.resolve(name)})
        return {"stats": stats, "items": items}

    @app.get("/api/variant-vocab/{market_hash_name}")
    def variant_vocab_one(market_hash_name: str) -> dict[str, Any]:
        """单个饰品的档位词表 + 各档位最新求购价。"""
        from . import variants as variants_mod
        vocab = variants_mod.load_vocab(db, market_hash_name)
        return {
            **vocab.to_dict(),
            "display_name": app.state.names.resolve(market_hash_name),
            "tier_quotes": db.variant_quotes(market_hash_name),
        }

    @app.get("/api/advice")
    def advice_list(limit: int = Query(30, ge=1, le=200),
                    market_hash_name: str | None = None) -> dict[str, Any]:
        from .llm import LLMConfig
        return {
            "items": db.recent_advice(limit=limit, market_hash_name=market_hash_name),
            "stats": db.advice_stats(),
            "llm": LLMConfig.from_env().describe(),
        }

    @app.get("/api/advice/context/{market_hash_name}")
    def advice_context(market_hash_name: str,
                       platform: str | None = None) -> dict[str, Any]:
        """展示送给 LLM 的原始上下文 —— 让「模型凭什么这么说」可被核查。"""
        from .advice import build_context
        ctx = build_context(db, market_hash_name, platform=platform,
                            patterns=app.state.patterns,
                            display_name=app.state.names.resolve(market_hash_name))
        return {"market_hash_name": market_hash_name,
                "digest": ctx.digest, "context": ctx.payload}

    # ── 写 API ─────────────────────────────────────────────

    @app.post("/api/watch")
    def add_watch(payload: WatchPayload) -> dict[str, Any]:
        rule = WatchRule.from_dict(payload.market_hash_name, payload.model_dump())
        db.upsert_watch(rule, source="api")
        mon.mapping.set_ids(payload.market_hash_name)
        return {"ok": True, "watch": rule.to_dict(),
                "market_hash_name": payload.market_hash_name}

    @app.delete("/api/watch/{market_hash_name}")
    def delete_watch(market_hash_name: str) -> dict[str, Any]:
        removed = db.remove_watch(market_hash_name)
        if not removed:
            raise HTTPException(status_code=404, detail="监控项不存在")
        return {"ok": True, "removed": market_hash_name}

    @app.get("/api/watch")
    def list_watch() -> list[dict[str, Any]]:
        return [{"market_hash_name": r.market_hash_name, **r.to_dict()}
                for r in db.list_watch(enabled_only=False)]

    @app.post("/api/run")
    def run_once() -> dict[str, Any]:
        """手动触发一轮采集（同步执行，便于按钮反馈）。"""
        result = mon.run_once()
        return {
            "watched": result.watched, "quotes": result.quotes,
            "alerts": len(result.alerts), "duration": result.duration,
            "notes": result.notes,
            "sources": [{"source": r.source, "requested": r.requested,
                         "succeeded": r.succeeded, "failed": r.failed,
                         "quotes": r.quotes, "errors": r.errors}
                        for r in result.reports],
        }

    return app


def serve(config: Config, store: Store | None = None) -> None:
    """启动看板（阻塞）。"""
    import uvicorn

    app = create_app(config, store)
    logger.info("看板启动：http://%s:%d", config.web.host, config.web.port)
    uvicorn.run(app, host=config.web.host, port=config.web.port, log_level="info")
