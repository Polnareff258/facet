"""档位词表测试：从平台原始行提取档位、剔除占位值、落库查询、档位级报价。

样本取自实测响应（AK-47 淬火 T1/T2、刺刀多普勒六个相位、AWP 渐变区间）。
"""
from __future__ import annotations

from csmon import variants as variants_mod
from csmon.config import SourceConfig
from csmon.models import ItemRef, SourceQuote
from csmon.ratelimit import GateRegistry
from csmon.sources.youpin_direct import YouPinDirectAdapter, _tier_label
from csmon.store import Store


# 实测样本：字段名与取值都来自真实响应
CASE_HARDENED_ROWS = [
    {"commodityName": "AK-47 | 表面淬火 (久经沙场)", "templateId": 494,
     "purchasePrice": 6880.0, "specialStyle": "T1", "fadeText": None,
     "abradeText": "不限"},
    {"commodityName": "AK-47 | 表面淬火 (久经沙场)", "templateId": 494,
     "purchasePrice": 5200.0, "specialStyle": "T1", "fadeText": None,
     "abradeText": "0.15-0.18"},
    {"commodityName": "AK-47 | 表面淬火 (久经沙场)", "templateId": 494,
     "purchasePrice": 3100.0, "specialStyle": "T2", "fadeText": None,
     "abradeText": "不限"},
    {"commodityName": "AK-47 | 表面淬火 (久经沙场)", "templateId": 494,
     "purchasePrice": 2500.0, "specialStyle": "不限", "fadeText": None,
     "abradeText": "不限"},
]

DOPPLER_ROWS = [
    {"commodityName": "刺刀（★） | 多普勒 (崭新出厂)", "templateId": 1681,
     "purchasePrice": 9800.0, "specialStyle": "红宝石", "fadeText": None,
     "abradeText": "0-0.01"},
    {"commodityName": "刺刀（★） | 多普勒 (崭新出厂)", "templateId": 1681,
     "purchasePrice": 7600.0, "specialStyle": "蓝宝石", "fadeText": None,
     "abradeText": "0-0.02"},
    {"commodityName": "刺刀（★） | 多普勒 (崭新出厂)", "templateId": 1681,
     "purchasePrice": 2400.0, "specialStyle": "P2", "fadeText": None,
     "abradeText": "不限"},
    {"commodityName": "刺刀（★） | 多普勒 (崭新出厂)", "templateId": 1681,
     "purchasePrice": 2300.0, "specialStyle": "P2", "fadeText": None,
     "abradeText": "0-0.04"},
    {"commodityName": "刺刀（★） | 多普勒 (崭新出厂)", "templateId": 1681,
     "purchasePrice": 2100.0, "specialStyle": "P4", "fadeText": None,
     "abradeText": "不限"},
]

FADE_ROWS = [
    {"commodityName": "AWP | 渐变之色 (崭新出厂)", "templateId": 45724,
     "purchasePrice": 9000.0, "specialStyle": None, "fadeText": "99-100",
     "abradeText": "不限"},
    {"commodityName": "AWP | 渐变之色 (崭新出厂)", "templateId": 45724,
     "purchasePrice": 7000.0, "specialStyle": None, "fadeText": "97-99",
     "abradeText": "不限"},
    {"commodityName": "AWP | 渐变之色 (崭新出厂)", "templateId": 45724,
     "purchasePrice": 4000.0, "specialStyle": None, "fadeText": "0-100",
     "abradeText": "不限"},
]


# ── 提取 ───────────────────────────────────────────────────

def test_extract_styles_from_case_hardened() -> None:
    vocab = variants_mod.extract_vocab(CASE_HARDENED_ROWS,
                                       "AK-47 | Case Hardened (Field-Tested)")
    assert vocab.styles == ["T1", "T2"]          # 按样本数降序：T1 有 2 条
    assert "不限" not in vocab.styles            # 占位值必须剔除
    assert vocab.cn_name == "AK-47 | 表面淬火 (久经沙场)"
    assert vocab.template_id == 494
    assert vocab.sample_rows == 4
    assert vocab.counts["style"]["T1"] == 2


def test_extract_all_doppler_phases() -> None:
    vocab = variants_mod.extract_vocab(DOPPLER_ROWS,
                                       "★ Bayonet | Doppler (Factory New)")
    assert set(vocab.styles) == {"红宝石", "蓝宝石", "P2", "P4"}
    assert vocab.styles[0] == "P2"               # 样本最多（2 条）
    assert vocab.has_variants is True


