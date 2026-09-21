"""LLM 交易建议：把数据组织成上下文 → 调用模型 → 落库。

三条设计原则：

1. **数据先于观点。** 送进模型的是结构化的行情事实（指标数值、在售量、
   跨平台价差、磨损档分布、你自己的目标价），不是让模型凭空发挥。
   模型看不到本工具没有的数据 —— 这一点在提示词里明确写了。

2. **强制给出反面证据。** 输出 schema 里 `risks` 与 `counter_evidence` 是必填。
   只输出看多理由的建议没有决策价值，还会强化确认偏误。

3. **可追溯。** 每条建议连同 `context_digest`（输入数据的指纹）一起落库。
   价格变了之后可以据此判断「这条建议是不是已经过期」。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from . import analytics, indicators
from .llm import LLMClient, LLMError, LLMConfig
from .models import utcnow
from .patterns import PatternTable
from .skins import WEAR_EN_TO_CN, parse_name
from .store import Store

logger = logging.getLogger(__name__)

ACTIONS = ("buy", "hold", "sell", "avoid", "watch")
ACTION_CN = {"buy": "买入", "hold": "持有", "sell": "卖出",
             "avoid": "回避", "watch": "观察"}

SYSTEM_PROMPT = """\
你是一名 CS2 饰品市场的资深交易员，负责为一个个人投资者提供第二意见。

你必须遵守的边界：
- 你只能依据用户提供的这组数据判断。数据里没有的东西（赛事日程、版本更新、
  贴纸价值、大商动向、平台活动）你不知道，**不要编造**。若某项信息对结论关键
  但你手头没有，就把它写进 risks 里，而不是假设它。
- CS2 饰品市场流动性差、单件差异大，价格可以长期不回归。你的结论要按
  「概率 + 条件」表达，不要给出确定性预测。
- 你必须主动寻找与你的结论相反的证据，写进 counter_evidence。只列看多理由
  的回答是不合格的。
- 不要给出「保证收益」「稳赚」这类表述。

关于租赁（如果数据里有 rental 段）：
- CS2 饰品除了低买高卖，还可以**出租收租**。有租赁数据的标的其收益来自
  「租金 + 价格变动 − 手续费」，只盯价差会看漏这条路。
- 注意区分「理论年化」（日租金×365÷价格，满租上限）与「平台年化」。
  出租率是推算值，平台算法未公开，不要把它当成精确事实。
- 长租日租金可能**低于**短租，但空置率更低、折算年化反而更高 ——
  不要只看日租金高低就下结论。
- 租赁标的最大的风险是「租金赚了几个月，价格跌掉一半」，请评估这个风险。

