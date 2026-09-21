"""探测 BUFF 商品 ID 空间分布，定位 CS2 饰品的密集区段。

BUFF 的 goods_id 是跨游戏共享的（Dota2/CS2/Rust...）。CS2 适配器要建
market_hash_name -> goods_id 索引，先要知道该扫哪一段。本脚本用二分+抽样
估计 CS2 区段，输出可直接喂给 facet.indexer 的区间配置。
"""
from __future__ import annotations

import json
import sys
import time

import requests

S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://buff.163.com/market/csgo",
})

CS2_MARKERS = ("★", "StatTrak", "Souvenir", "|", "Case", "Sticker", "Graffiti",
               "Music Kit", "Agent", "Charm", "Patch", "Pin")


def probe(good_id: int) -> dict | None:
    try:
        r = S.get("https://buff.163.com/api/market/goods/info",
                  params={"game": "csgo", "goods_id": good_id}, timeout=12)
        data = r.json().get("data") or {}
        mhn = data.get("market_hash_name")
        if not mhn:
            return None
        return {"goods_id": good_id, "mhn": mhn, "name": data.get("name"),
                "appid": data.get("appid"), "game": data.get("game")}
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    lo, hi = 1, int(sys.argv[1]) if len(sys.argv) > 1 else 300000
    step = int(sys.argv[2]) if len(sys.argv) > 2 else 200

    # 1) 粗扫：找出哪些区段存在商品
    segments: list[dict] = []
    print(f"粗扫 {lo}..{hi} step={step}")
    for gid in range(lo, hi + 1, step):
        hit = probe(gid)
        if hit:
            segments.append(hit)
            if len(segments) % 25 == 0:
                print(f"  ...{gid} 命中 {len(segments)}")
        time.sleep(0.12)

    dense = [s for s in segments if s.get("game") == "csgo"]
    dota = [s for s in segments if s.get("game") and s.get("game") != "csgo"]
    max_id = max((s["goods_id"] for s in segments), default=0)

    print(f"\n总命中 {len(segments)}：csgo={len(dense)} 其他={len(dota)} 最大 id={max_id}")
    if dense:
        print(f"csgo 段: {min(s['goods_id'] for s in dense)} .. {max(s['goods_id'] for s in dense)}")
    if dota:
        print(f"非 csgo 段: {min(s['goods_id'] for s in dota)} .. {max(s['goods_id'] for s in dota)}")
    print("样本（csgo）:")
    for s in dense[:15]:
        print(f"  {s['goods_id']:7d}  {s['mhn'][:60]}")

    out = sys.argv[3] if len(sys.argv) > 3 else "probe/buff_id_space.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"max_id": max_id, "csgo": dense, "other": dota}, f,
                  ensure_ascii=False, indent=2)
    print(f"写入 {out}")


if __name__ == "__main__":
    main()
