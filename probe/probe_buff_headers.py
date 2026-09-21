"""定位 BUFF 匿名 sell_order 的最小可用请求头组合。

背景：直接用 requests 裸请求时该接口匿名可读（返回 code=OK），
但加上 X-Requested-With 等头之后返回 code=Login Required。
需要逐个变量隔离，找出触发登录要求的那一项，避免适配器带上
「看起来更真实、实际更容易被拦」的请求头。
"""
from __future__ import annotations

import json
import time

import requests

URL = "https://buff.163.com/api/market/goods/sell_order"
PARAMS = {"game": "csgo", "goods_id": 43076, "page_num": 1}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

VARIANTS = {
    "A_bare_ua": {"User-Agent": UA},
    "B_ua_accept": {"User-Agent": UA, "Accept": "application/json"},
    "C_first_probe_exact": {
        "User-Agent": UA, "Accept": "application/json",
        "Referer": "https://buff.163.com/market/csgo",
    },
    "D_with_xrw": {
        "User-Agent": UA, "Accept": "application/json",
        "Referer": "https://buff.163.com/market/csgo",
        "X-Requested-With": "XMLHttpRequest",
    },
    "E_full_browser": {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://buff.163.com/market/csgo",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    },
    "F_requests_default": {},
}


def probe(name: str, headers: dict[str, str]) -> dict:
    session = requests.Session()
    session.headers.clear()
    session.headers.update(headers)
    try:
        r = session.get(URL, params=PARAMS, timeout=15)
        try:
            body = r.json()
        except ValueError:
            return {"variant": name, "http": r.status_code, "code": "NON_JSON",
                    "len": len(r.text)}
        data = body.get("data") or {}
        items = data.get("items") or []
        return {
            "variant": name, "http": r.status_code, "code": body.get("code"),
            "error": body.get("error"),
            "total_count": data.get("total_count"),
            "lowest": min((float(i["price"]) for i in items if i.get("price")), default=None),
            "sent_headers": dict(session.headers),
        }
    except Exception as exc:  # noqa: BLE001
        return {"variant": name, "error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    results = []
    for name, headers in VARIANTS.items():
        out = probe(name, headers)
        results.append(out)
        print(f"[{str(out.get('code')):>15}] {name:<22} "
              f"HTTP {out.get('http')} total={out.get('total_count')} "
              f"lowest={out.get('lowest')} err={out.get('error')}")
        time.sleep(1.2)

    print("\n结论：可用组合 ->", [r["variant"] for r in results
                                  if r.get("code") == "OK"])
    with open("probe/buff_header_matrix.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
