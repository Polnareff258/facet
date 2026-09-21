"""SQLite 存储层：饰品身份、报价时序、告警、监控清单、索引进度。

要点：
  - WAL 模式，支持「采集线程写 / 看板线程读」并发
  - 报价表按 (market_hash_name, platform, observed_at) 建索引，历史查询走索引
  - 写操作串行化（单锁），读操作直连游标
"""
from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import AlertEvent, ItemRef, SourceQuote, WatchRule, iso, utcnow

logger = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- 饰品身份表：market_hash_name 为跨平台主键
CREATE TABLE IF NOT EXISTS items (
    market_hash_name    TEXT PRIMARY KEY,
    display_name        TEXT,
    buff_goods_id       INTEGER,
    youpin_template_id  INTEGER,
    steam_nameid        TEXT,
    tags                TEXT,
    updated_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_buff
    ON items(buff_goods_id) WHERE buff_goods_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_youpin
    ON items(youpin_template_id) WHERE youpin_template_id IS NOT NULL;

-- 报价时序表
CREATE TABLE IF NOT EXISTS quotes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    market_hash_name    TEXT NOT NULL,
    platform            TEXT NOT NULL,
    source              TEXT NOT NULL,
    sell_price          REAL,
    sell_count          INTEGER,
    bid_price           REAL,
    bid_count           INTEGER,
    currency            TEXT NOT NULL DEFAULT 'CNY',
    observed_at         TEXT NOT NULL,
    source_updated_at   TEXT,
    variant_label       TEXT
);
CREATE INDEX IF NOT EXISTS idx_quotes_lookup
    ON quotes(market_hash_name, platform, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_quotes_variant
    ON quotes(market_hash_name, platform, variant_label, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_quotes_source_time
    ON quotes(source, observed_at DESC);

-- 告警表
CREATE TABLE IF NOT EXISTS alerts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    market_hash_name    TEXT NOT NULL,
    platform            TEXT NOT NULL,
    rule                TEXT NOT NULL,
    severity            TEXT NOT NULL DEFAULT 'info',
    message             TEXT NOT NULL,
    current_price       REAL,
    baseline_price      REAL,
    change_percent      REAL,
    created_at          TEXT NOT NULL,
    notified            INTEGER NOT NULL DEFAULT 0,
    notify_result       TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_cooldown
    ON alerts(market_hash_name, platform, rule, created_at DESC);

-- 监控清单（YAML 为初始来源，DB 为运行时真源）
CREATE TABLE IF NOT EXISTS watchlist (
    market_hash_name    TEXT PRIMARY KEY,
    rules               TEXT NOT NULL,
    enabled             INTEGER NOT NULL DEFAULT 1,
    source              TEXT NOT NULL DEFAULT 'yaml',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- 可中断索引器的游标
CREATE TABLE IF NOT EXISTS index_progress (
    scanner             TEXT PRIMARY KEY,
    cursor              INTEGER NOT NULL DEFAULT 0,
    hits                INTEGER NOT NULL DEFAULT 0,
    scanned             INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT NOT NULL
);

-- 采集日志：看板用它展示各源健康度
CREATE TABLE IF NOT EXISTS fetch_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT NOT NULL,
    requested           INTEGER NOT NULL DEFAULT 0,
    succeeded           INTEGER NOT NULL DEFAULT 0,
    failed              INTEGER NOT NULL DEFAULT 0,
    quotes              INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    errors              TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_time ON fetch_log(started_at DESC);

-- 日线 OHLC 缓存：报价表累积到百万行后，现算日线会变慢，这里预聚合
CREATE TABLE IF NOT EXISTS ohlc_cache (
    market_hash_name    TEXT NOT NULL,
    platform            TEXT NOT NULL,
    date                TEXT NOT NULL,
    open                REAL NOT NULL,
    high                REAL NOT NULL,
    low                 REAL NOT NULL,
    close               REAL NOT NULL,
    count               INTEGER NOT NULL DEFAULT 0,
    avg_count           REAL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (market_hash_name, platform, date)
);
CREATE INDEX IF NOT EXISTS idx_ohlc_lookup
    ON ohlc_cache(market_hash_name, platform, date DESC);

-- 价格归档：超过保留期的明细按天聚合成一行，既留住趋势又控制体积
CREATE TABLE IF NOT EXISTS archived_prices (
    market_hash_name    TEXT NOT NULL,
    platform            TEXT NOT NULL,
    date                TEXT NOT NULL,
    avg_price           REAL NOT NULL,
    min_price           REAL,
    max_price           REAL,
    first_price         REAL,
    last_price          REAL,
    record_count        INTEGER NOT NULL,
    archived_at         TEXT NOT NULL,
    PRIMARY KEY (market_hash_name, platform, date)
);

-- 极致追踪配置：单件高频轮询
CREATE TABLE IF NOT EXISTS extreme_watch (
    market_hash_name    TEXT NOT NULL,
    platform            TEXT NOT NULL,
    interval_seconds    INTEGER NOT NULL DEFAULT 60,
    enabled             INTEGER NOT NULL DEFAULT 1,
    price_mode          TEXT NOT NULL DEFAULT 'percent',
    price_threshold     REAL NOT NULL DEFAULT 1.0,
    quantity_mode       TEXT NOT NULL DEFAULT 'percent',
    quantity_threshold  REAL NOT NULL DEFAULT 10.0,
    cooldown_seconds    INTEGER NOT NULL DEFAULT 300,
    quiet_start         INTEGER,
    quiet_end           INTEGER,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (market_hash_name, platform)
);

-- 极致追踪快照：高频采样单独存，避免污染主报价表
CREATE TABLE IF NOT EXISTS extreme_samples (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    market_hash_name    TEXT NOT NULL,
    platform            TEXT NOT NULL,
    sell_price          REAL,
    sell_count          INTEGER,
    bid_price           REAL,
    observed_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extreme_samples
    ON extreme_samples(market_hash_name, platform, observed_at DESC);

-- 中文名称缓存：从 BUFF / CSQAQ 学到，避免每次展示都联网查
-- base_en 存英文基础名，base_cn 存中文基础名：两者必须同时存，
-- 否则「学了久经沙场的中文名，想给崭新出厂复用」时无从关联
-- （中文名和英文名之间没有可推导的关系）。
CREATE TABLE IF NOT EXISTS cn_names (
    market_hash_name    TEXT PRIMARY KEY,
    cn_name             TEXT NOT NULL,
    base_en             TEXT,
    base_cn             TEXT,
    source              TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cn_names_base_en ON cn_names(base_en);
CREATE INDEX IF NOT EXISTS idx_cn_names_base_cn ON cn_names(base_cn);

-- 关注清单：带交易意图（准备买 / 准备卖 / 仅观察），与通用监控清单区分开
CREATE TABLE IF NOT EXISTS focus (
    market_hash_name    TEXT PRIMARY KEY,
    intent              TEXT NOT NULL DEFAULT 'watch',   -- buy | sell | watch
    priority            INTEGER NOT NULL DEFAULT 3,      -- 1 最高
    target_price        REAL,                            -- 目标价（买：≤此价；卖：≥此价）
    acceptable_wear     TEXT,                            -- JSON: 可接受的磨损档
    acceptable_quality  TEXT,                            -- JSON: 普通/暗金/纪念品
    acceptable_patterns TEXT,                            -- JSON: 可接受的图案档位
    max_budget          REAL,                            -- 预算上限
    quantity            INTEGER NOT NULL DEFAULT 1,
    note                TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_focus_intent ON focus(intent, priority);

-- 图案档位学习样本：从挂单明细采集的 (种子, 价格)
CREATE TABLE IF NOT EXISTS seed_samples (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    market_hash_name    TEXT NOT NULL,
    paint_index         INTEGER,
    paint_seed          INTEGER NOT NULL,
    paint_wear          REAL,
    price               REAL NOT NULL,
    observed_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seed_samples
    ON seed_samples(market_hash_name, paint_seed);

-- LLM 建议记录：保留输入摘要与原文，便于回看「当时凭什么给的结论」
CREATE TABLE IF NOT EXISTS advice (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    market_hash_name    TEXT NOT NULL,
    platform            TEXT,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    action              TEXT NOT NULL,          -- buy | hold | sell | avoid | watch
    confidence          REAL,
    target_buy          REAL,
    target_sell         REAL,
    stop_loss           REAL,
    horizon_days        INTEGER,
    reasoning           TEXT,
    risks               TEXT,
    raw_response        TEXT,
    context_digest      TEXT,                   -- 送进去的数据指纹，便于判断结论是否过期
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_advice_item ON advice(market_hash_name, created_at DESC);

-- 档位词表：从平台（悠悠求购）直接采集的档位取值，不是推算结果
--   kind = style(宝石/相位/Tier) | fade(渐变区间) | abrade(磨损区间)
-- 平台不会给静态清单，但「买家实际挂出的档位约束」就是这个品类真实在流通的档位集合
CREATE TABLE IF NOT EXISTS variant_terms (
    market_hash_name    TEXT NOT NULL,
    kind                TEXT NOT NULL,
    value               TEXT NOT NULL,
    sample_count        INTEGER NOT NULL DEFAULT 0,
    source              TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (market_hash_name, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_variant_terms_kind
    ON variant_terms(kind, market_hash_name);
"""

# 全文索引与同步触发器。
# 用独立的 FTS 表（而非 external-content）是为了避免依赖 items 表的具体列布局：
# items 以 market_hash_name 文本作主键、没有自增 id，用 external-content 会很容易
# 在后续改表时踩坑。代价是名称文本存两份，对几万条饰品完全可接受。
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    market_hash_name, display_name, tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS items_fts_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts (market_hash_name, display_name)
    VALUES (new.market_hash_name, COALESCE(new.display_name, ''));
END;

CREATE TRIGGER IF NOT EXISTS items_fts_ad AFTER DELETE ON items BEGIN
    DELETE FROM items_fts WHERE market_hash_name = old.market_hash_name;
END;

CREATE TRIGGER IF NOT EXISTS items_fts_au AFTER UPDATE ON items BEGIN
    DELETE FROM items_fts WHERE market_hash_name = old.market_hash_name;
    INSERT INTO items_fts (market_hash_name, display_name)
    VALUES (new.market_hash_name, COALESCE(new.display_name, ''));
END;
"""


class Store:
    """线程安全的 SQLite 封装。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._write_lock:
            self._ensure_legacy_columns()   # 必须先于 executescript（见其 docstring）
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._fts_enabled = self._init_fts()

    # ── 生命周期 ───────────────────────────────────────────

    def _ensure_legacy_columns(self) -> None:
        """轻量前向迁移：只加列，不改列、不删数据。

        **必须在 executescript(SCHEMA) 之前调用**：SCHEMA 里有
        `CREATE INDEX ... ON cn_names(base_en)`，而老库的 cn_names 表
        还没有 base_en 列（CREATE TABLE IF NOT EXISTS 对已存在的表是空操作），
        索引会先报 "no such column"。先补列再建索引，老库就能直接升级，
        不需要用户导出重导。
        """
        migrations: dict[str, dict[str, str]] = {
            "cn_names": {"base_en": "TEXT"},
            "quotes": {"variant_label": "TEXT"},
        }
        for table, columns in migrations.items():
            try:
                existing = {row["name"] for row in
                            self._conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:
                continue
            if not existing:
                continue          # 表还不存在，等 executescript 按新 schema 建
            for column, sql_type in columns.items():
                if column in existing:
                    continue
                try:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
                    logger.info("[store] 迁移：%s 增加列 %s", table, column)
                except sqlite3.Error as exc:
                    logger.warning("[store] 迁移 %s.%s 失败（忽略）：%s",
                                   table, column, exc)
        try:
            self._conn.commit()
        except sqlite3.Error:
            pass

    def _init_fts(self) -> bool:
        """建立全文索引。返回是否可用（不可用时搜索退回 LIKE）。

        首次建立或从旧版本升级时，items 里可能已有数据而索引为空，
        因此这里做一次一次性回填 —— 否则用户升级后会发现「搜不到已有饰品」。
        """
        try:
            with self._write_lock:
                self._conn.executescript(FTS_SCHEMA)
                indexed = self._conn.execute(
                    "SELECT COUNT(*) FROM items_fts").fetchone()[0]
                total = self._conn.execute(
                    "SELECT COUNT(*) FROM items").fetchone()[0]
                if indexed == 0 and total > 0:
                    self._conn.execute(
                        """INSERT INTO items_fts (market_hash_name, display_name)
                           SELECT market_hash_name, COALESCE(display_name, '')
                           FROM items""")
                    logger.info("[store] 已为 %d 条饰品回填全文索引", total)
                self._conn.commit()
            return True
        except sqlite3.Error as exc:
            logger.warning("[store] 全文索引不可用（%s），搜索退回 LIKE 模式", exc)
            return False

    def close(self) -> None:
        with self._write_lock:
            self._conn.commit()
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── 饰品身份 ───────────────────────────────────────────

    def upsert_item(self, item: ItemRef) -> None:
        with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO items (market_hash_name, display_name, buff_goods_id,
                                   youpin_template_id, steam_nameid, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_hash_name) DO UPDATE SET
                    display_name = COALESCE(excluded.display_name, items.display_name),
                    buff_goods_id = COALESCE(excluded.buff_goods_id, items.buff_goods_id),
                    youpin_template_id = COALESCE(excluded.youpin_template_id, items.youpin_template_id),
                    steam_nameid = COALESCE(excluded.steam_nameid, items.steam_nameid),
                    updated_at = excluded.updated_at
                """,
                (item.market_hash_name, item.display_name, item.buff_goods_id,
                 item.youpin_template_id, item.steam_nameid, iso(item.updated_at)),
            )
            self._conn.commit()

    def upsert_items(self, items: Iterable[ItemRef]) -> int:
        rows = [
            (i.market_hash_name, i.display_name, i.buff_goods_id,
             i.youpin_template_id, i.steam_nameid, iso(i.updated_at))
            for i in items
        ]
        if not rows:
            return 0
        with self._write_lock:
            self._conn.executemany(
                """
                INSERT INTO items (market_hash_name, display_name, buff_goods_id,
                                   youpin_template_id, steam_nameid, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_hash_name) DO UPDATE SET
                    display_name = COALESCE(excluded.display_name, items.display_name),
                    buff_goods_id = COALESCE(excluded.buff_goods_id, items.buff_goods_id),
                    youpin_template_id = COALESCE(excluded.youpin_template_id, items.youpin_template_id),
                    steam_nameid = COALESCE(excluded.steam_nameid, items.steam_nameid),
                    updated_at = excluded.updated_at
                """,
                rows,
            )
            self._conn.commit()
        return len(rows)

    def get_item(self, market_hash_name: str) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "SELECT * FROM items WHERE market_hash_name = ?", (market_hash_name,))
        row = cur.fetchone()
        return dict(row) if row else None

    def find_item_by_buff_id(self, goods_id: int) -> dict[str, Any] | None:
        cur = self._conn.execute("SELECT * FROM items WHERE buff_goods_id = ?", (goods_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def find_item_by_youpin_id(self, template_id: int) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "SELECT * FROM items WHERE youpin_template_id = ?", (template_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def items_with_buff_id(self, names: Sequence[str] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM items WHERE buff_goods_id IS NOT NULL"
        params: list[Any] = []
        if names:
            sql += f" AND market_hash_name IN ({','.join('?' * len(names))})"
            params.extend(names)
        return [dict(r) for r in self._conn.execute(sql, params)]

    def items_with_youpin_id(self, names: Sequence[str] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM items WHERE youpin_template_id IS NOT NULL"
        params: list[Any] = []
        if names:
            sql += f" AND market_hash_name IN ({','.join('?' * len(names))})"
            params.extend(names)
        return [dict(r) for r in self._conn.execute(sql, params)]

    def count_items(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])

    def find_items_by_base(self, base: str, limit: int = 300) -> list[dict[str, Any]]:
        """找同一基础皮肤的所有变体（各磨损档 / 暗金 / 纪念品）。

        用 LIKE 做粗筛（走不了索引，但 items 表通常只有几万行，一次全扫是可接受的），
        精确的同基础名判定交给调用方用 parse_name 复核 —— 因为 LIKE 会误命中
        「AK-47 | Redline」与「AK-47 | RedlineX」这类前缀重叠的情况。
        """
        if not base:
            return []
        like = f"%{base}%"
        cur = self._conn.execute(
            """
            SELECT * FROM items
            WHERE market_hash_name LIKE ? OR display_name LIKE ?
            ORDER BY market_hash_name
            LIMIT ?
            """,
            (like, like, limit),
        )
        return [dict(r) for r in cur]

    def search_items(self, keyword: str, limit: int = 30) -> list[dict[str, Any]]:
        """按关键词搜索饰品。

        优先走 FTS5 全文索引（几万条饰品下仍是毫秒级）；若该 SQLite 构建
        不含 FTS5（部分精简嵌入式环境），自动退回 LIKE —— 功能不降级，只是变慢。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        if self._fts_enabled:
            try:
                # FTS5 把引号/星号当运算符，直接拼用户输入会抛语法错误；
                # 包成短语 + 前缀匹配，既安全又符合「输入前缀找饰品」的直觉
                safe = '"' + keyword.replace('"', '""') + '"*'
                cur = self._conn.execute(
                    """
                    SELECT i.* FROM items_fts f
                    JOIN items i ON i.market_hash_name = f.market_hash_name
                    WHERE items_fts MATCH ?
                    ORDER BY i.market_hash_name
                    LIMIT ?
                    """,
                    (safe, limit),
                )
                rows = [dict(r) for r in cur.fetchall()]
                if rows:
                    return rows
            except sqlite3.Error:
                pass    # 查询语法不被接受时静默退回 LIKE，不打断用户

        like = f"%{keyword}%"
        cur = self._conn.execute(
            """
            SELECT * FROM items
            WHERE market_hash_name LIKE ? OR display_name LIKE ?
            ORDER BY (buff_goods_id IS NULL), market_hash_name
            LIMIT ?
            """,
            (like, like, limit),
        )
        return [dict(r) for r in cur]

    @property
    def fts_enabled(self) -> bool:
        """FTS5 全文索引是否可用。"""
        return self._fts_enabled

    # ── 报价 ───────────────────────────────────────────────

    def insert_quotes(self, quotes: Iterable[SourceQuote]) -> int:
        rows = [
            (q.market_hash_name, q.platform, q.source, q.sell_price, q.sell_count,
             q.bid_price, q.bid_count, q.currency, iso(q.observed_at),
             iso(q.source_updated_at) if q.source_updated_at else None,
             q.variant_label)
            for q in quotes
        ]
        if not rows:
            return 0
        with self._write_lock:
            self._conn.executemany(
                """
                INSERT INTO quotes (market_hash_name, platform, source, sell_price,
                                    sell_count, bid_price, bid_count, currency,
                                    observed_at, source_updated_at, variant_label)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._conn.commit()
        return len(rows)

    def latest_quote(self, market_hash_name: str, platform: str,
                     variant_label: str | None = None) -> dict[str, Any] | None:
        """取最新报价。variant_label 为空时只取「未细分档位」的那条。

        这一点很重要：档位级报价（如多普勒红宝石）和整品报价是两个不同的东西，
        混在一起取最新会把红宝石的价格当成整把刀的均价。
        """
        if variant_label is None:
            sql = ("SELECT * FROM quotes WHERE market_hash_name = ? AND platform = ? "
                   "AND variant_label IS NULL "
                   "ORDER BY observed_at DESC, id DESC LIMIT 1")
            params: list[Any] = [market_hash_name, platform]
        else:
            sql = ("SELECT * FROM quotes WHERE market_hash_name = ? AND platform = ? "
                   "AND variant_label = ? "
                   "ORDER BY observed_at DESC, id DESC LIMIT 1")
            params = [market_hash_name, platform, variant_label]
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def latest_by_platform(self, market_hash_name: str) -> dict[str, dict[str, Any]]:
        """每个平台取一条最新报价（用窗口函数避开 N+1 查询）。

        只取**未细分档位**的报价，保持与「整品行情」语义一致；
        档位级报价用 variant_quotes() 单独取。
        """
        cur = self._conn.execute(
            """
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY platform ORDER BY observed_at DESC, id DESC
                ) AS rn
                FROM quotes WHERE market_hash_name = ? AND variant_label IS NULL
            ) WHERE rn = 1
            """,
            (market_hash_name,),
        )
        return {r["platform"]: dict(r) for r in cur.fetchall()}

    def variant_quotes(self, market_hash_name: str,
                       platform: str | None = None) -> list[dict[str, Any]]:
        """按档位取最新报价，返回行列表（含 platform 与 variant_label 字段）。

        用于「同一把刀的不同相位分别值多少」这类判断 —— 这是档位监控的核心价值，
        因为红宝石与 P2 的价差常达数倍，合成一个均价毫无意义。

        返回列表而不是 {档位: 行} 字典：不限定平台时，两个平台可能都有「T1」档，
        用档位名做键会互相覆盖，静默丢数据。
        """
        sql = """
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY platform, variant_label
                    ORDER BY observed_at DESC, id DESC
                ) AS rn
                FROM quotes
                WHERE market_hash_name = ? AND variant_label IS NOT NULL
                  {platform_filter}
            ) WHERE rn = 1
            ORDER BY platform, variant_label
        """.format(platform_filter="AND platform = ?" if platform else "")
        params: list[Any] = [market_hash_name]
        if platform:
            params.append(platform)
        return [dict(r) for r in self._conn.execute(sql, params)]

    def variant_labels(self, market_hash_name: str) -> list[str]:
        rows = self._conn.execute(
            """SELECT DISTINCT variant_label FROM quotes
               WHERE market_hash_name = ? AND variant_label IS NOT NULL
               ORDER BY variant_label""",
            (market_hash_name,)).fetchall()
        return [r["variant_label"] for r in rows]

    def price_history(self, market_hash_name: str, platform: str,
                      hours: int = 720, limit: int = 2000) -> list[dict[str, Any]]:
        """按时间升序返回明细报价。

        排序必须带 id 兜底：同一时间戳可能有多条记录（同轮多源写入、或测试里
        人为构造），而 idx_quotes_lookup 是 observed_at DESC 索引，
        仅按 observed_at ASC 排序时相同键的行会以「索引内 rowid 逆序」返回，
        导致 OHLC 的开盘/收盘价在并列时间戳上变成随机值。
        """
        since = iso(utcnow() - timedelta(hours=hours))
        cur = self._conn.execute(
            """
            SELECT * FROM quotes
            WHERE market_hash_name = ? AND platform = ? AND observed_at >= ?
            ORDER BY observed_at ASC, id ASC
            LIMIT ?
            """,
            (market_hash_name, platform, since, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def baseline_median(self, market_hash_name: str, platform: str,
                        hours: int = 168, exclude_last: int = 1) -> float | None:
        """基准价：最近 N 小时在售价的中位数。

        用中位数而非均值，避免个别畸形挂单（如 1 元钓鱼单）拉偏基准。
        exclude_last 用于排除「刚写进去的本次采样」，让基准是真正的前期水位。
        """
        since = iso(utcnow() - timedelta(hours=hours))
        cur = self._conn.execute(
            """
            SELECT sell_price FROM quotes
            WHERE market_hash_name = ? AND platform = ? AND observed_at >= ?
              AND sell_price IS NOT NULL AND sell_price > 0
            ORDER BY observed_at DESC, id DESC
            """,
            (market_hash_name, platform, since),
        )
        prices = [r[0] for r in cur.fetchall()]
        if exclude_last:
            prices = prices[exclude_last:]
        if len(prices) < 2:
            return None
        return float(statistics.median(prices))

    def latest_snapshot_all(self, limit: int = 500) -> list[dict[str, Any]]:
        """看板用：每个 (饰品, 平台) 的最新一条报价。"""
        cur = self._conn.execute(
            """
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY market_hash_name, platform ORDER BY observed_at DESC, id DESC
                ) AS rn
                FROM quotes
            ) WHERE rn = 1
            ORDER BY market_hash_name, platform
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]

    def quote_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0])

    # ── 告警 ───────────────────────────────────────────────

    def insert_alert(self, alert: AlertEvent) -> int:
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO alerts (market_hash_name, platform, rule, severity, message,
                                    current_price, baseline_price, change_percent, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (alert.market_hash_name, alert.platform, alert.rule, alert.severity,
                 alert.message, alert.current_price, alert.baseline_price,
                 alert.change_percent, iso(alert.created_at)),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def mark_alert_notified(self, alert_id: int, result: str) -> None:
        with self._write_lock:
            self._conn.execute(
                "UPDATE alerts SET notified = 1, notify_result = ? WHERE id = ?",
                (result[:500], alert_id))
            self._conn.commit()

    def last_alert_at(self, market_hash_name: str, platform: str,
                      rule: str | None = None) -> datetime | None:
        sql = ("SELECT created_at FROM alerts WHERE market_hash_name = ? AND platform = ?")
        params: list[Any] = [market_hash_name, platform]
        if rule:
            sql += " AND rule = ?"
            params.append(rule)
        sql += " ORDER BY created_at DESC LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        if not row:
            return None
        return datetime.fromisoformat(row[0])

    def recent_alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM alerts ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def alert_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])

    # ── 监控清单 ───────────────────────────────────────────

    def upsert_watch(self, rule: WatchRule, source: str = "yaml") -> None:
        now = iso()
        with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO watchlist (market_hash_name, rules, enabled, source, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?, ?)
                ON CONFLICT(market_hash_name) DO UPDATE SET
                    rules = excluded.rules, updated_at = excluded.updated_at
                """,
                (rule.market_hash_name, json.dumps(rule.to_dict(), ensure_ascii=False),
                 source, now, now),
            )
            self._conn.commit()

    def list_watch(self, enabled_only: bool = True) -> list[WatchRule]:
        sql = "SELECT * FROM watchlist"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY market_hash_name"
        out: list[WatchRule] = []
        for row in self._conn.execute(sql):
            data = json.loads(row["rules"] or "{}")
            out.append(WatchRule.from_dict(row["market_hash_name"], data))
        return out

    def remove_watch(self, market_hash_name: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM watchlist WHERE market_hash_name = ?", (market_hash_name,))
            self._conn.commit()
            return cur.rowcount > 0

    def watch_count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM watchlist WHERE enabled = 1").fetchone()[0])

    # ── 索引进度 ───────────────────────────────────────────

    def get_cursor(self, scanner: str) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT cursor, hits, scanned FROM index_progress WHERE scanner = ?",
            (scanner,)).fetchone()
        return dict(row) if row else {"cursor": 0, "hits": 0, "scanned": 0}

    def set_cursor(self, scanner: str, cursor: int, hits: int, scanned: int) -> None:
        with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO index_progress (scanner, cursor, hits, scanned, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scanner) DO UPDATE SET
                    cursor = excluded.cursor, hits = excluded.hits,
                    scanned = excluded.scanned, updated_at = excluded.updated_at
                """,
                (scanner, cursor, hits, scanned, iso()),
            )
            self._conn.commit()

    # ── 采集日志 / 统计 ────────────────────────────────────

    def log_fetch(self, source: str, requested: int, succeeded: int, failed: int,
                  quotes: int, started_at: str, finished_at: str | None,
                  errors: Sequence[str] = ()) -> None:
        with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO fetch_log (source, requested, succeeded, failed, quotes,
                                       started_at, finished_at, errors)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source, requested, succeeded, failed, quotes, started_at, finished_at,
                 json.dumps(list(errors)[:20], ensure_ascii=False)),
            )
            self._conn.commit()

    def source_health(self, limit_per_source: int = 1) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY source ORDER BY started_at DESC, id DESC
                ) AS rn FROM fetch_log
            ) WHERE rn <= ?
            ORDER BY source, started_at DESC
            """,
            (limit_per_source,),
        )
        return [dict(r) for r in cur.fetchall()]

    # ── 日线 OHLC 缓存 ─────────────────────────────────────

    def distinct_quote_pairs(self) -> list[tuple[str, str]]:
        """所有出现过的 (饰品, 平台) 组合，用于批量回填 OHLC。"""
        cur = self._conn.execute(
            "SELECT DISTINCT market_hash_name, platform FROM quotes "
            "ORDER BY market_hash_name, platform")
        return [(r[0], r[1]) for r in cur.fetchall()]

    def upsert_ohlc(self, market_hash_name: str, platform: str,
                    bars: Sequence[dict[str, Any]]) -> int:
        if not bars:
            return 0
        now = iso()
        rows = [
            (market_hash_name, platform, b["date"], b["open"], b["high"],
             b["low"], b["close"], b.get("count", 0), b.get("avg_count"), now)
            for b in bars
        ]
        with self._write_lock:
            self._conn.executemany(
                """
                INSERT INTO ohlc_cache (market_hash_name, platform, date, open, high,
                                        low, close, count, avg_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_hash_name, platform, date) DO UPDATE SET
                    open = excluded.open, high = excluded.high, low = excluded.low,
                    close = excluded.close, count = excluded.count,
                    avg_count = excluded.avg_count, updated_at = excluded.updated_at
                """,
                rows,
            )
            self._conn.commit()
        return len(rows)

    def get_ohlc(self, market_hash_name: str, platform: str,
                 days: int = 90) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT date, open, high, low, close, count, avg_count
            FROM ohlc_cache
            WHERE market_hash_name = ? AND platform = ?
            ORDER BY date DESC LIMIT ?
            """,
            (market_hash_name, platform, max(1, days)),
        )
        return [dict(r) for r in reversed(cur.fetchall())]

    # ── 归档 ───────────────────────────────────────────────

    def archive_old_quotes(self, keep_days: int = 90) -> dict[str, int]:
        """把超过保留期的明细报价按天聚合成一行，然后删除明细。

        为什么不是直接删：趋势分析需要长期历史，但保留每一条高频采样
        既占空间又没必要。按天聚合（均价/最高/最低/首末）足以支撑日线，
        同时把行数压到 1/N。
        """
        cutoff = iso(utcnow() - timedelta(days=keep_days))
        with self._write_lock:
            cur = self._conn.execute(
                """
                SELECT market_hash_name, platform, substr(observed_at, 1, 10) AS day,
                       AVG(sell_price) AS avg_price, MIN(sell_price) AS min_price,
                       MAX(sell_price) AS max_price, COUNT(*) AS n
                FROM quotes
                WHERE observed_at < ? AND sell_price IS NOT NULL AND sell_price > 0
                GROUP BY market_hash_name, platform, day
                """,
                (cutoff,),
            )
            groups = cur.fetchall()

            archived = 0
            for row in groups:
                # 首末价单独取，保持与日线 open/close 的口径一致；
                # 同样带 id 兜底，避免并列时间戳导致归档价每次跑结果不同
                first = self._conn.execute(
                    """SELECT sell_price FROM quotes
                       WHERE market_hash_name = ? AND platform = ?
                         AND substr(observed_at,1,10) = ? AND sell_price > 0
                       ORDER BY observed_at ASC, id ASC LIMIT 1""",
                    (row["market_hash_name"], row["platform"], row["day"])).fetchone()
                last = self._conn.execute(
                    """SELECT sell_price FROM quotes
                       WHERE market_hash_name = ? AND platform = ?
                         AND substr(observed_at,1,10) = ? AND sell_price > 0
                       ORDER BY observed_at DESC, id DESC LIMIT 1""",
                    (row["market_hash_name"], row["platform"], row["day"])).fetchone()
                self._conn.execute(
                    """
                    INSERT INTO archived_prices
                        (market_hash_name, platform, date, avg_price, min_price,
                         max_price, first_price, last_price, record_count, archived_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market_hash_name, platform, date) DO UPDATE SET
                        avg_price = excluded.avg_price, min_price = excluded.min_price,
                        max_price = excluded.max_price, first_price = excluded.first_price,
                        last_price = excluded.last_price, record_count = excluded.record_count,
                        archived_at = excluded.archived_at
                    """,
                    (row["market_hash_name"], row["platform"], row["day"],
                     row["avg_price"], row["min_price"], row["max_price"],
                     first[0] if first else None, last[0] if last else None,
                     row["n"], iso()),
                )
                archived += 1

            deleted = self._conn.execute(
                "DELETE FROM quotes WHERE observed_at < ?", (cutoff,)).rowcount
            self._conn.commit()
        return {"groups_archived": archived, "rows_deleted": deleted,
                "keep_days": keep_days}

    def archived_history(self, market_hash_name: str, platform: str,
                         days: int = 3650) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT date, avg_price, min_price, max_price, first_price,
                   last_price, record_count
            FROM archived_prices
            WHERE market_hash_name = ? AND platform = ?
            ORDER BY date DESC LIMIT ?
            """,
            (market_hash_name, platform, days),
        )
        return [dict(r) for r in reversed(cur.fetchall())]

    def archive_stats(self) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(record_count), 0) FROM archived_prices"
        ).fetchone()
        return {"archived_days": int(row[0]), "archived_raw_rows": int(row[1])}

    # ── 极致追踪 ───────────────────────────────────────────

    def upsert_extreme(self, market_hash_name: str, platform: str,
                       **fields: Any) -> None:
        now = iso()
        allowed = ("interval_seconds", "enabled", "price_mode", "price_threshold",
                   "quantity_mode", "quantity_threshold", "cooldown_seconds",
                   "quiet_start", "quiet_end")
        cols = {k: v for k, v in fields.items() if k in allowed}
        with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO extreme_watch (market_hash_name, platform, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(market_hash_name, platform) DO NOTHING
                """,
                (market_hash_name, platform, now, now),
            )
            if cols:
                assignments = ", ".join(f"{k} = ?" for k in cols)
                self._conn.execute(
                    f"UPDATE extreme_watch SET {assignments}, updated_at = ? "
                    "WHERE market_hash_name = ? AND platform = ?",
                    (*cols.values(), now, market_hash_name, platform),
                )
            self._conn.commit()

    def list_extreme(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM extreme_watch"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY market_hash_name, platform"
        return [dict(r) for r in self._conn.execute(sql)]

    def remove_extreme(self, market_hash_name: str, platform: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM extreme_watch WHERE market_hash_name = ? AND platform = ?",
                (market_hash_name, platform))
            self._conn.commit()
            return cur.rowcount > 0

    def insert_extreme_samples(self, samples: Sequence[dict[str, Any]]) -> int:
        if not samples:
            return 0
        rows = [
            (s["market_hash_name"], s["platform"], s.get("sell_price"),
             s.get("sell_count"), s.get("bid_price"), s.get("observed_at") or iso())
            for s in samples
        ]
        with self._write_lock:
            self._conn.executemany(
                """INSERT INTO extreme_samples
                   (market_hash_name, platform, sell_price, sell_count, bid_price, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def latest_extreme_sample(self, market_hash_name: str,
                              platform: str, before_id: int | None = None) -> dict[str, Any] | None:
        sql = ("SELECT * FROM extreme_samples WHERE market_hash_name = ? AND platform = ?")
        params: list[Any] = [market_hash_name, platform]
        if before_id is not None:
            sql += " AND id < ?"
            params.append(before_id)
        sql += " ORDER BY id DESC LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def prune_extreme_samples(self, keep_days: int = 7) -> int:
        cutoff = iso(utcnow() - timedelta(days=keep_days))
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM extreme_samples WHERE observed_at < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # ── 中文名 ─────────────────────────────────────────────

    def upsert_cn_names(self, rows: Sequence[tuple[str, str, str, str | None, str | None]]) -> int:
        """写入 (market_hash_name, cn_name, source, base_cn, base_en)。

        已有条目被**新的权威源**覆盖时才更新：手工录入 > 官方 API > 机器拼装。
        """
        if not rows:
            return 0
        priority = {"manual": 100, "quote_raw": 50, "composed": 1}
        now = iso()
        written = 0
        with self._write_lock:
            for mhn, cn, source, base_cn, base_en in rows:
                existing = self._conn.execute(
                    "SELECT source FROM cn_names WHERE market_hash_name = ?",
                    (mhn,)).fetchone()
                if existing and priority.get(existing["source"], 0) > priority.get(source, 0):
                    continue     # 已有更高优先级的名字，不覆盖
                self._conn.execute(
                    """
                    INSERT INTO cn_names (market_hash_name, cn_name, base_en, base_cn,
                                          source, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market_hash_name) DO UPDATE SET
                        cn_name = excluded.cn_name,
                        base_en = COALESCE(excluded.base_en, cn_names.base_en),
                        base_cn = excluded.base_cn,
                        source = excluded.source, updated_at = excluded.updated_at
                    """,
                    (mhn, cn, base_en, base_cn, source, now),
                )
                written += 1
            self._conn.commit()
        return written

    def get_cn_name(self, market_hash_name: str) -> str | None:
        row = self._conn.execute(
            "SELECT cn_name FROM cn_names WHERE market_hash_name = ?",
            (market_hash_name,)).fetchone()
        return row["cn_name"] if row else None

    def find_cn_name_by_base(self, base_en: str) -> str | None:
        """按**英文**基础名找已知的中文基础名。

        用途：学过「久经沙场」的中文名后，同皮肤的「崭新出厂」也能显示中文，
        不必为每个磨损档都联网查一次 —— 磨损只有 5 档，皮肤有上万个。

        必须用 base_en 关联：中文名与英文名之间没有可推导的关系，
        拿英文基础名去 LIKE 中文列永远匹配不上。
        """
        if not base_en:
            return None
        row = self._conn.execute(
            """
            SELECT base_cn FROM cn_names
            WHERE base_en = ? AND base_cn IS NOT NULL AND base_cn != ''
            ORDER BY updated_at DESC LIMIT 1
            """,
            (base_en,),
        ).fetchone()
        if row:
            return row["base_cn"]
        # 兜底：老库里可能没填 base_en，用中文前缀粗匹配一次
        row = self._conn.execute(
            """
            SELECT base_cn FROM cn_names
            WHERE base_cn IS NOT NULL AND base_cn != ''
              AND REPLACE(base_cn, ' ', '') LIKE REPLACE(?, ' ', '') || '%'
            LIMIT 1
            """,
            (base_en,),
        ).fetchone()
        return row["base_cn"] if row else None

    def cn_name_stats(self) -> dict[str, Any]:
        total_items = self.count_items()
        learned = self._conn.execute("SELECT COUNT(*) FROM cn_names").fetchone()[0]
        by_source = {
            r["source"]: r["n"] for r in self._conn.execute(
                "SELECT source, COUNT(*) AS n FROM cn_names GROUP BY source")
        }
        return {
            "learned": int(learned),
            "items_in_db": total_items,
            "coverage": round(learned / total_items, 4) if total_items else 0.0,
            "by_source": by_source,
        }

    # ── 关注清单（带交易意图）──────────────────────────────

    def upsert_focus(self, market_hash_name: str, **fields: Any) -> None:
        allowed = ("intent", "priority", "target_price", "acceptable_wear",
                   "acceptable_quality", "acceptable_patterns", "max_budget",
                   "quantity", "note", "enabled")
        cols = {k: v for k, v in fields.items() if k in allowed and v is not None}
        now = iso()
        with self._write_lock:
            self._conn.execute(
                """INSERT INTO focus (market_hash_name, created_at, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(market_hash_name) DO NOTHING""",
                (market_hash_name, now, now))
            if cols:
                assignments = ", ".join(f"{k} = ?" for k in cols)
                self._conn.execute(
                    f"UPDATE focus SET {assignments}, updated_at = ? "
                    "WHERE market_hash_name = ?",
                    (*cols.values(), now, market_hash_name))
            self._conn.commit()

    def list_focus(self, intent: str | None = None,
                   enabled_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM focus WHERE 1=1"
        params: list[Any] = []
        if enabled_only:
            sql += " AND enabled = 1"
        if intent:
            sql += " AND intent = ?"
            params.append(intent)
        sql += " ORDER BY priority ASC, market_hash_name"
        return [dict(r) for r in self._conn.execute(sql, params)]

    def get_focus(self, market_hash_name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM focus WHERE market_hash_name = ?",
            (market_hash_name,)).fetchone()
        return dict(row) if row else None

    def remove_focus(self, market_hash_name: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM focus WHERE market_hash_name = ?", (market_hash_name,))
            self._conn.commit()
            return cur.rowcount > 0

    def focus_stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT intent, COUNT(*) AS n FROM focus WHERE enabled = 1 GROUP BY intent")
        stats = {r["intent"]: int(r["n"]) for r in rows}
        stats["total"] = sum(stats.values())
        return stats

    # ── 图案档位样本 ───────────────────────────────────────

    def insert_seed_samples(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        payload = [
            (r["market_hash_name"], r.get("paint_index"), r["paint_seed"],
             r.get("paint_wear"), r["price"], r.get("observed_at") or iso())
            for r in rows if r.get("paint_seed") is not None and r.get("price")
        ]
        if not payload:
            return 0
        with self._write_lock:
            self._conn.executemany(
                """INSERT INTO seed_samples
                   (market_hash_name, paint_index, paint_seed, paint_wear, price, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                payload)
            self._conn.commit()
        return len(payload)

    def seed_samples_for(self, market_hash_name: str,
                         limit: int = 5000) -> list[tuple[int, float]]:
        """返回 [(paint_seed, price)]，供档位学习使用。"""
        rows = self._conn.execute(
            """SELECT paint_seed, price FROM seed_samples
               WHERE market_hash_name = ? AND price > 0
               ORDER BY id DESC LIMIT ?""",
            (market_hash_name, limit)).fetchall()
        return [(int(r["paint_seed"]), float(r["price"])) for r in rows]

    def seed_sample_stats(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT market_hash_name) FROM seed_samples"
        ).fetchone()
        return {"samples": int(row[0]), "items": int(row[1])}

    def prune_seed_samples(self, keep_days: int = 90) -> int:
        cutoff = iso(utcnow() - timedelta(days=keep_days))
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM seed_samples WHERE observed_at < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # ── LLM 建议 ───────────────────────────────────────────

    def insert_advice(self, record: dict[str, Any]) -> int:
        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO advice (market_hash_name, platform, provider, model,
                                    action, confidence, target_buy, target_sell,
                                    stop_loss, horizon_days, reasoning, risks,
                                    raw_response, context_digest, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (record["market_hash_name"], record.get("platform"),
                 record["provider"], record["model"], record["action"],
                 record.get("confidence"), record.get("target_buy"),
                 record.get("target_sell"), record.get("stop_loss"),
                 record.get("horizon_days"), record.get("reasoning"),
                 record.get("risks"), record.get("raw_response"),
                 record.get("context_digest"), record.get("created_at") or iso()),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def recent_advice(self, limit: int = 50,
                      market_hash_name: str | None = None) -> list[dict[str, Any]]:
        if market_hash_name:
            rows = self._conn.execute(
                """SELECT * FROM advice WHERE market_hash_name = ?
                   ORDER BY created_at DESC, id DESC LIMIT ?""",
                (market_hash_name, limit))
        else:
            rows = self._conn.execute(
                "SELECT * FROM advice ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,))
        return [dict(r) for r in rows]

    def advice_stats(self) -> dict[str, Any]:
        total = self._conn.execute("SELECT COUNT(*) FROM advice").fetchone()[0]
        by_action = {
            r["action"]: int(r["n"]) for r in self._conn.execute(
                "SELECT action, COUNT(*) AS n FROM advice GROUP BY action")
        }
        return {"total": int(total), "by_action": by_action}

    # ── 档位词表 ───────────────────────────────────────────

    def upsert_variant_terms(self, rows: Sequence[tuple[str, str, str, int, str]]) -> int:
        """写入 (market_hash_name, kind, value, sample_count, source)。

        同一取值重复写入时**累加**样本数而不是覆盖：多轮采集能逐步逼近
        该品类真实的档位覆盖度（冷门档位往往要多拉几页才出现）。
        """
        if not rows:
            return 0
        now = iso()
        with self._write_lock:
            self._conn.executemany(
                """
                INSERT INTO variant_terms
                    (market_hash_name, kind, value, sample_count, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_hash_name, kind, value) DO UPDATE SET
                    sample_count = variant_terms.sample_count + excluded.sample_count,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                [(mhn, kind, value, count, source, now)
                 for mhn, kind, value, count, source in rows],
            )
            self._conn.commit()
        return len(rows)

    def variant_terms_for(self, market_hash_name: str,
                          kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM variant_terms WHERE market_hash_name = ?"
        params: list[Any] = [market_hash_name]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY kind, sample_count DESC, value"
        return [dict(r) for r in self._conn.execute(sql, params)]

    def variant_terms_by_kind(self, kind: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM variant_terms WHERE kind = ? "
            "ORDER BY market_hash_name, sample_count DESC", (kind,))]

    def variant_vocab_stats(self) -> dict[str, Any]:
        rows = self._conn.execute(
            """SELECT kind, COUNT(DISTINCT market_hash_name) AS items,
                      COUNT(*) AS terms FROM variant_terms GROUP BY kind""")
        by_kind = {r["kind"]: {"items": int(r["items"]), "terms": int(r["terms"])}
                   for r in rows}
        items = self._conn.execute(
            "SELECT COUNT(DISTINCT market_hash_name) FROM variant_terms").fetchone()[0]
        return {"items_with_vocab": int(items), "by_kind": by_kind}

    # ── 统计 ───────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        return {
            "items": self.count_items(),
            "quotes": self.quote_count(),
            "alerts": self.alert_count(),
            "watching": self.watch_count(),
            "database": str(self.path),
        }