def test_extract_fade_ranges_exclude_unbounded() -> None:
    """「0-100」表示买家不限渐变，是占位值不是档位。"""
    vocab = variants_mod.extract_vocab(FADE_ROWS, "AWP | Fade (Factory New)")
    assert set(vocab.fades) == {"99-100", "97-99"}
    assert "0-100" not in vocab.fades
    assert vocab.has_variants is True


def test_extract_abrade_ranges() -> None:
    vocab = variants_mod.extract_vocab(DOPPLER_ROWS, "X")
    assert "0-0.01" in vocab.abrades
    assert "不限" not in vocab.abrades


def test_extract_empty_rows() -> None:
    vocab = variants_mod.extract_vocab([], "X")
    assert vocab.sample_rows == 0
    assert vocab.has_variants is False
    assert vocab.terms == {}


def test_extract_ignores_unrelated_fields() -> None:
    vocab = variants_mod.extract_vocab(
        [{"commodityName": "X", "purchasePrice": 1.0, "userName": "someone"}], "X")
    assert vocab.terms == {}


# ── 落库与查询 ─────────────────────────────────────────────

def test_save_and_load_vocab(store: Store) -> None:
    vocab = variants_mod.extract_vocab(DOPPLER_ROWS,
                                       "★ Bayonet | Doppler (Factory New)")
    written = variants_mod.save_vocab(store, vocab)
    assert written >= 4

    loaded = variants_mod.load_vocab(store, "★ Bayonet | Doppler (Factory New)")
    assert set(loaded.styles) == {"红宝石", "蓝宝石", "P2", "P4"}
    assert loaded.has_variants is True


def test_save_vocab_accumulates_samples(store: Store) -> None:
    """多轮采集应累加样本数，而不是覆盖 —— 冷门档位要多拉几页才出现。"""
    name = "★ Bayonet | Doppler (Factory New)"
    variants_mod.save_vocab(store, variants_mod.extract_vocab(DOPPLER_ROWS, name))
    first = {r["value"]: r["sample_count"]
             for r in store.variant_terms_for(name, "style")}

    variants_mod.save_vocab(store, variants_mod.extract_vocab(DOPPLER_ROWS, name))
    second = {r["value"]: r["sample_count"]
              for r in store.variant_terms_for(name, "style")}
    assert second["P2"] == first["P2"] * 2


def test_save_vocab_learns_chinese_name(store: Store) -> None:
    """词表里带官方中文名，顺便收录（这本来就是权威来源）。"""
    from csmon.names import NameResolver

    variants_mod.save_vocab(store, variants_mod.extract_vocab(
        DOPPLER_ROWS, "★ Bayonet | Doppler (Factory New)"))
    assert NameResolver(store).resolve("★ Bayonet | Doppler (Factory New)") == \
        "刺刀（★） | 多普勒 (崭新出厂)"


def test_vocab_stats(store: Store) -> None:
    variants_mod.save_vocab(store, variants_mod.extract_vocab(DOPPLER_ROWS, "A"))
    variants_mod.save_vocab(store, variants_mod.extract_vocab(FADE_ROWS, "B"))
    stats = store.variant_vocab_stats()
    assert stats["items_with_vocab"] == 2
    assert stats["by_kind"]["style"]["items"] == 1
    assert stats["by_kind"]["fade"]["items"] == 1


# ── 档位标签 ───────────────────────────────────────────────

def test_tier_label_prefers_style() -> None:
    assert _tier_label({"specialStyle": "红宝石"}) == "红宝石"
    assert _tier_label({"specialStyle": "T1", "fadeText": "99-100"}) == "T1"


def test_tier_label_falls_back_to_fade() -> None:
    assert _tier_label({"specialStyle": None, "fadeText": "99-100"}) == "渐变 99-100"
    assert _tier_label({"specialStyle": "不限", "fadeText": "97-99"}) == "渐变 97-99"


def test_tier_label_placeholders_yield_none() -> None:
    """「不限」不是档位 —— 把它当成一个档位会让统计多出一档。"""
    assert _tier_label({"specialStyle": "不限"}) is None
    assert _tier_label({"specialStyle": None, "fadeText": None}) is None
    assert _tier_label({"specialStyle": None, "fadeText": "0-100"}) is None
    assert _tier_label({}) is None


# ── 档位级报价 ─────────────────────────────────────────────

class _FakeAdapter(YouPinDirectAdapter):
    """覆写取数，验证档位聚合逻辑而不联网。"""

    def __init__(self, rows):
        super().__init__(SourceConfig(name="youpin_direct", min_interval=0.0),
                         GateRegistry())
        self._rows = rows

    def fetch_purchase_rows(self, template_id, pages=3, page_size=100):
        return list(self._rows)


