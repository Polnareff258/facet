"""BUFF goods_id 索引器（可中断、可续跑）。

背景：BUFF 没有免登录的「按名称搜 ID」接口（/api/market/goods 需登录），
但 /api/market/goods/info?goods_id=N 免登录返回 market_hash_name。
因此可以扫描 ID 空间，把 market_hash_name -> goods_id 建成一次性的本地索引；
之后监控只按 ID 精确查价，不再重复扫描。

工程要点：
  - 游标持久化在 index_progress，进程被杀也能从断点续跑
  - 全程走限速闸门；命中风控自动长冷却并优雅退出
  - 支持只扫监控清单里缺 ID 的饰品（resolve_missing），避免无谓的全空间扫描
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Iterable

from .mapping import MappingService
from .models import ItemRef
from .ratelimit import RateLimitExceeded
from .sources.buff_direct import BuffDirectAdapter
from .store import Store

logger = logging.getLogger(__name__)

SCANNER_NAME = "buff_goods_id"


class BuffIndexer:
    """扫描 BUFF ID 空间，建立并增量维护本地索引。"""

    def __init__(self, store: Store, adapter: BuffDirectAdapter,
                 mapping: MappingService | None = None) -> None:
        self.store = store
        self.adapter = adapter
        self.mapping = mapping or MappingService(store)

    # ── 主流程 ─────────────────────────────────────────────

    def scan(self, start: int = 1, end: int = 60000, step: int = 1,
             resume: bool = True, max_seconds: float | None = None,
             only_cs2: bool = True,
             progress: Callable[[int, int, int], None] | None = None) -> dict[str, int]:
        """扫描 [start, end)，返回统计。

        step>1 用于快速摸底（会漏 ID）；正式建索引用 step=1。
        """
        cursor = start
        if resume:
            saved = self.store.get_cursor(SCANNER_NAME)
            cursor = max(start, int(saved.get("cursor", 0)))
            hits = int(saved.get("hits", 0))
            scanned = int(saved.get("scanned", 0))
        else:
            hits = scanned = 0

        begun = time.monotonic()
        batch: list[ItemRef] = []
        stop_reason = "completed"

        gid = cursor
        while gid < end:
            if max_seconds is not None and time.monotonic() - begun > max_seconds:
                stop_reason = "time_budget"
                break
            try:
                info = self.adapter.resolve_goods_id(gid)
            except RateLimitExceeded as exc:
                stop_reason = f"rate_limited: {exc}"
                logger.warning("[indexer] 触发风控，已保存游标 %d 并退出", gid)
                break

            scanned += 1
            if info and (not only_cs2 or info.get("game") == "csgo"):
                batch.append(ItemRef(
                    market_hash_name=info["market_hash_name"],
                    display_name=info.get("name"),
                    buff_goods_id=int(info["goods_id"]),
                ))
                hits += 1

            if len(batch) >= 50:
                self.store.upsert_items(batch)
                batch.clear()

            gid += step
            if scanned % 200 == 0:
                self.store.set_cursor(SCANNER_NAME, gid, hits, scanned)
                if progress:
                    progress(gid, hits, scanned)
                logger.info("[indexer] 游标 %d，命中 %d，已扫 %d", gid, hits, scanned)

        if batch:
            self.store.upsert_items(batch)
        self.store.set_cursor(SCANNER_NAME, gid, hits, scanned)
        if progress:
            progress(gid, hits, scanned)

        stats = {"cursor": gid, "hits": hits, "scanned": scanned,
                 "stop_reason": stop_reason}  # type: ignore[dict-item]
        logger.info("[indexer] 结束：%s", stats)
        return stats

    # ── 定向补齐 ───────────────────────────────────────────

    def resolve_missing(self, names: Iterable[str], scan_range: tuple[int, int] = (1, 60000),
                        max_seconds: float | None = None) -> dict[str, int]:
        """只为指定饰品补 ID：扫描空间直到全部找到或超出预算。

        相比全量扫描，这是「按需付费」的路径：加一个监控就补一个 ID。
        """
        wanted = {n for n in names if n}
        if not wanted:
            return {"resolved": 0, "remaining": 0, "scanned": 0}

        for row in self.store.items_with_buff_id(sorted(wanted)):
            wanted.discard(row["market_hash_name"])

        resolved = 0
        scanned = 0
        begun = time.monotonic()
        start, end = scan_range

        gid = start
        batch: list[ItemRef] = []
        while wanted and gid < end:
            if max_seconds is not None and time.monotonic() - begun > max_seconds:
                break
            try:
                info = self.adapter.resolve_goods_id(gid)
            except RateLimitExceeded:
                break
            scanned += 1
            if info and info.get("game") == "csgo" and info["market_hash_name"] in wanted:
                batch.append(ItemRef(
                    market_hash_name=info["market_hash_name"],
                    display_name=info.get("name"),
                    buff_goods_id=int(info["goods_id"]),
                ))
                wanted.discard(info["market_hash_name"])
                resolved += 1
                if len(batch) >= 20:
                    self.store.upsert_items(batch)
                    batch.clear()
            gid += 1
            if scanned % 500 == 0:
                logger.info("[indexer] 定向补齐中：已扫 %d，剩余 %d", scanned, len(wanted))

        if batch:
            self.store.upsert_items(batch)
        return {"resolved": resolved, "remaining": len(wanted), "scanned": scanned}

    def status(self) -> dict[str, int]:
        saved = self.store.get_cursor(SCANNER_NAME)
        indexed = len(self.store.items_with_buff_id())
        return {"cursor": int(saved.get("cursor", 0)),
                "hits": int(saved.get("hits", 0)),
                "scanned": int(saved.get("scanned", 0)),
                "indexed_items": indexed}
