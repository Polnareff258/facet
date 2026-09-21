"""doctor：开跑之前的自检。

一次跑完所有「能让工具跑不起来」的检查项，并给出可直接照做的修复建议。
设计原则：**永远不因为某项失败就拒绝启动**——只报告，让用户自己决定。
（监控工具最糟的行为是静默不工作，第二糟是在缺一个可选凭证时拒绝启动。）
"""
from __future__ import annotations

import importlib
import shutil
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, SOURCE_BUFF, SOURCE_BUFF_DIRECT
from .platform import PlatformProfile, detect, pip_index_args
from .sources import REGISTRY

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_FAIL = "fail"

REQUIRED_MODULES = ["requests", "yaml"]
OPTIONAL_MODULES = ["fastapi", "uvicorn", "pydantic", "jinja2"]


@dataclass
class Check:
    name: str
    level: str
    detail: str
    fix: str = ""

    @property
    def icon(self) -> str:
        return {"ok": "✓", "warn": "!", "fail": "✗"}.get(self.level, "?")


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    profile: PlatformProfile | None = None

    def add(self, name: str, level: str, detail: str, fix: str = "") -> Check:
        check = Check(name=name, level=level, detail=detail, fix=fix)
        self.checks.append(check)
        return check

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.level == LEVEL_FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.level == LEVEL_WARN]

    @property
    def ok(self) -> bool:
        """只有 fail 才代表「跑不起来」；warn 是能力缺失，不影响启动。"""
        return not self.failed

    def render(self, verbose: bool = True) -> str:
        lines: list[str] = []
        for c in self.checks:
            lines.append(f"  [{c.icon}] {c.name:<26} {c.detail}")
            if verbose and c.fix:
                lines.append(f"      → {c.fix}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "profile": self.profile.describe() if self.profile else None,
            "checks": [{"name": c.name, "level": c.level, "detail": c.detail,
                        "fix": c.fix} for c in self.checks],
        }


def _check_python(report: Report) -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 11):
        report.add("Python 版本", LEVEL_OK, f"{major}.{minor}（>= 3.11）")
    elif (major, minor) >= (3, 10):
        report.add("Python 版本", LEVEL_WARN, f"{major}.{minor}",
                   "建议升级到 3.11+：本项目使用了 dataclass slots 与 X | None 语法")
    else:
        report.add("Python 版本", LEVEL_FAIL, f"{major}.{minor}",
                   "需要 Python 3.10 以上，推荐 3.11")


def _check_modules(report: Report) -> None:
    missing_required = [m for m in REQUIRED_MODULES if not _has_module(m)]
    if missing_required:
        report.add("依赖（必需）", LEVEL_FAIL, "缺少 " + ", ".join(missing_required),
                   "运行 python bootstrap.py --setup 自动安装")
    else:
        report.add("依赖（必需）", LEVEL_OK, "requests / PyYAML 已就绪")

    missing_optional = [m for m in OPTIONAL_MODULES if not _has_module(m)]
    if missing_optional:
        report.add("依赖（看板）", LEVEL_WARN, "缺少 " + ", ".join(missing_optional),
                   "缺少这些时 CLI 采集仍可用，但 Web 看板无法启动；"
                   "运行 python bootstrap.py --setup 安装")
    else:
        report.add("依赖（看板）", LEVEL_OK, "fastapi / uvicorn / pydantic 已就绪")


def _has_module(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def _check_config(report: Report, config: Config) -> None:
    if config.config_path and config.config_path.exists():
        report.add("配置文件", LEVEL_OK, str(config.config_path))
    else:
        report.add("配置文件", LEVEL_WARN,
                   f"{config.config_path or 'config.yaml'} 不存在，使用内置默认值",
                   "复制 config.yaml 后按需修改监控清单与阈值")

    if config.watchlist:
        report.add("监控清单（配置）", LEVEL_OK, f"{len(config.watchlist)} 条")
    else:
        report.add("监控清单（配置）", LEVEL_WARN, "config.yaml 里没有 watchlist",
                   '用 python -m facet watch add "AK-47 | Redline (Field-Tested)" --below 100 添加')


def _check_database(report: Report, config: Config) -> None:
    db_path = Path(config.database)
    parent = db_path.parent if str(db_path.parent) not in ("", ".") else Path(".")
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / ".facet_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        report.add("数据库可写", LEVEL_FAIL, f"{parent} 不可写：{exc}",
                   "换一个可写目录，或在 .env 里设置 FACET_DB=<可写路径>")
        return

    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        report.add("数据库可写", LEVEL_OK, f"{db_path}（已存在，{size_mb:.1f} MB）")
    else:
        report.add("数据库可写", LEVEL_OK, f"{db_path}（首次运行会创建）")

    if config.database.startswith("/mnt/") or config.database.startswith("/media/"):
        report.add("数据库位置", LEVEL_WARN, "数据库位于可移动介质",
                   "可移动介质掉线会让 SQLite 损坏，建议放本机磁盘")


