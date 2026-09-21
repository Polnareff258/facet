"""探测清单项里是否带得出「变体指纹」（paint_seed / paint_index / 磨损值）。

为什么重要：Steam 的 market_hash_name **不编码**多普勒相位、渐变百分比、
淬火蓝钢档位 —— 这些由饰品实例的 paint_seed 决定。如果清单接口能拿到
paint_seed，就能在本地建立「实例 → 变体档位」的映射，而不必依赖
别人发表的种子表（那些表按刀型不同，抄错会直接导致错误交易建议）。
"""
from __future__ import annotations

import json
import time

import requests

BUFF = "https://buff.163.com"
S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://buff.163.com/market/csgo",
})

# 多普勒系、渐变系、淬火系 —— 变体区分最关键的几类
CANDIDATES = [
    (43076, "M9 刺刀（★） | 澄澈之水 (破损不堪)", "已知可读"),
    (44001, "多普勒系（探针）", "尝试读 paintseed"),
]


def probe_sell_order(goods_id: int) -> dict | None:
    try:
        resp = S.get(f"{BUFF}/api/market/goods/sell_order",
                     params={"game": "csgo", "goods_id": goods_id, "page_num": 1},
                     timeout=15)
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"goods_id": goods_id, "error": f"{type(exc).__name__}: {exc}"}

    if body.get("code") != "OK":
        return {"goods_id": goods_id, "code": body.get("code"), "error": body.get("error")}

    data = body.get("data") or {}
    items = data.get("items") or []
    out = {"goods_id": goods_id, "code": "OK", "count": len(items), "items": []}
    for it in items[:3]:
        asset = it.get("asset_info") or {}
        info = asset.get("info") or {}
        out["items"].append({
            "price": it.get("price"),
            "paintwear": asset.get("paintwear"),
            "paintseed": info.get("paintseed"),
            "paintindex": info.get("paintindex"),
            "quality": (info.get("tags") or {}).get("quality", {}).get("internal_name"),
            "rarity": (info.get("tags") or {}).get("rarity", {}).get("internal_name"),
            "name_tag": info.get("fraudwarnings"),
            "has_stickers": len(info.get("stickers") or []),
            "keychains": len(info.get("keychains") or []),
        })
    return out


def main() -> None:
    results = []
    for goods_id, label, note in CANDIDATES:
        print(f"\n=== goods_id={goods_id}  {label}  ({note}) ===")
        result = probe_sell_order(goods_id)
        results.append({"goods_id": goods_id, "label": label, "result": result})
        print(json.dumps(result, ensure_ascii=False, indent=2)[:1400])
        time.sleep(1.5)

    with open("probe/variant_fingerprint.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    print("\n写入 probe/variant_fingerprint.json")

    # 结论汇总
    print("\n" + "=" * 70)
    for entry in results:
        res = entry["result"] or {}
        if res.get("code") == "OK":
            fields = set()
            for item in res.get("items", []):
                fields |= {k for k, v in item.items() if v is not None}
            print(f"  goods_id={entry['goods_id']:<8} 可读；可用变体字段：{sorted(fields)}")
        else:
            print(f"  goods_id={entry['goods_id']:<8} 不可读：{res.get('code')} {res.get('error')}")


if __name__ == "__main__":
    main()
