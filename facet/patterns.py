"""图案档位分类：多普勒相位、渐变百分比、淬火蓝钢、特殊模板。

**这里有一个必须讲清楚的设计决定。**

多普勒的红宝石/蓝宝石/P1-P4、渐变的百分比、淬火的蓝钢档，都**不由名称决定**，
而由饰品实例的 `paint_seed`（图案编号，0-1000）决定。网上流传的种子对照表
是**按刀型/枪型分别不同**的 —— 把 M9 的多普勒表套到蝴蝶刀上会给出完全错误的
档位判断，进而给出错误的价格预期。

所以本模块**不内置任何种子对照表**，改为两条数据驱动的路径：

  1. **实测学习**（推荐）：`learn_from_listings()` 采集市场上在售的
     (paint_seed, price) 对，按种子聚合价格中位数，把显著高于基准的种子
     标为「溢价档」。这是自校准的 —— 市场变了，档位跟着变，
     不需要维护任何静态表。

  2. **手工规则**：`patterns.yaml` 里可以写名称正则 + 种子区间，
     把你确认过的表填进去。规则优先于学习结果。

这个取舍的代价是「首次要跑一次学习」，收益是不会因为抄错表而亏钱。
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

logger = logging.getLogger(__name__)

DEFAULT_RULES_PATH = "patterns.yaml"

#: 溢价种子判定阈值：价格中位数高于该饰品中位数这么多倍，即视为特殊档
DEFAULT_PREMIUM_RATIO = 1.5
#: 参与判定的最少样本数（太少会把噪声当档位）
MIN_SAMPLES_PER_SEED = 2
MIN_TOTAL_SAMPLES = 8


@dataclass(slots=True)
class SeedRule:
    """一条种子区间规则。"""

    tier: str                   # 内部档位标识，如 "ruby" / "p2" / "blue_gem"
    label_cn: str               # 中文档位名，如 "红宝石"
    premium_hint: float = 1.0   # 相对基准的典型溢价倍数（仅作展示参考）
    seeds: tuple[int, ...] = ()  # 精确种子（优先于区间）
    seed_ranges: tuple[tuple[int, int], ...] = ()

    def matches(self, seed: int) -> bool:
        if self.seeds and seed in self.seeds:
            return True
        return any(lo <= seed <= hi for lo, hi in self.seed_ranges)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SeedRule:
        ranges: list[tuple[int, int]] = []
        for item in data.get("seed_ranges") or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                ranges.append((int(item[0]), int(item[1])))
            elif isinstance(item, int):
                ranges.append((item, item))
        return cls(
            tier=str(data.get("tier") or "unknown"),
            label_cn=str(data.get("label_cn") or data.get("tier") or "未知档"),
            premium_hint=float(data.get("premium_hint") or 1.0),
            seeds=tuple(int(s) for s in (data.get("seeds") or [])),
            seed_ranges=tuple(ranges),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "label_cn": self.label_cn,
                "premium_hint": self.premium_hint,
                "seeds": list(self.seeds),
                "seed_ranges": [list(r) for r in self.seed_ranges]}


@dataclass(slots=True)
class NameRule:
    """名称规则：用于「名称本身就写明了档位」的情况。

    有些品类的名称确实包含档位信息（例如第三方市场把相位写进标题），
    这类可以直接用正则抽出来，不必等种子学习。
    """

    pattern: str
    tier: str
    label_cn: str
    premium_hint: float = 1.0

    def match(self, name: str) -> bool:
        import re

        try:
            return re.search(self.pattern, name, re.IGNORECASE) is not None
        except re.error:
            return False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NameRule:
        return cls(pattern=str(data.get("pattern") or ""),
                   tier=str(data.get("tier") or "unknown"),
                   label_cn=str(data.get("label_cn") or data.get("tier") or "未知档"),
                   premium_hint=float(data.get("premium_hint") or 1.0))


@dataclass(slots=True)
class SkinRuleSet:
    """针对某个基础饰品（武器 | 皮肤）的规则集合。

    `scope` 匹配 raw market_hash_name 的子串（大小写不敏感），
    例如 "Doppler" 会命中所有多普勒变体。
    """

    scope: str
    name_rules: list[NameRule] = field(default_factory=list)
    seed_rules: list[SeedRule] = field(default_factory=list)

    def matches_name(self, market_hash_name: str) -> bool:
        return self.scope.lower() in (market_hash_name or "").lower()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkinRuleSet:
        return cls(
            scope=str(data.get("scope") or ""),
            name_rules=[NameRule.from_dict(d) for d in (data.get("name_rules") or [])],
            seed_rules=[SeedRule.from_dict(d) for d in (data.get("seed_rules") or [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {"scope": self.scope,
                "name_rules": [r.__dict__ for r in self.name_rules],
                "seed_rules": [r.to_dict() for r in self.seed_rules]}


@dataclass(slots=True)
class PatternMatch:
    """一次档位判定结果。"""

    tier: str
    label_cn: str
    source: str                 # "name_rule" | "seed_rule" | "learned" | "none"
    premium_hint: float = 1.0
    detail: str = ""

    @property
    def is_special(self) -> bool:
        return self.tier not in ("", "base", "none", "unknown")

    def to_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "label_cn": self.label_cn,
                "source": self.source, "premium_hint": self.premium_hint,
                "is_special": self.is_special, "detail": self.detail}


class PatternTable:
    """规则表：加载 patterns.yaml，并提供档位判定。"""

    def __init__(self, rule_sets: Sequence[SkinRuleSet] | None = None,
                 premium_ratio: float = DEFAULT_PREMIUM_RATIO) -> None:
        self.rule_sets = list(rule_sets or [])
        self.premium_ratio = premium_ratio
        #: 学习得到的「饰品 → {种子: 溢价倍数}」，键用 raw_name
        self.learned: dict[str, dict[int, float]] = {}

    # ── 装配 ───────────────────────────────────────────────

    @classmethod
    def load(cls, path: str | Path = DEFAULT_RULES_PATH) -> PatternTable:
        p = Path(path)
        if not p.exists():
            logger.info("[patterns] 未找到 %s，使用空规则表（可跑 patterns learn 自学）", p)
            return cls()
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        sets = [SkinRuleSet.from_dict(d) for d in (raw.get("rules") or [])]
        table = cls(sets, premium_ratio=float(
            raw.get("premium_ratio") or DEFAULT_PREMIUM_RATIO))
        table.learned = {
            str(k): {int(s): float(v) for s, v in (val or {}).items()}
            for k, val in (raw.get("learned") or {}).items()
        }
        logger.info("[patterns] 载入 %d 组规则、%d 个学习结果",
                    len(sets), len(table.learned))
        return table

    def save(self, path: str | Path = DEFAULT_RULES_PATH) -> None:
        payload = {
            "premium_ratio": self.premium_ratio,
            "rules": [rs.to_dict() for rs in self.rule_sets],
            "learned": {k: {str(s): round(v, 4) for s, v in seeds.items()}
                        for k, seeds in self.learned.items()},
        }
        Path(path).write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8")

    # ── 判定 ───────────────────────────────────────────────

    def rule_set_for(self, market_hash_name: str) -> SkinRuleSet | None:
        for rule_set in self.rule_sets:
            if rule_set.matches_name(market_hash_name):
                return rule_set
        return None

    def classify(self, market_hash_name: str, paint_seed: int | None = None,
                 paint_index: int | None = None) -> PatternMatch:
        """判定档位。优先级：名称规则 > 种子规则 > 学习结果 > 无档位。"""
        # 1) 名称规则（最可靠：名称直接写了）
        rule_set = self.rule_set_for(market_hash_name)
        if rule_set:
            for rule in rule_set.name_rules:
                if rule.match(market_hash_name):
                    return PatternMatch(rule.tier, rule.label_cn, "name_rule",
                                        rule.premium_hint,
                                        f"名称匹配 /{rule.pattern}/")

        # 2) 显式种子规则
        if paint_seed is not None and rule_set:
            for seed_rule in rule_set.seed_rules:
                if seed_rule.matches(paint_seed):
                    return PatternMatch(seed_rule.tier, seed_rule.label_cn, "seed_rule",
                                        seed_rule.premium_hint,
                                        f"种子 {paint_seed} 命中 {seed_rule.tier}")

        # 3) 学习结果（自校准）
        if paint_seed is not None:
            per_skin = self.learned.get(market_hash_name) or {}
            ratio = per_skin.get(paint_seed)
            if ratio is not None and ratio >= self.premium_ratio:
                return PatternMatch(
                    "premium_seed", f"溢价种子 #{paint_seed}", "learned", ratio,
                    f"该种子价格中位数是本品类基准的 {ratio:.2f} 倍")

        return PatternMatch("base", "常规档", "none", 1.0,
                            "未命中特殊档位规则")

    # ── 学习 ───────────────────────────────────────────────

    def learn_single(self, market_hash_name: str,
                     samples: Iterable[tuple[int, float]]) -> dict[int, float]:
        """从 (paint_seed, price) 样本中学习溢价种子。

        做法：先算该饰品的整体价格中位数作为基准，再看每个种子的中位数
        相对基准的倍数。倍数 ≥ premium_ratio 的种子被记为溢价档。

        为什么用中位数而不是均值：单条错标价（比如把红宝石按普通价挂上）
        会把均值带偏，而中位数对这种离群值稳健。
        """
        usable = [(int(s), float(p)) for s, p in samples if p and p > 0]
        if len(usable) < MIN_TOTAL_SAMPLES:
            return {}

        baseline = statistics.median([p for _, p in usable])
        if baseline <= 0:
            return {}

        by_seed: dict[int, list[float]] = {}
        for seed, price in usable:
            by_seed.setdefault(seed, []).append(price)

        ratios: dict[int, float] = {}
        for seed, prices in by_seed.items():
            if len(prices) < MIN_SAMPLES_PER_SEED:
                continue
            ratios[seed] = statistics.median(prices) / baseline

        self.learned[market_hash_name] = ratios
        logger.info("[patterns] %s：学习 %d 个种子（样本 %d，基准 ¥%.2f），"
                    "%d 个达溢价阈值",
                    market_hash_name, len(ratios), len(usable), baseline,
                    sum(1 for r in ratios.values() if r >= self.premium_ratio))
        return ratios

    def premium_seeds(self, market_hash_name: str) -> dict[int, float]:
        """列出该饰品的溢价种子（按溢价倍数降序）。"""
        per_skin = self.learned.get(market_hash_name) or {}
        return {s: r for s, r in sorted(per_skin.items(), key=lambda kv: -kv[1])
                if r >= self.premium_ratio}


# ── 规则文件脚手架 ─────────────────────────────────────────

EXAMPLE_RULES_YAML = """\
# 图案档位规则表
#
# ⚠ 请先读这段再填内容
#
# 多普勒相位、渐变百分比、淬火蓝钢档都由饰品实例的 paint_seed 决定，
# 而**种子对照表是按刀型/枪型分别不同的**。把 M9 多普勒的表套到蝴蝶刀上
# 会得到完全错误的档位判断。因此本文件默认是空的。
#
# 推荐路径：先跑实测学习，让市场自己告诉你哪些种子贵
#     python -m facet patterns learn "★ 蝴蝶刀 | 多普勒 (崭新出厂)"
#     python -m facet patterns show  "★ 蝴蝶刀 | 多普勒 (崭新出厂)"
# 学习结果会写回本文件的 learned 段，并随市场变化自动更新。
#
# 如果你已经从可信来源（官方 API / 实盘核对）拿到确定的种子表，
# 按下面的结构填进 seed_rules，规则优先于学习结果。

