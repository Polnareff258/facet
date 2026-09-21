"""复测 BUFF 当前匿名/带 Cookie 的可用面。

不带 Cookie 与带 Cookie 各跑一遍，输出对照表 —— 这决定新适配器怎么设计：
哪些能力必须登录、哪些匿名就行。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CASES = [
    ("sell_order 基础", "GET", "https://buff.163.com/api/market/goods/sell_order",
     {"game": "csgo", "goods_id": 43076, "page_num": 1}),
    ("sell_order +paintseed", "GET", "https://buff.163.com/api/market/goods/sell_order",
     {"game": "csgo", "goods_id": 43076, "page_num": 1, "paintseed": 755}),
    ("sell_order +sort_by", "GET", "https://buff.163.com/api/market/goods/sell_order",
     {"game": "csgo", "goods_id": 43076, "page_num": 1, "sort_by": "price.asc"}),
    ("goods 列表", "GET", "https://buff.163.com/api/market/goods",
     {"game": "csgo", "page_num": 1, "page_size": 3}),
    ("goods 搜索", "GET", "https://buff.163.com/api/market/goods",
     {"game": "csgo", "page_num": 1, "page_size": 3, "search": "AK-47 | Redline"}),
    ("goods info", "GET", "https://buff.163.com/api/market/goods/info",
     {"game": "csgo", "goods_id": 43076}),
    ("bill_order 成交", "GET", "https://buff.163.com/api/market/goods/bill_order",
     {"game": "csgo", "goods_id": 43076}),
    ("buy_order 求购", "GET", "https://buff.163.com/api/market/goods/buy_order",
     {"game": "csgo", "goods_id": 43076, "page_num": 1}),
    ("用户信息", "GET", "https://buff.163.com/api/market/user/info", {}),
]


def cookie_from_env() -> str:
    """按优先级从环境变量 / .env 取 BUFF Cookie。"""
    value = os.environ.get("BUFF_COOKIE", "").strip()
    if value:
        return value
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("BUFF_COOKIE=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def probe(cookie: str | None, label: str) -> dict:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/json",
                            "Referer": "https://buff.163.com/market/csgo"})
    if cookie:
        session.headers["Cookie"] = cookie

    print(f"\n### {label}")
    results = {}
    for name, method, url, params in CASES:
        try:
            resp = session.get(url, params=params, timeout=15)
            try:
                body = resp.json()
            except ValueError:
                results[name] = {"http": resp.status_code, "code": "非JSON"}
                print(f"    {name:<22} HTTP {resp.status_code} 非 JSON")
                continue
            code = body.get("code")
            data = body.get("data") if isinstance(body.get("data"), dict) else None
            extra = ""
            if data:
                if "items" in data:
                    extra = f" items={len(data['items'])} total={data.get('total_count')}"
                elif "goods_id" in data:
                    extra = f" mhn={data.get('market_hash_name')}"
            results[name] = {"code": code, "error": body.get("error"), "extra": extra}
            mark = "OK " if code == "OK" else "   "
            print(f"    {mark}{name:<22} code={code}{extra}")
        except Exception as exc:  # noqa: BLE001
            results[name] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"    ERR {name:<22} {type(exc).__name__}")
    return results


def main() -> int:
    cookie = cookie_from_env()
    report = {"anonymous": probe(None, "匿名（无 Cookie）")}
    if cookie:
        report["with_cookie"] = probe(cookie, f"带 Cookie（长度 {len(cookie)}）")
        print("\n" + "=" * 74)
        print("差异对照：Cookie 解锁了哪些能力")
        print("=" * 74)
        anon, authed = report["anonymous"], report["with_cookie"]
        for name in anon:
            a = anon[name].get("code")
            b = authed.get(name, {}).get("code")
            if a != b:
                print(f"  {name:<24} 匿名={a}  →  带Cookie={b}")
        unlocked = [n for n in anon if anon[n].get("code") != "OK"
                    and authed.get(n, {}).get("code") == "OK"]
        print(f"\n  Cookie 解锁 {len(unlocked)} 项：{unlocked}")
    else:
        print("\n未配置 BUFF_COOKIE —— 只测了匿名面。")
        print("若你有 BUFF 账号，把浏览器 Cookie 写进 .env 的 BUFF_COOKIE，可解锁：")
        print("  · 按名称搜索饰品（不用再扫 goods_id 空间）")
        print("  · 按 paintseed / 磨损区间筛选挂单（档位级在售价）")
        print("  · 成交记录与求购挂单")

    out = ROOT / "probe" / "buff_capability.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
