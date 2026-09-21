#!/usr/bin/env python3
"""youyoumonitor 一键启动器（Windows / Linux / 树莓派 / macOS 通用）。

设计目标：把「从零到跑起来」压成一条命令，且可重复执行不出错。

    python bootstrap.py              # 检查环境 → 装依赖 → 自检 → 启动采集+看板
    python bootstrap.py --setup      # 只建虚拟环境与依赖
    python bootstrap.py --check      # 只自检（不启动）
    python bootstrap.py --run        # 只跑一轮采集
    python bootstrap.py --serve      # 只启动看板
    python bootstrap.py --daemon     # 后台常驻（写 PID 与日志）
    python bootstrap.py --stop       # 停止后台实例
    python bootstrap.py --status     # 查看后台实例状态

它做三件容易出错的事，并且做对：
  1. 解释器自举：当前 Python 不满足要求时，自动找到/创建合适的 venv 并重新进入；
  2. 依赖安装：按平台选择 pip 索引（32 位 ARM 附加 piwheels，避免本地编译）；
  3. 启动前自检：把「缺凭证 / 不可写 / 缺依赖」这类问题在启动前讲清楚。
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 控制台适配必须在任何输出之前：Windows 中文版控制台默认是 GBK，
# 直接 print("✓") 会抛 UnicodeEncodeError 让启动器崩在第一步。
from csmon.console import safe_print, setup_console, sym  # noqa: E402

CONSOLE = setup_console()

MIN_PYTHON = (3, 10)
RECOMMENDED_PYTHON = (3, 11)

RUN_DIR = ROOT / "run"
LOG_DIR = ROOT / "logs"
PID_FILE = RUN_DIR / "csmon.pid"
LOG_FILE = LOG_DIR / "csmon.log"

C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_CYAN = "\033[36m"


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if sys.platform.startswith("win"):
        # Windows 10+ 的终端支持 ANSI；旧版 cmd 会显示乱码，交给下面显式开关
        return bool(os.environ.get("WT_SESSION") or os.environ.get("ANSICON")
                    or os.environ.get("TERM") or sys.getwindowsversion().major >= 10)
    return True


USE_COLOR = _supports_color()


def c(text: str, color: str = "") -> str:
    return f"{color}{text}{C_RESET}" if USE_COLOR and color else text


def banner() -> None:
    from csmon.platform import summary_line

    sym_ = lambda n, fallback=" ": sym(n) or fallback
    print()
    safe_print(c("  youyoumonitor", C_BOLD + C_CYAN)
               + c("  ·  CS 饰品多源行情监控（BUFF / 悠悠有品）", C_DIM))
    safe_print(c(f"  {summary_line()}", C_DIM))
    safe_print(c("  " + sym_("line", "-") * 62, C_DIM))


def step(text: str) -> None:
    safe_print(c(f"  {sym('arrow')} ", C_CYAN) + text)


def info(text: str) -> None:
    safe_print(c("    ", C_DIM) + text)


def warn(text: str) -> None:
    safe_print(c(f"  {sym('warn')} ", C_YELLOW) + text)


def fail(text: str) -> None:
    safe_print(c(f"  {sym('fail')} ", C_RED) + text)


def ok(text: str) -> None:
    safe_print(c(f"  {sym('ok')} ", C_GREEN) + text)


# ── 虚拟环境自举 ───────────────────────────────────────────

def venv_python() -> Path:
    from csmon.platform import venv_paths

    return venv_paths(ROOT)["python"]


def running_in_target_venv() -> bool:
    try:
        return Path(sys.executable).resolve() == venv_python().resolve()
    except OSError:
        return False


def ensure_venv(skip: bool) -> Path:
    """确保 .venv 存在；返回其中的 python 路径（或当前解释器）。"""
    from csmon.platform import find_system_python

    if skip:
        return Path(sys.executable)

    target = venv_python()
    if target.exists():
        return target

    step("创建虚拟环境 .venv")
    base = find_system_python()
    if not base:
        fail("找不到 Python 3.10+ 解释器")
        info("Windows：从 https://www.python.org/downloads/ 安装并勾选 Add to PATH")
        info("Debian/树莓派：sudo apt install -y python3 python3-venv python3-pip")
        raise SystemExit(2)

    result = subprocess.run([base, "-m", "venv", str(ROOT / ".venv")],
                            capture_output=True, text=True)
    if result.returncode != 0:
        fail("创建虚拟环境失败")
        info((result.stderr or result.stdout).strip()[:400])
        info("Debian/Ubuntu 需要先装：sudo apt install -y python3-venv")
        raise SystemExit(2)

    if not target.exists():
        fail(f"虚拟环境创建后仍找不到 {target}")
        raise SystemExit(2)
    ok(f"已创建 {target}")
    return target


def install_deps(python: Path, force: bool = False) -> None:
    """安装依赖。已装齐则跳过（除非 force）。"""
    from csmon.platform import pip_index_args

    if not force and _deps_ready(python):
        ok("依赖已就绪（跳过安装）")
        return

    step("安装依赖（首次约需 1-3 分钟）")
    python = Path(python)

    # 先升级 pip；失败不致命（某些发行版的 venv 自带 pip 就够用）
    subprocess.run([str(python), "-m", "pip", "install", "--quiet",
                    "--upgrade", "pip"], capture_output=True, text=True)

    req = ROOT / "requirements.txt"
    cmd = [str(python), "-m", "pip", "install", "--quiet", "-r", str(req)]
    cmd += pip_index_args()
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        fail("依赖安装失败")
        tail = (result.stderr or result.stdout).strip().splitlines()
        for line in tail[-12:]:
            info(line)
        info("")
        info("常见原因与处理：")
        info("  · 网络问题 → 换镜像：CSMON_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple")
        info("  · 32 位 ARM 编译失败 → 换 64 位系统，或 sed -i 改用 piwheels")
        info("  · 缺编译工具 → sudo apt install -y build-essential python3-dev")
        raise SystemExit(3)

    ok("依赖安装完成")


def _deps_ready(python: Path) -> bool:
    probe = "import requests, yaml, fastapi, uvicorn, pydantic"
    result = subprocess.run([str(python), "-c", probe],
                            capture_output=True, text=True)
    return result.returncode == 0


def reexec_in_venv(args: list[str]) -> None:
    """若当前不在目标 venv 里，则用 venv 解释器重新执行本脚本。

    注意：execv 不会自动 flush Python 层缓冲，必须显式 flush，
    否则「切换解释器」这类提示可能丢失或与子进程输出交错。
    """
    if running_in_target_venv() or not venv_python().exists():
        return
    target = venv_python()
    info(f"切换到虚拟环境解释器：{target}")
    cmd = [str(target), str(Path(__file__).resolve()), *args]
    sys.stdout.flush()
    sys.stderr.flush()
    if sys.platform.startswith("win"):
        raise SystemExit(subprocess.call(cmd))
    os.execv(str(target), cmd)


# ── 自检 ───────────────────────────────────────────────────

def run_doctor(config, network: bool) -> bool:
    from csmon.doctor import format_report, run_checks

    step("运行环境自检")
    report = run_checks(config, network=network)
    print(format_report(report))
    if not report.ok:
        fail("存在阻断性问题，已停止启动")
        info("修复上面标 ✗ 的项后重试；只跑自检：python bootstrap.py --check")
        return False
    if report.warned:
        warn(f"{len(report.warned)} 项提示：功能会受限，但不影响启动")
    return True


# ── 后台运行管理 ───────────────────────────────────────────

def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    if sys.platform.startswith("win"):
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                capture_output=True, text=True)
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def status() -> int:
    pid = _read_pid()
    if pid is None:
        print("后台实例：未运行（无 PID 文件）")
        return 1
    if _pid_alive(pid):
        print(f"后台实例：运行中（PID {pid}）")
        print(f"日志：{LOG_FILE}")
        return 0
    print(f"后台实例：已停止（残留 PID {pid}）")
    return 1


def stop() -> int:
    pid = _read_pid()
    if pid is None:
        print("没有找到运行中的后台实例")
        return 1
    if not _pid_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        print("进程已不在，清理了残留 PID 文件")
        return 0

    print(f"正在停止 PID {pid} …")
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
            for _ in range(30):
                if not _pid_alive(pid):
                    break
                time.sleep(0.5)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        fail(f"停止失败：{exc}")
        return 1

    PID_FILE.unlink(missing_ok=True)
    ok("已停止")
    return 0


def daemonize(argv: list[str]) -> int:
    """后台启动：以 --foreground 重新拉起自己，写 PID 与日志。"""
    pid = _read_pid()
    if pid and _pid_alive(pid):
        warn(f"已有实例在运行（PID {pid}）；先执行 --stop")
        return 1

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, str(Path(__file__).resolve()), "--foreground", *argv]
    log_handle = open(LOG_FILE, "a", encoding="utf-8")  # noqa: SIM115 — 交给子进程持有

    if sys.platform.startswith("win"):
        creation = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=str(ROOT),
                                creationflags=creation)
    else:
        proc = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=str(ROOT),
                                start_new_session=True)

    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    ok(f"已在后台启动（PID {proc.pid}）")
    info(f"日志：{LOG_FILE}")
    info("查看状态：python bootstrap.py --status")
    info("停止：python bootstrap.py --stop")
    return 0


# ── 启动 ───────────────────────────────────────────────────

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="youyoumonitor 一键启动器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-c", "--config", default=None, help="配置文件（默认 config.yaml）")
    p.add_argument("--setup", action="store_true", help="只建环境装依赖，不启动")
    p.add_argument("--check", action="store_true", help="只自检，不启动")
    p.add_argument("--no-network", action="store_true", help="自检跳过网络连通性探测")
    p.add_argument("--skip-install", action="store_true", help="跳过依赖安装")
    p.add_argument("--no-venv", action="store_true", help="不使用虚拟环境，直接用当前解释器")
    p.add_argument("--reinstall", action="store_true", help="强制重装依赖")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="只跑一轮采集后退出")
    mode.add_argument("--serve", action="store_true", help="只启动看板")
    mode.add_argument("--collect", action="store_true",
                      help="只常驻采集，不起看板（适合树莓派省资源）")

    p.add_argument("--loop", action="store_true",
                   help="与 --serve 同用时：采集常驻 + 看板常驻")
    p.add_argument("--interval", type=int, default=None, help="采集间隔秒（默认按平台自动）")
    p.add_argument("--host", default=None, help="看板监听地址（默认 127.0.0.1）")
    p.add_argument("--port", type=int, default=None, help="看板端口（默认 8787）")

    p.add_argument("--daemon", action="store_true", help="后台常驻")
    p.add_argument("--foreground", action="store_true",
                   help=argparse.SUPPRESS)  # 内部使用：--daemon 的子进程标记
    p.add_argument("--stop", action="store_true", help="停止后台实例")
    p.add_argument("--status", action="store_true", help="查看后台实例状态")
    p.add_argument("--doctor-json", action="store_true",
                   help="以 JSON 输出自检结果（供脚本消费）")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))

    # 这两个子命令不需要环境与配置
    if args.stop:
        return stop()
    if args.status:
        return status()

    # 1) 环境自举必须发生在打印 banner 之前：
    #    否则切到 venv 重新执行后 banner 会打两遍
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    ensure_venv(skip=args.no_venv)
    if not args.no_venv:
        reexec_in_venv(raw_argv)

    banner()

    # 2) 依赖
    if not args.skip_install:
        install_deps(Path(sys.executable), force=args.reinstall)

    # 3) 配置与平台调优
    from csmon.config import load_config
    from csmon.platform import detect

    profile = detect()
    config = load_config(args.config)
    if args.interval:
        config.poll_interval = args.interval
    else:
        config.poll_interval = config.poll_interval or profile.default_poll_interval
    if args.host:
        config.web.host = args.host
    if args.port:
        config.web.port = args.port

    # 4) 自检
    if args.check or args.doctor_json:
        from csmon.doctor import run_checks
        report = run_checks(config, network=not args.no_network, profile=profile)
        if args.doctor_json:
            import json
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            from csmon.doctor import format_report
            print(format_report(report))
        return 0 if report.ok else 1

    if not run_doctor(config, network=not args.no_network):
        return 1

    if args.setup:
        ok("环境准备完成。启动命令：python bootstrap.py")
        return 0

    # 5) 启动
    if args.daemon and not args.foreground:
        passthrough = [a for a in raw_argv if a not in ("--daemon",)]
        return daemonize(passthrough)

    if args.run:
        return _run_once(config)
    if args.collect:
        return _collect_only(config)
    if args.serve:
        return _serve_only(config)
    if args.loop:
        return _run_all(config, with_web=True)
    return _run_all(config, with_web=True)


def _run_once(config) -> int:
    from csmon.scheduler import Monitor

    step("执行一轮采集")
    with Monitor(config) as monitor:
        monitor.seed_watchlist()
        result = monitor.run_once()
        print()
        print("  " + result.summary())
        for note in result.notes:
            info("· " + note)
        for report in result.reports:
            tag = c("OK ", C_GREEN) if not report.failed else c("部分失败", C_YELLOW)
            info(f"[{tag}] {report.source}: 请求 {report.requested} "
                 f"成功 {report.succeeded} 报价 {report.quotes}")
            for err in report.errors[:3]:
                info(f"    ! {err}")
        if result.alerts:
            print()
            for ev in result.alerts:
                color = C_RED if ev.severity == "critical" else C_YELLOW
                print(c(f"  [{ev.severity}] ", color) + f"{ev.market_hash_name} "
                      f"@{ev.platform} {ev.message}")
    return 0


def _build_monitor(config):
    from csmon.scheduler import Monitor

    monitor = Monitor(config)
    monitor.seed_watchlist()
    return monitor


def _collect_only(config) -> int:
    monitor = _build_monitor(config)
    try:
        ok(f"常驻采集已启动（间隔 {config.poll_interval}s，Ctrl+C 退出）")
        info("提示：树莓派等低资源环境建议用 --collect 而不起看板")
        monitor.run_forever(config.poll_interval)
    except KeyboardInterrupt:
        print()
        ok("已停止")
    finally:
        monitor.close()
    return 0


def _serve_only(config) -> int:
    from csmon.web import create_app
    import uvicorn

    app = create_app(config)
    ok(f"看板地址：http://{config.web.host}:{config.web.port}")
    info("按 Ctrl+C 退出")
    uvicorn.run(app, host=config.web.host, port=config.web.port, log_level="warning")
    return 0


def _run_all(config, with_web: bool = True) -> int:
    """采集常驻 + 看板常驻（看板跑在子线程，采集跑在主线程）。"""
    import threading

    monitor = _build_monitor(config)
    stop_event = threading.Event()

    if with_web:
        from csmon.web import create_app
        import uvicorn

        app = create_app(config, monitor.store, monitor)
        server_config = uvicorn.Config(app, host=config.web.host,
                                       port=config.web.port, log_level="warning")
        server = uvicorn.Server(server_config)

        def serve() -> None:
            try:
                server.run()
            except Exception as exc:  # noqa: BLE001
                warn(f"看板异常退出：{exc}")

        threading.Thread(target=serve, name="csmon-web", daemon=True).start()
        ok(f"看板地址：http://{config.web.host}:{config.web.port}")

    ok(f"采集间隔：{config.poll_interval}s")
    if config.notify.enabled:
        mode = "dry-run（只打印不发送）" if config.notify.dry_run else "已启用"
        info(f"通知：{mode}，渠道 {len(config.notify.channels)} 个")
    else:
        info("通知：未启用（config.yaml → notify.enabled）")
    info("按 Ctrl+C 退出")
    print()

    try:
        monitor.run_forever(config.poll_interval)
    except KeyboardInterrupt:
        print()
        ok("正在停止…")
    finally:
        stop_event.set()
        monitor.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
