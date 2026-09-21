"""控制台输出适配：让中文与符号在 Windows GBK 控制台下也不炸。

**为什么需要这个模块**

Windows 中文版的默认控制台代码页是 936(GBK)。这带来两类问题：

1. **源文件被错误解码**（更严重）。`.cmd`/`.ps1` 里的中文若以 UTF-8 保存，
   cmd.exe 会按 GBK 读取，字节错位后**可能吃掉换行符**，把两行合成一行，
   于是命令碎片被当作命令执行。这类故障现象是「莫名其妙的一堆
   'xxx' 不是内部或外部命令」，与代码逻辑毫无关系。
   → 对策：`start.cmd` 一律写成纯 ASCII，中文提示交给 Python 输出。

2. **宽字符无法编码**。`✓ ▸ ─ ★ —` 这些符号不在 GBK 字符集里，
   `print()` 会抛 UnicodeEncodeError 直接中断程序。
   → 对策：本模块把控制台切到 UTF-8；切不动就降级为 ASCII 符号集。

本模块在 `bootstrap.py` 与 `csmon/cli.py` 的入口处各调用一次，
覆盖「双击启动」与「命令行直接跑」两条路径。
"""
from __future__ import annotations

import os
import sys
from typing import Any

#: 符号集：优先 Unicode，控制台不支持时降级为纯 ASCII
SYMBOLS_UNICODE: dict[str, str] = {
    "ok": "✓", "warn": "!", "fail": "✗", "skip": "·",
    "arrow": "▸", "bullet": "—", "line": "─", "star": "★",
    "block": "█", "box": "▪",
}

SYMBOLS_ASCII: dict[str, str] = {
    "ok": "OK", "warn": "!", "fail": "X", "skip": ".",
    "arrow": ">", "bullet": "-", "line": "-", "star": "*",
    "block": "#", "box": "*",
}

#: 当前生效的符号集（由 setup_console 决定）
SYMBOLS: dict[str, str] = dict(SYMBOLS_ASCII)

_SETUP_DONE = False
_DETAIL: dict[str, Any] = {}
_ORIGINAL_CODEPAGE: int | None = None
_RESTORE_REGISTERED = False


def _register_codepage_restore() -> None:
    """退出时把控制台代码页还原回去。

    SetConsoleOutputCP 改的是**控制台**的状态，进程退出后不会自动恢复。
    改了不还原，会让用户后续在这个窗口里跑的命令看到乱码 —— 那是把自己的
    便利建立在别人的困惑上。
    """
    global _RESTORE_REGISTERED
    if _RESTORE_REGISTERED:
        return
    import atexit

    def _restore() -> None:
        if _ORIGINAL_CODEPAGE is None:
            return
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(_ORIGINAL_CODEPAGE)
            ctypes.windll.kernel32.SetConsoleCP(_ORIGINAL_CODEPAGE)
        except Exception:  # noqa: BLE001
            pass

    atexit.register(_restore)
    _RESTORE_REGISTERED = True


def setup_console(force: bool = False) -> dict[str, Any]:
    """把当前进程的控制台调到能正常显示中文与符号的状态。

    幂等。返回诊断信息（供 `--doctor-json` 之类的场景使用）。
    """
    global _SETUP_DONE, SYMBOLS, _ORIGINAL_CODEPAGE
    if _SETUP_DONE and not force:
        return dict(_DETAIL)

    detail: dict[str, Any] = {"platform": sys.platform}

    # 1) Windows：切控制台代码页到 UTF-8（并在退出时还原）
    if sys.platform.startswith("win"):
        codepage_ok = False
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            _ORIGINAL_CODEPAGE = int(kernel32.GetConsoleOutputCP())
            # 65001 = UTF-8。对输出与输入都设，避免混用。
            codepage_ok = bool(kernel32.SetConsoleOutputCP(65001))
            kernel32.SetConsoleCP(65001)
            _register_codepage_restore()
        except Exception as exc:  # noqa: BLE001 — 无控制台/受限环境时忽略
            detail["codepage_error"] = f"{type(exc).__name__}: {exc}"
        detail["codepage_utf8"] = codepage_ok
        detail["original_codepage"] = _ORIGINAL_CODEPAGE

        # 让子进程也走 UTF-8（Python 会读 PYTHONIOENCODING）
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # 2) 重设 stdout/stderr 编码
    #    errors="replace" 是关键：宁可把个别符号显示成 ?，也不要因为一个
    #    字符编码失败让整个程序崩掉。
    reconfigured = False
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
                reconfigured = True
            except (ValueError, OSError):
                pass
    detail["streams_reconfigured"] = reconfigured

    # 3) 判断能否安全输出 Unicode 符号
    detail["symbols"] = _probe_symbols()
    SYMBOLS = dict(SYMBOLS_UNICODE) if detail["symbols"] == "unicode" \
        else dict(SYMBOLS_ASCII)

    _SETUP_DONE = True
    _DETAIL.clear()
    _DETAIL.update(detail)
    return dict(_DETAIL)


def _probe_symbols() -> str:
    """探测当前输出流能否编码 Unicode 符号。"""
    stream = getattr(sys, "stdout", None)
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        for symbol in ("✓", "─", "★"):
            symbol.encode(encoding)
        return "unicode"
    except (UnicodeEncodeError, LookupError, TypeError):
        return "ascii"


def sym(name: str) -> str:
    """取一个符号，控制台不支持时自动降级。"""
    if not _SETUP_DONE:
        setup_console()
    return SYMBOLS.get(name, SYMBOLS_ASCII.get(name, ""))


def safe_print(*args: Any, **kwargs: Any) -> None:
    """print 的容错版本：编码失败时降级而不是抛异常。

    适用于看板之外的终端输出。`file` 默认 stdout。
    """
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        stream = kwargs.get("file") or sys.stdout
        encoding = getattr(stream, "encoding", "ascii") or "ascii"
        cleaned = []
        for arg in args:
            text = str(arg)
            cleaned.append(text.encode(encoding, errors="replace").decode(encoding))
        print(*cleaned, **kwargs)


def describe() -> str:
    """一行诊断信息，用于排错。"""
    if not _SETUP_DONE:
        setup_console()
    enc = getattr(sys.stdout, "encoding", "?")
    return (f"stdout={enc} symbols={_DETAIL.get('symbols', '?')} "
            f"codepage_utf8={_DETAIL.get('codepage_utf8', 'n/a')}")
