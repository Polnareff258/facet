"""端到端验证：档位级求购价（真实接口）。

跑法：.venv\\Scripts\\python.exe tools\\verify_tier_quotes.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from csmon.config import load_config                      # noqa: E402
from csmon.sources.youpin_direct import YouPinDirectAdapter  # noqa: E402

# 实测确认过的 templateId（来自随包映射资源）
TARGETS = [
    (1681, "★ Bayonet | Doppler (Factory New)", "多普勒相位/宝石"),
    (494, "AK-47 | Case Hardened (Field-Tested)", "淬火 Tier"),
    (45724, "AWP | Fade (Factory New)", "渐变百分比"),
]


def main() -> int:
    config = load_config("config.demo.yaml")
    adapter = YouPinDirectAdapter(config.source("youpin_direct"))
    try:
        for template_id, label, note in TARGETS:
            print(f"\n=== {label}   （{note}）")
            quotes = adapter.fetch_tier_quotes(label, template_id, pages=2)
            if not quotes:
                print("    未取到档位报价（无求购挂单或该品类无档位维度）")
                continue
            print(f"    {'档位':<14} {'最高求购':>12} {'样本数':>6}")
            for quote in sorted(quotes, key=lambda q: -(q.bid_price or 0)):
                print(f"    {str(quote.variant_label):<14} "
                      f"{quote.bid_price:>12.2f} {quote.bid_count:>6}")

            aggregate = adapter._aggregate_from_tiers(label, quotes)
            if aggregate:
                print(f"    {'(整品汇总)':<14} {aggregate.bid_price:>12.2f} "
                      f"{aggregate.bid_count:>6}   ← 档位最高价，仅用于横向对比")
    finally:
        adapter.close()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
