"""多源可达性探测：BUFF 匿名面 / 悠悠有品在售价通道 / 加密库可用性。

只做只读探测，不写任何数据。用于选定 facet 的默认适配器组合。
"""
from __future__ import annotations

import json
import random
import string
import sys
import time

import requests

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
UA_OKHTTP = "okhttp/3.14.9"

results: dict[str, dict] = {}


def record(name: str, ok: bool, detail: str = "", payload=None) -> None:
    results[name] = {"ok": ok, "detail": detail, "payload": payload}
    print(f"[{'OK  ' if ok else 'FAIL'}] {name} :: {detail[:220]}")


def probe_crypto() -> None:
    for mod in ("Crypto", "cryptography"):
        try:
            __import__(mod)
            record(f"crypto:{mod}", True, "available")
        except Exception as e:  # noqa: BLE001
            record(f"crypto:{mod}", False, type(e).__name__)


def probe_buff() -> None:
    s = requests.Session()
    s.headers.update({"User-Agent": UA_BROWSER, "Accept": "application/json",
                      "Referer": "https://buff.163.com/market/csgo"})

    # 1) 在售挂单（匿名）
    try:
        r = s.get("https://buff.163.com/api/market/goods/sell_order",
                  params={"game": "csgo", "goods_id": 43076, "page_num": 1}, timeout=15)
        j = r.json()
        items = (j.get("data") or {}).get("items") or []
        first = items[0] if items else {}
        record("buff.sell_order.anon", j.get("code") == "OK",
               f"HTTP {r.status_code} code={j.get('code')} items={len(items)} keys={sorted(first.keys())[:12]}")
        if items:
            print("     first item sample:", json.dumps(first, ensure_ascii=False)[:600])
    except Exception as e:  # noqa: BLE001
        record("buff.sell_order.anon", False, f"{type(e).__name__}: {e}")

    # 2) 商品列表（匿名）
    try:
        r = s.get("https://buff.163.com/api/market/goods",
                  params={"game": "csgo", "page_num": 1, "page_size": 3}, timeout=15)
        j = r.json()
        record("buff.goods_list.anon", j.get("code") == "OK", f"code={j.get('code')} err={j.get('error')}")
    except Exception as e:  # noqa: BLE001
        record("buff.goods_list.anon", False, f"{type(e).__name__}: {e}")

    # 3) 站内搜索（匿名）
    for name, url, params in [
        ("buff.search.anon", "https://buff.163.com/api/market/goods",
         {"game": "csgo", "page_num": 1, "page_size": 3, "search": "AK-47 | Redline"}),
        ("buff.goods_detail.anon", "https://buff.163.com/api/market/goods/detail",
         {"game": "csgo", "goods_id": 43076}),
        ("buff.goods_infos.anon", "https://buff.163.com/api/market/goods/info",
         {"game": "csgo", "goods_id": 43076}),
        ("buff.bill_order.anon", "https://buff.163.com/api/market/goods/bill_order",
         {"game": "csgo", "goods_id": 43076}),
    ]:
        try:
            r = s.get(url, params=params, timeout=15)
            j = r.json()
            code = j.get("code")
            record(name, code == "OK", f"code={code} err={j.get('error')} text={r.text[:110]}")
        except Exception as e:  # noqa: BLE001
            record(name, False, f"{type(e).__name__}: {e}")


