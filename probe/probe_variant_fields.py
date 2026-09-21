"""定位「档位信息到底藏在哪个字段」——分两路查证。

线索来自两处：
  1) BUFF 商品页上可以按图案/相位筛选 → 接口必有对应维度
  2) 悠悠求购接口返回字段里有 fadeText / specialStyle → 疑似档位描述

本脚本：
  A. 从随包的 top-1000 映射里挑出变体敏感的饰品（多普勒/渐变/淬火等），
     拿到它们真实的 templateId 与英文名
  B. 用这些 templateId 打悠悠求购接口，**完整 dump 每一行**，
     看 fadeText / specialStyle / commodityName 到底长什么样
"""
from __future__ import annotations

import gzip
import json
import random
import string
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
MAPPING = (ROOT / "refs" / "CS2TradeMonitor" / "CS2TradeMonitor.YouPinPrivacyAudit"
           / "Resources" / "hot-top1000.youpin-mapping.json.gz")

#: 档位决定价格的品类关键词
SENSITIVE = ("doppler", "gamma doppler", "fade", "case hardened", "marble fade",
             "crimson web", "slaughter", "tiger tooth")

ALNUM = string.ascii_letters + string.digits


def load_candidates() -> list[dict]:
    if not MAPPING.exists():
        print(f"映射资源不存在：{MAPPING}")
        return []
    raw = json.loads(gzip.open(MAPPING, "rt", encoding="utf-8").read())
    data = raw.get("data") or {}
    out = []
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        name = value.get("steam_hash_name") or key
        low = name.lower()
        if any(k in low for k in SENSITIVE):
            out.append({"market_hash_name": name, "template_id": value.get("yyyp_id")})
    return out


def youpin_headers() -> dict:
    device = "".join(random.choices(ALNUM, k=24))
    return {
        "authorization": "Bearer ",
        "uk": "".join(random.choices(ALNUM, k=65)),
        "user-agent": "Android/15 official com.uu898.uuhavequality/5.45.4 okhttp/4.9.3",
        "App-Version": "5.45.4", "AppType": "4", "deviceType": "1",
        "package-type": "uuyp", "DeviceToken": device, "DeviceId": device,
        "deviceUk": "".join(random.choices(ALNUM, k=65)),
        "platform": "android", "Gameid": "730",
        "requestTag": "".join(random.choices("0123456789ABCDEF", k=32)),
        "deviceBrand": "xiaomi", "systemVersion": "15", "App-Source": "h5",
        "traceId": "".join(random.choices("0123456789abcdef", k=32)),
        "currentTheme": "Dark", "Accept-Language": "zh-CN,zh;q=0.8",
        "content-type": "application/json; charset=utf-8",
        "Device-Info": json.dumps({
            "deviceType": device, "systemName ": "Android", "hasSteamApp": 1,
            "deviceId": device,
            "requestTag": "".join(random.choices("0123456789ABCDEF", k=32)),
            "systemVersion": "15"}, ensure_ascii=False),
    }


def probe_youpin(template_id: int) -> dict:
    """完整 dump 求购接口的一行，找出档位字段。"""
    url = ("https://api.youpin898.com/api/youpin/bff/trade/purchase/order/"
           "getTemplatePurchaseOrderPageList")
    try:
        resp = requests.post(url, json={
            "pageIndex": 1, "pageSize": 5, "showMaxPriceFlag": False,
            "templateId": template_id}, headers=youpin_headers(), timeout=20)
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    rows = (body.get("data") or {}).get("responseList") or []
    return {"code": body.get("code"), "rows": len(rows),
            "sample": rows[0] if rows else None}


def main() -> None:
    candidates = load_candidates()
    print(f"从映射里挑出 {len(candidates)} 个变体敏感饰品\n")
    for item in candidates[:25]:
        print(f"  templateId={item['template_id']:<8} {item['market_hash_name']}")

    # 优先探「多普勒」和「渐变」——它们的档位最典型
    priority = [c for c in candidates
                if any(k in c["market_hash_name"].lower()
                       for k in ("doppler", "fade", "case hardened"))]
    probes = (priority or candidates)[:4]

    print(f"\n{'=' * 74}\n悠悠求购接口完整字段 dump（找 fadeText / specialStyle）\n{'=' * 74}")
    results = []
    for item in probes:
        print(f"\n### {item['market_hash_name']}  (templateId={item['template_id']})")
        result = probe_youpin(int(item["template_id"]))
        results.append({**item, "probe": result})
        if result.get("error"):
            print("  失败：", result["error"])
        else:
            sample = result.get("sample")
            print(f"  code={result['code']} 行数={result['rows']}")
            if sample:
                for key in ("commodityName", "fadeText", "specialStyle", "type",
                            "typeId", "abradeText", "templateId", "purchasePrice"):
                    print(f"    {key:<16} = {sample.get(key)!r}")
                print(f"    ---- 全部字段 ----")
                print("    " + json.dumps(sample, ensure_ascii=False)[:900])
        time.sleep(1.2)

    out = ROOT / "probe" / "youpin_variant_fields.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