输出要求：只输出一个 JSON 对象，字段如下（全部必填）：
{
  "action": "buy|hold|sell|avoid|watch",
  "confidence": 0.0-1.0 之间的数（对你自己这个判断的把握，不是收益率）,
  "target_buy": 建议买入价上限（数字，人民币）或 null,
  "target_sell": 建议卖出价下限（数字）或 null,
  "stop_loss": 建议止损价位（数字）或 null,
  "horizon_days": 建议的持有周期天数（整数）或 null,
  "reasoning": "你的判断依据，200 字以内，必须引用数据里的具体数字",
  "counter_evidence": "与你的结论相反的证据，100 字以内，不能为空",
  "risks": "这笔交易的主要风险，150 字以内，不能为空",
  "data_gaps": ["为做出更好判断，你还缺少哪些数据"]
}
"""


@dataclass
class AdviceContext:
    """送给模型的结构化上下文。"""

    market_hash_name: str
    payload: dict[str, Any]
    digest: str

    def to_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, indent=2)


def _digest(payload: dict[str, Any]) -> str:
    """输入数据的指纹：用于判断建议是否已过期。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_context(store: Store, market_hash_name: str,
                  platform: str | None = None,
                  patterns: PatternTable | None = None,
                  display_name: str | None = None,
                  hours: int = 720) -> AdviceContext:
    """收集一个饰品的全部相关数据，构成模型输入。"""
    variant = parse_name(market_hash_name)
    latest = store.latest_by_platform(market_hash_name)

    # 平台选择：显式指定优先，否则取在售价最低的（对买方最有利）
    chosen = platform
    if chosen is None and latest:
        priced = {p: q for p, q in latest.items() if q.get("sell_price")}
        if priced:
            chosen = min(priced, key=lambda p: priced[p]["sell_price"] or 1e18)
    quote = latest.get(chosen or "") or {}

    # 历史序列 → 指标
    history_rows = store.price_history(market_hash_name, chosen or "BUFF",
                                       hours=hours, limit=5000)
    closes = [float(r["sell_price"]) for r in history_rows
              if r.get("sell_price")]
    snap = indicators.snapshot(closes) if closes else None
    bars = analytics.daily_ohlc(store, market_hash_name, chosen or "BUFF", days=180)

    # 该饰品的关注设置（如果有）
    focus = store.get_focus(market_hash_name) or {}

    # 图案档位
    pattern_info = None
    if patterns is not None:
        match = patterns.classify(market_hash_name)
        premium = patterns.premium_seeds(market_hash_name)
        pattern_info = {
            **match.to_dict(),
            "premium_seeds_observed": [{"seed": s, "ratio": round(r, 2)}
                                       for s, r in list(premium.items())[:10]],
        }

    # 磨损档分布：同基础名的各磨损档现价，用于判断「这个档位贵不贵」
    wear_ladder: list[dict[str, Any]] = []
    if variant.base:
        siblings = store.find_items_by_base(variant.base)
        for row in siblings:
            sib = parse_name(row["market_hash_name"])
            # LIKE 粗筛会误命中前缀重叠的名字，这里用解析结果精确复核
            if sib.base != variant.base:
                continue
            sib_quote = store.latest_quote(row["market_hash_name"], chosen or "BUFF")
            if sib_quote and sib_quote.get("sell_price"):
                wear_ladder.append({
                    "wear": sib.wear, "wear_cn": sib.wear_cn,
                    "quality": sib.quality.value,
                    "market_hash_name": row["market_hash_name"],
                    "sell_price": sib_quote["sell_price"],
                })
        wear_ladder.sort(key=lambda r: (r["quality"], r["wear"] or ""))

    # 跨平台价差
    spreads = []
    try:
        for spread in analytics.spread_radar(store, min_net_percent=-1, min_net_profit=-1e9,
                                             limit=500):
            if spread.market_hash_name == market_hash_name:
                spreads.append(spread.to_dict())
    except Exception:  # noqa: BLE001
        pass

    # 近期告警
    alerts = [a for a in store.recent_alerts(limit=100)
              if a["market_hash_name"] == market_hash_name][:5]

    # 极致追踪采样（如果有）
    extreme = store.latest_extreme_sample(market_hash_name, chosen or "BUFF")

    # 租赁收益（如果有）—— 这是「持有型」标的的核心收益来源，
    # 没有它模型看不到「不卖也能赚钱」这条路径，会把租赁标的误判成纯投机
    rental_block = None
    try:
        from . import rental as rental_mod

        rent_row = store.latest_rent_snapshot(market_hash_name)
        if rent_row:
            from .cli import _snapshot_from_row
            rent_snapshot = _snapshot_from_row(rent_row)
            if rent_snapshot:
                yields = rental_mod.analyze_all(rent_snapshot)
                verdict = rental_mod.judge(rent_snapshot, yields)
                rental_block = {
                    "short_daily_rent": rent_snapshot.short_daily_rent,
                    "long_daily_rent": rent_snapshot.long_daily_rent,
                    "short_annual_pct": rent_snapshot.short_annual_pct,
                    "long_annual_pct": rent_snapshot.long_annual_pct,
                    "lease_listings": rent_snapshot.lease_listings,
                    "transfer_price": rent_snapshot.transfer_price,
                    "observed_at": rent_row.get("observed_at"),
                    "scenarios": [y.to_dict() for y in yields],
                    "verdict": verdict.to_dict(),
                }
    except Exception:  # noqa: BLE001 — 租赁数据缺失不能拖垮建议生成
        logger.exception("租赁上下文构建失败（忽略）")

    payload: dict[str, Any] = {
        "item": {
            "market_hash_name": market_hash_name,
            "display_name": display_name or market_hash_name,
            "base": variant.base,
            "weapon": variant.weapon,
            "finish": variant.finish,
            "is_star": variant.is_star,
            "quality": variant.quality.value,
            "quality_cn": variant.quality.cn,
            "wear": variant.wear,
            "wear_cn": variant.wear_cn,
            "wear_rank": variant.wear_rank,
            "variant_rich": variant.is_variant_rich,
        },
        "current": {
            "platform": chosen,
            "sell_price": quote.get("sell_price"),
            "bid_price": quote.get("bid_price"),
            "sell_count": quote.get("sell_count"),
            "bid_count": quote.get("bid_count"),
            "observed_at": quote.get("observed_at"),
            "source": quote.get("source"),
            "all_platforms": {
                p: {"sell_price": q.get("sell_price"), "sell_count": q.get("sell_count"),
                    "bid_price": q.get("bid_price")}
                for p, q in latest.items()
            },
        },
        "indicators": snap.to_dict() if snap else None,
        "indicator_readings": indicators.interpret(snap) if snap else [],
        "recent_daily_bars": [
            {"date": b.date, "open": b.open, "high": b.high,
             "low": b.low, "close": b.close, "samples": b.count}
            for b in bars[-30:]
        ],
        "wear_ladder": wear_ladder,
        "cross_platform_spreads": spreads,
        "pattern": pattern_info,
        "my_focus": {
            "intent": focus.get("intent"),
            "target_price": focus.get("target_price"),
            "max_budget": focus.get("max_budget"),
            "quantity": focus.get("quantity"),
            "note": focus.get("note"),
        } if focus else None,
        "recent_alerts": [
            {"rule": a["rule"], "severity": a["severity"], "message": a["message"],
             "created_at": a["created_at"]}
            for a in alerts
        ],
        "latest_extreme_sample": extreme,
        "rental": rental_block,
        "data_window_hours": hours,
    }

    return AdviceContext(market_hash_name=market_hash_name,
                         payload=payload, digest=_digest(payload))