def probe_youpin() -> None:
    s = requests.Session()
    s.headers.update({"User-Agent": UA_OKHTTP, "Content-Type": "application/json; charset=utf-8"})
    rand_uk = "".join(random.choices(string.ascii_letters + string.digits, k=65))

    # A) 匿名求购挂单（已验证可用）
    try:
        r = s.post(
            "https://api.youpin898.com/api/youpin/bff/trade/purchase/order/getTemplatePurchaseOrderPageList",
            json={"pageIndex": 1, "pageSize": 3, "showMaxPriceFlag": False, "templateId": 43076},
            timeout=15)
        j = r.json()
        rl = (j.get("data") or {}).get("responseList") or []
        record("youpin.purchase.anon", j.get("code") == 0,
               f"code={j.get('code')} rows={len(rl)}")
        if rl:
            print("     purchase row keys:", sorted(rl[0].keys()))
    except Exception as e:  # noqa: BLE001
        record("youpin.purchase.anon", False, f"{type(e).__name__}: {e}")

    # B) 在售价格通道：随机 uk 探测（区分「必须真实 uk」与「无需 uk」）
    common = {
        "uk": rand_uk, "AppType": "4", "App-Version": "5.26.0",
        "DeviceId": "abcdefghij", "DeviceToken": "abcdefghij", "deviceType": "1",
        "package-type": "uuyp", "Gameid": "730",
        "Device-Info": json.dumps({"deviceId": "abcdefghij", "deviceType": "abcdefghij",
                                   "hasSteamApp": 1, "requestTag": "A" * 32,
                                   "systemName": "Android", "systemVersion": "15"}),
    }
    candidates = [
        ("youpin.onsale.pc.randomuk", "https://api.youpin898.com/api/homepage/pc/goods/market/queryOnSaleCommodityList",
         {"listSortType": "2", "pageIndex": 1, "pageSize": 5, "templateId": 43076, "gameId": "730"}, {**common, "platform": "pc"}),
        ("youpin.onsale.bff.v1", "https://api.youpin898.com/api/youpin/bff/commodity/v1/commodity/list/onSale",
         {"pageIndex": 1, "pageSize": 5, "templateId": 43076, "gameId": "730"}, {**common, "platform": "android"}),
        ("youpin.onsale.bff.market", "https://api.youpin898.com/api/youpin/bff/commodity/market/queryOnSaleCommodityList",
         {"listSortType": "2", "pageIndex": 1, "pageSize": 5, "templateId": 43076, "gameId": "730"}, {**common, "platform": "android"}),
        ("youpin.market.detail.v3", "https://api.youpin898.com/api/homepage/v3/detail/commodity/list/sell",
         {"templateId": 43076, "pageSize": 5, "gameId": "730"}, {**common, "platform": "android"}),
        ("youpin.lease.v3.anon", "https://api.youpin898.com/api/homepage/v3/detail/commodity/list/lease",
         {"templateId": 43076, "pageSize": 5, "status": "20", "hasLease": "true", "gameId": "730"},
         {**common, "platform": "android"}),
    ]
    for name, url, body, hdr in candidates:
        try:
            r = s.post(url, json=body, headers=hdr, timeout=15)
            try:
                j = r.json()
            except ValueError:
                j = {}
            print(f"     {name}: HTTP {r.status_code} body={r.text[:200]}")
            record(name, r.status_code == 200 and j.get("code") in (0, None),
                   f"HTTP {r.status_code} code={j.get('code')} msg={j.get('msg')}")
        except Exception as e:  # noqa: BLE001
            record(name, False, f"{type(e).__name__}: {e}")

    # C) 关键词搜索 → templateId 解析通道
    for name, url, body in [
        ("youpin.search.keyword", "https://api.youpin898.com/api/homepage/search/GetKeyWordSearchList",
         {"keyWords": "AK-47 | Redline", "pageIndex": 1, "pageSize": 5, "gameId": "730"}),
        ("youpin.search.es", "https://api.youpin898.com/api/homepage/es/template/GetCsList",
         {"templateId": "", "pageIndex": 1, "pageSize": 5, "sortType": 1, "gameId": "730",
          "listSortType": 1, "commodityType": 2}),
    ]:
        try:
            r = s.post(url, json=body, headers={**common, "platform": "android"}, timeout=15)
            try:
                j = r.json()
            except ValueError:
                j = {}
            record(name, r.status_code == 200, f"HTTP {r.status_code} code={j.get('code')} text={r.text[:180]}")
        except Exception as e:  # noqa: BLE001
            record(name, False, f"{type(e).__name__}: {e}")


def main() -> None:
    print("=" * 78)
    print("facet 数据源可达性探测")
    print("=" * 78)
    t0 = time.time()
    probe_crypto()
    print("-" * 78)
    probe_buff()
    print("-" * 78)
    probe_youpin()
    print("=" * 78)
    ok = sum(1 for v in results.values() if v["ok"])
    print(f"总计 {ok}/{len(results)} 项可用，耗时 {time.time() - t0:.1f}s")
    out = str(sys.argv[1]) if len(sys.argv) > 1 else "probe/result.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"结果已写入 {out}")


if __name__ == "__main__":
    main()