def _check_credentials(report: Report, config: Config) -> None:
    usable: list[str] = []
    unusable: list[str] = []
    for name, cls in REGISTRY.items():
        scfg = config.source(name)
        if not scfg.enabled:
            continue
        required = cls.required_credential
        if required is None:
            usable.append(f"{name}(免凭据)")
        elif getattr(scfg, required, None):
            usable.append(f"{name}(已配置)")
        else:
            unusable.append(f"{name}:{cls.credential_env}")

    if usable:
        report.add("可用数据源", LEVEL_OK, "、".join(usable))
    if unusable:
        report.add("缺少凭证的源", LEVEL_WARN, "、".join(unusable),
                   "CSQAQ_TOKEN 是唯一同时覆盖 BUFF 与悠悠有品在售价的源，"
                   "建议优先在 https://csqaq.com 注册并绑定白名单 IP；"
                   "或运行 python -m facet setup 交互式配置")

    if not usable and not unusable:
        report.add("可用数据源", LEVEL_FAIL, "没有任何启用的数据源",
                   "在 config.yaml 里至少启用一个源（推荐 csqaq）")

    _check_buff_capability(report, config)
    _check_llm(report, config)


def _check_buff_capability(report: Report, config: Config) -> None:
    """如实报告 BUFF 当前能做什么 —— 匿名与带 Cookie 的能力差很多。"""
    scfg = config.source(SOURCE_BUFF)
    if not scfg.enabled and not config.source(SOURCE_BUFF_DIRECT).enabled:
        return
    if scfg.cookie or config.source(SOURCE_BUFF_DIRECT).cookie:
        report.add("BUFF 模式", LEVEL_OK,
                   "已配置 Cookie：在售价 + 求购价 + 按名称搜索 + 档位筛选",
                   "若搜索失效说明 Cookie 过期，重新复制一次：python -m facet setup buff")
    else:
        report.add("BUFF 模式", LEVEL_WARN,
                   "匿名模式：可用在售价 + 求购价；无搜索与档位筛选",
                   "匿名额度会被用量触发的风控关闭。配置 Cookie 可解锁搜索，"
                   "也就省掉了扫描 goods_id（那正是触发风控的操作）："
                   "python -m facet setup buff")


def _check_llm(report: Report, config: Config) -> None:
    """LLM 是否可用。未配置不算问题 —— 建议功能不是必需功能。"""
    try:
        from .llm import LLMConfig

        cfg = LLMConfig.from_settings(config.llm)
    except Exception as exc:  # noqa: BLE001
        report.add("LLM 接入", LEVEL_WARN, f"配置读取失败：{exc}")
        return

    if cfg.is_configured():
        report.add("LLM 接入", LEVEL_OK,
                   f"{cfg.provider} / {cfg.resolved_model()}（预设 {cfg.preset or '自定义'}）")
    elif config.llm.preset:
        report.add("LLM 接入", LEVEL_WARN,
                   f"预设已选（{config.llm.preset}）但缺少密钥 {cfg.api_key_env}",
                   "运行 python -m facet setup llm 补上密钥")
    else:
        report.add("LLM 接入", LEVEL_WARN, "未配置",
                   "需要交易建议时再配：python -m facet setup llm（选预设 + 粘密钥）")


