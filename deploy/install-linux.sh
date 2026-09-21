#!/usr/bin/env bash
# 把 youyoumonitor 安装为 systemd 服务（Linux / Raspberry Pi）
#
#   sudo ./deploy/install-linux.sh              # 安装并启动
#   sudo ./deploy/install-linux.sh --uninstall  # 卸载
#   sudo ./deploy/install-linux.sh --user pi    # 指定运行用户（默认取 sudo 调用者）
#
# 装好后常用命令：
#   systemctl status csmon
#   journalctl -u csmon -f          # 跟随日志
#   systemctl restart csmon
#
# 树莓派注意：本脚本会提示把数据库移出 SD 卡（长期写入会磨损 SD 卡）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_NAME="csmon"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}.service"

GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; DIM=$'\033[2m'; RESET=$'\033[0m'
ok()   { printf '%s\n' "${GREEN}  ✓ ${RESET}$*"; }
warn() { printf '%s\n' "${YELLOW}  ! ${RESET}$*"; }
err()  { printf '%s\n' "${RED}  ✗ ${RESET}$*" >&2; }
dim()  { printf '%s\n' "${DIM}    $*${RESET}"; }

if [ "$(id -u)" -ne 0 ]; then
  err "需要 root 权限：sudo ./deploy/install-linux.sh"
  exit 1
fi

RUN_USER="${SUDO_USER:-root}"
UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) UNINSTALL=1; shift ;;
    --user) RUN_USER="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) err "未知参数：$1"; exit 1 ;;
  esac
done

if [ "$UNINSTALL" -eq 1 ]; then
  if systemctl list-unit-files | grep -q "^${UNIT_NAME}.service"; then
    systemctl stop "${UNIT_NAME}" 2>/dev/null || true
    systemctl disable "${UNIT_NAME}" 2>/dev/null || true
    rm -f "$UNIT_PATH"
    systemctl daemon-reload
    ok "已卸载 ${UNIT_NAME} 服务（项目文件与数据库未删除）"
  else
    warn "未找到 ${UNIT_NAME} 服务"
  fi
  exit 0
fi

# ── 前置检查 ───────────────────────────────────────────────
if [ ! -f "$PROJECT_DIR/bootstrap.py" ]; then
  err "在 $PROJECT_DIR 找不到 bootstrap.py，请把本脚本放在项目的 deploy/ 目录下"
  exit 1
fi

VENV_PY="$PROJECT_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  warn "尚未创建虚拟环境，先跑一次首次配置"
  sudo -u "$RUN_USER" "$PROJECT_DIR/start.sh" --setup || {
    err "首次配置失败，请先手动执行：./start.sh --setup"
    exit 1
  }
fi

if [ ! -x "$VENV_PY" ]; then
  err "虚拟环境仍不可用：$VENV_PY"
  exit 1
fi

# ── 生成 unit ──────────────────────────────────────────────
# 说明：这里用 bootstrap.py --foreground 而不是直接跑 csmon，
# 是为了让「依赖自检 + 平台调优 + 采集与看板同进程」这套逻辑在服务里也生效。
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=youyoumonitor - CS 饰品多源行情监控 (BUFF / 悠悠有品)
Documentation=file://${PROJECT_DIR}/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${PROJECT_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=NO_COLOR=1
ExecStart=${VENV_PY} ${PROJECT_DIR}/bootstrap.py --foreground --skip-install --interval \${CSMON_INTERVAL:-1800}
Restart=always
RestartSec=15
# 优雅停止：给采集循环留出收尾时间
TimeoutStopSec=30
KillSignal=SIGINT

# 资源限制：低配树莓派上防止异常膨胀拖垮整机
MemoryMax=512M
CPUQuota=80%

# 基础加固：只允许写项目目录与 /tmp
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=${PROJECT_DIR}

[Install]
WantedBy=multi-user.target
EOF

ok "已写入 ${UNIT_PATH}"

systemctl daemon-reload
systemctl enable "${UNIT_NAME}" >/dev/null
systemctl restart "${UNIT_NAME}"

sleep 3
if systemctl is-active --quiet "${UNIT_NAME}"; then
  ok "服务已启动并设为开机自启"
  echo
  dim "查看状态： systemctl status ${UNIT_NAME}"
  dim "跟随日志： journalctl -u ${UNIT_NAME} -f"
  dim "重启：     sudo systemctl restart ${UNIT_NAME}"
  dim "停止：     sudo systemctl stop ${UNIT_NAME}"
  dim "卸载：     sudo ./deploy/install-linux.sh --uninstall"
else
  err "服务启动失败，最近日志："
  journalctl -u "${UNIT_NAME}" -n 25 --no-pager || true
  exit 1
fi

# ── 树莓派专属提醒 ─────────────────────────────────────────
if [ -r /proc/device-tree/model ] && grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
  echo
  warn "树莓派提示：数据库默认在 ${PROJECT_DIR}/data/（SD 卡上）"
  dim "长期高频写入会磨损 SD 卡，建议改到 USB/SSD："
  dim "  1) 挂载外置盘到 /mnt/csmon-data"
  dim "  2) 在 ${PROJECT_DIR}/.env 里设置 CSMON_DB=/mnt/csmon-data/csmon.db"
  dim "  3) sudo systemctl restart ${UNIT_NAME}"
  dim "另外可把轮询拉长以省资源：在 .env 里设 CSMON_INTERVAL=3600"
fi

# ── 看板访问提醒 ───────────────────────────────────────────
BIND_HOST="$(grep -A3 '^web:' "$PROJECT_DIR/config.yaml" 2>/dev/null | grep 'host:' | head -1 | awk '{print $2}' || echo '127.0.0.1')"
PORT="$(grep -A3 '^web:' "$PROJECT_DIR/config.yaml" 2>/dev/null | grep 'port:' | head -1 | awk '{print $2}' || echo '8787')"
echo
dim "看板默认只绑回环（${BIND_HOST}:${PORT}）。"
dim "若要从其它机器访问，建议用 SSH 端口转发而不是改 bind："
dim "  ssh -L ${PORT}:127.0.0.1:${PORT} ${RUN_USER}@<树莓派IP>"
dim "然后在本地浏览器打开 http://127.0.0.1:${PORT}"