premium_ratio: 1.5     # 价格中位数达到基准的多少倍算「溢价档」

rules:
  # 示例：名称里直接写了相位的市场（正则匹配）
  - scope: "Doppler"
    name_rules:
      - pattern: "\\\\bRuby\\\\b"
        tier: ruby
        label_cn: 红宝石
        premium_hint: 8.0
      - pattern: "\\\\bSapphire\\\\b"
        tier: sapphire
        label_cn: 蓝宝石
        premium_hint: 7.0
      - pattern: "Black Pearl"
        tier: black_pearl
        label_cn: 黑珍珠
        premium_hint: 5.0
    # 种子规则格式（确认过再打开）：
    # seed_rules:
    #   - tier: p2
    #     label_cn: 相位 2
    #     premium_hint: 1.3
    #     seed_ranges: [[100, 140], [300, 320]]
    #   - tier: ruby
    #     label_cn: 红宝石
    #     premium_hint: 8.0
    #     seeds: [1, 2, 3]

  - scope: "Case Hardened"
    name_rules: []
    # AK-47 淬火蓝钢这类特殊模板，用 seed 或 seed_ranges 标注
    # seed_rules:
    #   - tier: blue_gem
    #     label_cn: 蓝钢
    #     premium_hint: 20.0
    #     seeds: [661, 670]

  - scope: "Fade"
    name_rules: []
    # 渐变通常按百分比定价，可用 seed_ranges 近似分段
    # seed_rules:
    #   - tier: fade_100
    #     label_cn: 全渐变 100%
    #     premium_hint: 3.0
    #     seed_ranges: [[1, 1]]

  - scope: "Marble Fade"
    name_rules: []
    # 大理石渐变档位（冰火 / 红头 / 蓝尖 / 金）
    # 冰火与红头属于高溢价档，务必用实盘核对后再填

learned: {}
"""


def write_example_rules(path: str | Path = DEFAULT_RULES_PATH,
                        overwrite: bool = False) -> bool:
    """写出脚手架规则文件。已存在时不覆盖（除非 overwrite）。"""
    p = Path(path)
    if p.exists() and not overwrite:
        return False
    p.write_text(EXAMPLE_RULES_YAML, encoding="utf-8")
    return True