def _check_network(report: Report, config: Config, timeout: float = 8.0) -> None:
    """只测 TCP 可达性，不发业务请求（避免探测本身触发风控）。"""
    targets = {
        "csqaq": ("api.csqaq.com", 443),
        "steamdt": ("open.steamdt.com", 443),
        "buff_direct": ("buff.163.com", 443),
        "youpin_direct": ("api.youpin898.com", 443),
    }
    reachable: list[str] = []
    unreachable: list[str] = []
    for name, (host, port) in targets.items():
        if not config.source(name).enabled:
            continue
        try:
            with socket.create_connection((host, port), timeout=timeout):
                reachable.append(name)
        except OSError:
            unreachable.append(name)
    if reachable:
        report.add("网络连通性", LEVEL_OK, "已连通 " + "、".join(reachable))
    if unreachable:
        report.add("网络连通性", LEVEL_WARN, "不可达：" + "、".join(unreachable),
                   "检查网络/代理/防火墙；若走代理请设置 HTTPS_PROXY")


def _check_environment(report: Report) -> None:
    # 控制台编码：中文 Windows 默认 GBK，输出 ✓ ─ ★ 这类符号会抛异常。
    # 排错时这一条能直接排除「命令莫名报错」的一整类问题。
    try:
        from .console import describe, setup_console

        setup_console()
        report.add("控制台编码", LEVEL_OK, describe())
    except Exception as exc:  # noqa: BLE001
        report.add("控制台编码", LEVEL_WARN, f"探测失败：{exc}")

    # 时区：时序库按 UTC 存，但静默时段按本地时区算，时区错会让告警时间误导人
    try:
        from datetime import datetime
        offset = datetime.now().astimezone().utcoffset()
        name = datetime.now().astimezone().tzname()
        report.add("本地时区", LEVEL_OK, f"{name}（UTC{offset}）")
    except Exception as exc:  # noqa: BLE001
        report.add("本地时区", LEVEL_WARN, f"读取失败：{exc}")

    # 磁盘空间：时序数据长期累积，留 500MB 底线
    try:
        usage = shutil.disk_usage(Path.cwd())
        free_mb = usage.free // (1024 * 1024)
        level = LEVEL_OK if free_mb > 500 else LEVEL_WARN
        report.add("可用磁盘", level, f"{free_mb} MB",
                   "" if level == LEVEL_OK else "空间不足会让写入失败，建议清理或迁移数据库")
    except OSError:
        pass


def _check_env_file(report: Report) -> None:
    env_path = Path(".env")
    if env_path.exists():
        report.add(".env 文件", LEVEL_OK, "已存在")
    else:
        report.add(".env 文件", LEVEL_WARN, "未创建",
                   "复制 .env.example 为 .env 并填入凭证（不填则只有免凭据源可用）")


def run_checks(config: Config, network: bool = True,
               profile: PlatformProfile | None = None) -> Report:
    """执行全部自检。"""
    report = Report()
    prof = profile or detect()
    report.profile = prof

    report.add("运行平台", LEVEL_OK,
               f"{prof.os_name} / {prof.arch} / Python {prof.python_version}"
               + (f" / {prof.memory_mb}MB" if prof.memory_mb else ""))
    for note in prof.notes:
        report.add("平台提示", LEVEL_WARN, note)
    if prof.recommend_service:
        report.add("常驻方式", LEVEL_OK if not prof.is_windows else LEVEL_WARN,
                   "建议安装为系统服务（deploy/ 下有 systemd 与 Windows 方案）"
                   if not prof.is_windows else
                   "Windows 可用 start.cmd --install-service 注册计划任务",
                   "" if not prof.is_windows else "")

    _check_python(report)
    _check_modules(report)
    _check_env_file(report)
    _check_config(report, config)
    _check_database(report, config)
    _check_credentials(report, config)
    _check_environment(report)
    if network:
        _check_network(report, config)

    extra = pip_index_args(prof)
    if extra:
        report.add("pip 索引", LEVEL_WARN,
                   f"32 位 ARM：将附加 piwheels 源 {extra[1]}",
                   "piwheels 提供 ARM 预编译轮子，可避免本地编译失败")
    return report


def format_report(report: Report) -> str:
    lines = ["", "=" * 66, "  facet 自检报告", "=" * 66,
             report.render(verbose=True)]
    if report.failed:
        lines.append("")
        lines.append(f"  ✗ {len(report.failed)} 项失败 —— 需要先修复才能正常采集")
    elif report.warned:
        lines.append("")
        lines.append(f"  ! {len(report.warned)} 项提示 —— 可以启动，但功能会受限")
    else:
        lines.append("")
        lines.append("  ✓ 全部通过")
    lines.append("=" * 66)
    return "\n".join(lines)
