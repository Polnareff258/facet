"""关注清单：带交易意图的「我真正关心的那几个」。

与通用监控清单（`watchlist`）的区别：

| | watchlist | focus |
| --- | --- | --- |
| 目的 | 泛化盯盘，找机会 | 我确定要买/要卖的标的 |
| 意图 | 无 | buy / sell / watch |
| 价格 | 触发阈值 | **目标价 + 预算 + 数量** |
| 筛选 | 无 | 可接受的磨损档 / 品质 / 图案档位 |

为什么要有这一层：用户的原话是「我真正关注的只有一部分特定的饰品，是需要买入或
卖出的」。泛化监控负责「发现」，关注清单负责「执行」—— 两者的信息需求不同：
执行需要知道预算、数量、以及「这个价我买不买得起、值不值得」。

变体展开是这个模块的核心能力：你要买「AK-47 | Redline」，实际要盯的是
5 个磨损档里你接受的那 2 个，而不是一个笼统的名字。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .skins import (
    Quality,
    WEAR_EN_TO_CN,
    WEAR_ORDER,
    expand_variants,
    group_variants,
    parse_name,
)
from .store import Store

logger = logging.getLogger(__name__)

INTENT_BUY = "buy"
INTENT_SELL = "sell"
INTENT_WATCH = "watch"
INTENTS = (INTENT_BUY, INTENT_SELL, INTENT_WATCH)

INTENT_CN = {"buy": "买入", "sell": "卖出", "watch": "观察"}


@dataclass(slots=True)
class FocusTarget:
    """一个关注标的（展开到具体变体）。"""

    market_hash_name: str
    intent: str = INTENT_WATCH
    priority: int = 3
    target_price: float | None = None
    max_budget: float | None = None
    quantity: int = 1
    note: str = ""
    #: 运行时填充
    display_name: str = ""
    current_price: float | None = None
    current_platform: str | None = None
    bid_price: float | None = None
    sell_count: int | None = None
    pattern_label: str | None = None
    progress: float | None = None      # 距离目标的完成度 0..1
    status: str = "unknown"
    status_cn: str = "无数据"

    def to_dict(self) -> dict[str, Any]:
        variant = parse_name(self.market_hash_name)
        return {
            "market_hash_name": self.market_hash_name,
            "display_name": self.display_name or self.market_hash_name,
            "intent": self.intent,
            "intent_cn": INTENT_CN.get(self.intent, self.intent),
            "priority": self.priority,
            "target_price": self.target_price,
            "max_budget": self.max_budget,
            "quantity": self.quantity,
            "note": self.note,
            "wear": variant.wear,
            "wear_cn": variant.wear_cn,
            "quality": variant.quality.value,
            "quality_cn": variant.quality.cn,
            "is_star": variant.is_star,
            "current_price": self.current_price,
            "current_platform": self.current_platform,
            "bid_price": self.bid_price,
            "sell_count": self.sell_count,
            "pattern_label": self.pattern_label,
            "progress": self.progress,
            "status": self.status,
            "status_cn": self.status_cn,
        }


@dataclass
class FocusBoard:
    """看板用的关注清单视图：按意图分组。"""

    buy: list[FocusTarget] = field(default_factory=list)
    sell: list[FocusTarget] = field(default_factory=list)
    watch: list[FocusTarget] = field(default_factory=list)
    actionable: int = 0          # 已达标、可以直接动手的条数

    def to_dict(self) -> dict[str, Any]:
        return {
            "buy": [t.to_dict() for t in self.buy],
            "sell": [t.to_dict() for t in self.sell],
            "watch": [t.to_dict() for t in self.watch],
            "counts": {"buy": len(self.buy), "sell": len(self.sell),
                       "watch": len(self.watch)},
            "actionable": self.actionable,
        }


# ── 变体展开 ───────────────────────────────────────────────

def resolve_wear_filter(spec: str | Iterable[str] | None) -> list[str] | None:
    """把人话的磨损筛选转成标准 EN 名。

    接受：FN / MW / 略磨 / 崭新出厂 / "Factory New" / 逗号分隔的多个。
    """
    from .skins import WEAR_ALIASES

    if spec is None:
        return None
    items = [spec] if isinstance(spec, str) else list(spec)
    out: list[str] = []
    for raw in items:
        for piece in str(raw).replace("，", ",").split(","):
            piece = piece.strip()
            if not piece:
                continue
            canonical = WEAR_ALIASES.get(piece.lower())
            if canonical and canonical not in out:
                out.append(canonical)
    return out or None


def resolve_quality_filter(spec: str | Iterable[str] | None) -> list[Quality] | None:
    """把人话的品质筛选转成 Quality 枚举。接受：普通/暗金/纪念品/st/正常。"""
    if spec is None:
        return None
    items = [spec] if isinstance(spec, str) else list(spec)
    mapping = {
        "normal": Quality.NORMAL, "普通": Quality.NORMAL, "正常": Quality.NORMAL,
        "": Quality.NORMAL,
        "stattrak": Quality.STATTRAK, "暗金": Quality.STATTRAK,
        "st": Quality.STATTRAK, "stat": Quality.STATTRAK,
        "souvenir": Quality.SOUVENIR, "纪念品": Quality.SOUVENIR,
    }
    out: list[Quality] = []
    for raw in items:
        for piece in str(raw).replace("，", ",").split(","):
            quality = mapping.get(piece.strip().lower())
            if quality and quality not in out:
                out.append(quality)
    return out or None


def expand_focus(base_name: str, wears: str | Iterable[str] | None = None,
                 qualities: str | Iterable[str] | None = None,
                 is_star: bool | None = None) -> list[str]:
    """把「一个基础饰品」展开成用户实际要盯的具体变体列表。

    例：expand_focus("AK-47 | Redline", wears="FT,MW")
        → ["AK-47 | Redline (Field-Tested)", "AK-47 | Redline (Minimal Wear)"]
    """
    wear_list = resolve_wear_filter(wears)
    quality_list = resolve_quality_filter(qualities)
    return expand_variants(base_name, wears=wear_list,
                           qualities=quality_list, is_star=is_star)


# ── 写 ─────────────────────────────────────────────────────

def add_focus(store: Store, name_or_base: str, intent: str = INTENT_WATCH,
              target_price: float | None = None, max_budget: float | None = None,
              quantity: int = 1, priority: int = 3, note: str = "",
              wears: str | Iterable[str] | None = None,
              qualities: str | Iterable[str] | None = None,
              patterns: str | Iterable[str] | None = None,
              expand: bool | None = None) -> list[str]:
    """添加关注标的，返回实际写入的市场名列表。

    `expand` 为 None 时自动判断：
      - 传入的是基础名（不带磨损后缀）→ 展开成变体
      - 传入的已是完整变体名 → 原样添加
    这样「加一个皮肤」和「加某一个磨损档」都能用同一条命令。
    """
    parsed = parse_name(name_or_base)
    auto_expand = (parsed.wear is None) if expand is None else expand

    if auto_expand and parsed.base:
        names = expand_focus(parsed.base, wears=wears, qualities=qualities,
                             is_star=parsed.is_star)
    else:
        names = [name_or_base]

    if patterns is not None:
        pattern_list = ([patterns] if isinstance(patterns, str)
                        else list(patterns))
        pattern_json = json.dumps(pattern_list, ensure_ascii=False)
    else:
        pattern_json = None

    wear_json = None
    wear_filter = resolve_wear_filter(wears)
    if wear_filter:
        wear_json = json.dumps(wear_filter, ensure_ascii=False)

    quality_filter = resolve_quality_filter(qualities)
    quality_json = None
    if quality_filter:
        quality_json = json.dumps([q.value for q in quality_filter], ensure_ascii=False)

    for name in names:
        store.upsert_focus(
            name, intent=intent, priority=priority, target_price=target_price,
            max_budget=max_budget, quantity=quantity, note=note or None,
            acceptable_wear=wear_json, acceptable_quality=quality_json,
            acceptable_patterns=pattern_json, enabled=1)
    logger.info("[focus] 添加 %d 个标的（意图=%s）", len(names), intent)
    return names


# ── 评估 ───────────────────────────────────────────────────

def _status_for(intent: str, current: float | None,
                target: float | None) -> tuple[str, str, float | None]:
    """返回 (status, status_cn, progress)。"""
    if current is None:
        return "no_data", "无数据", None
    if target is None:
        return "tracking", "仅跟踪", None

    if intent == INTENT_BUY:
        # 买入：现价 ≤ 目标价才算达标
        progress = min(1.0, target / current) if current > 0 else 1.0
        if current <= target:
            return "ready", "达到买点", 1.0
        if current <= target * 1.05:
            return "near", "接近买点", progress
        return "waiting", "等待回落", progress

    if intent == INTENT_SELL:
        # 卖出：现价 ≥ 目标价才算达标
        progress = min(1.0, current / target) if target > 0 else 1.0
        if current >= target:
            return "ready", "达到卖点", 1.0
        if current >= target * 0.95:
            return "near", "接近卖点", progress
        return "waiting", "等待上涨", progress

    return "tracking", "仅跟踪", None


def build_board(store: Store, patterns: Any = None,
                name_resolver: Any = None) -> FocusBoard:
    """组装关注看板：把当前行情与目标价对照，给出状态。"""
    board = FocusBoard()
    rows = store.list_focus(enabled_only=True)
    if not rows:
        return board

    for row in rows:
        mhn = row["market_hash_name"]
        intent = row.get("intent") or INTENT_WATCH

        target = FocusTarget(
            market_hash_name=mhn, intent=intent,
            priority=int(row.get("priority") or 3),
            target_price=row.get("target_price"),
            max_budget=row.get("max_budget"),
            quantity=int(row.get("quantity") or 1),
            note=row.get("note") or "",
        )

        if name_resolver is not None:
            try:
                target.display_name = name_resolver.resolve(mhn)
            except Exception:  # noqa: BLE001 — 取名失败不能影响看板
                target.display_name = mhn

        # 每个平台取最新报价，挑最有利的一条展示
        latest = store.latest_by_platform(mhn)
        best_price: float | None = None
        best_platform: str | None = None
        for platform, quote in latest.items():
            price = quote.get("sell_price")
            if not price:
                continue
            if intent == INTENT_SELL:
                # 卖出关心的是能卖多高，优先看求购价
                candidate = quote.get("bid_price") or price
                if best_price is None or candidate > best_price:
                    best_price, best_platform = float(candidate), platform
            else:
                if best_price is None or float(price) < best_price:
                    best_price, best_platform = float(price), platform

        target.current_price = best_price
        target.current_platform = best_platform
        if best_platform:
            quote = latest.get(best_platform) or {}
            target.bid_price = quote.get("bid_price")
            target.sell_count = quote.get("sell_count")

        if patterns is not None:
            try:
                match = patterns.classify(mhn)
                if match.is_special:
                    target.pattern_label = match.label_cn
            except Exception:  # noqa: BLE001
                pass

        status, status_cn, progress = _status_for(
            intent, target.current_price, target.target_price)
        target.status, target.status_cn, target.progress = status, status_cn, progress
        if status == "ready":
            board.actionable += 1

        bucket = {INTENT_BUY: board.buy, INTENT_SELL: board.sell}.get(
            intent, board.watch)
        bucket.append(target)

    for bucket in (board.buy, board.sell, board.watch):
        bucket.sort(key=lambda t: (t.status != "ready", t.priority,
                                   t.market_hash_name))
    return board


# ── 分组展示 ───────────────────────────────────────────────

def grouped_view(names: Sequence[str], resolver: Any = None) -> list[dict[str, Any]]:
    """把一组名字按「基础饰品 → 变体」聚成树，UI 折叠展示用。"""
    groups = group_variants(names)
    out: list[dict[str, Any]] = []
    for group in groups:
        payload = group.to_dict()
        if resolver is not None:
            try:
                payload["display_name"] = resolver.resolve(
                    group.variants[0].raw_name) if group.variants else group.base
            except Exception:  # noqa: BLE001
                payload["display_name"] = group.base
        out.append(payload)
    return out


def describe_filters(row: dict[str, Any]) -> str:
    """把关注项的可接受条件渲染成人话，用于 CLI/看板。"""
    bits: list[str] = []
    raw_wear = row.get("acceptable_wear")
    if raw_wear:
        try:
            wears = json.loads(raw_wear)
            bits.append("磨损 " + "/".join(
                WEAR_EN_TO_CN.get(w, w) for w in
                sorted(wears, key=lambda w: WEAR_ORDER.get(w, 99))))
        except (ValueError, TypeError):
            pass
    raw_quality = row.get("acceptable_quality")
    if raw_quality:
        try:
            qualities = json.loads(raw_quality)
            cn = {"normal": "普通", "stattrak": "暗金", "souvenir": "纪念品"}
            bits.append("品质 " + "/".join(cn.get(q, q) for q in qualities))
        except (ValueError, TypeError):
            pass
    raw_patterns = row.get("acceptable_patterns")
    if raw_patterns:
        try:
            bits.append("档位 " + "/".join(json.loads(raw_patterns)))
        except (ValueError, TypeError):
            pass
    return "；".join(bits) or "不限"
