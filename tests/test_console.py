"""控制台编码适配测试。

这组测试保护的是「在中文 Windows 上能不能跑起来」——
这类故障的现象是一堆「'xxx' 不是内部或外部命令」，与代码逻辑毫无关系，
排查起来极耗时间。所以把结论钉成测试。
"""
from __future__ import annotations

import codecs
from pathlib import Path

import pytest

from facet import console as console_mod

ROOT = Path(__file__).resolve().parent.parent


# ── 符号降级 ───────────────────────────────────────────────

def test_setup_console_is_idempotent() -> None:
    first = console_mod.setup_console(force=True)
    second = console_mod.setup_console()
    assert first["symbols"] in ("unicode", "ascii")
    assert second["symbols"] == first["symbols"]


def test_sym_returns_something_for_every_key() -> None:
    console_mod.setup_console()
    for key in ("ok", "warn", "fail", "arrow", "line", "star", "bullet"):
        value = console_mod.sym(key)
        assert value, f"符号 {key} 不能为空"


def test_ascii_symbols_are_encodable_in_gbk() -> None:
    """降级符号集必须能编进 GBK —— 否则降级没有意义。"""
    gbk = codecs.lookup("gbk")
    for key, value in console_mod.SYMBOLS_ASCII.items():
        value.encode("gbk")      # 不抛异常即通过


def test_unicode_symbols_are_not_all_gbk_encodable() -> None:
    """反证：Unicode 符号集里确实有 GBK 编不进去的字符。

    这条测试说明「降级逻辑是必要的」—— 如果哪天全都能编码了，
    说明符号集被改过，应该重新评估降级分支是否还需要。
    """
    failures = []
    for key, value in console_mod.SYMBOLS_UNICODE.items():
        try:
            value.encode("gbk")
        except UnicodeEncodeError:
            failures.append(key)
    assert failures, "Unicode 符号集全都可编入 GBK，降级分支可能已无必要"


class _AsciiOnlyStream:
    """只接受 ASCII 的输出流，写入非 ASCII 时抛 UnicodeEncodeError。

    模拟真实场景：控制台代码页没切成功、或重定向到只认 ASCII 的管道。
    """

    encoding = "ascii"

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        text.encode("ascii")          # 非 ASCII 在此抛出
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    @property
    def value(self) -> str:
        return "".join(self.chunks)


def test_safe_print_degrades_on_ascii_only_stream() -> None:
    """输出流只认 ASCII 时，safe_print 必须降级为可编码文本而不是崩掉。"""
    stream = _AsciiOnlyStream()
    console_mod.safe_print("中文 ✓ ★", file=stream)      # 不应抛异常
    assert stream.value.strip(), "降级后仍应有输出"
    stream.value.encode("ascii")                        # 结果确实是纯 ASCII


def test_safe_print_ascii_input_passes_through() -> None:
    stream = _AsciiOnlyStream()
    console_mod.safe_print("plain ascii", file=stream)
    assert stream.value.strip() == "plain ascii"


def test_safe_print_passes_through_normally() -> None:
    """能正常编码时不做任何改写。"""
    stream = _AsciiOnlyStream()
    console_mod.safe_print("normal output", file=stream)
    assert stream.value.strip() == "normal output"


def test_describe_is_a_single_line() -> None:
    console_mod.setup_console(force=True)
    text = console_mod.describe()
    assert "stdout=" in text and "\n" not in text


# ── 入口文件的编码约束 ─────────────────────────────────────
#
# 这几条是本次「一堆 'xxx' 不是内部或外部命令」故障的根因回归测试。

def test_start_cmd_is_pure_ascii() -> None:
    """start.cmd 必须是纯 ASCII。

    cmd.exe 用系统 OEM 代码页（中文 Windows 是 936）读 .cmd 文件。
    UTF-8 的中文会被错误解码，字节错位时甚至吃掉换行符把两行合成一行，
    于是命令碎片被当成命令执行 —— 现象就是刷屏的「不是内部或外部命令」。
    """
    raw = (ROOT / "start.cmd").read_bytes()
    offenders = [(i, b) for i, b in enumerate(raw) if b > 127]
    assert not offenders, (
        f"start.cmd 含 {len(offenders)} 个非 ASCII 字节，"
        f"首个位于偏移 {offenders[0][0]}（第 "
        f"{raw[:offenders[0][0]].count(10) + 1} 行）。"
        "请把中文提示交给 Python 输出，本文件保持纯 ASCII。")


def test_start_cmd_has_no_bom() -> None:
    """cmd.exe 会把 BOM 当成命令的一部分，报 '' 不是内部或外部命令。"""
    raw = (ROOT / "start.cmd").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "start.cmd 不能有 UTF-8 BOM"


def test_powershell_scripts_have_bom_if_non_ascii() -> None:
    """含中文的 .ps1 必须有 UTF-8 BOM。

    Windows PowerShell 5.1 读无 BOM 的 .ps1 会按系统 ANSI（GBK）解码，
    中文变乱码，严重时字节错位破坏语法导致脚本无法解析。
    """
    for name in ("start.ps1", "deploy/install-windows.ps1"):
        path = ROOT / name
        if not path.exists():
            continue
        raw = path.read_bytes()
        has_non_ascii = any(b > 127 for b in raw)
        has_bom = raw.startswith(b"\xef\xbb\xbf")
        if has_non_ascii:
            assert has_bom, f"{name} 含中文但缺少 UTF-8 BOM，PS 5.1 下会乱码"


def test_shell_script_has_no_bom() -> None:
    """反过来的约束：shell 脚本绝不能有 BOM（会破坏 shebang）。"""
    raw = (ROOT / "start.sh").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "start.sh 不能有 BOM，否则 shebang 失效"
    assert raw.startswith(b"#!"), "start.sh 必须以 shebang 开头"


def test_shell_script_is_valid_utf8() -> None:
    (ROOT / "start.sh").read_bytes().decode("utf-8")


def test_service_unit_script_is_valid_utf8() -> None:
    (ROOT / "deploy" / "install-linux.sh").read_bytes().decode("utf-8")


# ── doctor 集成 ────────────────────────────────────────────

def test_doctor_reports_console_encoding(tmp_path: Path) -> None:
    """自检报告要包含控制台编码状态，便于远程排错。"""
    from facet.config import Config, NotifyConfig, SourceConfig, WebConfig
    from facet.doctor import run_checks

    cfg = Config(database=str(tmp_path / "d.db"),
                 sources={"mock": SourceConfig(name="mock", enabled=True)},
                 notify=NotifyConfig(), web=WebConfig())
    report = run_checks(cfg, network=False)
    names = [c.name for c in report.checks]
    assert any("控制台" in n for n in names), names
