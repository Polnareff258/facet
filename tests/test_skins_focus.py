"""名称解析、变体分类、关注清单测试。

这些是最容易「看起来对、实则错」的部分：解析错一个前缀，整条监控就盯错了标的，
而价格数字照样在跳，不会报错。所以这里对边界情况覆盖得比较密。
"""
from __future__ import annotations

import json

import pytest

from csmon import focus as focus_mod
from csmon.names import NameResolver, split_cn_base
from csmon.patterns import PatternTable, SeedRule, SkinRuleSet, NameRule
from csmon.skins import (
    Quality,
    WEAR_EN_TO_CN,
    compose_cn,
    expand_variants,
    group_variants,
    parse_name,
)
from csmon.store import Store


# ── 名称解析 ───────────────────────────────────────────────

def test_parse_plain_skin() -> None:
    v = parse_name("AK-47 | Redline (Field-Tested)")
    assert v.weapon == "AK-47"
    assert v.finish == "Redline"
    assert v.wear == "Field-Tested"
    assert v.wear_cn == "久经沙场"
    assert v.quality is Quality.NORMAL
    assert v.is_star is False
    assert v.base == "AK-47 | Redline"


def test_parse_star_knife() -> None:
    v = parse_name("★ M9 Bayonet | Bright Water (Well-Worn)")
    assert v.is_star is True
    assert v.weapon == "M9 Bayonet"
    assert v.finish == "Bright Water"
    assert v.wear == "Well-Worn"
    assert v.to_en_name() == "★ M9 Bayonet | Bright Water (Well-Worn)"


def test_parse_stattrak_with_trademark() -> None:
    v = parse_name("StatTrak™ AK-47 | Redline (Minimal Wear)")
    assert v.quality is Quality.STATTRAK
    assert v.quality.cn == "暗金"
    assert v.weapon == "AK-47"
    assert v.wear == "Minimal Wear"


def test_parse_stattrak_star_combination() -> None:
    """★ 与 StatTrak™ 的组合顺序在真实数据里是固定的，但要能容错。"""
    v = parse_name("★ StatTrak™ Karambit | Doppler (Factory New)")
    assert v.is_star is True
    assert v.quality is Quality.STATTRAK
    assert v.weapon == "Karambit"
    assert v.finish == "Doppler"
    assert v.base == "Karambit | Doppler"


def test_parse_souvenir() -> None:
    v = parse_name("Souvenir AWP | Dragon Lore (Factory New)")
    assert v.quality is Quality.SOUVENIR
    assert v.quality.cn == "纪念品"
    assert v.weapon == "AWP"
    assert v.wear == "Factory New"


def test_parenthetical_at_end_is_not_wear() -> None:
    """关键边界：末尾括号里不是已知磨损时，不能当成磨损切掉。"""
    v = parse_name("Sticker | Titan (Holo)")
    assert v.wear is None
    assert v.parsed is True
    assert any("不是已知磨损" in n for n in v.notes)


def test_parenthetical_in_middle_untouched() -> None:
    """括号在中间（非末尾）时正则根本不该命中，名字要原样保留。"""
    v = parse_name("Sticker | Titan (Holo) | Katowice 2014")
    assert v.wear is None
    assert v.base == "Sticker | Titan (Holo) | Katowice 2014"
    # 不应该报「不是已知磨损」—— 那个提示只对末尾括号有意义
    assert not any("不是已知磨损" in n for n in v.notes)
    assert v.no_wear is True      # 但确实是无磨损档品类


def test_case_hardened_parenthetical_not_wear() -> None:
    v = parse_name("AK-47 | Case Hardened (Battle-Scarred)")
    assert v.wear == "Battle-Scarred"
    assert v.finish == "Case Hardened"


def test_no_wear_category_with_pipe() -> None:
    """带竖线的无磨损品类（Music Kit | ...）首段是 weapon，不是 category。"""
    v = parse_name("Music Kit | AWOLNATION, I Am")
    assert v.wear is None
    assert v.weapon == "Music Kit"
    assert v.finish == "AWOLNATION, I Am"
    assert v.no_wear is True
    assert any("无磨损档" in n for n in v.notes)


def test_no_wear_category_without_pipe() -> None:
    v = parse_name("Graffiti | GTG")
    assert v.no_wear is True or v.weapon == "Graffiti"


def test_skin_is_not_no_wear() -> None:
    assert parse_name("AK-47 | Redline (Field-Tested)").no_wear is False


def test_parse_empty_and_garbage() -> None:
    empty = parse_name("")
    assert empty.parsed is False and empty.raw_name == ""

    weird = parse_name("some random string")
    assert weird.parsed is True          # 解析不了也要保住数据，不能丢
    assert weird.base == "some random string"


