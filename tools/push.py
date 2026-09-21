"""推送助手：自动处理网络与凭据，避免每次手敲一长串参数。

背景（都是实测踩出来的）：
  1. **schannel TLS 失败** —— Windows 上 git 默认用 schannel，本机报
     `AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS`。
     → 改用 OpenSSL 后端。
  2. **凭据助手 spawn bash 被拦** —— git-credential-manager 要起 sh.exe，
     在受限环境里报 `couldn't create signal pipe`。
     → 直接内联 token，且不写进 .git/config。
  3. **直连 github.com:443 间歇性超时** —— 但 api.github.com 通。
     → 走本机代理（v2rayN/xray 的 mixed 入站，默认 127.0.0.1:10808，
        同一端口同时支持 SOCKS5 与 HTTP）。

用法：
    python tools/push.py                     # 推送
    python tools/push.py --message "说明"    # 先提交再推
    python tools/push.py --probe             # 只探测网络路径，不推送
    python tools/push.py --no-proxy          # 强制直连
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 常见代理端口（v2rayN/xray mixed 入站优先）
PROXY_CANDIDATES = [
    "http://127.0.0.1:10808",   # v2rayN 新版的 mixed 入站
    "http://127.0.0.1:10809",   # v2rayN 旧版的 http 入站
    "http://127.0.0.1:7890",    # Clash
    "http://127.0.0.1:1080",    # 通用
]


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 120
        ) -> tuple[int, str]:
    result = subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=timeout)
    return result.returncode, (result.stdout + result.stderr).strip()


def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh_token() -> str | None:
    code, out = run(["gh", "auth", "token"])
    return out.strip() if code == 0 and out.strip() else None


def probe_proxy(proxy: str, timeout: int = 12) -> bool:
    """用 python 试一个 HTTPS 请求，判断代理是否真的能用。

    不用 curl：本机 curl.exe 在受限环境里即使直连也返回 000，会把结论带偏。
    """
    # 用 repr() 注入字符串，避免在 f-string 里嵌套引号
    script = (
        "import requests\n"
        f"proxies = {{'http': {proxy!r}, 'https': {proxy!r}}}\n"
        "r = requests.get('https://github.com', timeout=10, proxies=proxies)\n"
        "print(r.status_code)\n"
    )
    code, out = run([sys.executable, "-c", script], timeout=timeout)
    return code == 0 and "200" in out


def probe_direct(timeout: int = 12) -> bool:
    script = (
        "import requests\n"
        "r = requests.get('https://github.com', timeout=10)\n"
        "print(r.status_code)\n"
    )
    code, out = run([sys.executable, "-c", script], timeout=timeout)
    return code == 0 and "200" in out


def find_working_proxy() -> str | None:
    for candidate in PROXY_CANDIDATES:
        if probe_proxy(candidate):
            return candidate
    return None


def repo_url() -> str:
    code, out = run(["git", "remote", "get-url", "origin"])
    return out.strip() if code == 0 else ""


def push_with(url: str, token: str | None, proxy: str | None) -> tuple[int, str]:
    """用「内联 token + 指定代理」推送，不改 .git/config。"""
    target = url
    if token and url.startswith("https://github.com/"):
        target = url.replace("https://github.com/",
                             f"https://x-access-token:{token}@github.com/")

    cmd = ["git", "-c", "credential.helper=", "-c", "http.sslBackend=openssl"]
    if proxy:
        cmd += ["-c", f"http.proxy={proxy}", "-c", f"https.proxy={proxy}"]
    cmd += ["push", target, "main:main"]
    return run(cmd, timeout=180)


def main() -> int:
    parser = argparse.ArgumentParser(description="推送助手（自动选网络路径）")
    parser.add_argument("--message", "-m", default=None, help="先提交再推")
    parser.add_argument("--probe", action="store_true", help="只探测网络")
    parser.add_argument("--no-proxy", action="store_true", help="强制直连")
    parser.add_argument("--proxy", default=None, help="指定代理地址")
    args = parser.parse_args()

    url = repo_url()
    if not url:
        print("✗ 没有配置 origin 远端")
        return 1
    print(f"远端：{url}")

    # 探测网络路径
    print("\n探测网络路径…")
    direct_ok = probe_direct()
    print(f"  直连 github.com      : {'可用' if direct_ok else '不可用'}")
    proxy = args.proxy
    if not args.no_proxy:
        if proxy:
            ok = probe_proxy(proxy)
            print(f"  指定代理 {proxy}: {'可用' if ok else '不可用'}")
            if not ok:
                proxy = None
        else:
            proxy = find_working_proxy()
            print(f"  自动探测代理          : {proxy or '未找到可用代理'}")
    else:
        proxy = None

    if args.probe:
        print("\n（仅探测，未推送）")
        return 0 if (direct_ok or proxy) else 1

    if not direct_ok and not proxy:
        print("\n✗ 直连与代理都不可用。检查：")
        print("  · v2rayN/Clash 是否在运行，且已选好节点")
        print("  · 用 --proxy http://127.0.0.1:<端口> 手动指定")
        return 1

    # 可选提交
    if args.message:
        run(["git", "add", "-A"])
        code, out = run(["git", "-c", "core.autocrlf=false", "commit",
                         "-q", "-m", args.message])
        if code != 0 and "nothing to commit" not in out:
            print(f"✗ 提交失败：{out[:200]}")
            return 1
        print("已提交")

    token = gh_token() if gh_available() else None
    if not token:
        print("! 未取到 gh token，将依赖系统凭据（可能弹出认证）")

    # 先用直连（能通就不绕代理），失败再走代理
    attempts: list[str | None] = []
    if direct_ok:
        attempts.append(None)
    if proxy:
        attempts.append(proxy)

    for index, candidate in enumerate(attempts, start=1):
        label = candidate or "直连"
        print(f"\n[{index}/{len(attempts)}] 通过 {label} 推送…")
        code, out = push_with(url, token, candidate)
        tail = "\n".join(out.splitlines()[-3:])
        if code == 0:
            print(f"✓ 推送成功（{label}）")
            print(tail)
            if candidate:
                print(f"\n提示：把代理写进本仓库配置可省去每次指定 ——")
                print(f"  git config http.proxy {candidate}")
                print(f"  git config https.proxy {candidate}")
            return 0
        print(f"✗ 失败：{tail[:200]}")
        if index < len(attempts):
            time.sleep(3)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