def test_tier_quotes_split_by_phase() -> None:
    adapter = _FakeAdapter(DOPPLER_ROWS)
    quotes = adapter.fetch_tier_quotes("★ Bayonet | Doppler (Factory New)", 1681)

    by_tier = {q.variant_label: q for q in quotes}
    assert set(by_tier) == {"红宝石", "蓝宝石", "P2", "P4"}
    assert by_tier["红宝石"].bid_price == 9800.0
    assert by_tier["P2"].bid_price == 2400.0     # 该档最高价，不是全部最高价
    assert all(q.sell_price is None for q in quotes)
    assert all(q.platform == "YOUPIN" for q in quotes)


def test_tier_quotes_aggregate_excludes_tiers() -> None:
    """汇总口径的 bid_price 为 None（不设整品价），避免与档位价混淆。"""
    adapter = _FakeAdapter(DOPPLER_ROWS)
    aggregate = adapter._aggregate_from_tiers(
        "X", adapter.fetch_tier_quotes("X", 1681))
    assert aggregate is not None
    assert aggregate.variant_label is None
    assert aggregate.bid_price == 9800.0         # 档位中最高
    assert aggregate.raw["best_tier"] == "红宝石"


def test_tier_quotes_fallback_when_no_tier_info() -> None:
    """没有档位标注的普通品类不应产出档位（否则报价会重复计数）。"""
    plain = [{"commodityName": "AK-47 | 红线 (久经沙场)", "templateId": 1,
              "purchasePrice": 100.0, "specialStyle": "不限", "abradeText": "不限"}]
    adapter = _FakeAdapter(plain)
    assert adapter.fetch_tier_quotes("AK-47 | Redline (Field-Tested)", 1) == []


def test_store_variant_quotes_isolated_from_aggregate(store: Store) -> None:
    """档位报价与整品报价必须隔离存取，否则红宝石价会被当成整刀均价。"""
    name = "★ Bayonet | Doppler (Factory New)"
    store.insert_quotes([
        SourceQuote(market_hash_name=name, platform="YOUPIN", source="youpin_direct",
                    bid_price=9800.0, variant_label="红宝石"),
        SourceQuote(market_hash_name=name, platform="YOUPIN", source="youpin_direct",
                    bid_price=2400.0, variant_label="P2"),
        SourceQuote(market_hash_name=name, platform="YOUPIN", source="youpin_direct",
                    bid_price=9800.0, variant_label=None),
    ])

    tiers = {r["variant_label"]: r for r in store.variant_quotes(name)}
    assert set(tiers) == {"红宝石", "P2"}
    assert tiers["红宝石"]["bid_price"] == 9800.0

    aggregate = store.latest_quote(name, "YOUPIN")
    assert aggregate is not None and aggregate["variant_label"] is None

    by_platform = store.latest_by_platform(name)
    assert by_platform["YOUPIN"]["variant_label"] is None
    assert store.variant_labels(name) == ["P2", "红宝石"]


def test_variant_quotes_filter_by_platform(store: Store) -> None:
    """不限平台时必须两条都返回 —— 用档位名做键会让两个平台互相覆盖。"""
    name = "X"
    store.insert_quotes([
        SourceQuote(market_hash_name=name, platform="YOUPIN", source="s",
                    bid_price=1.0, variant_label="T1"),
        SourceQuote(market_hash_name=name, platform="BUFF", source="s",
                    bid_price=2.0, variant_label="T1"),
    ])
    both = store.variant_quotes(name)
    assert len(both) == 2
    assert {r["platform"] for r in both} == {"BUFF", "YOUPIN"}

    only_buff = store.variant_quotes(name, platform="BUFF")
    assert len(only_buff) == 1 and only_buff[0]["bid_price"] == 2.0


# ── 词表 → 规则 ────────────────────────────────────────────

def test_vocab_to_pattern_rules() -> None:
    vocab = variants_mod.extract_vocab(DOPPLER_ROWS,
                                       "★ Bayonet | Doppler (Factory New)")
    rules = variants_mod.vocab_to_pattern_rules(vocab)
    labels = {r["label_cn"] for r in rules}
    assert {"红宝石", "蓝宝石", "P2", "P4"} <= labels
    assert all(r["pattern"] for r in rules)


def test_vocab_to_pattern_rules_fade() -> None:
    vocab = variants_mod.extract_vocab(FADE_ROWS, "AWP | Fade (Factory New)")
    rules = variants_mod.vocab_to_pattern_rules(vocab)
    assert any("渐变" in r["label_cn"] for r in rules)