def test_all_five_wears_parse() -> None:
    for wear in WEAR_EN_TO_CN:
        v = parse_name(f"AWP | Asiimov ({wear})")
        assert v.wear == wear, wear
        assert v.wear_cn == WEAR_EN_TO_CN[wear]


def test_variant_key_distinguishes_wear_and_quality() -> None:
    base = "AK-47 | Redline"
    keys = {
        parse_name(f"{base} (Field-Tested)").variant_key,
        parse_name(f"{base} (Minimal Wear)").variant_key,
        parse_name(f"StatTrak™ {base} (Field-Tested)").variant_key,
    }
    assert len(keys) == 3, "磨损与品质必须产生不同的变体键"


def test_variant_rich_detection() -> None:
    assert parse_name("★ Karambit | Doppler (Factory New)").is_variant_rich
    assert parse_name("AK-47 | Case Hardened (Field-Tested)").is_variant_rich
    assert parse_name("AWP | Asiimov (Field-Tested)").is_variant_rich is False


def test_wear_rank_ordering() -> None:
    ranks = [parse_name(f"X | Y ({w})").wear_rank for w in WEAR_EN_TO_CN]
    assert ranks == [1, 2, 3, 4, 5]


# ── 变体展开 ───────────────────────────────────────────────

def test_expand_all_wears() -> None:
    names = expand_variants("AK-47 | Redline")
    assert len(names) == 5
    assert "AK-47 | Redline (Field-Tested)" in names


def test_expand_selected_wears_only() -> None:
    names = expand_variants("AK-47 | Redline",
                            wears=["Field-Tested", "Minimal Wear"])
    assert sorted(names) == sorted(["AK-47 | Redline (Field-Tested)",
                                    "AK-47 | Redline (Minimal Wear)"])


def test_expand_preserves_star_and_quality() -> None:
    names = expand_variants("★ Karambit | Doppler", wears=["Factory New"],
                            qualities=[Quality.STATTRAK])
    assert names == ["★ StatTrak™ Karambit | Doppler (Factory New)"]


def test_expand_avoids_double_prefix() -> None:
    """传入已带前缀/磨损的名字时，不能展开成「★ ★ ...」。"""
    names = expand_variants("★ Karambit | Doppler (Factory New)",
                            wears=["Factory New"])
    assert names == ["★ Karambit | Doppler (Factory New)"]
    assert not any("★ ★" in n for n in names)


def test_expand_no_wear_category() -> None:
    """贴纸/音乐盒没有磨损档，不该被展开成 5 份。"""
    assert expand_variants("Sticker | Titan") == ["Sticker | Titan"]
    assert expand_variants("Music Kit | AWOLNATION, I Am") == [
        "Music Kit | AWOLNATION, I Am"]


# ── 分组 ───────────────────────────────────────────────────

def test_group_variants_tree() -> None:
    names = [
        "AK-47 | Redline (Field-Tested)",
        "AK-47 | Redline (Minimal Wear)",
        "StatTrak™ AK-47 | Redline (Field-Tested)",
        "AWP | Asiimov (Field-Tested)",
    ]
    groups = group_variants(names)
    assert len(groups) == 2

    redline = next(g for g in groups if g.base == "AK-47 | Redline")
    assert len(redline.variants) == 3
    assert set(redline.qualities) == {"normal", "stattrak"}
    assert redline.wears == ["Minimal Wear", "Field-Tested"]   # 按磨损顺序
    payload = redline.to_dict()
    assert payload["wears_cn"] == ["略有磨损", "久经沙场"]


def test_group_star_sorted_last() -> None:
    groups = group_variants(["★ Karambit | Doppler (Factory New)",
                             "AK-47 | Redline (Field-Tested)"])
    assert groups[-1].is_star is True


# ── 中文名 ─────────────────────────────────────────────────

def test_split_cn_base() -> None:
    base, wear = split_cn_base("AK-47 | 红线 (久经沙场)")
    assert base == "AK-47 | 红线"
    assert wear == "久经沙场"


def test_split_cn_base_keeps_non_wear_parenthetical() -> None:
    """'(★)' 不是磨损，不能切掉。"""
    base, wear = split_cn_base("M9 刺刀（★） | 澄澈之水")
    assert wear is None
    assert base == "M9 刺刀（★） | 澄澈之水"


def test_resolver_prefers_learned_name(store: Store) -> None:
    resolver = NameResolver(store)
    resolver.learn([("AK-47 | Redline (Field-Tested)",
                     "AK-47 | 红线 (久经沙场)")], source="quote_raw")
    assert resolver.resolve("AK-47 | Redline (Field-Tested)") == "AK-47 | 红线 (久经沙场)"


