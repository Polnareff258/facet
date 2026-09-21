"""验证：能否通过悠悠求购接口枚举出某个饰品的**完整档位词表**。

若成立，本工具就获得了一个权威的、中文的档位词典来源：
不需要维护任何 paint_seed 对照表，也不需要从价格反推档位。
"""
from __future__ import annotations

import json
import random
import string
import time
from collections import Counter
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
ALNUM = string.ascii_letters + string.digits

TARGETS = [
    (1681, "★ 刺刀 | 多普勒 (崭新出厂)", "相位 + 宝石"),
    (45985, "★ 蝴蝶刀 | 多普勒 (崭新出厂)", "相位 + 宝石（另一把刀）"),
    (45724, "AWP | 渐变之色 (崭新出厂)", "渐变百分比"),
    (44937, "★ 刺刀 | 渐变大理石 (崭新出厂)", "大理石档位"),
    (494, "AK-47 | 表面淬火 (久经沙场)", "淬火 Tier"),
    (62097, "★ 鲍伊猎刀 | 伽马多普勒 (崭新出厂)", "伽马多普勒（绿宝石）"),
]


def headers() -> dict:
    device = "".join(random.choices(ALNUM, k=24))
    tag = "".join(random.choices("0123456789ABCDEF", k=32))
    return {
        "authorization": "Bearer ", "uk": "".join(random.choices(ALNUM, k=65)),
        "user-agent": "Android/15 official com.uu898.uuhavequality/5.45.4 okhttp/4.9.3",
        "App-Version": "5.45.4", "AppType": "4", "deviceType": "1",
        "package-type": "uuyp", "DeviceToken": device, "DeviceId": device,
        "deviceUk": "".join(random.choices(ALNUM, k=65)),
        "platform": "android", "Gameid": "730", "requestTag": tag,
        "deviceBrand": "xiaomi", "systemVersion": "15", "App-Source": "h5",
        "traceId": "".join(random.choices("0123456789abcdef", k=32)),
        "currentTheme": "Dark", "Accept-Language": "zh-CN,zh;q=0.8",
        "content-type": "application/json; charset=utf-8",
        "Device-Info": json.dumps({
            "deviceType": device, "systemName ": "Android", "hasSteamApp": 1,
            "deviceId": device, "requestTag": tag, "systemVersion": "15"},
            ensure_ascii=False),
    }


def fetch_all(template_id: int, pages: int = 3, size: int = 100) -> list[dict]:
    url = ("https://api.youpin898.com/api/youpin/bff/trade/purchase/order/"
           "getTemplatePurchaseOrderPageList")
    rows: list[dict] = []
    for page in range(1, pages + 1):
        try:
            resp = requests.post(url, json={
                "pageIndex": page, "pageSize": size, "showMaxPriceFlag": False,
                "templateId": template_id}, headers=headers(), timeout=25)
            text = resp.text
            if not text.strip():
                break
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"      第 {page} 页失败：{type(exc).__name__}")
            break
        batch = (body.get("data") or {}).get("responseList") or []
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < size:
            break
        time.sleep(1.2)
    return rows


def main() -> None:
    report = []
    print("=" * 80)
    print("档位词表枚举验证")
    print("=" * 80)

    for template_id, label, note in TARGETS:
        print(f"\n### {label}  (templateId={template_id})   考察：{note}")
        rows = fetch_all(template_id)
        if not rows:
            print("    未取到求购挂单（该模板当前无买家出价，或接口抖动）")
            report.append({"template_id": template_id, "label": label, "rows": 0})
            time.sleep(1.5)
            continue

        styles = Counter(str(r.get("specialStyle")) for r in rows)
        fades = Counter(str(r.get("fadeText")) for r in rows)
        abrades = Counter(str(r.get("abradeText")) for r in rows)
        cn_names = Counter(str(r.get("commodityName")) for r in rows)

        # 排除占位值，得到真实档位词表
        style_vocab = sorted(k for k in styles if k not in ("None", "不限", ""))
        fade_vocab = sorted(k for k in fades if k not in ("None", "不限", "0-100", ""))

        print(f"    样本 {len(rows)} 行")
        print(f"    中文名：{list(cn_names)[:1]}")
        print(f"    档位词表 specialStyle：{style_vocab or '（无）'}")
        print(f"    渐变词表 fadeText    ：{fade_vocab or '（无）'}")
        if style_vocab:
            print(f"    各档位样本数：{ {k: styles[k] for k in style_vocab} }")
        if fade_vocab:
            print(f"    各渐变档样本：{ {k: fades[k] for k in fade_vocab} }")
        print(f"    磨损区间取值（前 6）：{list(abrades)[:6]}")

        report.append({
            "template_id": template_id, "label": label, "rows": len(rows),
            "cn_name": list(cn_names)[0] if cn_names else None,
            "style_vocab": style_vocab, "style_counts": dict(styles),
            "fade_vocab": fade_vocab, "fade_counts": dict(fades),
            "abrade_vocab": list(abrades)[:10],
        })
        time.sleep(1.5)

    print("\n" + "=" * 80)
    print("汇总")
    print("=" * 80)
    for entry in report:
        vocab = entry.get("style_vocab") or entry.get("fade_vocab") or []
        print(f"  {entry['label'][:34]:<34} 样本 {entry['rows']:>4}  "
              f"档位 {len(vocab)} 个  {vocab}")

    out = ROOT / "probe" / "variant_vocab.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
