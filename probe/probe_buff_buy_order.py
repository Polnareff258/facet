"""检查 BUFF 匿名可用的 buy_order（求购挂单）结构。

如果这里能取到最高求购价，buff_direct 就能同时提供在售价与求购价 ——
此前只有悠悠有品有求购价。
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept": "application/json",
                  "Referer": "https://buff.163.com/market/csgo"})


def main() -> None:
    out: dict[str, object] = {}
    for goods_id, label in [(43076, "M9 刺刀 | 澄澈之水 (破损不堪)"),
                            (46682, "探针")]:
        print(f"\n=== buy_order goods_id={goods_id}  {label}")
        try:
            resp = S.get("https://buff.163.com/api/market/goods/buy_order",
                         params={"game": "csgo", "goods_id": goods_id, "page_num": 1},
                         timeout=15)
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            print("  失败:", type(exc).__name__, exc)
            continue

        print(f"  code={body.get('code')}  err={body.get('error')}")
        data = body.get("data") or {}
        if isinstance(data, dict):
            print(f"  data keys: {sorted(data.keys())}")
            items = data.get("items") or []
            print(f"  条数 {len(items)}  total_count={data.get('total_count')}")
            if items:
                print("  第一行全部字段：")
                print("   " + json.dumps(items[0], ensure_ascii=False)[:900])
                prices = [it.get("price") for it in items]
                print(f"  价格序列: {prices}")
                print(f"  最高求购价: {max(p for p in prices if p is not None) if any(prices) else None}")
            out[str(goods_id)] = {"code": body.get("code"),
                                  "keys": sorted(data.keys()),
                                  "items": items[:3]}
        else:
            out[str(goods_id)] = {"code": body.get("code"), "data": str(data)[:200]}
        # 也试一下 goods_infos 里有没有求购字段
        ginfos = (data or {}).get("goods_infos") if isinstance(data, dict) else None
        if ginfos:
            print(f"  goods_infos 键: {list(ginfos)[:3]}")

    path = ROOT / "probe" / "buff_buy_order.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {path}")


if __name__ == "__main__":
    main()
