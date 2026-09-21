"""适配器注册表：把配置翻译成可用的适配器实例集合。"""
from __future__ import annotations

from typing import Type

from ..config import (
    SOURCE_BUFF,
    SOURCE_BUFF_DIRECT,
    SOURCE_CSQAQ,
    SOURCE_MOCK,
    SOURCE_STEAMDT,
    SOURCE_YOUPIN_DIRECT,
    Config,
)
from ..ratelimit import GateRegistry
from .base import SourceAdapter, SourceUnavailable
from .buff_direct import BuffDirectAdapter
from .csqaq import CsqaqAdapter
from .mock import MockAdapter, ReplayAdapter
from .steamdt import SteamDtAdapter
from .youpin_direct import YouPinDirectAdapter

REGISTRY: dict[str, Type[SourceAdapter]] = {
    SOURCE_CSQAQ: CsqaqAdapter,
    SOURCE_STEAMDT: SteamDtAdapter,
    SOURCE_BUFF: BuffDirectAdapter,
    # 旧名兼容：早期版本把 BUFF 源叫 buff_direct，老配置仍能直接用
    SOURCE_BUFF_DIRECT: BuffDirectAdapter,
    SOURCE_YOUPIN_DIRECT: YouPinDirectAdapter,
    SOURCE_MOCK: MockAdapter,
    "replay": ReplayAdapter,
}

#: 别名 -> 规范名（用于展示时去重）
ALIASES: dict[str, str] = {SOURCE_BUFF_DIRECT: SOURCE_BUFF}

__all__ = [
    "REGISTRY", "SourceAdapter", "SourceUnavailable",
    "CsqaqAdapter", "SteamDtAdapter", "BuffDirectAdapter",
    "YouPinDirectAdapter", "MockAdapter", "ReplayAdapter",
    "build_sources",
]


def build_sources(config: Config, gates: GateRegistry | None = None,
                  include_names: set[str] | None = None) -> list[SourceAdapter]:
    """按优先级构造启用的适配器。

    include_names 用于临时只跑某几个源（如 CLI 的 --source）。
    """
    registry = gates or GateRegistry()
    sources: list[SourceAdapter] = []
    for scfg in config.enabled_sources():
        if include_names and scfg.name not in include_names:
            continue
        cls = REGISTRY.get(scfg.name)
        if cls is None:
            continue
        sources.append(cls(scfg, registry))
    return sources


def describe_registry() -> list[dict[str, object]]:
    """给 CLI / 看板展示「本工具支持哪些源」（按实现去重，只列规范名）。"""
    rows: list[dict[str, object]] = []
    seen: set[Type[SourceAdapter]] = set()
    for name, cls in REGISTRY.items():
        if cls in seen:
            continue
        seen.add(cls)
        rows.append({
            "name": name, "class": cls.__name__,
            "platforms": list(cls.platforms),
            "sell": cls.provides_sell, "bid": cls.provides_bid,
            "required_credential": cls.required_credential,
            "credential_env": cls.credential_env,
            "note": cls.degraded_note,
            "aliases": [a for a, canon in ALIASES.items() if canon == name],
        })
    return rows