def render_user_prompt(context: AdviceContext) -> str:
    """把上下文转成给模型的问题。"""
    intent = (context.payload.get("my_focus") or {}).get("intent")
    ask = {
        "buy": "我准备买入这个饰品，请判断现在是否是好时机。",
        "sell": "我持有这个饰品、准备卖出，请判断现在是否该卖。",
        "watch": "我在观察这个饰品，请判断是否值得关注。",
    }.get(intent, "请分析这个饰品当前是否值得买入或卖出。")

    return (
        f"{ask}\n\n"
        "以下是这个饰品的全部可用数据（JSON）。数据里没有的，就是我没有采集到，"
        "请不要假设它存在：\n\n"
        f"```json\n{context.to_json()}\n```\n\n"
        "请按系统提示要求的 JSON schema 输出。"
    )


# ── 生成 ───────────────────────────────────────────────────

@dataclass
class AdviceResult:
    market_hash_name: str
    action: str
    confidence: float | None
    target_buy: float | None
    target_sell: float | None
    stop_loss: float | None
    horizon_days: int | None
    reasoning: str
    counter_evidence: str
    risks: str
    data_gaps: list[str]
    provider: str
    model: str
    context_digest: str
    advice_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_hash_name": self.market_hash_name,
            "action": self.action,
            "action_cn": ACTION_CN.get(self.action, self.action),
            "confidence": self.confidence,
            "target_buy": self.target_buy,
            "target_sell": self.target_sell,
            "stop_loss": self.stop_loss,
            "horizon_days": self.horizon_days,
            "reasoning": self.reasoning,
            "counter_evidence": self.counter_evidence,
            "risks": self.risks,
            "data_gaps": self.data_gaps,
            "provider": self.provider,
            "model": self.model,
            "context_digest": self.context_digest,
            "advice_id": self.advice_id,
        }


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _as_int(value: Any) -> int | None:
    out = _as_float(value)
    return int(out) if out is not None else None


