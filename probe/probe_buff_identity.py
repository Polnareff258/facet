"""关键验证：CSQAQ 的 goodId 是否等于 BUFF 的 goods_id，以及 BUFF 匿名挂单排序语义。

若成立，则 CSQAQ 同时提供：BUFF+悠悠有品在售价 + BUFF goods_id 映射，
BUFF 直连适配器即可零登录运行（只用公开的 market/goods/info 与 sell_order）。
"""
from __future__ import annotations

import json

import requests

BUFF = "https://buff.163.com"
S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://buff.163.com/market/csgo",
})

# CSQAQ 文档示例里的 goodId
CSQAQ_SAMPLES = [
    (6733, "★ Bowie Knife", "鲍伊猎刀（★）"),
    (7189, "★ Huntsman Knife | Tiger Tooth (Factory New)", "猎杀者匕首（★） | 虎牙 (崭新出厂)"),
    (301, "AWP | Snake Camo (Factory New)", "AWP | 蝮蛇迷彩 (崭新出厂)"),
]

print("=" * 78)
print("假设验证：CSQAQ goodId == BUFF goods_id ?")
print("=" * 78)
hits = 0
for good_id, expected_mhn, expected_cn in CSQAQ_SAMPLES:
    try:
        r = S.get(f"{BUFF}/api/market/goods/info",
                  params={"game": "csgo", "goods_id": good_id}, timeout=15)
        payload = r.json()
        data = payload.get("data") or {}
        mhn = data.get("market_hash_name")
        name = data.get("name")
        match = (mhn == expected_mhn)
        hits += 1 if match else 0
        print(f"[{'MATCH' if match else 'MISS '}] goods_id={good_id}")
        print(f"        BUFF mhn  = {mhn}")
        print(f"        CSQAQ mhn = {expected_mhn}")
        print(f"        BUFF name = {name}   | CSQAQ name = {expected_cn}")
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR  ] goods_id={good_id} {type(exc).__name__}: {exc}")
print(f"\n命中 {hits}/{len(CSQAQ_SAMPLES)}")

print()
print("=" * 78)
print("BUFF 匿名 sell_order 语义（排序 / 价格字段 / 字段完整度）")
print("=" * 78)
for good_id in (43076, 6733):
    for sort_by in ("default", "price.asc"):
        try:
            r = S.get(f"{BUFF}/api/market/goods/sell_order",
                      params={"game": "csgo", "goods_id": good_id, "page_num": 1,
                              "sort_by": sort_by}, timeout=15)
            j = r.json()
            d = j.get("data") or {}
            items = d.get("items") or []
            prices = [it.get("price") for it in items]
            print(f"goods_id={good_id} sort_by={sort_by:10s} code={j.get('code')} "
                  f"total_count={d.get('total_count')} prices={prices}")
        except Exception as exc:  # noqa: BLE001
            print(f"goods_id={good_id} sort_by={sort_by} ERR {exc}")
    print("-" * 60)

print()
print("=" * 78)
print("BUFF goods_id 枚举可行性：范围抽样命中率（用于建 mhn→goods_id 索引）")
print("=" * 78)
sample_ids = [1, 2, 10, 100, 500, 1000, 5000, 10000, 50000, 100000, 200000, 500000]
ok = 0
for gid in sample_ids:
    try:
        r = S.get(f"{BUFF}/api/market/goods/info",
                  params={"game": "csgo", "goods_id": gid}, timeout=12)
        d = (r.json().get("data") or {})
        mhn = d.get("market_hash_name")
        if mhn:
            ok += 1
        print(f"  goods_id={gid:7d} -> {str(mhn)[:56]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  goods_id={gid:7d} -> ERR {type(exc).__name__}")
print(f"\n有效命中 {ok}/{len(sample_ids)}")