def test_resolver_reuses_sibling_wear(store: Store) -> None:
    """学过久经沙场后，同皮肤的崭新出厂也应显示中文（磨损只有 5 档，皮肤上万）。"""
    resolver = NameResolver(store)
    resolver.learn([("AK-47 | Redline (Field-Tested)",
                     "AK-47 | 红线 (久经沙场)")], source="quote_raw")
    out = resolver.resolve("AK-47 | Redline (Factory New)")
    assert "崭新出厂" in out
    assert "红线" in out


def test_resolver_falls_back_to_compose(store: Store) -> None:
    resolver = NameResolver(store)
    out = resolver.resolve("AK-47 | Redline (Field-Tested)")
    assert "久经沙场" in out          # 拼装路径至少要把磨损翻对


def test_manual_name_beats_auto(store: Store) -> None:
    """手工录入优先级最高，不会被自动收录覆盖。"""
    resolver = NameResolver(store)
    resolver.learn([("X | Y (Field-Tested)", "自动名 (久经沙场)")], source="quote_raw")
    resolver.learn([("X | Y (Field-Tested)", "手工名 (久经沙场)")], source="manual")
    assert resolver.resolve("X | Y (Field-Tested)") == "手工名 (久经沙场)"

    # 反向：自动收录不得覆盖手工名
    resolver.learn([("X | Y (Field-Tested)", "又一个自动名 (久经沙场)")],
                   source="quote_raw")
    assert resolver.resolve("X | Y (Field-Tested)") == "手工名 (久经沙场)"


def test_compose_cn_with_prefixes() -> None:
    v = parse_name("★ StatTrak™ Karambit | Doppler (Factory New)")
    out = compose_cn(v, base_cn="爪子刀（★） | 多普勒")
    assert "暗金" in out and "崭新出厂" in out


def test_resolve_with_meta_reports_source(store: Store) -> None:
    resolver = NameResolver(store)
    assert resolver.resolve_with_meta("A | B (Field-Tested)")["source"] == "composed"
    resolver.learn([("A | B (Field-Tested)", "甲 | 乙 (久经沙场)")], source="quote_raw")
    assert resolver.resolve_with_meta("A | B (Field-Tested)")["source"] == "learned"


# ── 关注清单 ───────────────────────────────────────────────

def test_add_focus_expands_base_name(store: Store) -> None:
    names = focus_mod.add_focus(store, "AK-47 | Redline", intent="buy",
                                target_price=95.0, wears="FT,MW")
    assert sorted(names) == sorted(["AK-47 | Redline (Field-Tested)",
                                    "AK-47 | Redline (Minimal Wear)"])
    rows = store.list_focus()
    assert len(rows) == 2
    assert all(r["intent"] == "buy" for r in rows)
    assert all(r["target_price"] == 95.0 for r in rows)


def test_add_focus_full_name_not_expanded(store: Store) -> None:
    names = focus_mod.add_focus(store, "AK-47 | Redline (Field-Tested)", intent="sell")
    assert names == ["AK-47 | Redline (Field-Tested)"]


def test_wear_filter_parsing() -> None:
    assert focus_mod.resolve_wear_filter("FT,MW") == ["Field-Tested", "Minimal Wear"]
    assert focus_mod.resolve_wear_filter("崭新,略磨") == ["Factory New", "Minimal Wear"]
    assert focus_mod.resolve_wear_filter("FN") == ["Factory New"]
    assert focus_mod.resolve_wear_filter("胡说") is None
    assert focus_mod.resolve_wear_filter(None) is None


def test_quality_filter_parsing() -> None:
    from csmon.skins import Quality

    assert focus_mod.resolve_quality_filter("暗金") == [Quality.STATTRAK]
    assert focus_mod.resolve_quality_filter("普通,纪念品") == [
        Quality.NORMAL, Quality.SOUVENIR]


def test_status_buy_ready_and_waiting(store: Store) -> None:
    focus_mod.add_focus(store, "X | Y (Field-Tested)", intent="buy", target_price=100.0)

    from csmon.models import SourceQuote

    # 现价 95 ≤ 目标 100 → 达到买点
    store.insert_quotes([SourceQuote(market_hash_name="X | Y (Field-Tested)",
                                     platform="BUFF", source="t", sell_price=95.0)])
    board = focus_mod.build_board(store)
    assert board.buy[0].status == "ready"
    assert board.actionable == 1

    # 现价 130 → 等待回落
    store.insert_quotes([SourceQuote(market_hash_name="X | Y (Field-Tested)",
                                     platform="BUFF", source="t", sell_price=130.0)])
    board = focus_mod.build_board(store)
    assert board.buy[0].status == "waiting"


def test_status_sell_ready(store: Store) -> None:
    focus_mod.add_focus(store, "X | Y (Field-Tested)", intent="sell", target_price=200.0)
    from csmon.models import SourceQuote

    store.insert_quotes([SourceQuote(market_hash_name="X | Y (Field-Tested)",
                                     platform="BUFF", source="t",
                                     sell_price=210.0, bid_price=205.0)])
    board = focus_mod.build_board(store)
    assert board.sell[0].status == "ready"