def request_advice(store: Store, market_hash_name: str,
                   client: LLMClient | None = None,
                   platform: str | None = None,
                   patterns: PatternTable | None = None,
                   display_name: str | None = None,
                   persist: bool = True) -> AdviceResult:
    """生成一条建议并（可选）落库。"""
    llm = client or LLMClient()
    context = build_context(store, market_hash_name, platform=platform,
                            patterns=patterns, display_name=display_name)
    prompt = render_user_prompt(context)

    payload = llm.complete_json(SYSTEM_PROMPT, prompt)

    action = str(payload.get("action") or "watch").strip().lower()
    if action not in ACTIONS:
        logger.warning("[advice] 模型返回未知动作 %r，归为 watch", action)
        action = "watch"

    confidence = _as_float(payload.get("confidence"))
    if confidence is not None and confidence > 1:
        confidence = confidence / 100 if confidence <= 100 else 1.0   # 兼容百分比写法

    gaps = payload.get("data_gaps")
    if isinstance(gaps, str):
        gaps = [gaps]
    elif not isinstance(gaps, list):
        gaps = []

    result = AdviceResult(
        market_hash_name=market_hash_name,
        action=action,
        confidence=confidence,
        target_buy=_as_float(payload.get("target_buy")),
        target_sell=_as_float(payload.get("target_sell")),
        stop_loss=_as_float(payload.get("stop_loss")),
        horizon_days=_as_int(payload.get("horizon_days")),
        reasoning=str(payload.get("reasoning") or "").strip(),
        counter_evidence=str(payload.get("counter_evidence") or "").strip(),
        risks=str(payload.get("risks") or "").strip(),
        data_gaps=[str(g) for g in gaps][:8],
        provider=llm.config.provider,
        model=llm.config.resolved_model(),
        context_digest=context.digest,
    )

    if persist:
        result.advice_id = store.insert_advice({
            "market_hash_name": market_hash_name,
            "platform": context.payload["current"].get("platform"),
            "provider": result.provider, "model": result.model,
            "action": result.action, "confidence": result.confidence,
            "target_buy": result.target_buy, "target_sell": result.target_sell,
            "stop_loss": result.stop_loss, "horizon_days": result.horizon_days,
            "reasoning": _compose_text(result),
            "risks": result.risks,
            "raw_response": json.dumps(payload, ensure_ascii=False),
            "context_digest": result.context_digest,
            "created_at": utcnow().isoformat(timespec="seconds"),
        })
    return result


def _compose_text(result: AdviceResult) -> str:
    parts = [result.reasoning]
    if result.counter_evidence:
        parts.append(f"[反面证据] {result.counter_evidence}")
    return "\n".join(p for p in parts if p)


def advice_is_stale(store: Store, market_hash_name: str, digest: str,
                    platform: str | None = None,
                    patterns: PatternTable | None = None,
                    max_age_hours: int = 24) -> dict[str, Any]:
    """判断一条建议是否已过期。

    两个维度：输入数据是否变了（digest 不同），以及时间过去了多久。
    价格剧烈波动的品类上，一天前的建议可能已经完全失效。
    """
    current = build_context(store, market_hash_name, platform=platform,
                            patterns=patterns)
    changed = current.digest != digest
    return {
        "digest_changed": changed,
        "current_digest": current.digest,
        "advice_digest": digest,
        "stale": changed,
    }


def batch_advice(store: Store, names: Sequence[str],
                 client: LLMClient | None = None,
                 patterns: PatternTable | None = None,
                 delay_seconds: float = 1.0) -> dict[str, Any]:
    """对多个饰品依次生成建议（串行，带间隔以防触发限流）。"""
    import time

    llm = client or LLMClient()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for idx, name in enumerate(names):
        try:
            result = request_advice(store, name, client=llm, patterns=patterns)
            results.append(result.to_dict())
        except LLMError as exc:
            errors.append({"market_hash_name": name, "error": str(exc)})
        if idx < len(names) - 1 and delay_seconds:
            time.sleep(delay_seconds)
    return {"requested": len(names), "succeeded": len(results),
            "failed": len(errors), "results": results, "errors": errors}
