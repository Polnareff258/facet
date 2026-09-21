"""饰品名称解析与变体分类。

**为什么这是核心问题**：CS 饰品同名不同价是常态。「AK-47 | Redline」这一串
在 BUFF 上对应 5 个磨损档；「★ 蝴蝶刀 | 多普勒」还要再按相位（红宝石/蓝宝石/
P1-P4）分成 6 档，价差可以是 3-10 倍。把「同一种饰品」当一个东西监控，
等于把一辆车和它的备胎当成同一个商品定价。

本模块负责**能从名称里可靠解析出来**的部分：

  ★  StatTrak™  AK-47 | Redline  (Field-Tested)
  │   │         │                 └─ 磨损（5 档）
  │   │         └─ 武器 | 皮肤
  │   └─ 品质（普通 / 暗金 / 纪念品）
  └─ 星标（匕首、手套）

**名称解析不出来的部分**（多普勒相位、渐变百分比、淬火蓝钢档、特殊模板）由
`facet/patterns.py` 处理 —— 那些取决于饰品实例的 `paint_seed`，不取决于名称。
这条边界必须划清楚：靠名称猜相位会给出错误的交易建议。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

# ── 磨损档 ─────────────────────────────────────────────────
#
# EN 名称与 Steam 官方完全一致（做键匹配必须精确）；
# CN 名称同时收录 BUFF 与社区惯用写法。

WEAR_EN_TO_CN: dict[str, str] = {
    "Factory New": "崭新出厂",
    "Minimal Wear": "略有磨损",
    "Field-Tested": "久经沙场",
    "Well-Worn": "破损不堪",
    "Battle-Scarred": "战痕累累",
}

WEAR_ALIASES: dict[str, str] = {
    "factory new": "Factory New", "fn": "Factory New", "崭新出厂": "Factory New",
    "崭新": "Factory New",
    "minimal wear": "Minimal Wear", "mw": "Minimal Wear", "略有磨损": "Minimal Wear",
    "略磨": "Minimal Wear",
    "field-tested": "Field-Tested", "field tested": "Field-Tested",
    "ft": "Field-Tested", "久经沙场": "Field-Tested", "久经": "Field-Tested",
    "well-worn": "Well-Worn", "well worn": "Well-Worn", "ww": "Well-Worn",
    "破损不堪": "Well-Worn", "破损": "Well-Worn",
    "battle-scarred": "Battle-Scarred", "battle scarred": "Battle-Scarred",
    "bs": "Battle-Scarred", "战痕累累": "Battle-Scarred", "战痕": "Battle-Scarred",
}

#: 磨损等级排序（1 = 最新，5 = 最旧）。用于「越磨越便宜」这类判断。
WEAR_ORDER: dict[str, int] = {
    "Factory New": 1, "Minimal Wear": 2, "Field-Tested": 3,
    "Well-Worn": 4, "Battle-Scarred": 5,
}


class Quality(str, Enum):
    """品质前缀。Steam 命名里三个前缀互斥。"""

    NORMAL = "normal"
    STATTRAK = "stattrak"
    SOUVENIR = "souvenir"

    @property
    def cn(self) -> str:
        return {"normal": "", "stattrak": "暗金", "souvenir": "纪念品"}[self.value]

    @property
    def prefix_en(self) -> str:
        return {"normal": "", "stattrak": "StatTrak™ ", "souvenir": "Souvenir "}[self.value]


# ── 品类 ───────────────────────────────────────────────────

#: 这些品类没有磨损档（用它们的市场名里不会出现磨损后缀）
NO_WEAR_CATEGORIES = (
    "Sticker", "Music Kit", "Graffiti", "Patch", "Charm", "Collectible",
    "Container", "Key", "Pass", "Tool", "Tag", "Gift", "Agent",
)

#: 纪念品只存在于这些品类（避免把普通饰品误判成纪念品）
SOUVENIR_CATEGORIES = (
    "Collection", "Case", "Package",
)


@dataclass(slots=True)
class SkinVariant:
    """一个「可交易单位」的结构化描述。

    注意：这描述的是 **market_hash_name 层级** 的交易单位
    （即 BUFF/悠悠有品上一个可下单的品类），**不是**实例层级的图案档位。
    """

    raw_name: str                       # 原始 market_hash_name
    base: str                           # 「武器 | 皮肤」部分（已去掉所有前缀后缀）
    weapon: str | None = None           # 竖线左侧
    finish: str | None = None           # 竖线右侧
    category: str | None = None         # 品类（无竖线时的第一段）
    is_star: bool = False               # ★ 匕首 / 手套
    quality: Quality = Quality.NORMAL
    wear: str | None = None             # 磨损 EN 名；None = 无磨损档
    parsed: bool = True                 # 是否成功解析（失败时保留原始名）
    notes: list[str] = field(default_factory=list)

    # ── 派生属性 ──

    @property
    def wear_cn(self) -> str:
        return WEAR_EN_TO_CN.get(self.wear or "", "")

    @property
    def wear_rank(self) -> int | None:
        return WEAR_ORDER.get(self.wear or "")

    @property
    def variant_key(self) -> str:
        """变体去重键：同一个 key 表示「BUFF 上同一个可交易品类」。

        刻意只包含名称能确定的部分（基础名 + 品质 + 磨损）；
        图案档位不进来，因为同一个 market_hash_name 会混装多个档位。
        """
        parts = [self.base, self.quality.value, self.wear or "nowear"]
        if self.is_star:
            parts.append("star")
        return "|".join(parts)

    @property
    def is_variant_rich(self) -> bool:
        """是否属于「变体决定价格」的品类，值得做图案档位细分。

        判定依据是社区公认的高变体品类关键词；未命中不代表没有变体，
        只是当前规则库不覆盖。
        """
        text = f"{self.weapon or ''} {self.finish or ''}".lower()
        return any(k in text for k in VARIANT_RICH_KEYWORDS)

    @property
    def no_wear(self) -> bool:
        """该品类是否不存在磨损档。

        要同时看 `weapon` 与 `category`：像 "Sticker | Titan" 这种带竖线的
        名字，首段会被解析成 weapon='Sticker'，category 反而是 None。
        只看 category 会漏判，进而把贴纸错误地展开成 5 个磨损档。

        匹配用「整段相等或后跟空格」而不是取首词：品类名本身含空格
        （"Music Kit" / "Container"），取首词会得到 "Music" 而漏判。
        """
        head = (self.weapon or self.category or "").strip()
        if not head:
            return False
        return any(head == cat or head.startswith(cat + " ")
                   for cat in NO_WEAR_CATEGORIES)

    def to_dict(self) -> dict[str, object]:
        return {
            "raw_name": self.raw_name,
            "base": self.base,
            "weapon": self.weapon,
            "finish": self.finish,
            "category": self.category,
            "is_star": self.is_star,
            "quality": self.quality.value,
            "quality_cn": self.quality.cn,
            "wear": self.wear,
            "wear_cn": self.wear_cn,
            "wear_rank": self.wear_rank,
            "variant_key": self.variant_key,
            "variant_rich": self.is_variant_rich,
            "parsed": self.parsed,
            "notes": list(self.notes),
        }

    def to_en_name(self) -> str:
        """按解析结果重建英文市场名（用于校验解析是否无损）。"""
        prefix = ("★ " if self.is_star else "") + self.quality.prefix_en
        suffix = f" ({self.wear})" if self.wear else ""
        return f"{prefix}{self.base}{suffix}"


#: 变体决定价格的品类关键词。命中后 UI 会提示「建议做档位细分」。
VARIANT_RICH_KEYWORDS = (
    "doppler", "gamma doppler", "marble fade", "fade", "case hardened",
    "crimson web", "slaughter", "tiger tooth", "autotronic", "bright water",
    "多普勒", "伽马", "大理石", "渐变", "淬火", "深红之网",
)

_STAR_RE = re.compile(r"^★\s*")
_STATTRAK_RE = re.compile(r"^StatTrak\s*(?:™|\u2122)?\s*")
_SOUVENIR_RE = re.compile(r"^Souvenir\s+")
# 磨损必须精确匹配已知字符串，否则 "Sticker | Titan (Holo)" 会被当成有磨损
_WEAR_SUFFIX_RE = re.compile(r"\s*\(([^()]+)\)\s*$")


def _strip_prefixes(raw: str) -> tuple[str, bool, Quality, list[str]]:
    """剥掉 ★ / StatTrak / Souvenir 前缀，返回 (剩余, 是否星标, 品质, 提示)。"""
    notes: list[str] = []
    text = raw.strip()
    is_star = False

    # ★ 与 StatTrak 的先后顺序在 Steam 命名里是固定的，但容错处理两种顺序
    changed = True
    while changed:
        changed = False
        if _STAR_RE.match(text):
            text = _STAR_RE.sub("", text, count=1)
            if is_star:
                notes.append("名称中出现多个 ★，已按一个处理")
            is_star = True
            changed = True
    if is_star is False and text.startswith("★"):
        text = text.lstrip("★").strip()
        is_star = True

    quality = Quality.NORMAL
    if _STATTRAK_RE.match(text):
        text = _STATTRAK_RE.sub("", text, count=1).strip()
        quality = Quality.STATTRAK
    elif _SOUVENIR_RE.match(text):
        text = _SOUVENIR_RE.sub("", text, count=1).strip()
        quality = Quality.SOUVENIR

    if is_star:
        # ★ 也可能出现在 StatTrak 之后（"StatTrak™ ★ ..." 的写法偶见于第三方数据）
        text = _STAR_RE.sub("", text, count=1).strip()

    return text, is_star, quality, notes


def parse_name(raw: str) -> SkinVariant:
    """把 market_hash_name 解析成结构化变体。

    解析失败不抛异常 —— 判定标准是「能不能定位到基础名」，而非「是否符合预期格式」。
    未知格式的饰品照样进库（保留 raw_name 与 parsed=False），
    因为市场永远在加新东西，宁可显示得粗糙也不要丢数据。
    """
    original = (raw or "").strip()
    if not original:
        return SkinVariant(raw_name="", base="", parsed=False,
                           notes=["名称为空"])

    text, is_star, quality, notes = _strip_prefixes(original)

    # 磨损后缀：精确匹配已知磨损名，避免误吃 "(Holo)"
    wear: str | None = None
    match = _WEAR_SUFFIX_RE.search(text)
    if match:
        candidate = match.group(1).strip()
        canonical = WEAR_ALIASES.get(candidate.lower())
        if canonical:
            wear = canonical
            text = text[:match.start()].strip()
        else:
            notes.append(f"末尾括号「{candidate}」不是已知磨损，按非磨损处理")

    base = text.strip()
    weapon: str | None = None
    finish: str | None = None
    category: str | None = None

    if " | " in base:
        weapon, _, finish = base.partition(" | ")
        weapon, finish = weapon.strip(), finish.strip()
    else:
        # 无竖线：Sticker / Music Kit / 探员 等；取第一段作为品类
        category = base.split()[0] if base.split() else base

    if wear is None and category and category in NO_WEAR_CATEGORIES:
        notes.append(f"{category} 品类无磨损档")

    parsed = bool(base)
    variant = SkinVariant(
        raw_name=original, base=base, weapon=weapon, finish=finish,
        category=category, is_star=is_star, quality=quality, wear=wear,
        parsed=parsed, notes=notes,
    )
    if variant.no_wear and not any("无磨损档" in n for n in notes):
        notes.append(f"{variant.weapon or variant.category} 品类无磨损档")
    return variant


def parse_many(names: Iterable[str]) -> list[SkinVariant]:
    return [parse_name(n) for n in names]


# ── 变体展开 ───────────────────────────────────────────────

def expand_variants(base_name: str,
                    wears: Iterable[str] | None = None,
                    qualities: Iterable[Quality] | None = None,
                    is_star: bool | None = None) -> list[str]:
    """由基础名展开出所有变体市场名。

    用途：用户说「我要关注 AK-47 | Redline」，工具可以自动展开成 5 个磨损档
    （或只展开他指定的档位），而不是逼着他手工敲 5 个全名。

    注意 base_name 里若已含前缀/磨损，会先被解析掉再展开，避免出现
    「★ ★ 蝴蝶刀」这类双重前缀。
    """
    parsed = parse_name(base_name)
    base = parsed.base
    star = parsed.is_star if is_star is None else is_star
    quality_list = list(qualities) if qualities else [parsed.quality or Quality.NORMAL]

    # 无磨损档的品类：只按品质展开
    if parsed.no_wear:
        wear_list: list[str | None] = [None]
    else:
        wear_list = list(wears) if wears else list(WEAR_EN_TO_CN.keys())

    out: list[str] = []
    for quality in quality_list:
        for wear in wear_list:
            canonical = WEAR_ALIASES.get((wear or "").lower(), wear) if wear else None
            prefix = ("★ " if star else "") + Quality(quality).prefix_en
            suffix = f" ({canonical})" if canonical else ""
            out.append(f"{prefix}{base}{suffix}")
    return out


# ── 分组（给 UI 做「基础饰品 → 变体」树）────────────────────

@dataclass(slots=True)
class VariantGroup:
    """一个基础饰品及其所有变体。UI 关心的是这个结构。"""

    base: str
    is_star: bool
    variants: list[SkinVariant] = field(default_factory=list)

    @property
    def weapon(self) -> str | None:
        return self.variants[0].weapon if self.variants else None

    @property
    def finish(self) -> str | None:
        return self.variants[0].finish if self.variants else None

    @property
    def variant_rich(self) -> bool:
        return any(v.is_variant_rich for v in self.variants)

    @property
    def qualities(self) -> list[str]:
        seen: list[str] = []
        for v in self.variants:
            if v.quality.value not in seen:
                seen.append(v.quality.value)
        return seen

    @property
    def wears(self) -> list[str]:
        present = {v.wear for v in self.variants if v.wear}
        return sorted(present, key=lambda w: WEAR_ORDER.get(w, 99))

    def to_dict(self) -> dict[str, object]:
        return {
            "base": self.base,
            "weapon": self.weapon,
            "finish": self.finish,
            "is_star": self.is_star,
            "variant_count": len(self.variants),
            "qualities": self.qualities,
            "wears": self.wears,
            "wears_cn": [WEAR_EN_TO_CN.get(w, w) for w in self.wears],
            "variant_rich": self.variant_rich,
            "variants": [v.to_dict() for v in self.variants],
        }


def group_variants(names: Iterable[str]) -> list[VariantGroup]:
    """把一串市场名按「基础饰品」聚合，便于 UI 折行展示。"""
    buckets: dict[tuple[str, bool], VariantGroup] = {}
    for name in names:
        variant = parse_name(name)
        key = (variant.base, variant.is_star)
        group = buckets.get(key)
        if group is None:
            group = VariantGroup(base=variant.base, is_star=variant.is_star)
            buckets[key] = group
        group.variants.append(variant)

    for group in buckets.values():
        group.variants.sort(key=lambda v: (
            v.quality.value, WEAR_ORDER.get(v.wear or "", 0), v.raw_name))
    # 非星标排前面（枪皮是绝大多数场景），星标（匕首/手套）排后面
    return sorted(buckets.values(), key=lambda g: (g.is_star, g.base))


# ── 展示名 ─────────────────────────────────────────────────

def compose_cn(variant: SkinVariant,
               base_cn: str | None = None,
               finish_cn: str | None = None) -> str:
    """在没有权威中文名时，按结构拼一个中文显示名。

    这是**兜底**路径。优先用从 BUFF / CSQAQ 学到的官方中文名（见 facet/names.py），
    因为中文的语序和括号风格与英文不同（例如 BUFF 把 ★ 写在刀名后：
    「M9 刺刀（★） | 澄澈之水 (破损不堪)」），拼接结果只能算可读，谈不上地道。
    """
    weapon_cn = base_cn or variant.weapon or ""
    if finish_cn:
        core = f"{weapon_cn} | {finish_cn}"
    elif variant.finish:
        core = f"{weapon_cn} | {variant.finish}" if variant.weapon else variant.base
    else:
        core = base_cn or variant.base

    if variant.is_star and "（★）" not in core and "★" not in core:
        core = f"{core}（★）"
    prefix = variant.quality.cn
    suffix = f" ({variant.wear_cn})" if variant.wear_cn else ""
    return f"{prefix}{core}{suffix}" if prefix else f"{core}{suffix}"
