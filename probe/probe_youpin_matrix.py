"""悠悠有品通道探测 v2：按 CS2TradeMonitor 的移动端请求头配方（形状校验）重测。

关键形状约束（来自参考实现 YouPinMobileApiClient.ApplyHeaders 与设备档案校验）：
  - DeviceToken / DeviceId : 24 字符（不得以 CS2M 开头）
  - requestTag             : 32 字符大写十六进制
  - deviceUk               : 65 字符
  - uk                     : 65 字符（真实 uk 走 /api/deviceW2 协商）
"""
from __future__ import annotations

import json
import random
import string
import sys
import time
import uuid

import requests

BASE = "https://api.youpin898.com"
TEMPLATE_ID = 43076

ENDPOINTS = [
    ("sell.v3", "/api/homepage/v3/detail/commodity/list/sell", "android",
     {"templateId": TEMPLATE_ID, "pageSize": 5, "gameId": "730"}),
    ("lease.v3", "/api/homepage/v3/detail/commodity/list/lease", "android",
     {"templateId": TEMPLATE_ID, "pageSize": 5, "status": "20", "hasLease": "true", "gameId": "730"}),
    ("purchase.bff", "/api/youpin/bff/trade/purchase/order/getTemplatePurchaseOrderPageList", "android",
     {"pageIndex": 1, "pageSize": 3, "showMaxPriceFlag": False, "templateId": TEMPLATE_ID}),
    ("pc.onsale", "/api/homepage/pc/goods/market/queryOnSaleCommodityList", "pc",
     {"listSortType": "2", "pageIndex": 1, "pageSize": 5, "templateId": TEMPLATE_ID, "gameId": "730"}),
]

VERSIONS = ["5.45.4", "5.46.1", "5.52.0", "6.0.0"]

ALNUM = string.ascii_letters + string.digits


def shape_correct_random() -> dict[str, str]:
    return {
        "device_token": "".join(random.choices(ALNUM, k=24)),
        "request_tag": "".join(random.choices("0123456789ABCDEF", k=32)),
        "device_uk": "".join(random.choices(ALNUM, k=65)),
        "uk": "".join(random.choices(ALNUM, k=65)),
        "trace_id": "".join(random.choices("0123456789abcdef", k=32)),
    }


def build_headers(version: str, platform: str, ids: dict[str, str], token: str = "") -> dict[str, str]:
    device_id = ids["device_token"]
    return {
        "authorization": f"Bearer {token}" if token else "Bearer ",
        "uk": ids["uk"],
        "user-agent": f"Android/15 official com.uu898.uuhavequality/{version} okhttp/4.9.3",
        "App-Version": version,
        "AppType": "4",
        "deviceType": "1",
        "package-type": "uuyp",
        "DeviceToken": device_id,
        "DeviceId": device_id,
        "deviceUk": ids["device_uk"],
        "platform": platform,
        "Gameid": "730",
        "requestTag": ids["request_tag"],
        "deviceBrand": "xiaomi",
        "systemVersion": "15",
        "App-Source": "h5",
        "traceId": ids["trace_id"],
        "currentTheme": "Dark",
        "Accept-Language": "zh-CN,zh;q=0.8",
        "content-type": "application/json; charset=utf-8",
        "Device-Info": json.dumps({
            "deviceType": device_id,
            "systemName ": "Android",
            "hasSteamApp": 1,
            "deviceId": device_id,
            "requestTag": ids["request_tag"],
            "systemVersion": "15",
        }, ensure_ascii=False),
    }


def extract_rows(payload: dict) -> int:
    data = payload.get("data") or payload.get("Data") or {}
    if isinstance(data, dict):
        for field in ("commodityList", "responseList", "list", "commodityInfoList"):
            if isinstance(data.get(field), list):
                return len(data[field])
    if isinstance(data, list):
        return len(data)
    return 0


def main() -> None:
    session = requests.Session()
    matrix: dict[str, dict] = {}
    for version in VERSIONS:
        ids = shape_correct_random()
        for name, path, platform, body in ENDPOINTS:
            try:
                r = session.post(BASE + path, json=body,
                                 headers=build_headers(version, platform, ids), timeout=12)
                try:
                    payload = r.json()
                except ValueError:
                    payload = {}
                code = payload.get("code", payload.get("Code"))
                msg = str(payload.get("msg", payload.get("Msg", "")))[:34]
                rows = extract_rows(payload)
                ok = r.status_code == 200 and code == 0
                matrix[f"{version}|{name}"] = {
                    "ok": ok, "http": r.status_code, "code": code, "msg": msg, "rows": rows,
                }
                print(f"[{'OK  ' if ok else 'NO  '}] {version} {name:13s} HTTP {r.status_code} "
                      f"code={code} rows={rows} {msg}")
                if rows:
                    data = payload.get("data") or {}
                    sample = (data.get("commodityList") or data.get("responseList")
                              or data.get("list") or data)
                    if isinstance(sample, list) and sample:
                        print("        fields:", sorted(sample[0].keys())[:22])
            except Exception as exc:  # noqa: BLE001
                matrix[f"{version}|{name}"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                print(f"[ERR ] {version} {name:13s} {type(exc).__name__}: {str(exc)[:70]}")
            time.sleep(0.9)
        print("-" * 74)

    working = [k for k, v in matrix.items() if v.get("ok")]
    print(f"\n可用组合 {len(working)}/{len(matrix)}: {working}")
    out = sys.argv[1] if len(sys.argv) > 1 else "probe/youpin_matrix.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(matrix, f, ensure_ascii=False, indent=2)
    print(f"矩阵已写入 {out}")


if __name__ == "__main__":
    main()
