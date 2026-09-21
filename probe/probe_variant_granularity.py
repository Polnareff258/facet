"""判定档位字段的粒度：是「每件饰品各不相同」还是「整个 templateId 统一」。

这一步决定架构走向：
  · 若 specialStyle/fadeText 在同一 templateId 内多变 → 档位是实例属性，
    需要按实例采集、聚合、分类（重活，但信息最全）
  · 若同一 templateId 内固定 → 说明平台的 ID 本身已分档，
    直接按 ID 建映射即可（轻活，且最可靠）

同时查证 BUFF 侧：商品页能按图案筛选，接口是否有对应参数或档位列表接口。
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

# 覆盖四类档位结构：淬火(T1/T2)、多普勒(相位)、渐变(百分比)、大理石(档位)
TARGETS = [
    (494, "AK-47 | Case Hardened (Field-Tested)", "淬火档位 T1/T2/T3/T4"),
    (43804, "AK-47 | Case Hardened (Minimal Wear)", "淬火档位（略磨）"),
    (1681, "★ Bayonet | Doppler (Factory New)", "多普勒相位"),
    (45985, "★ Butterfly Knife | Doppler (Factory New)", "多普勒相位（蝴蝶刀）"),
    (45724, "AWP | Fade (Factory New)", "渐变百分比"),
    (44937, "★ Bayonet | Marble Fade (Factory New)", "大理石档位"),
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


def youpin_page(template_id: int, size: int = 100) -> list[dict]:
    url = ("https://api.youpin898.com/api/youpin/bff/trade/purchase/order/"
           "getTemplatePurchaseOrderPageList")
    try:
        resp = requests.post(url, json={
            "pageIndex": 1, "pageSize": size, "showMaxPriceFlag": False,
            "templateId": template_id}, headers=headers(), timeout=25)
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        return [{"__error__": f"{type(exc).__name__}: {exc}"}]
    return (body.get("data") or {}).get("responseList") or []


def analyse(template_id: int, label: str, note: str) -> dict:
    print(f"\n### {label}\n    templateId={template_id}   考察点：{note}")
    rows = youpin_page(template_id)
    if rows and "__error__" in rows[0]:
        print("    请求失败：", rows[0]["__error__"])
        return {"template_id": template_id, "error": rows[0]["__error__"]}
    if not rows:
        print("    无求购挂单（该 templateId 当前没有买家出价）")
        return {"template_id": template_id, "rows": 0}

    styles = Counter(str(r.get("specialStyle")) for r in rows)
    fades = Counter(str(r.get("fadeText")) for r in rows)
    abrades = Counter(str(r.get("abradeText")) for r in rows)
    names = Counter(str(r.get("commodityName")) for r in rows)

    print(f"    行数 {len(rows)}")
    print(f"    specialStyle 取值分布：{dict(styles)}")
    print(f"    fadeText     取值分布：{dict(fades)}")
    print(f"    abradeText   取值分布：{dict(list(abrades.items())[:6])}")
    print(f"    commodityName 取值分布：{dict(list(names.items())[:4])}")

    verdict = ("行级（多变）" if len(styles) > 1 or len(fades) > 1
               else "统一（该 templateId 内固定）")
    print(f"    → 判定：specialStyle/fadeText 呈现【{verdict}】")

    return {
        "template_id": template_id, "label": label, "note": note,
        "rows": len(rows),
        "specialStyle": {k: v for k, v in styles.items()},
        "fadeText": {k: v for k, v in fades.items()},
        "abradeText": {k: v for k, v in list(abrades.items())[:8]},
        "commodityName": {k: v for k, v in names.items()},
    }


def probe_buff_pattern_params() -> dict:
    """查证 BUFF 的图案维度：sell_order 是否吃 paintseed，是否有档位列表接口。"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        "Accept": "application/json",
        "Referer": "https://buff.163.com/market/csgo",
    })
    out: dict[str, object] = {}

    def probe(name: str, url: str, params: dict) -> None:
        try:
            resp = session.get(url, params=params, timeout=15)
            try:
                body = resp.json()
            except ValueError:
                out[name] = {"http": resp.status_code, "body": resp.text[:160]}
                return
            code = body.get("code")
            data = body.get("data") or {}
            out[name] = {
                "http": resp.status_code, "code": code,
                "keys": sorted(data.keys())[:14] if isinstance(data, dict) else None,
                "total": data.get("total_count") if isinstance(data, dict) else None,
                "err": body.get("error"),
            }
            print(f"    {name:<34} code={code} total={out[name].get('total')} "
                  f"keys={out[name].get('keys')}")
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"    {name:<34} 失败 {type(exc).__name__}")

    print("\n### BUFF 图案维度查证")
    base = "https://buff.163.com/api/market/goods/sell_order"
    probe("sell_order 无 paintseed", base,
          {"game": "csgo", "goods_id": 43076, "page_num": 1})
    time.sleep(1)
    probe("sell_order + paintseed=755", base,
          {"game": "csgo", "goods_id": 43076, "page_num": 1, "paintseed": 755})
    time.sleep(1)
    # 档位列表类接口的候选路径
    for name, path, params in [
        ("goods/paintseed", "https://buff.163.com/api/market/goods/paintseed",
         {"game": "csgo", "goods_id": 43076}),
        ("goods/seed", "https://buff.163.com/api/market/goods/seed",
         {"game": "csgo", "goods_id": 43076}),
        ("goods/tag", "https://buff.163.com/api/market/goods/tag",
         {"game": "csgo", "goods_id": 43076}),
        ("goods/filters", "https://buff.163.com/api/market/goods/filters",
         {"game": "csgo", "goods_id": 43076}),
        ("goods/aggregate", "https://buff.163.com/api/market/goods/aggregate",
         {"game": "csgo", "goods_id": 43076}),
    ]:
        time.sleep(0.9)
        probe(name, path, params)
    return out


def main() -> None:
    print("=" * 78)
    print("悠悠有品：档位字段粒度判定")
    print("=" * 78)
    results = []
    for template_id, label, note in TARGETS:
        results.append(analyse(template_id, label, note))
        time.sleep(1.4)

    buff = probe_buff_pattern_params()

    payload = {"youpin": results, "buff": buff}
    out = ROOT / "probe" / "variant_granularity.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print("结论汇总")
    print("=" * 78)
    for r in results:
        if r.get("error"):
            print(f"  {r.get('label','?'):<44} 失败")
            continue
        multi = len(r.get("specialStyle", {})) > 1 or len(r.get("fadeText", {})) > 1
        print(f"  {r.get('label','?')[:44]:<44} 行数 {r.get('rows',0):<4} "
              f"{'行级多变' if multi else 'ID 内固定'}")
        if r.get("specialStyle"):
            print(f"      specialStyle = {r['specialStyle']}")
        if r.get("fadeText") and list(r["fadeText"]) != ["None"]:
            print(f"      fadeText     = {r['fadeText']}")
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
