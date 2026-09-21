"""BUFF 侧档位信息补充查证。

已知：BUFF 商品页可以按图案/相位筛选，但 sell_order 一旦带上 paintseed 参数
就返回 Login Required（不带则 OK）。本脚本继续排查其他可能：
  1) 商品页 HTML 是否服务端渲染了档位选项
  2) 是否存在未发现的档位列表端点
  3) sell_order 带其他筛选参数的行为
  4) 挂单明细里的 paintseed 覆盖度（这是 BUFF 侧唯一匿名可见的档位线索）
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BUFF = "https://buff.163.com"

S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://buff.163.com/market/csgo",
})

report: dict[str, object] = {}


def check_sell_order_params() -> None:
    """逐个参数测试 sell_order：哪些匿名可用、哪些触发登录。"""
    print("\n### 1) sell_order 参数行为")
    cases = [
        ("base", {"game": "csgo", "goods_id": 43076, "page_num": 1}),
        ("+paintseed", {"game": "csgo", "goods_id": 43076, "page_num": 1,
                        "paintseed": 755}),
        ("+paintwear", {"game": "csgo", "goods_id": 43076, "page_num": 1,
                        "paintwear": "0.44"}),
        ("+min_paintwear", {"game": "csgo", "goods_id": 43076, "page_num": 1,
                            "min_paintwear": "0.4"}),
        ("+sort_by", {"game": "csgo", "goods_id": 43076, "page_num": 1,
                      "sort_by": "price.asc"}),
        ("+search", {"game": "csgo", "goods_id": 43076, "page_num": 1,
                     "search": "test"}),
        ("+use_suggestion", {"game": "csgo", "goods_id": 43076, "page_num": 1,
                             "use_suggestion": "1"}),
    ]
    results = []
    for name, params in cases:
        try:
            resp = S.get(f"{BUFF}/api/market/goods/sell_order", params=params, timeout=15)
            body = resp.json()
            item_keys: list[str] = []
            if body.get("code") == "OK":
                items = (body.get("data") or {}).get("items") or []
                if items:
                    item_keys = sorted(items[0].keys())[:8]
            print(f"    {name:<18} code={body.get('code')!s:<16} "
                  f"err={body.get('error')}")
            results.append({"case": name, "code": body.get("code"),
                            "error": body.get("error"), "item_keys": item_keys})
        except Exception as exc:  # noqa: BLE001
            print(f"    {name:<18} 失败 {type(exc).__name__}")
            results.append({"case": name, "error": f"{type(exc).__name__}: {exc}"})
        time.sleep(1.1)
    report["sell_order_params"] = results


def check_extra_endpoints() -> None:
    """继续排查可能的档位列表端点。"""
    print("\n### 2) 候选档位端点")
    candidates = [
        ("goods/paintseed_list", "/api/market/goods/paintseed_list", {}),
        ("goods/pattern", "/api/market/goods/pattern", {}),
        ("goods/paintseed", "/api/market/goods/paintseed", {}),
        ("goods/sell_order_options", "/api/market/goods/sell_order_options", {}),
        ("goods/detail", "/api/market/goods/detail", {"goods_id": 43076}),
        ("goods/tags", "/api/market/goods/tags", {"goods_id": 43076}),
        ("market/banner", "/api/market/banner", {}),
        ("goods/special_style", "/api/market/goods/special_style", {"goods_id": 43076}),
    ]
    results = []
    for name, path, extra in candidates:
        params = {"game": "csgo", **extra}
        try:
            resp = S.get(BUFF + path, params=params, timeout=12)
            try:
                body = resp.json()
            except ValueError:
                body = {"code": f"HTTP {resp.status_code} (非 JSON)"}
            code = body.get("code")
            print(f"    {name:<26} code={code}")
            results.append({"endpoint": name, "code": code,
                            "sample": json.dumps(body, ensure_ascii=False)[:180]})
        except Exception as exc:  # noqa: BLE001
            print(f"    {name:<26} 失败 {type(exc).__name__}")
            results.append({"endpoint": name, "error": str(exc)[:100]})
        time.sleep(0.9)
    report["endpoints"] = results


def check_page_html() -> None:
    """商品页 HTML 是否服务端渲染了档位选项。"""
    print("\n### 3) 商品页 HTML 是否含档位选项")
    try:
        resp = S.get(f"{BUFF}/market/csgo", params={"goods_id": 43076}, timeout=20)
        html = resp.text
        print(f"    HTTP {resp.status_code}  长度 {len(html)}")
        hits = {}
        for keyword in ("paintseed", "paint_seed", "图案", "相位", "多普勒",
                        "红宝石", "蓝宝石", "special_style", "specialStyle"):
            hits[keyword] = html.count(keyword)
        print(f"    关键词出现次数：{hits}")
        report["page_html"] = {"http": resp.status_code, "length": len(html),
                               "keyword_hits": hits}
        # 找内嵌 JSON 里可能的档位定义
        patterns = [
            'paintseed[_"]*:\\s*\\[[^\\]]{0,200}',
            'special[_"]?[Ss]tyle["\\s]*:\\s*\\[[^\\]]{0,200}',
            '\\d+\\s*:\\s*"[^"]{1,20}(?:多普勒|渐变|淬火|宝石)[^"]{0,20}"',
        ]
        for pattern in patterns:
            found = re.findall(pattern, html, re.IGNORECASE)
            if found:
                print(f"    正则命中 {pattern[:30]}… → {found[:3]}")
    except Exception as exc:  # noqa: BLE001
        print(f"    失败 {type(exc).__name__}: {exc}")
        report["page_html"] = {"error": str(exc)[:150]}


def check_paintseed_coverage() -> None:
    """挂单明细里 paintseed 的覆盖度 —— BUFF 侧唯一匿名可见的档位线索。"""
    print("\n### 4) 挂单明细 paintseed 覆盖度")
    seeds: Counter = Counter()
    total = 0
    for goods_id in (43076, 1681 if False else 43076):
        try:
            resp = S.get(f"{BUFF}/api/market/goods/sell_order",
                         params={"game": "csgo", "goods_id": goods_id, "page_num": 1},
                         timeout=15)
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"    goods_id={goods_id} 失败 {type(exc).__name__}")
            continue
        if body.get("code") != "OK":
            print(f"    goods_id={goods_id} code={body.get('code')}（匿名通道被风控）")
            continue
        for item in ((body.get("data") or {}).get("items") or []):
            info = ((item.get("asset_info") or {}).get("info") or {})
            total += 1
            seeds[info.get("paintseed")] += 1
        time.sleep(1.2)
    print(f"    样本 {total} 条，paintseed 取值 {len(seeds)} 个：{dict(list(seeds.items())[:12])}")
    report["paintseed"] = {"samples": total, "distinct": len(seeds),
                           "values": {str(k): v for k, v in list(seeds.items())[:20]}}


def main() -> None:
    print("=" * 78)
    print("BUFF 侧档位信息查证")
    print("=" * 78)
    check_sell_order_params()
    check_extra_endpoints()
    check_page_html()
    check_paintseed_coverage()

    out = ROOT / "probe" / "buff_variant_probe.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print("结论")
    print("=" * 78)
    params = report.get("sell_order_params") or []
    ok = [p["case"] for p in params if p.get("code") == "OK"]
    denied = [p["case"] for p in params if p.get("code") == "Login Required"]
    print(f"  sell_order 匿名可用参数组合：{ok}")
    print(f"  触发登录的参数组合：{denied}")
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
