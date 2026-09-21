"""平台适配：识别运行环境并按硬件能力调整默认参数。

为什么要单独一层：同一个工具在 Windows 台式机、x86 Linux 服务器、
树莓派（armv7/arm64，512MB~4GB 内存）上的合理默认值完全不同。
把「按平台调参」集中在这里，而不是散落在各模块的魔法数字里。

树莓派要点：
  - 32 位系统（armv7l）缺很多 wheel，pydantic-core 需要 Rust 工具链才能编译；
    优先引导用户用 64 位系统，同时在安装时附加 piwheels 源作为兜底。
  - 内存小、SD 卡 IO 慢 → 降低并发、拉长轮询间隔、SQLite 用更保守的同步级别。
  - 常见于 7×24 常驻场景 → 默认建议装 systemd 服务。
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


def _is_raspberry_pi() -> bool:
    """判定是否树莓派（model 文件是最可靠的信号，其次看设备树）。"""
    model = Path("/proc/device-tree/model")
    if model.exists():
        try:
            if "raspberry pi" in model.read_text(errors="ignore").lower():
                return True
        except OSError:
            pass
    for candidate in ("/proc/cpuinfo", "/sys/firmware/devicetree/base/model"):
        p = Path(candidate)
        if p.exists():
            try:
                text = p.read_text(errors="ignore").lower()
            except OSError:
                continue
            if "raspberry pi" in text or "bcm27" in text:
                return True
    return False


def _total_memory_mb() -> int:
    """总内存（MB）。读不到时返回 0，调用方按未知处理。"""
    if sys.platform.startswith("linux"):
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            try:
                for line in meminfo.read_text().splitlines():
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) // 1024
            except (OSError, ValueError, IndexError):
                pass
        return 0
    if sys.platform == "win32":
        try:
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys // (1024 * 1024))
        except Exception:  # noqa: BLE001
            return 0
    return 0


@dataclass(frozen=True)
class PlatformProfile:
    """当前运行环境的画像，供各处读取默认参数。"""

    os_name: str            # windows / linux / darwin
    arch: str               # x86_64 / aarch64 / armv7l ...
    is_pi: bool
    is_arm: bool
    is_64bit: bool
    memory_mb: int
    python: str
    python_version: str
    # ── 调优后的默认值 ──
    max_fetch_workers: int
    default_poll_interval: int
    sqlite_synchronous: str
    recommend_service: bool
    notes: tuple[str, ...] = ()

    @property
    def is_windows(self) -> bool:
        return self.os_name == "windows"

    @property
    def is_linux(self) -> bool:
        return self.os_name == "linux"

    @property
    def is_low_resource(self) -> bool:
        """低资源环境（树莓派 Zero/3、小内存 VPS）：需要更克制的默认值。"""
        return self.is_pi or (0 < self.memory_mb <= 1024)

    def describe(self) -> dict[str, object]:
        return {
            "os": self.os_name, "arch": self.arch, "is_pi": self.is_pi,
            "is_arm": self.is_arm, "is_64bit": self.is_64bit,
            "memory_mb": self.memory_mb,
            "python": self.python, "python_version": self.python_version,
            "max_fetch_workers": self.max_fetch_workers,
            "default_poll_interval": self.default_poll_interval,
            "sqlite_synchronous": self.sqlite_synchronous,
            "recommend_service": self.recommend_service,
            "notes": list(self.notes),
        }


_CACHED: PlatformProfile | None = None


def detect(force: bool = False) -> PlatformProfile:
    """探测当前平台并给出调优参数（结果缓存）。"""
    global _CACHED
    if _CACHED is not None and not force:
        return _CACHED

    arch = platform.machine().lower()
    is_arm = arch.startswith(("arm", "aarch"))
    is_64bit = sys.maxsize > 2**32
    is_pi = _is_raspberry_pi()
    memory_mb = _total_memory_mb()

    if sys.platform.startswith("win"):
        os_name = "windows"
    elif sys.platform == "darwin":
        os_name = "darwin"
    else:
        os_name = "linux"

    notes: list[str] = []
    recommend_service = False

    # 并发数：按平台与内存给保守值。适配器之间并发，源内部串行限速，
    # 所以 2~4 已经足够，没必要开大。
    if is_pi or (0 < memory_mb <= 1024):
        max_workers = 2
        poll_interval = 3600
        sqlite_sync = "NORMAL"
        notes.append("检测到低资源环境（树莓派/小内存），已降低并发并拉长轮询间隔")
        recommend_service = True
    elif 0 < memory_mb <= 2048:
        max_workers = 3
        poll_interval = 2700
        sqlite_sync = "NORMAL"
    else:
        max_workers = 4
        poll_interval = 1800
        sqlite_sync = "NORMAL"

    if is_arm and not is_64bit:
        notes.append(
            "32 位 ARM 系统：部分依赖缺少预编译 wheel（pydantic-core 需 Rust 工具链）。"
            "强烈建议改用 64 位系统（Raspberry Pi OS 64-bit / Ubuntu arm64）")
    if os_name == "linux":
        recommend_service = True

    if is_pi:
        notes.append("树莓派建议把数据库放在 USB/SSD 上，SD 卡长期频繁写入容易损坏")

    profile = PlatformProfile(
        os_name=os_name,
        arch=arch,
        is_pi=is_pi,
        is_arm=is_arm,
        is_64bit=is_64bit,
        memory_mb=memory_mb,
        python=sys.executable,
        python_version=platform.python_version(),
        max_fetch_workers=max_workers,
        default_poll_interval=poll_interval,
        sqlite_synchronous=sqlite_sync,
        recommend_service=recommend_service,
        notes=tuple(notes),
    )
    _CACHED = profile
    return profile


def pip_index_args(profile: PlatformProfile | None = None) -> list[str]:
    """按平台给出额外的 pip 索引参数。

    32 位 ARM 上 piwheels 提供大量预编译轮子，能省掉本地编译。
    仅在 32 位 ARM 或显式设置 CSMON_PIP_EXTRA_INDEX 时启用。
    """
    prof = profile or detect()
    extra: list[str] = []
    if prof.is_arm and not prof.is_64bit:
        extra += ["--extra-index-url", "https://www.piwheels.org/simple"]
    env_extra = os.environ.get("CSMON_PIP_EXTRA_INDEX")
    if env_extra:
        extra += ["--extra-index-url", env_extra]
    return extra


def venv_paths(root: Path) -> dict[str, Path]:
    """返回虚拟环境的关键路径（跨平台）。"""
    venv = root / ".venv"
    if sys.platform.startswith("win"):
        bin_dir = venv / "Scripts"
        python = bin_dir / "python.exe"
    else:
        bin_dir = venv / "bin"
        python = bin_dir / "python"
    return {
        "venv": venv,
        "bin": bin_dir,
        "python": python,
        "pip": bin_dir / ("pip.exe" if sys.platform.startswith("win") else "pip"),
    }


def find_system_python() -> str | None:
    """找一个可用的系统 Python 3.10+ 解释器。"""
    candidates: list[str] = []
    if sys.version_info >= (3, 10):
        candidates.append(sys.executable)
    for name in ("python3.13", "python3.12", "python3.11", "python3.10",
                 "python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for cand in candidates:
        try:
            import subprocess

            out = subprocess.run(
                [cand, "-c", "import sys; print(sys.version_info[:2])"],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode == 0 and "3, 1" in out.stdout:
                return cand
        except Exception:  # noqa: BLE001
            continue
    return None


def summary_line(profile: PlatformProfile | None = None) -> str:
    prof = profile or detect()
    bits = [prof.os_name, prof.arch, f"Python {prof.python_version}"]
    if prof.is_pi:
        bits.append("Raspberry Pi")
    if prof.memory_mb:
        bits.append(f"{prof.memory_mb}MB RAM")
    return " · ".join(bits)
