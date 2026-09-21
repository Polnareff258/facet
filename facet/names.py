"""中文名称层：学习、缓存、兜底拼装。

**为什么不直接翻译**：CS 饰品的中文名是社区约定俗成的，不是英文的逐词直译。
例如 Steam 的 `★ M9 Bayonet | Bright Water (Well-Worn)`，BUFF 写作
「M9 刺刀（★） | 澄澈之水 (破损不堪)」—— 星标位置、括号形态、刀名译法
都不是规则能推出来的。所以正确做法是**从权威源学习**（BUFF / CSQAQ 都返回中文名），
缓存到本地，只在学不到时才退回结构化拼装。

学习到的中文名同时用来反推**基础名**（剥掉磨损后缀），
这样同一皮肤的其他磨损档也能显示中文，不必每个变体都联网查一次。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .skins import WEAR_EN_TO_CN, WEAR_ALIASES, parse_name
from .store import Store

logger = logging.getLogger(__name__)

#: 中文名末尾的磨损标记：BUFF 用半角括号，部分来源用全角
_CN_WEAR_SUFFIX = re.compile(r"\s*[（(]\s*([^（()）]+?)\s*[)）]\s*$")


@dataclass(slots=True)
class NameEntry:
    market_hash_name: str
    cn_name: str
    source: str
    base_cn: str | None = None
    base_en: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"market_hash_name": self.market_hash_name, "cn_name": self.cn_name,
                "source": self.source, "base_cn": self.base_cn,
                "base_en": self.base_en}


def split_cn_base(cn_name: str) -> tuple[str, str | None]:
    """把中文全名拆成 (基础名, 磨损中文)。拆不出磨损时返回 (原名, None)。"""
    match = _CN_WEAR_SUFFIX.search(cn_name or "")
    if not match:
        return cn_name, None
    candidate = match.group(1).strip()
    # 必须是已知的磨损译名才算，避免把「(★)」「(Holo)」当磨损切掉
    canonical_en = WEAR_ALIASES.get(candidate.lower())
    if not canonical_en:
        return cn_name, None
    return cn_name[:match.start()].strip(), candidate


class NameResolver:
    """中文名解析器：DB 缓存优先 → 结构化拼装兜底。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ── 查询 ───────────────────────────────────────────────

    def resolve(self, market_hash_name: str) -> str:
        """返回尽量地道的中文显示名。"""
        entry = self.store.get_cn_name(market_hash_name)
        if entry:
            return entry

        variant = parse_name(market_hash_name)

        # 同皮肤的其它磨损档学到的中文名 → 替换磨损后缀复用
        if variant.wear and variant.weapon:
            sibling = self.store.find_cn_name_by_base(variant.base)
            if sibling:
                base_cn, _ = split_cn_base(sibling)
                wear_cn = variant.wear_cn
                star = "（★）" if variant.is_star and "（★）" not in base_cn else ""
                prefix = variant.quality.cn
                core = f"{base_cn}{star}"
                return f"{prefix}{core} ({wear_cn})" if prefix else f"{core} ({wear_cn})"

        # 完全没学过：结构化拼装，并显式标注这是机器拼的
        return self._compose(variant)

    def _compose(self, variant) -> str:
        from .skins import compose_cn

        base_cn = self.store.find_cn_name_by_base(variant.base) if variant.base else None
        if base_cn:
            base_cn, _ = split_cn_base(base_cn)
        return compose_cn(variant, base_cn=base_cn)

    def resolve_many(self, names: Sequence[str]) -> dict[str, str]:
        return {n: self.resolve(n) for n in names}

    def resolve_with_meta(self, market_hash_name: str) -> dict[str, Any]:
        """带来源信息的解析结果。

        三种来源要区分清楚，因为它们的中文名可信度不同：
          learned   —— 官方源直接给的中文名，最可信
          derived   —— 由同皮肤其它磨损档的中文基础名 + 磨损译名拼出，
                       基础名可信、磨损词是标准译名，整体可放心用
          composed  —— 完全由英文结构化拼装，中文语序可能不地道
        """
        learned = self.store.get_cn_name(market_hash_name)
        if learned:
            return {"market_hash_name": market_hash_name, "display_name": learned,
                    "learned": True, "source": "learned"}

        variant = parse_name(market_hash_name)
        if variant.wear and variant.base:
            sibling = self.store.find_cn_name_by_base(variant.base)
            if sibling:
                return {"market_hash_name": market_hash_name,
                        "display_name": self.resolve(market_hash_name),
                        "learned": False, "source": "derived"}

        return {"market_hash_name": market_hash_name,
                "display_name": self.resolve(market_hash_name),
                "learned": False, "source": "composed"}

    # ── 写入 ───────────────────────────────────────────────

    def learn(self, pairs: Iterable[tuple[str, str]], source: str) -> int:
        """录入 (market_hash_name, 中文名)。

        同时存下英文基础名与中文基础名：前者用于「按英文名反查同皮肤的中文名」，
        后者用于换磨损档时替换后缀。两者缺一不可。
        """
        rows: list[NameEntry] = []
        for mhn, cn in pairs:
            if not mhn or not cn or cn == mhn:
                continue
            base_cn, _ = split_cn_base(cn)
            base_en = parse_name(mhn).base or None
            rows.append(NameEntry(market_hash_name=mhn, cn_name=cn, source=source,
                                  base_cn=base_cn, base_en=base_en))
        if not rows:
            return 0
        written = self.store.upsert_cn_names(
            [(r.market_hash_name, r.cn_name, r.source, r.base_cn, r.base_en)
             for r in rows])
        logger.info("[names] 从 %s 收录 %d 条中文名", source, written)
        return written

    def coverage(self) -> dict[str, Any]:
        return self.store.cn_name_stats()


def learn_from_quotes(resolver: NameResolver, quotes: Iterable[Any]) -> int:
    """从采集到的报价对象里回收中文名。

    为什么能这么做：CSQAQ 与 BUFF 的响应里都带中文名，适配器已把它放进
    `SourceQuote.raw["name"]`。所以只要采过一轮，中文名就顺手收到了，
    不需要为「查名字」额外发请求 —— 这一点很重要，因为额外请求要花限额，
    还可能触发风控。
    """
    pairs: list[tuple[str, str]] = []
    for quote in quotes:
        raw = getattr(quote, "raw", None) or {}
        cn = raw.get("name") or raw.get("cn_name")
        if cn:
            pairs.append((quote.market_hash_name, str(cn)))
    return resolver.learn(pairs, source="quote_raw")
