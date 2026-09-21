"""核实参考项目的许可元数据（GitHub API + 本地 LICENSE 文件）。"""
from __future__ import annotations

import json
from pathlib import Path

import requests

REPOS = [
    "Pgooone/cs-monitor",
    "cs2juece/CS2TradeMonitor",
    "allureking/cs2-inventory-manager",
    "lecrix/buff-price-alert",
]
REFS = Path(__file__).resolve().parent.parent / "refs"
LOCAL = {
    "Pgooone/cs-monitor": REFS / "cs-monitor",
    "cs2juece/CS2TradeMonitor": REFS / "CS2TradeMonitor",
    "allureking/cs2-inventory-manager": REFS / "cs2-inventory-manager",
}

session = requests.Session()
session.headers.update({"User-Agent": "youyoumonitor-license-check",
                        "Accept": "application/vnd.github+json"})

for repo in REPOS:
    try:
        r = session.get(f"https://api.github.com/repos/{repo}", timeout=20)
    except Exception as exc:  # noqa: BLE001
        print(f"{repo:<38} API ERR {type(exc).__name__}")
        continue
    if r.status_code != 200:
        print(f"{repo:<38} API HTTP {r.status_code}")
        continue
    j = r.json()
    lic = j.get("license") or {}
    print(f"{repo:<38} API license={lic.get('spdx_id')!s:<10} "
          f"name={lic.get('name')} stars={j.get('stargazers_count')} "
          f"pushed={str(j.get('pushed_at'))[:10]}")

print()
for repo, path in LOCAL.items():
    files = [p.name for p in path.glob("*") if p.is_file()] if path.exists() else []
    lic_files = [f for f in files if f.upper().startswith(("LICENSE", "LICENCE", "COPYING"))]
    print(f"{repo:<38} 本地 LICENSE 文件: {lic_files or '（无）'}")
    for name in lic_files:
        head = (path / name).read_text(encoding="utf-8", errors="replace")[:80]
        print(f"    {name}: {head.splitlines()[0][:70]}")

print("\n资源文件核对（是否可直接复用其数据资源）:")
basis = (REFS / "CS2TradeMonitor" / "CS2TradeMonitor.YouPinPrivacyAudit" / "Resources")
if basis.exists():
    for f in sorted(basis.iterdir()):
        print(f"    {f.name:<45} {f.stat().st_size:>8} bytes")
