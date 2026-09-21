"""推送后的远端校验与小配置（避免 PowerShell 的引号拆参数问题）。

跑法： .venv\\Scripts\\python.exe tools/github_check.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "Polnareff258/youyoumonitor"

#: 这些文件**绝对不该**出现在公开仓库里
FORBIDDEN = [
    ".env", "config.local.yaml", "patterns.yaml",
    "data/csmon.db", ".venv", "refs",
]

TOPICS = ["cs2", "counter-strike", "price-monitor", "buff", "youpin",
          "steam-market", "market-monitor", "python", "trading-tools"]


def gh(*args: str, check: bool = True) -> str:
    result = subprocess.run(["gh", *args], cwd=str(ROOT), capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        return f"__ERROR__ {result.stderr.strip()[:200]}"
    return result.stdout.strip()


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    return result.stdout.strip()


def main() -> int:
    print("=" * 66)
    print(f"  远端校验：{REPO}")
    print("=" * 66)

    # 1) 本地 HEAD 与远端 HEAD 是否一致
    local_head = git("rev-parse", "HEAD")
    remote_head = gh("api", f"repos/{REPO}/commits/main", "--jq", ".sha")
    print(f"\n本地 HEAD : {local_head}")
    print(f"远端 HEAD : {remote_head}")
    synced = local_head == remote_head
    print(f"同步状态   : {'一致 ✓' if synced else '不一致 ✗'}")

    # 2) 远端文件清单
    tree = gh("api", f"repos/{REPO}/git/trees/main?recursive=1")
    files: list[str] = []
    try:
        files = [n["path"] for n in json.loads(tree)["tree"] if n["type"] == "blob"]
    except (json.JSONDecodeError, KeyError, TypeError):
        print(f"  无法解析远端 tree：{tree[:160]}")
    print(f"\n远端文件数 : {len(files)}（本地提交 {len(git('ls-files').splitlines())} 个）")

    # 3) 敏感文件必须不存在
    print("\n敏感文件检查：")
    leaked: list[str] = []
    for name in FORBIDDEN:
        present = any(f == name or f.startswith(name + "/") for f in files)
        marker = "存在 ✗" if present else "不在 ✓"
        print(f"  {name:<22} {marker}")
        if present:
            leaked.append(name)

    # 4) 顶层结构
    tops: dict[str, int] = {}
    for path in files:
        top = path.split("/")[0]
        tops[top] = tops.get(top, 0) + 1
    print("\n顶层结构：")
    for name, count in sorted(tops.items()):
        print(f"  {name:<30} {count} 个文件")

    # 5) 关键文件确实在
    must_have = ["README.md", "LICENSE", "start.cmd", "start.ps1", "start.sh",
                 "bootstrap.py", ".env.example", "config.local.example.yaml",
                 "config.yaml", ".gitignore", ".gitattributes",
                 "docs/DEPLOY.md", "docs/VARIANTS.md", "docs/DATA_SOURCES.md"]
    missing = [f for f in must_have if f not in files]
    print(f"\n关键文件：{'齐全 ✓' if not missing else '缺失 ✗ ' + str(missing)}")

    # 6) 行尾：远端 start.sh 必须是 LF
    blob = gh("api", f"repos/{REPO}/contents/start.sh", "--jq", ".content")
    if not blob.startswith("__ERROR__"):
        try:
            import base64
            raw = base64.b64decode(blob.replace("\n", ""))
            crlf = b"\r\n" in raw
            print(f"start.sh 行尾：{'CRLF ✗（Linux 会 bad interpreter）' if crlf else 'LF ✓'}")
        except Exception as exc:  # noqa: BLE001
            print(f"start.sh 行尾检查失败：{exc}")

    # 7) 设置主题
    payload = json.dumps({"names": TOPICS})
    result = subprocess.run(
        ["gh", "api", "-X", "PUT", f"repos/{REPO}/topics", "--input", "-",
         "--jq", ".names | join(\", \")"],
        cwd=str(ROOT), input=payload, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if result.returncode == 0:
        print(f"\n仓库主题已设置：{result.stdout.strip()}")
    else:
        print(f"\n主题设置失败：{result.stderr.strip()[:160]}")

    print("\n" + "=" * 66)
    if leaked:
        print(f"  ✗ 有 {len(leaked)} 个敏感文件被推送：{leaked}")
        return 1
    if not synced or missing:
        print("  ! 存在问题，见上文")
        return 1
    print(f"  ✓ 校验通过  https://github.com/{REPO}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
