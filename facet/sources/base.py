"""适配器基类与公共工具。

一个 SourceAdapter 的职责边界：
  - 声明自己覆盖哪些平台、能提供哪些字段（在售/求购）
  - 接受一批饰品身份（ItemRef），返回统一的 SourceQuote
  - 自己做限速与退避，失败不抛到调用方（记进 FetchReport）
  - 绝不写数据库（存储由调度器统一负责），便于离线回放测试
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Iterable, Sequence

from ..config import SourceConfig
from ..models import FetchReport, ItemRef, SourceQuote
from ..ratelimit import GateRegistry

logger = logging.getLogger(__name__)


class SourceUnavailable(RuntimeError):
    """源缺少必要凭证/依赖，无法工作（区别于临时失败）。"""


class SourceAdapter(ABC):
    """所有数据源的统一接口。"""

    #: 源标识，与 config.SourceConfig.name 对应
    name: str = "base"
    #: 该源声明的平台覆盖
    platforms: tuple[str, ...] = ()
    #: 该源是否能提供在售价
    provides_sell: bool = True
    #: 该源是否能提供求购价
    provides_bid: bool = False
    #: 该源是否提供在售量
    provides_count: bool = True

    #: 该源是否必需凭证；None 表示免凭据即可工作
    #: 取值与 SourceConfig 的字段名对应（如 "api_token" / "api_key"）
    required_credential: str | None = None
    #: 该凭证对应的环境变量名，用于在 CLI 里提示用户去配哪个变量
    credential_env: str = ""
    #: 凭证缺失时该源的表现（用于提示，不是硬错误）
    degraded_note: str = ""

    def __init__(self, config: SourceConfig, gates: GateRegistry | None = None) -> None:
        self.config = config
        self.gates = gates or GateRegistry()
        # 以配置里的名字为准，而不是类属性：同一个适配器类可能被注册成
        # 多个源名（如 buff 与旧名 buff_direct），限速闸门与报告要按实际
        # 启用的名字区分，否则两个别名会共用同一把闸门。
        if config.name:
            self.name = config.name

    # ── 生命周期 ───────────────────────────────────────────

    def preflight(self) -> None:
        """检查运行前提（凭证是否齐全）。不满足时抛 SourceUnavailable。"""

    def close(self) -> None:
        """释放连接资源。"""

    # ── 主接口 ─────────────────────────────────────────────

    @abstractmethod
    def fetch(self, items: Sequence[ItemRef]) -> list[SourceQuote]:
        """取一批饰品的报价。无法取到的饰品直接跳过，不要抛异常。"""

    # ── 便捷封装 ───────────────────────────────────────────

    def fetch_report(self, items: Sequence[ItemRef]) -> tuple[list[SourceQuote], FetchReport]:
        report = FetchReport(source=self.name, requested=len(items))
        quotes: list[SourceQuote] = []
        try:
            self.preflight()
        except SourceUnavailable as exc:
            report.errors.append(f"preflight: {exc}")
            report.failed = len(items)
            report.finish()
            logger.warning("[%s] 不可用：%s", self.name, exc)
            return quotes, report

        try:
            quotes = self.fetch(items)
            report.succeeded = len({q.market_hash_name for q in quotes})
            report.empty = max(0, len(items) - report.succeeded)
            report.quotes = len(quotes)
        except Exception as exc:  # noqa: BLE001 — 单源崩溃不能拖垮整轮采集
            report.errors.append(f"{type(exc).__name__}: {exc}")
            report.failed = len(items)
            logger.exception("[%s] 采集异常", self.name)
        finally:
            report.finish()
        return quotes, report

    # ── 工具 ───────────────────────────────────────────────

    @staticmethod
    def chunked(seq: Sequence[ItemRef], size: int) -> Iterable[list[ItemRef]]:
        size = max(1, size)
        for i in range(0, len(seq), size):
            yield list(seq[i:i + size])

    def describe(self) -> dict[str, Any]:
        credential_state = self.credential_state()
        return {
            "name": self.name,
            "enabled": self.config.enabled,
            "priority": self.config.priority,
            "platforms": list(self.platforms),
            "provides": {
                "sell": self.provides_sell,
                "bid": self.provides_bid,
                "count": self.provides_count,
            },
            "credential": credential_state,
            "gates": [g.snapshot() for g in self.gates.snapshot()
                      if str(g.get("name", "")).startswith(self.name + ":")],
        }

    def credential_state(self) -> dict[str, Any]:
        """判断凭证是否齐备，供 CLI / 看板如实展示。"""
        if not self.required_credential:
            return {"required": False, "ok": True, "hint": "免凭据"}
        value = getattr(self.config, self.required_credential, None)
        return {
            "required": True,
            "ok": bool(value),
            "env": self.credential_env,
            "hint": ("已配置" if value
                     else f"缺少 {self.credential_env}：{self.degraded_note or '该源不可用'}"),
        }
