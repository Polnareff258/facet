"""档位词表：从平台直接采集「有哪些档位」，而不是从 paint_seed 反推。

**这个模块推翻了早期设计。**

早期做法是：BUFF 挂单明细带 `paint_seed` → 从价格分布反推哪些种子贵。
那是在没有更好信息时的办法。后来实测发现，悠悠有品的公开接口直接返回
**平台自己分类好的中文档位名**：

    specialStyle : 红宝石 / 蓝宝石 / 黑珍珠 / P1 / P2 / P4 / 绿宝石 / T1 / T2
    fadeText     : 99-100 / 97-99          （渐变百分比区间）
    abradeText   : 0-0.01 / 0.07-0.08      （比 5 档更精细的磨损区间）
    commodityName: 刺刀（★） | 多普勒 (崭新出厂)   （官方中文名）

三件事一次拿到，而且是**免凭据**的。

对照 BUFF 侧（实测）：带档位筛选的查询一律需要登录 ——
`sell_order?paintseed=N`、`?min_paintwear=N`、`?sort_by=...` 全部返回
`{"code":"Login Required"}`，而不带这些参数的基础查询可用。
匿名只能拿到 `paint_seed` 原始数字，拿不到档位名称。

**所以：档位词表以悠悠为准，BUFF 的 paint_seed 仅作辅助。**

本模块负责采集、缓存、查询这套词表。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .store import Store

logger = logging.getLogger(__name__)

# 档位字段的语义
KIND_STYLE = "style"      # specialStyle：宝石 / 相位 / Tier
KIND_FADE = "fade"        # fadeText：渐变百分比区间
KIND_ABRADE = "abrade"    # abradeText：磨损区间

KIND_CN = {KIND_STYLE: "档位", KIND_FADE: "渐变", KIND_ABRADE: "磨损区间"}

#: 表示「买家不限档位」的占位值，不是真实档位，必须剔除
PLACEHOLDERS: dict[str, set[str]] = {
    KIND_STYLE: {"不限", "无", "None", "null", "", "-1"},
    KIND_FADE: {"不限", "无", "None", "null", "", "0-100"},
    KIND_ABRADE: {"不限", "无", "None", "null", ""},
}

#: 已实测确认的档位取值形态（用于校验与归类，不是白名单）
_KNOWN_STYLE_HINTS = (
    "红宝石", "蓝宝石", "黑珍珠", "绿宝石", "蓝宝石(黑)", "P1", "P2", "P3", "P4",
    "T1", "T2", "T3", "T4", "冰火", "红头", "蓝尖", "金", "全渐变",
)


@dataclass(slots=True)
class VariantTerm:
    market_hash_name: str
    kind: str
    value: str
    sample_count: int = 0
    source: str = "youpin_purchase"

    def to_dict(self) -> dict[str, Any]:
        return {"market_hash_name": self.market_hash_name, "kind": self.kind,
                "kind_cn": KIND_CN.get(self.kind, self.kind),
                "value": self.value, "sample_count": self.sample_count,
                "source": self.source}


@dataclass
class VariantVocabulary:
    """某个饰品的档位词表。"""

    market_hash_name: str
    cn_name: str | None = None
    template_id: int | None = None
    #: kind -> [取值...]（已剔除占位值，按样本数降序）
    terms: dict[str, list[str]] = field(default_factory=dict)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    sample_rows: int = 0
    synced_at: str | None = None

    @property
    def styles(self) -> list[str]:
        return self.terms.get(KIND_STYLE, [])

    @property
    def fades(self) -> list[str]:
        return self.terms.get(KIND_FADE, [])

    @property
    def abrades(self) -> list[str]:
        return self.terms.get(KIND_ABRADE, [])

    @property
    def has_variants(self) -> bool:
        return bool(self.styles or self.fades)

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_hash_name": self.market_hash_name,
            "cn_name": self.cn_name,
            "template_id": self.template_id,
            "terms": self.terms,
            "counts": self.counts,
            "sample_rows": self.sample_rows,
            "synced_at": self.synced_at,
            "style_count": len(self.styles),
            "fade_count": len(self.fades),
            "has_variants": self.has_variants,
        }


def extract_vocab(rows: Sequence[dict[str, Any]],
                  market_hash_name: str) -> VariantVocabulary:
    """从悠悠求购行里提取档位词表。

    只看真实出现的取值 —— 平台不会提供一份静态的档位清单，
    但「买家实际挂出的档位约束」就是这个品类真实在流通的档位集合。
    样本越多覆盖越全（P3 这种冷门相位可能要拉多页才出现）。
    """
    counters: dict[str, dict[str, int]] = {KIND_STYLE: {}, KIND_FADE: {}, KIND_ABRADE: {}}
    cn_name: str | None = None
    template_id: int | None = None

    field_map = {"specialStyle": KIND_STYLE, "fadeText": KIND_FADE,
                 "abradeText": KIND_ABRADE}

    for row in rows:
        if not isinstance(row, dict):
            continue
        if cn_name is None and row.get("commodityName"):
            cn_name = str(row["commodityName"])
        if template_id is None and row.get("templateId"):
            try:
                template_id = int(row["templateId"])
            except (TypeError, ValueError):
                pass
        for field_name, kind in field_map.items():
            value = row.get(field_name)
            if value is None:
                continue
            text = str(value).strip()
            if text in PLACEHOLDERS.get(kind, set()):
                continue
            counters[kind][text] = counters[kind].get(text, 0) + 1

    vocab = VariantVocabulary(market_hash_name=market_hash_name, cn_name=cn_name,
                              template_id=template_id, sample_rows=len(rows))
    for kind, counts in counters.items():
        if not counts:
            continue
        # 按样本数降序：出现得多的档位通常也是更主流的档位
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        vocab.terms[kind] = [k for k, _ in ordered]
        vocab.counts[kind] = dict(ordered)
    return vocab


def save_vocab(store: Store, vocab: VariantVocabulary) -> int:
    """把词表写入数据库（同一 (饰品, 类别, 取值) 刷新样本数与时间）。"""
    rows: list[tuple[str, str, str, int, str]] = []
    for kind, values in vocab.terms.items():
        counts = vocab.counts.get(kind, {})
        for value in values:
            rows.append((vocab.market_hash_name, kind, value,
                         counts.get(value, 0), "youpin_purchase"))
    written = store.upsert_variant_terms(rows)
    if vocab.cn_name:
        # 顺便把官方中文名收录进去（这本来就是权威来源）
        from .names import NameResolver
        NameResolver(store).learn([(vocab.market_hash_name, vocab.cn_name)],
                                  source="quote_raw")
    return written


def load_vocab(store: Store, market_hash_name: str) -> VariantVocabulary:
    rows = store.variant_terms_for(market_hash_name)
    vocab = VariantVocabulary(market_hash_name=market_hash_name)
    for row in rows:
        kind = row["kind"]
        vocab.terms.setdefault(kind, []).append(row["value"])
        vocab.counts.setdefault(kind, {})[row["value"]] = int(row["sample_count"])
    for kind in vocab.counts:
        vocab.counts[kind] = dict(sorted(vocab.counts[kind].items(),
                                         key=lambda kv: (-kv[1], kv[0])))
    # 已按样本数排过序，terms 保持同序
    for kind, counts in vocab.counts.items():
        vocab.terms[kind] = list(counts.keys())
    return vocab


def sync_youpin_vocab(store: Store, adapter: Any, market_hash_name: str,
                      template_id: int, pages: int = 3,
                      page_size: int = 100) -> VariantVocabulary | None:
    """从悠悠求购接口采集档位词表并落库。

    多页是为了覆盖冷门档位：实测刺刀多普勒的 P3 相位在第一页 41 条里没出现，
    只拉一页会漏掉它。
    """
    fetch = getattr(adapter, "fetch_purchase_rows", None)
    if fetch is None:
        raise TypeError("适配器不支持 fetch_purchase_rows，无法采集档位词表")

    rows = fetch(template_id, pages=pages, page_size=page_size)
    if not rows:
        logger.warning("[variants] %s 未取到求购挂单，跳过", market_hash_name)
        return None

    vocab = extract_vocab(rows, market_hash_name)
    vocab.template_id = template_id or vocab.template_id
    save_vocab(store, vocab)
    logger.info("[variants] %s：%d 行样本 → 档位 %s，渐变 %s",
                market_hash_name, vocab.sample_rows, vocab.styles, vocab.fades)
    return vocab


def sync_many(store: Store, adapter: Any,
              targets: Iterable[tuple[str, int]],
              pages: int = 3, delay: float = 1.2) -> dict[str, Any]:
    """批量采集。targets = [(market_hash_name, template_id), ...]"""
    import time

    synced: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for index, (name, template_id) in enumerate(targets):
        try:
            vocab = sync_youpin_vocab(store, adapter, name, int(template_id),
                                      pages=pages)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"market_hash_name": name, "reason": str(exc)[:120]})
            continue
        if vocab and vocab.has_variants:
            synced.append(vocab.to_dict())
        elif vocab:
            skipped.append({"market_hash_name": name,
                            "reason": "该品类无档位维度（或样本不足）"})
        else:
            skipped.append({"market_hash_name": name, "reason": "未取到求购挂单"})
        if index < len(list(targets)) - 1 and delay:
            time.sleep(delay)
    return {"synced": len(synced), "skipped": len(skipped),
            "results": synced, "skipped_detail": skipped}


# ── 与图案规则表的衔接 ─────────────────────────────────────

def vocab_to_pattern_rules(vocab: VariantVocabulary) -> list[dict[str, Any]]:
    """把词表转成 patterns.yaml 的 name_rules 结构。

    注意这里生成的是**名称规则**：悠悠的档位名（红宝石/P2/T1）是直接给出的标签，
    不是种子区间。本工具据此判定档位，不做任何种子里程碑推断。
    """
    from .skins import parse_name

    parsed = parse_name(vocab.market_hash_name)
    scope = parsed.finish or parsed.base
    rules: list[dict[str, Any]] = []
    for value in vocab.styles:
        rules.append({
            "pattern": re.escape(value),
            "tier": _slug(value),
            "label_cn": value,
            "premium_hint": 1.0,   # 溢价倍数由实盘学习补充，不在这里臆测
        })
    for value in vocab.fades:
        rules.append({
            "pattern": re.escape(value),
            "tier": f"fade_{_slug(value)}",
            "label_cn": f"渐变 {value}",
            "premium_hint": 1.0,
        })
    return rules


def _slug(text: str) -> str:
    slug = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE).strip("_").lower()
    return slug or "unnamed"
