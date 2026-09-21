"""验证租赁相关的 API 与看板（离线，不需要外部服务）。

跑法： .venv\\Scripts\\python.exe tools/verify_rent_api.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from facet.config import load_config   # noqa: E402
from facet.web import create_app       # noqa: E402


def main() -> int:
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("需要 httpx2：uv pip install httpx2")
        return 1

    config = load_config(str(ROOT / "config.demo.yaml"))
    app = create_app(config)
    with TestClient(app) as client:
        resp = client.get("/api/rent")
        print(f"GET /api/rent -> {resp.status_code}")
        if resp.status_code != 200:
            print(resp.text[:300])
            return 1

        data = resp.json()
        print(f"  命中 {data['count']} 条")
        print(f"  库统计：{data['stats']}")
        print(f"  费率假设：租赁抽成 {data['assumptions']['rent_fee'].get('YOUPIN')}"
              f" / 卖出 {data['assumptions']['sell_fee'].get('YOUPIN')}"
              f" / 提现 {data['assumptions']['withdraw_fee']}"
              f" / 缺省出租率 {data['assumptions']['occupancy_fallback']}")
        for item in data["items"][:5]:
            print(f"  {item['annualized_pct']:>7.1f}%  {item['mode_cn']}"
                  f"{item['horizon_days']:>4}天  日租 {item['daily_rent']:>6.2f}"
                  f"  出租率 {item['occupancy']*100:>3.0f}%"
                  f"  流动性 {item['liquidity_score'] or 0:>5.1f}"
                  f"  {(item['display_name'] or '')[:26]}")

        if data["items"]:
            name = data["items"][0]["market_hash_name"]
            detail = client.get(f"/api/rent/{name}")
            print(f"\nGET /api/rent/<name> -> {detail.status_code}")
            body = detail.json()
            print(f"  方案数 {len(body['scenarios'])}")
            print(f"  结论：{body['verdict']['headline']}")
            for reason in body["verdict"]["reasons"][:2]:
                print(f"    · {reason}")

        page = client.get("/")
        print(f"\nGET / -> {page.status_code}")
        html = page.text
        print(f"  含「租赁」标签页：{'租赁' in html}")
        print(f"  含 loadRent 函数：{'loadRent' in html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
