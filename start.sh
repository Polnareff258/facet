#!/usr/bin/env bash
# facet 一键启动（Linux / Raspberry Pi / macOS）
#
#   ./start.sh                  # 建环境 → 装依赖 → 自检 → 启动采集+看板
#   ./start.sh --setup          # 只准备环境
#   ./start.sh --check          # 只自检
#   ./start.sh --collect        # 只常驻采集（树莓派省资源）
#   ./start.sh --daemon         # 后台常驻
#   ./start.sh --stop           # 停止后台实例
#   ./start.sh --status         # 查看状态
#
# 首次使用请先赋予执行权限： chmod +x start.sh

set -euo pipefail

# 切到脚本所在目录，保证双击/从任意路径调用都能工作
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; RESET=""
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'
  YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s\n' "${GREEN}  ✓ ${RESET}$*"; }
warn() { printf '%s\n' "${YELLOW}  ! ${RESET}$*"; }
err()  { printf '%s\n' "${RED}  ✗ ${RESET}$*" >&2; }

# ── 找 Python 3.10+ ────────────────────────────────────────
find_python() {
  local candidates=()
  # 优先用已有的 venv（第二次启动会快很多）
  if [ -x ".venv/bin/python" ]; then
    printf '%s' ".venv/bin/python"; return 0
  fi
  candidates+=(python3.13 python3.12 python3.11 python3.10 python3 python)
  for name in "${candidates[@]}"; do
    if command -v "$name" >/dev/null 2>&1; then
      if "$name" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        command -v "$name"; return 0
      fi
    fi
  done
  return 1
}

if ! PYTHON="$(find_python)"; then
  err "找不到 Python 3.10 或更高版本"
  say ""
  say "  ${BOLD}按平台安装：${RESET}"
  say "    Debian / Ubuntu / Raspberry Pi OS:"
  say "      sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
  say "    Fedora / RHEL:"
  say "      sudo dnf install -y python3 python3-pip"
  say "    macOS:"
  say "      brew install python@3.12"
  say ""
  say "  装完再执行 ./start.sh"
  exit 2
fi

# 树莓派提示：低资源环境建议只用 --collect
if [ -r /proc/device-tree/model ] && grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
  MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo 'Raspberry Pi')"
  say "${DIM}  检测到：${MODEL}${RESET}"
  if [ -z "${FACET_PI_HINT_SHOWN:-}" ]; then
    ARCH="$(uname -m)"
    case "$ARCH" in
      armv7l|armv6l)
        warn "当前是 32 位系统（$ARCH）。部分依赖缺少预编译包，可能安装失败。"
        warn "强烈建议改用 64 位系统（Raspberry Pi OS 64-bit），再重新运行本脚本。"
        ;;
    esac
    say "${DIM}  提示：树莓派常驻可只跑采集以省资源 → ./start.sh --collect${RESET}"
    say "${DIM}        安装为开机自启服务 → sudo ./deploy/install-linux.sh${RESET}"
    say ""
  fi
fi

exec "$PYTHON" bootstrap.py "$@"