def test_status_no_data(store: Store) -> None:
    focus_mod.add_focus(store, "X | Y (Field-Tested)", intent="buy", target_price=100.0)
    board = focus_mod.build_board(store)
    assert board.buy[0].status == "no_data"
    assert board.actionable == 0


def test_board_groups_by_intent(store: Store) -> None:
    focus_mod.add_focus(store, "A | B (Field-Tested)", intent="buy")
    focus_mod.add_focus(store, "C | D (Field-Tested)", intent="sell")
    focus_mod.add_focus(store, "E | F (Field-Tested)", intent="watch")
    payload = focus_mod.build_board(store).to_dict()
    assert payload["counts"] == {"buy": 1, "sell": 1, "watch": 1}


def test_focus_remove(store: Store) -> None:
    focus_mod.add_focus(store, "A | B (Field-Tested)", intent="buy")
    assert store.remove_focus("A | B (Field-Tested)") is True
    assert store.list_focus() == []


def test_describe_filters_renders_chinese(store: Store) -> None:
    focus_mod.add_focus(store, "A | B", intent="buy", wears="FT,MW",
                        qualities="暗金")
    row = store.list_focus()[0]
    text = focus_mod.describe_filters(row)
    assert "磨损" in text and "久经沙场" in text
    assert "暗金" in text


# ── 图案档位 ───────────────────────────────────────────────

def test_pattern_name_rule_matches() -> None:
    table = PatternTable([SkinRuleSet(
        scope="Doppler",
        name_rules=[NameRule(r"\bRuby\b", "ruby", "红宝石", 8.0)],
    )])
    match = table.classify("★ Karambit | Doppler Ruby (Factory New)")
    assert match.is_special
    assert match.tier == "ruby" and match.label_cn == "红宝石"
    assert match.source == "name_rule"


def test_pattern_seed_rule_matches() -> None:
    table = PatternTable([SkinRuleSet(
        scope="Case Hardened",
        seed_rules=[SeedRule(tier="blue_gem", label_cn="蓝钢", premium_hint=20.0,
                             seeds=(661, 670))],
    )])
    hit = table.classify("AK-47 | Case Hardened (Field-Tested)", paint_seed=661)
    assert hit.tier == "blue_gem" and hit.source == "seed_rule"

    miss = table.classify("AK-47 | Case Hardened (Field-Tested)", paint_seed=100)
    assert miss.is_special is False


def test_pattern_seed_range_matches() -> None:
    rule = SeedRule(tier="p2", label_cn="相位 2", seed_ranges=((100, 140),))
    assert rule.matches(120) is True
    assert rule.matches(141) is False


def test_pattern_learns_premium_seed() -> None:
    """核心能力：不靠外部种子表，从实盘价格反推哪些种子贵。"""
    table = PatternTable()
    # 20 条常规样本（价格 ~100）+ 3 条高价样本（种子 42，价格 500）
    samples = [(i, 100.0 + (i % 5)) for i in range(20)]
    samples += [(42, 500.0), (42, 520.0), (42, 480.0)]

    ratios = table.learn_single("★ Karambit | Doppler (Factory New)", samples)
    assert ratios[42] > 4.0

    premium = table.premium_seeds("★ Karambit | Doppler (Factory New)")
    assert 42 in premium

    match = table.classify("★ Karambit | Doppler (Factory New)", paint_seed=42)
    assert match.is_special and match.source == "learned"


def test_pattern_learn_needs_enough_samples() -> None:
    table = PatternTable()
    assert table.learn_single("X", [(1, 100.0), (2, 200.0)]) == {}


def test_pattern_learned_not_premium_when_flat() -> None:
    """价格没有分层时不该凭空造出档位。"""
    table = PatternTable()
    samples = [(i % 5, 100.0 + (i % 3)) for i in range(40)]
    table.learn_single("X", samples)
    assert table.premium_seeds("X") == {}


def test_pattern_save_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "patterns.yaml"
    table = PatternTable([SkinRuleSet(
        scope="Fade",
        seed_rules=[SeedRule(tier="fade100", label_cn="全渐变", seeds=(1,))],
    )])
    table.learnt = None
    table.learned = {"X | Y (FN)": {7: 3.2}}
    table.save(path)

    loaded = PatternTable.load(path)
    assert len(loaded.rule_sets) == 1
    assert loaded.learned["X | Y (FN)"][7] == pytest.approx(3.2)
    assert loaded.classify("A | Fade (Factory New)", paint_seed=1).tier == "fade100"


def test_pattern_load_missing_file_is_empty(tmp_path) -> None:
    table = PatternTable.load(tmp_path / "nope.yaml")
    assert table.rule_sets == []
    assert table.classify("anything").is_special is False
