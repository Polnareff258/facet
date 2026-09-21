"""跨平台身份映射：market_hash_name ⇄ buff_goods_id ⇄ youpin_template_id。

为什么必须有这一层：
  BUFF 的匿名接口只认 goods_id，悠悠有品的接口只认 templateId，
  而跨平台比价与去重只能靠 Steam 官方命名 market_hash_name。
  三者之间的映射是「一次性成本、长期复用」的资产，必须落库缓存。

映射的三个来源（按可靠性排序）：
  1. 显式声明（CLI 设置 / 配置文件）—— 最可靠
  2. 社区沉淀的映射表（参考项目 CS2TradeMonitor 随包附带的 top-1000 映射资源）
  3. BUFF ID 空间扫描（indexer）—— 只产出 BUFF 侧
"""
from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any, Iterable

from .models import ItemRef
from .store import Store

logger = logging.getLogger(__name__)


def _read_json(path: str | Path) -> Any:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    if p.suffix == ".gz":
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


class MappingService:
    """饰品身份的解析、缓存与回填。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ── 查询 ───────────────────────────────────────────────

    def to_item_refs(self, names: Iterable[str]) -> list[ItemRef]:
        """把名称列表翻译成带平台 ID 的 ItemRef 列表。

        数据库中缺失的行也会返回（ID 置空），由适配器自行决定跳过，
        这样监控清单可以先行添加、身份随后补齐，不会互相阻塞。
        """
        refs: list[ItemRef] = []
        for name in names:
            row = self.store.get_item(name)
            if row:
                refs.append(ItemRef(
                    market_hash_name=row["market_hash_name"],
                    display_name=row["display_name"],
                    buff_goods_id=row["buff_goods_id"],
                    youpin_template_id=row["youpin_template_id"],
                ))
            else:
                refs.append(ItemRef(market_hash_name=name))
        return refs

    def unresolved(self, names: Iterable[str]) -> dict[str, list[str]]:
        """报告哪些名称还缺哪个平台的 ID，供 CLI 提示用户补全。"""
        missing: dict[str, list[str]] = {"buff_goods_id": [], "youpin_template_id": []}
        for name in names:
            row = self.store.get_item(name)
            if not row or row["buff_goods_id"] is None:
                missing["buff_goods_id"].append(name)
            if not row or row["youpin_template_id"] is None:
                missing["youpin_template_id"].append(name)
        return missing

    # ── 写入 ───────────────────────────────────────────────

    def set_ids(self, market_hash_name: str, buff_goods_id: int | None = None,
                youpin_template_id: int | None = None,
                display_name: str | None = None) -> None:
        self.store.upsert_item(ItemRef(
            market_hash_name=market_hash_name,
            display_name=display_name,
            buff_goods_id=buff_goods_id,
            youpin_template_id=youpin_template_id,
        ))

    def seed_from_youpin_mapping(self, path: str | Path) -> int:
        """导入参考项目随包附带的 mhn -> 悠悠有品 templateId 映射。

        该资源形如：
            {"success": true, "data": {"<steam_hash_name>":
                {"steam_hash_name": ..., "yyyp_id": 822}}}
        """
        raw = _read_json(path)
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            logger.warning("[mapping] %s 结构不符合预期（缺少 data 字典）", path)
            return 0

        seeded = 0
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            mhn = value.get("steam_hash_name") or key
            template_id = value.get("yyyp_id") or value.get("templateId")
            if not mhn or not isinstance(template_id, int):
                continue
            self.set_ids(mhn, youpin_template_id=template_id)
            seeded += 1
        logger.info("[mapping] 从 %s 导入 %d 条悠悠有品映射", path, seeded)
        return seeded

    def seed_from_pairs(self, pairs: Iterable[dict[str, Any]]) -> int:
        """通用导入：[{market_hash_name, buff_goods_id?, youpin_template_id?}]"""
        count = 0
        for row in pairs:
            mhn = row.get("market_hash_name")
            if not mhn:
                continue
            self.set_ids(
                mhn,
                buff_goods_id=row.get("buff_goods_id"),
                youpin_template_id=row.get("youpin_template_id"),
                display_name=row.get("display_name"),
            )
            count += 1
        return count

    # ── 统计 ───────────────────────────────────────────────

    def coverage(self) -> dict[str, int]:
        total = self.store.count_items()
        buff = len(self.store.items_with_buff_id())
        youpin = len(self.store.items_with_youpin_id())
        return {
            "items": total,
            "with_buff_goods_id": buff,
            "with_youpin_template_id": youpin,
            "buff_coverage": round(buff / total, 4) if total else 0.0,
            "youpin_coverage": round(youpin / total, 4) if total else 0.0,
        }


def default_reference_resources() -> dict[str, str]:
    """参考项目里可直接复用的映射资源（存在则用，不存在则跳过）。"""
    base = Path(__file__).resolve().parent.parent / "refs" / "CS2TradeMonitor" \
        / "CS2TradeMonitor.YouPinPrivacyAudit" / "Resources"
    return {
        "youpin_mapping": str(base / "hot-top1000.youpin-mapping.json.gz"),
        "youpin_catalog": str(base / "hot-top1000.catalog.json.gz"),
    }
