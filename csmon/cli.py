"""命令行入口：python -m csmon <命令>。

命令概览
  run        跑一轮采集（--loop 变常驻）
  serve      启动 Web 看板
  watch      管理监控清单（add / rm / ls）
  index      BUFF goods_id 索引（scan / resolve / status）
  seed       导入参考项目随包的映射资源
  report     终端打印行情快照与告警
  sources    列出支持的源与各源平台覆盖
  probe      连通性与鉴权自检
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .config import ALL_SOURCES, Config, load_config
from .indexer import BuffIndexer
from .mapping import MappingService, default_reference_resources
from .models import WatchRule
from .scheduler import Monitor
from .sources import describe_registry
from .store import Store


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ── 子命令实现 ─────────────────────────────────────────────

def cmd_run(args: argparse.Namespace, config: Config) -> int:
    with Monitor(config) as monitor:
        added = monitor.seed_watchlist()
        if added:
            print(f"已从配置导入 {added} 条监控规则")
        if args.loop:
            try:
                monitor.run_forever(args.interval)
            except KeyboardInterrupt:
                print("\n已停止")
            return 0
        result = monitor.run_once(only_sources=set(args.source) if args.source else None)
        print(f"\n{result.summary()}")
        for note in result.notes:
            print(f"  · {note}")
        for report in result.reports:
            status = "OK" if not report.failed else "部分失败"
            print(f"  [{status}] {report.source}: 请求 {report.requested} "
                  f"成功 {report.succeeded} 报价 {report.quotes}")
            for err in report.errors[:3]:
                print(f"        ! {err}")
        if result.alerts:
            print("\n告警：")
            for ev in result.alerts:
                print(f"  [{ev.severity}] {ev.platform} {ev.market_hash_name} :: {ev.message}")
    return 0


def cmd_serve(args: argparse.Namespace, config: Config) -> int:
    from .web import serve
    if args.port:
        config.web.port = args.port
    if args.host:
        config.web.host = args.host
    serve(config)
    return 0


def cmd_watch(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.database)
    try:
        if args.action == "ls":
            rules = store.list_watch(enabled_only=False)
            if not rules:
                print("监控清单为空。用 `csmon watch add \"AK-47 | Redline (Field-Tested)\"` 添加")
                return 0
            print(f"{'饰品':<52} {'跌破':>8} {'涨破':>8} {'跌%':>6} {'涨%':>6}  平台")
            for r in rules:
                print(f"{r.market_hash_name:<52} "
                      f"{_f(r.below):>8} {_f(r.above):>8} "
                      f"{_f(r.drop_percent):>6} {_f(r.rise_percent):>6}  "
                      f"{','.join(r.platforms) or '全部'}")
            return 0

        if args.action == "add":
            if not args.name:
                print("需要饰品名（Steam 官方 market_hash_name）", file=sys.stderr)
                return 2
            rule = WatchRule(
                market_hash_name=args.name,
                below=args.below, above=args.above,
                drop_percent=args.drop, rise_percent=args.rise,
                cooldown_minutes=args.cooldown,
                platforms=[p.upper() for p in (args.platform or [])],
            )
            store.upsert_watch(rule, source="cli")
            MappingService(store).set_ids(args.name,
                                          buff_goods_id=args.buff_id,
                                          youpin_template_id=args.youpin_id)
            print(f"已添加监控：{args.name}")
            if args.below is None and args.drop is None and args.rise is None:
                print("  提示：未设置阈值，仅记录价格。可加 --below 100 或 --drop 8")
            if args.buff_id is None or args.youpin_id is None:
                print("  提示：缺少平台 ID 时，直连源会跳过该饰品；"
                      "可用 `csmon index resolve` 或 `csmon watch add ... --buff-id N` 补齐")
            return 0

        if args.action == "rm":
            if not args.name:
                print("需要饰品名", file=sys.stderr)
                return 2
            ok = store.remove_watch(args.name)
            print("已删除" if ok else "未找到该监控项")
            return 0 if ok else 1

        print(f"未知操作：{args.action}", file=sys.stderr)
        return 2
    finally:
        store.close()


def cmd_index(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.database)
    adapter = None
    try:
        from .sources.buff_direct import BuffDirectAdapter
        adapter = BuffDirectAdapter(config.source("buff_direct"))
        indexer = BuffIndexer(store, adapter, MappingService(store))

        if args.action == "status":
            status = indexer.status()
            coverage = MappingService(store).coverage()
            print("BUFF 索引器状态：")
            for k, v in status.items():
                print(f"  {k:<16} {v}")
            print("\n映射覆盖率：")
            for k, v in coverage.items():
                print(f"  {k:<24} {v}")
            return 0

        if args.action == "scan":
            print(f"扫描 BUFF ID 空间 [{args.start}, {args.end}) step={args.step}"
                  f"（游标已持久化，可中断续跑）")
            stats = indexer.scan(
                start=args.start, end=args.end, step=args.step,
                resume=not args.no_resume,
                max_seconds=args.max_seconds,
                progress=lambda c, h, s: print(f"  游标 {c}  命中 {h}  已扫 {s}"),
            )
            print(f"\n完成：{stats}")
            return 0

        if args.action == "resolve":
            names = [r.market_hash_name for r in store.list_watch()]
            if not names:
                print("监控清单为空，无需补齐")
                return 0
            print(f"为 {len(names)} 个监控饰品定向补齐 BUFF goods_id…")
            stats = indexer.resolve_missing(
                names, scan_range=(args.start, args.end),
                max_seconds=args.max_seconds)
            print(f"完成：{stats}")
            return 0

        print(f"未知操作：{args.action}", file=sys.stderr)
        return 2
    finally:
        if adapter is not None:
            adapter.close()
        store.close()


def cmd_seed(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.database)
    try:
        mapping = MappingService(store)
        resources = default_reference_resources()
        total = 0
        for label, path in resources.items():
            if not Path(path).exists():
                print(f"跳过 {label}（资源不存在：{path}）")
                continue
            if "mapping" in label:
                n = mapping.seed_from_youpin_mapping(path)
                total += n
                print(f"导入 {label}: {n} 条")
            else:
                print(f"忽略 {label}（当前不需要）")
        if args.extra:
            for extra in args.extra:
                n = mapping.seed_from_youpin_mapping(extra)
                total += n
                print(f"导入 {extra}: {n} 条")
        print(f"\n共导入 {total} 条映射")
        for k, v in mapping.coverage().items():
            print(f"  {k:<24} {v}")
        return 0
    finally:
        store.close()


def cmd_report(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.database)
    try:
        stats = store.stats()
        print(f"库：{stats['database']}")
        print(f"饰品 {stats['items']} | 报价采样 {stats['quotes']} | "
              f"监控 {stats['watching']} | 告警 {stats['alerts']}\n")

        rows = store.latest_snapshot_all(limit=args.limit)
        if rows:
            print("最新行情：")
            print(f"{'饰品':<46} {'平台':<7} {'在售价':>9} {'在售量':>7} {'求购价':>9}  源")
            for r in rows:
                print(f"{(r['market_hash_name'] or '')[:46]:<46} {r['platform']:<7} "
                      f"{_f(r['sell_price']):>9} {_s(r['sell_count']):>7} "
                      f"{_f(r['bid_price']):>9}  {r['source']}")
        else:
            print("还没有报价数据。先运行 `python -m csmon run`")

        alerts = store.recent_alerts(limit=args.alerts)
        if alerts:
            print(f"\n最近 {len(alerts)} 条告警：")
            for a in alerts:
                print(f"  {(a['created_at'] or '')[:19]} [{a['severity']:<8}] "
                      f"{a['platform']:<7} {a['market_hash_name'][:40]:<40} {a['rule']}")
        return 0
    finally:
        store.close()


def cmd_kline(args: argparse.Namespace, config: Config) -> int:
    """打印某个饰品的日线 + 技术指标（看板图表的命令行等价物）。"""
    from .analytics import kline_bundle

    store = Store(config.database)
    try:
        bundle = kline_bundle(store, args.name, args.platform.upper(), days=args.days)
        bars = bundle["bars"]
        if not bars:
            print(f"没有 {args.name} 在 {args.platform.upper()} 的历史数据。")
            print("先跑一轮采集：python -m csmon run")
            return 1

        print(f"\n{args.name}  @ {args.platform.upper()}  共 {len(bars)} 根日线"
              f"（数据来源：{bundle['source']}）\n")
        print(f"{'日期':<12} {'开':>9} {'高':>9} {'低':>9} {'收':>9} {'采样':>5} {'在售量':>8}")
        for bar in bars[-args.tail:]:
            avg_count = bar.get("avg_count")
            count_text = f"{avg_count:.0f}" if avg_count else "—"
            print(f"{bar['date']:<12} {bar['open']:>9.2f} {bar['high']:>9.2f} "
                  f"{bar['low']:>9.2f} {bar['close']:>9.2f} {bar['count']:>5} "
                  f"{count_text:>8}")

        ind = bundle.get("indicators")
        if ind:
            print("\n指标：")
            print(f"  现价        {_f(ind.get('last'))}")
            ma = ind.get("ma") or {}
            print(f"  均线        MA7 {_f(ma.get('ma7'))}   MA30 {_f(ma.get('ma30'))}"
                  f"   MA90 {_f(ma.get('ma90'))}")
            print(f"  RSI(14)     {_f(ind.get('rsi14'))}")
            boll = ind.get("bollinger") or {}
            if boll:
                print(f"  布林带      上 {_f(boll.get('upper'))}  中 {_f(boll.get('middle'))}"
                      f"  下 {_f(boll.get('lower'))}   带宽 "
                      f"{(boll.get('width') or 0):.1%}")
            print(f"  年化波动率   {_fmt_pct(ind.get('annualized_volatility'))}")
            print(f"  年化收益率   {_fmt_pct(ind.get('annualized_return'))}")
            print(f"  7 日动量     {_fmt_pct(ind.get('momentum_7'))}")
            print(f"  最大回撤     {_fmt_pct(ind.get('max_drawdown'))}")
            print(f"  Z 值(30)     {_f(ind.get('zscore_30'))}")
            print(f"  7 日均价基准 {_f(bundle.get('baseline_median_7d'))}")
            for line in bundle.get("readings") or []:
                print(f"  · {line}")
        return 0
    finally:
        store.close()


def cmd_spread(args: argparse.Namespace, config: Config) -> int:
    """跨平台价差雷达。"""
    from .analytics import spread_radar

    store = Store(config.database)
    try:
        spreads = spread_radar(store, min_net_percent=args.min_percent,
                               min_net_profit=args.min_profit, limit=args.limit)
        if not spreads:
            print("没有找到满足条件的价差机会。")
            print("（阈值可用 --min-percent / --min-profit 调整；"
                  "数据少时先多跑几轮采集）")
            return 0

        print(f"\n跨平台价差雷达（手续费已计入；阈值 净收益 ≥ {args.min_percent:.1%} "
              f"且 ≥ ¥{args.min_profit:.2f}）\n")
        print(f"{'饰品':<40} {'买入':<7} {'买价':>9} {'卖出':<7} {'卖价':>9} "
              f"{'净收益':>9} {'净%':>7}  可即时")
        for s in spreads:
            print(f"{s.market_hash_name[:40]:<40} {s.buy_platform:<7} "
                  f"{s.buy_price:>9.2f} {s.sell_platform:<7} {s.sell_price:>9.2f} "
                  f"{s.net_profit:>9.2f} {s.net_percent:>6.1%}  "
                  f"{'是' if s.sell_is_bid else '否(需挂单)'}")
        print("\n说明：")
        print("  · 「可即时」= 对手平台有求购价，买入后能立刻卖给求购单")
        print("  · 否则只是两边挂售价有差，实际成交需要等买家，存在时间风险")
        print("  · 手续费默认 BUFF 2.5% / 悠悠有品 2.0% + 提现 1%，"
              "请在 config.yaml 的 arbitrage.fees 按实际费率覆盖")
        return 0
    finally:
        store.close()


def cmd_movers(args: argparse.Namespace, config: Config) -> int:
    """涨跌排行。"""
    from .analytics import liquidity_board, movers

    store = Store(config.database)
    try:
        board = movers(store, hours=args.hours, limit=args.limit)
        window = board["window_hours"]
        print(f"\n涨跌排行（相对近 {window}h 中位价）\n")
        for title, key in (("涨幅榜", "gained"), ("跌幅榜", "lost")):
            rows = board[key]
            print(f"{title}:")
            if not rows:
                print("  （数据不足，先多跑几轮采集）")
            for r in rows:
                print(f"  {r['change_percent']:>+7.1%}  {r['platform']:<7} "
                      f"{r['current']:>9.2f}  {r['market_hash_name'][:44]}")
            print()

        if args.liquidity:
            print("在售量榜（流动性）：")
            for row in liquidity_board(store, limit=args.limit):
                print(f"  {row['sell_count']:>6}  {row['platform']:<7} "
                      f"{_f(row['sell_price']):>9}  {row['market_hash_name'][:44]}")
        return 0
    finally:
        store.close()


def cmd_extreme(args: argparse.Namespace, config: Config) -> int:
    """极致追踪：单件高频轮询。"""
    from .extreme import ExtremeTracker

    store = Store(config.database)
    try:
        if args.action == "ls":
            rows = store.list_extreme(enabled_only=False)
            if not rows:
                print("还没有极致追踪任务。")
                print('添加：python -m csmon extreme add "AK-47 | Redline (Field-Tested)" '
                      '--platform BUFF --interval 30')
                return 0
            print(f"{'饰品':<42} {'平台':<7} {'间隔':>6} {'价格规则':>16} "
                  f"{'数量规则':>16} {'冷却':>6}")
            for r in rows:
                price_rule = f"{r['price_mode']}/{r['price_threshold']}"
                qty_rule = f"{r['quantity_mode']}/{r['quantity_threshold']}"
                print(f"{r['market_hash_name'][:42]:<42} {r['platform']:<7} "
                      f"{r['interval_seconds']:>6} {price_rule:>16} {qty_rule:>16} "
                      f"{r['cooldown_seconds']:>6}")
            return 0

        if args.action == "add":
            if not args.name:
                print("需要饰品名", file=sys.stderr)
                return 2
            platform = (args.platform or "BUFF").upper()
            store.upsert_extreme(
                args.name, platform,
                interval_seconds=args.interval,
                price_mode=args.price_mode, price_threshold=args.price_threshold,
                quantity_mode=args.quantity_mode,
                quantity_threshold=args.quantity_threshold,
                cooldown_seconds=args.cooldown,
                quiet_start=args.quiet_start, quiet_end=args.quiet_end,
                enabled=1,
            )
            print(f"已添加极致追踪：{args.name} @ {platform}，间隔 {args.interval}s")
            print("  启动追踪：python bootstrap.py   （或 python -m csmon extreme run）")
            return 0

        if args.action == "rm":
            if not args.name:
                print("需要饰品名", file=sys.stderr)
                return 2
            platform = (args.platform or "BUFF").upper()
            ok = store.remove_extreme(args.name, platform)
            print("已删除" if ok else "未找到该追踪任务")
            return 0 if ok else 1

        if args.action == "run":
            from .alerts import AlertEngine
            from .notify import Notifier
            from .ratelimit import GateRegistry
            from .sources import build_sources

            gates = GateRegistry()
            sources = build_sources(config, gates)
            tracker = ExtremeTracker(store, sources, AlertEngine(store),
                                     Notifier(config.notify), gates)
            if not store.list_extreme():
                print("没有启用的追踪任务，先执行 extreme add")
                return 0
            print(f"运行 {len(store.list_extreme())} 个追踪任务，Ctrl+C 退出…")
            tracker.start()
            try:
                while tracker._thread and tracker._thread.is_alive():
                    time.sleep(1.0)
            except KeyboardInterrupt:
                pass
            finally:
                tracker.stop()
                snap = tracker.snapshot()
                print(f"\n采样 {snap['stats']['ticks']} 次，"
                      f"变动 {snap['stats']['changes']} 次，"
                      f"降频 {snap['stats']['backoffs']} 次")
                for adapter in sources:
                    adapter.close()
            return 0

        print(f"未知操作：{args.action}", file=sys.stderr)
        return 2
    finally:
        store.close()


def cmd_archive(args: argparse.Namespace, config: Config) -> int:
    """归档与回填。"""
    from .analytics import backfill_ohlc

    store = Store(config.database)
    try:
        if args.action == "stats":
            stats = store.archive_stats()
            print("归档统计：")
            for k, v in stats.items():
                print(f"  {k:<22} {v}")
            return 0

        if args.action == "run":
            print(f"归档超过 {args.keep_days} 天的明细报价（按天聚合）…")
            result = store.archive_old_quotes(keep_days=args.keep_days)
            for k, v in result.items():
                print(f"  {k:<22} {v}")
            return 0

        if args.action == "ohlc":
            print(f"为所有已采集组合回填日线（{args.days} 天）…")
            result = backfill_ohlc(store, days=args.days)
            print(f"  组合数 {result['pairs']}，写入 K 线 {result['bars']}")
            return 0

        if args.action == "prune-extreme":
            deleted = store.prune_extreme_samples(keep_days=args.keep_days)
            print(f"清理极致追踪采样 {deleted} 条（保留 {args.keep_days} 天）")
            return 0

        print(f"未知操作：{args.action}", file=sys.stderr)
        return 2
    finally:
        store.close()


def cmd_search(args: argparse.Namespace, config: Config) -> int:
    """按关键词搜索本地饰品库。"""
    store = Store(config.database)
    try:
        rows = store.search_items(args.keyword, limit=args.limit)
        if not rows:
            print(f"没有匹配「{args.keyword}」的饰品。")
            print("本地库来自：python -m csmon seed（映射资源）"
                  "或 python -m csmon index scan（BUFF ID 扫描）")
            return 1
        print(f"全文索引：{'可用' if store.fts_enabled else '不可用（已退回 LIKE）'}\n")
        print(f"{'饰品':<54} {'BUFF ID':>9} {'悠悠 ID':>9}")
        for r in rows:
            print(f"{(r['market_hash_name'] or '')[:54]:<54} "
                  f"{(r['buff_goods_id'] if r['buff_goods_id'] is not None else '—'):>9} "
                  f"{(r['youpin_template_id'] if r['youpin_template_id'] is not None else '—'):>9}")
        return 0
    finally:
        store.close()


def cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    """环境自检（等价于 bootstrap.py --check）。"""
    import json as _json

    from .doctor import format_report, run_checks

    report = run_checks(config, network=not args.no_network)
    if getattr(args, "json", False):
        print(_json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report.ok else 1


def cmd_names(args: argparse.Namespace, config: Config) -> int:
    """中文名管理：查看学习进度、手工设置、从采集数据回收。"""
    store = Store(config.database)
    try:
        if args.action == "stats":
            stats = store.cn_name_stats()
            print("中文名覆盖情况：")
            print(f"  已收录        {stats['learned']}")
            print(f"  库内饰品      {stats['items_in_db']}")
            print(f"  覆盖率        {stats['coverage']:.1%}")
            if stats["by_source"]:
                print("  来源分布：")
                for src, n in stats["by_source"].items():
                    print(f"    {src:<14} {n}")
            if stats["coverage"] < 0.5:
                print("\n提示：中文名在采集时自动回收。先跑几轮采集，覆盖率会自己涨上去：")
                print("  python -m csmon run")
            return 0

        if args.action == "set":
            if not args.name or not args.cn:
                print("用法：csmon names set \"AK-47 | Redline (Field-Tested)\" \"AK-47 | 红线 (久经沙场)\"",
                      file=sys.stderr)
                return 2
            from .names import NameResolver as _NR
            written = _NR(store).learn([(args.name, args.cn)], source="manual")
            print(f"已写入 {written} 条（来源标记为 manual，优先级最高，不会被自动覆盖）")
            return 0

        if args.action == "show":
            from .names import NameResolver
            resolver = NameResolver(store)
            names = ([args.name] if args.name
                     else [r.market_hash_name for r in store.list_watch()])
            if not names:
                print("没有可展示的饰品（可用 csmon watch ls 查看监控清单）")
                return 0
            for name in names:
                meta = resolver.resolve_with_meta(name)
                tag = {"learned": "官方", "derived": "派生", "composed": "拼装"}[
                    meta["source"]]
                print(f"  [{tag}] {meta['display_name']}")
                print(f"          {name}")
            print("\n来源含义：官方=权威源中文名；派生=由同皮肤其它磨损档的中文基础名拼出；")
            print("          拼装=完全按英文结构拼的，语序可能不地道（可手工更正）")
            return 0

        if args.action == "learn":
            # 中文名在采集时就已经顺手收进库里了（适配器把中文名放进
            # SourceQuote.raw，Monitor 落库前调用 learn_from_quotes）。
            # 这里只做统计说明，不再重复联网拉取 —— 额外请求要花限额。
            stats = store.cn_name_stats()
            print("中文名在采集时自动收录，无需单独联网拉取。")
            print(f"当前已收录 {stats['learned']} 条，覆盖 "
                  f"{stats['coverage']:.1%} 的库内饰品。")
            if stats["coverage"] < 0.5:
                print("多跑几轮采集覆盖率会自己涨：python -m csmon run")
            print("\n如果某个饰品的中文名不对，手工覆盖（优先级最高，不会被自动改回）：")
            print('  python -m csmon names set "英文名" "中文名"')
            return 0

        print(f"未知操作：{args.action}", file=sys.stderr)
        return 2
    finally:
        store.close()


def cmd_focus(args: argparse.Namespace, config: Config) -> int:
    """关注清单（带买卖意图）。"""
    store = Store(config.database)
    try:
        from .focus import (
            INTENT_CN, INTENTS, add_focus, build_board, describe_filters,
        )
        from .names import NameResolver

        if args.action == "ls":
            board = build_board(store, name_resolver=NameResolver(store))
            payload = board.to_dict()
            if not payload["counts"]["buy"] and not payload["counts"]["sell"] \
                    and not payload["counts"]["watch"]:
                print("关注清单为空。添加方式：")
                print('  python -m csmon focus add "AK-47 | Redline" --intent buy '
                      '--target 95 --wears FT,MW --budget 300')
                return 0

            for key, title in (("buy", "准备买入"), ("sell", "准备卖出"), ("watch", "观察")):
                rows = payload[key]
                if not rows:
                    continue
                print(f"\n=== {title}（{len(rows)}）===")
                print(f"{'状态':<10} {'现价':>9} {'目标':>9} {'平台':<7} {'优先级':<6} 饰品")
                for item in rows:
                    price = f"{item['current_price']:.2f}" if item["current_price"] else "—"
                    target = f"{item['target_price']:.2f}" if item["target_price"] else "—"
                    print(f"{item['status_cn']:<10} {price:>9} {target:>9} "
                          f"{str(item['current_platform'] or '—'):<7} "
                          f"{'★' * (4 - item['priority']):<6} {item['display_name']}")
            if payload["actionable"]:
                print(f"\n⚡ {payload['actionable']} 个标的已达标，可以动手")
            return 0

        if args.action == "add":
            if not args.name:
                print("需要饰品名或基础名", file=sys.stderr)
                return 2
            intent = args.intent
            if intent not in INTENTS:
                print(f"intent 必须是 {'/'.join(INTENTS)}", file=sys.stderr)
                return 2
            names = add_focus(
                store, args.name, intent=intent,
                target_price=args.target, max_budget=args.budget,
                quantity=args.quantity, priority=args.priority,
                note=args.note or "", wears=args.wears, qualities=args.qualities,
            )
            resolver = NameResolver(store)
            print(f"已添加 {len(names)} 个标的（{INTENT_CN[intent]}）：")
            for name in names:
                print(f"  {resolver.resolve(name)}")
                print(f"    {name}")
            if not args.target:
                print("\n提示：未设目标价，只能跟踪不能提示买卖点。加 --target 95 设置。")
            return 0

        if args.action == "rm":
            if not args.name:
                print("需要饰品名", file=sys.stderr)
                return 2
            names = [args.name]
            if args.expand:
                from .focus import expand_focus
                names = expand_focus(args.name)
            removed = sum(1 for n in names if store.remove_focus(n))
            print(f"已移除 {removed} 个标的")
            return 0 if removed else 1

        if args.action == "detail":
            if not args.name:
                print("需要饰品名", file=sys.stderr)
                return 2
            row = store.get_focus(args.name)
            if not row:
                print("未找到该关注项")
                return 1
            resolver = NameResolver(store)
            print(f"饰品      {resolver.resolve(row['market_hash_name'])}")
            print(f"市场名    {row['market_hash_name']}")
            print(f"意图      {INTENT_CN.get(row['intent'], row['intent'])}")
            print(f"优先级    {row['priority']}（1 最高）")
            print(f"目标价    {row['target_price'] or '—'}")
            print(f"预算上限  {row['max_budget'] or '—'}")
            print(f"数量      {row['quantity']}")
            print(f"接受条件  {describe_filters(row)}")
            print(f"备注      {row['note'] or '—'}")
            return 0

        print(f"未知操作：{args.action}", file=sys.stderr)
        return 2
    finally:
        store.close()


def cmd_patterns(args: argparse.Namespace, config: Config) -> int:
    """图案档位规则与实测学习。"""
    from .patterns import PatternTable, write_example_rules

    store = Store(config.database)
    try:
        table = PatternTable.load(args.rules)

        if args.action == "init":
            written = write_example_rules(args.rules, overwrite=args.force)
            if written:
                print(f"已写出规则脚手架：{args.rules}")
                print("打开它按注释填写。注意：种子对照表按刀型不同，务必用实盘核对。")
            else:
                print(f"{args.rules} 已存在（加 --force 覆盖）")
            return 0

        if args.action == "show":
            if args.name:
                # 用全文搜索而不是 items_with_buff_id：档位判定只需要名字，
                # 不该因为「还没解析出 BUFF ID」就查不到
                matches = [row["market_hash_name"]
                           for row in store.search_items(args.name, limit=20)]
                if not matches:
                    print(f"本地库没有匹配「{args.name}」的饰品（先跑 csmon seed 或 index）")
                    return 1
                from .names import NameResolver
                resolver = NameResolver(store)
                for mhn in matches[:20]:
                    match = table.classify(mhn)
                    print(f"  {resolver.resolve(mhn)}")
                    print(f"    {mhn}")
                    print(f"    档位：{match.label_cn}（来源 {match.source}）  {match.detail}")
            else:
                print(f"规则组：{len(table.rule_sets)}    已学习饰品：{len(table.learned)}")
                for rs in table.rule_sets:
                    print(f"  scope={rs.scope!r}  名称规则 {len(rs.name_rules)}  "
                          f"种子规则 {len(rs.seed_rules)}")
                if not table.rule_sets:
                    print("\n规则表为空。两条路：")
                    print("  1) 实测学习（推荐）：python -m csmon patterns learn \"饰品名\"")
                    print("  2) 手工填表：python -m csmon patterns init 后编辑 patterns.yaml")
            return 0

        if args.action == "learn":
            if not args.name:
                print("需要饰品名（且本地库要有它的 buff_goods_id）", file=sys.stderr)
                return 2
            from .sources.buff_direct import BuffDirectAdapter
            item = store.get_item(args.name)
            if not item or not item.get("buff_goods_id"):
                print(f"本地库没有 {args.name} 的 buff_goods_id。")
                print("先补 ID：python -m csmon watch add \"名称\" --buff-id 43076")
                return 1

            adapter = BuffDirectAdapter(config.source("buff_direct"))
            try:
                print(f"抓取 {args.name} 的在售挂单明细（{args.pages} 页）…")
                listings = adapter.fetch_listings(int(item["buff_goods_id"]),
                                                  pages=args.pages)
            finally:
                adapter.close()

            if not listings:
                print("没有抓到明细（可能被 BUFF 风控拦下，或该饰品无挂单）")
                print("检查：python -m csmon probe")
                return 1

            samples = [{"market_hash_name": args.name,
                        "paint_index": row["paint_index"],
                        "paint_seed": row["paint_seed"],
                        "paint_wear": row["paint_wear"],
                        "price": row["price"]} for row in listings]
            store.insert_seed_samples(samples)

            seed_prices = store.seed_samples_for(args.name)
            ratios = table.learn_single(args.name, seed_prices)
            table.save(args.rules)

            print(f"\n采集 {len(listings)} 条挂单，入库 {len(samples)} 条样本")
            print(f"共 {len(seed_prices)} 个 (种子, 价格) 样本")
            if not ratios:
                print("样本量不足，无法判定档位（至少需要 8 条、每个种子 2 条）")
                return 0

            premium = table.premium_seeds(args.name)
            print(f"\n价格基准（中位数）与溢价种子（阈值 {table.premium_ratio}×）：")
            for seed, ratio in list(premium.items())[:15]:
                prices = [p for s, p in seed_prices if s == seed]
                print(f"  种子 #{seed:<5} 溢价 {ratio:>5.2f}×  样本 {len(prices)}  "
                      f"中位 ¥{sorted(prices)[len(prices)//2]:.2f}")
            if not premium:
                print("  无显著溢价种子（该饰品价格均匀，或样本还不够）")
            print(f"\n结果已写入 {args.rules}（learned 段），下次 classify 会自动用上")
            return 0

        if args.action == "stats":
            stats = store.seed_sample_stats()
            print("图案样本库：")
            print(f"  样本总数      {stats['samples']}")
            print(f"  覆盖饰品      {stats['items']}")
            print(f"  学习结果      {len(table.learned)} 个饰品")
            return 0

        print(f"未知操作：{args.action}", file=sys.stderr)
        return 2
    finally:
        store.close()


def cmd_advice(args: argparse.Namespace, config: Config) -> int:
    """LLM 交易建议。"""
    from .llm import LLMClient, LLMConfig, LLMError
    from .patterns import PatternTable

    store = Store(config.database)
    try:
        # 统一从「配置 + 环境变量」构造：预设写 config.local.yaml，密钥写 .env
        def build_client():
            return LLMClient(LLMConfig.from_settings(config.llm))

        if args.action == "presets":
            from .llm import PRESETS
            from .setup_wizard import PRESET_KEY_ENV, PRESET_MENU, PRESET_SIGNUP
            print("\n支持的 LLM 预设（在 config.local.yaml 里写 llm.preset）:\n")
            described = {name: label for name, label in PRESET_MENU}
            for name, spec in PRESETS.items():
                label = described.get(name, name)
                key_env = PRESET_KEY_ENV.get(name, "—")
                print(f"  {name:<14} {label}")
                print(f"                 base_url={spec['base_url']}")
                print(f"                 默认模型={spec['model']}   密钥变量={key_env}")
                if name in PRESET_SIGNUP:
                    print(f"                 申请：{PRESET_SIGNUP[name]}")
                print()
            print("配好之后：python -m csmon setup llm   （交互式，只问该问的）")
            print("                            python -m csmon advice probe   （验证）")
            return 0

        if args.action == "config":
            cfg = LLMConfig.from_settings(config.llm)
            print("LLM 配置（密钥不回显）：")
            for key, value in cfg.describe().items():
                print(f"  {key:<16} {value}")
            if config.local_path:
                print(f"\n本地配置：{config.local_path}")
            print("\n最快接入方式（推荐用向导，一行命令）：")
            print("  python -m csmon setup llm")
            print("\n或手工只改两处：")
            print("  config.local.yaml:  llm: {preset: deepseek}")
            print("  .env:               DEEPSEEK_API_KEY=sk-xxxx")
            print("  （本机 Ollama 不需要密钥，preset 填 ollama 即可）")
            return 0

        if args.action == "probe":
            client = build_client()
            result = client.probe()
            client.close()
            if result["ok"]:
                print(f"✓ LLM 可用（{result['latency_ms']}ms）模型回复：{result['reply']}")
                for key, value in result["config"].items():
                    print(f"    {key:<16} {value}")
            else:
                print(f"✗ {result['error']}")
                print("\n排查：")
                print("  1) 没配就走本机模型：python -m csmon setup llm 选 Ollama")
                print("  2) 云端模型检查密钥与余额：python -m csmon setup llm")
                print("  3) 看预设列表：python -m csmon advice presets")
            return 0 if result["ok"] else 1

        if args.action == "ask":
            names = ([args.name] if args.name
                     else [r.market_hash_name for r in store.list_focus()])
            if not names:
                print("没有可分析的标的。")
                print('先加关注：python -m csmon focus add "AK-47 | Redline" --intent buy --target 95')
                return 1

            from .names import NameResolver
            resolver = NameResolver(store)
            table = PatternTable.load(args.rules)
            client = build_client()
            if not client.config.is_configured():
                print("LLM 未配置。运行配置向导：python -m csmon setup llm")
                client.close()
                return 1
            print(f"使用 {client.config.provider} / {client.config.resolved_model()}")
            print(f"分析 {len(names)} 个标的…\n")
            try:
                report = batch_advice(store, names, client=client, patterns=table)
            except LLMError as exc:
                print(f"✗ {exc}")
                return 1
            finally:
                client.close()

            for row in report["results"]:
                display = resolver.resolve(row["market_hash_name"])
                print(f"── {display}")
                conf = f"{row['confidence']:.0%}" if row["confidence"] is not None else "—"
                print(f"   动作 {row['action_cn']}（把握 {conf}）")
                if row["target_buy"]:
                    print(f"   建议买入上限 ¥{row['target_buy']:.2f}")
                if row["target_sell"]:
                    print(f"   建议卖出下限 ¥{row['target_sell']:.2f}")
                if row["stop_loss"]:
                    print(f"   止损 ¥{row['stop_loss']:.2f}")
                if row["horizon_days"]:
                    print(f"   建议周期 {row['horizon_days']} 天")
                if row["reasoning"]:
                    print(f"   依据：{row['reasoning']}")
                if row["counter_evidence"]:
                    print(f"   反面证据：{row['counter_evidence']}")
                if row["risks"]:
                    print(f"   风险：{row['risks']}")
                if row["data_gaps"]:
                    print(f"   缺少数据：{'、'.join(row['data_gaps'])}")
                print()
            for err in report["errors"]:
                print(f"✗ {err['market_hash_name']}: {err['error']}")
            print("提醒：以上是模型的第二意见，不是投资建议。市场数据有限、")
            print("      饰品流动性差，请自己核对后再决定。")
            return 0

        if args.action == "ls":
            rows = store.recent_advice(limit=args.limit)
            if not rows:
                print("还没有生成过建议。")
                print("  python -m csmon advice ask")
                return 0
            from .names import NameResolver
            resolver = NameResolver(store)
            print(f"{'时间':<20} {'动作':<6} {'把握':>6} {'模型':<22} 饰品")
            for row in rows:
                conf = f"{row['confidence']:.0%}" if row["confidence"] is not None else "—"
                print(f"{(row['created_at'] or '')[:19]:<20} "
                      f"{row['action']:<6} {conf:>6} {row['model'][:22]:<22} "
                      f"{resolver.resolve(row['market_hash_name'])}")
            return 0

        if args.action == "show":
            rows = store.recent_advice(limit=5, market_hash_name=args.name)
            if not rows:
                print("没有该饰品的建议记录")
                return 1
            for row in rows:
                print(f"\n=== {(row['created_at'] or '')[:19]}  {row['model']} ===")
                print(f"动作    {row['action']}   把握 {row['confidence']}")
                print(f"目标买  {row['target_buy']}   目标卖 {row['target_sell']}")
                print(f"止损    {row['stop_loss']}   周期 {row['horizon_days']} 天")
                print(f"依据    {row['reasoning']}")
                print(f"风险    {row['risks']}")
                print(f"数据指纹 {row['context_digest']}")
            return 0

        print(f"未知操作：{args.action}", file=sys.stderr)
        return 2
    finally:
        store.close()


def cmd_variants(args: argparse.Namespace, config: Config) -> int:
    """档位词表：查看 / 采集某饰品有哪些相位、档位、渐变区间。"""
    from . import variants as variants_mod

    store = Store(config.database)
    adapter = None
    try:
        if args.action == "show":
            if not args.name:
                print("需要饰品名", file=sys.stderr)
                return 2
            vocab = variants_mod.load_vocab(store, args.name)
            from .names import NameResolver
            display = NameResolver(store).resolve(args.name)
            print(f"\n{display}")
            print(f"  {args.name}")

            if not vocab.terms:
                print("\n  尚未采集到该饰品的档位词表。")
                print("  采集方式：python -m csmon variants sync \"饰品名\"")
                print("  （需要该饰品有悠悠有品 templateId）")
                return 1

            for kind, label in (("style", "档位"), ("fade", "渐变区间"),
                                ("abrade", "磨损区间")):
                values = vocab.terms.get(kind) or []
                if not values:
                    continue
                counts = vocab.counts.get(kind, {})
                print(f"\n  {label}（{len(values)} 项）：")
                for value in values:
                    print(f"    {value:<14} 样本 {counts.get(value, 0)}")

            # 顺带展示档位级求购价
            tier_quotes = store.variant_quotes(args.name)
            if tier_quotes:
                print("\n  各档位最新求购价：")
                for row in tier_quotes:
                    bid = row.get("bid_price")
                    price = f"¥{bid:.2f}" if bid else "—"
                    print(f"    [{row['platform']:<7}] {row['variant_label']:<14} {price}")
            return 0

        if args.action == "sync":
            candidates: list[tuple[str, int]] = []
            if args.name:
                item = store.get_item(args.name)
                if not item or not item.get("youpin_template_id"):
                    print(f"本地库没有 {args.name} 的悠悠有品 templateId。")
                    print("导入映射：python -m csmon seed")
                    print("或手工指定：python -m csmon watch add \"名称\" --youpin-id 822")
                    return 1
                candidates.append((args.name, int(item["youpin_template_id"])))
            else:
                # 未指定时，自动挑「变体敏感且已有 templateId」的饰品，
                # 因为这些才是档位维度真正有价值的对象
                from .skins import parse_name
                for row in store.items_with_youpin_id():
                    mhn = row["market_hash_name"]
                    if parse_name(mhn).is_variant_rich:
                        candidates.append((mhn, int(row["youpin_template_id"])))
                if args.limit:
                    candidates = candidates[:args.limit]
                if not candidates:
                    print("本地库没有变体敏感的饰品（先跑 csmon seed 导入映射）")
                    return 1

            print(f"将从悠悠求购接口采集 {len(candidates)} 个饰品的档位词表")
            print("（每项拉 %d 页，间隔 %.1fs，避免触发风控）\n" % (args.pages, args.delay))

            from .sources.youpin_direct import YouPinDirectAdapter
            adapter = YouPinDirectAdapter(config.source("youpin_direct"))

            def _progress(index: int, total: int, name: str) -> None:
                print(f"  [{index}/{total}] {name}")

            synced = 0
            for index, (name, template_id) in enumerate(candidates, start=1):
                _progress(index, len(candidates), name[:60])
                try:
                    vocab = variants_mod.sync_youpin_vocab(
                        store, adapter, name, template_id, pages=args.pages)
                except Exception as exc:  # noqa: BLE001
                    print(f"        失败：{type(exc).__name__}: {str(exc)[:70]}")
                    continue
                if vocab and vocab.has_variants:
                    synced += 1
                    print(f"        档位 {vocab.styles or '—'}")
                    if vocab.fades:
                        print(f"        渐变 {vocab.fades}")
                else:
                    print("        无档位维度（该品类档位不决定价格）")
                if index < len(candidates):
                    time.sleep(args.delay)

            print(f"\n完成：{synced}/{len(candidates)} 个饰品采到档位词表")
            stats = store.variant_vocab_stats()
            print(f"词表库：覆盖 {stats['items_with_vocab']} 个饰品")
            for kind, info in stats["by_kind"].items():
                print(f"  {kind:<8} {info['items']} 个饰品 / {info['terms']} 个取值")
            return 0

        if args.action == "ls":
            stats = store.variant_vocab_stats()
            print(f"档位词表覆盖 {stats['items_with_vocab']} 个饰品")
            for kind, info in stats["by_kind"].items():
                print(f"  {kind:<8} {info['items']} 个饰品 / {info['terms']} 个取值")
            rows = store.latest_snapshot_all(limit=5000)
            names = sorted({r["market_hash_name"] for r in rows})
            shown = 0
            for name in names:
                vocab = variants_mod.load_vocab(store, name)
                if not vocab.has_variants:
                    continue
                print(f"  {name}")
                print(f"      档位 {vocab.styles}  渐变 {vocab.fades}")
                shown += 1
                if shown >= args.limit:
                    break
            return 0

        print(f"未知操作：{args.action}", file=sys.stderr)
        return 2
    finally:
        if adapter is not None:
            adapter.close()
        store.close()


def cmd_setup(args: argparse.Namespace, config: Config) -> int:
    """配置向导：选预设、粘密钥，其余自动写入本地私有配置。"""
    from .setup_wizard import run_setup

    targets = None
    if args.target and args.target != "auto":
        targets = [args.target]

    # 用 CLI 参数直接落配置（适合脚本化 / 无交互环境）
    if args.csqaq_token:
        from .setup_wizard import csqaq_wizard
        csqaq_wizard(config, interactive=False, token=args.csqaq_token,
                     verify=not args.no_verify)
    if args.buff_cookie:
        from .setup_wizard import buff_wizard
        buff_wizard(config, interactive=False, cookie=args.buff_cookie,
                    verify=not args.no_verify)
    if args.llm_preset or args.llm_key:
        from .setup_wizard import llm_wizard
        llm_wizard(config, interactive=False, preset=args.llm_preset,
                   api_key=args.llm_key, model=args.llm_model,
                   verify=not args.no_verify)
        return 0

    if args.csqaq_token or args.buff_cookie:
        return 0

    run_setup(config, targets=targets, verify=not args.no_verify)
    return 0


def cmd_rent(args: argparse.Namespace, config: Config) -> int:
    """租赁收益分析：日租金 + 市场波动 + 手续费 → 综合结论。"""
    import json as _json

    from . import rental as rental_mod
    from .names import NameResolver

    store = Store(config.database)
    adapter = None
    resolver = NameResolver(store)
    try:
        def build_adapter():
            from .sources.csqaq import CsqaqAdapter
            cfg = config.source("csqaq")
            if not cfg.api_token:
                print("租赁数据来自 CSQAQ（授权接口）。先配置 Token：")
                print("  python -m csmon setup csqaq")
                return None
            return CsqaqAdapter(cfg)

        if args.action == "show":
            if not args.name:
                print("需要饰品名", file=sys.stderr)
                return 2
            adapter = build_adapter()
            if adapter is None:
                return 1

            print(f"\n查询 {resolver.resolve(args.name)} …")
            good_id = adapter.find_good_id(args.name)
            if not good_id:
                print(f"CSQAQ 找不到「{args.name}」。")
                print("名称需为 Steam 官方 market_hash_name，可先用 csmon search 确认。")
                return 1

            goods = adapter.fetch_good_detail(good_id)
            if not goods:
                print("取详情失败（检查 Token / 白名单 IP / 该饰品是否被覆盖）")
                return 1

            snapshot = rental_mod.parse_detail(goods, resolver.resolve(args.name))
            store.insert_rent_snapshot(snapshot)
            _print_rent_report(snapshot, rental_mod)
            return 0

        if args.action == "scan":
            adapter = build_adapter()
            if adapter is None:
                return 1

            # 采集对象：优先关注清单，其次已监控的饰品
            names = [r.market_hash_name for r in store.list_focus()]
            if not names:
                names = [r.market_hash_name for r in store.list_watch()]
            if not names:
                print("没有可扫描的标的。先加关注：")
                print('  python -m csmon focus add "AK-47 | Redline" --intent buy')
                return 1
            if args.limit:
                names = names[:args.limit]

            print(f"将从 CSQAQ 采集 {len(names)} 个饰品的租赁数据")
            print(f"（每项一次请求，间隔 {args.delay}s，单 IP 限 1 次/秒）\n")

            ok = failed = 0
            for index, name in enumerate(names, start=1):
                display = resolver.resolve(name)
                print(f"  [{index}/{len(names)}] {display}")
                good_id = adapter.find_good_id(name)
                if not good_id:
                    print("        未找到对应 good_id，跳过")
                    failed += 1
                    continue
                goods = adapter.fetch_good_detail(good_id)
                if not goods:
                    print("        详情获取失败")
                    failed += 1
                    continue
                snapshot = rental_mod.parse_detail(goods, display)
                store.insert_rent_snapshot(snapshot)

                yields = rental_mod.analyze_all(
                    snapshot, horizons=_parse_horizons(args.horizons))
                verdict = rental_mod.judge(snapshot, yields)
                best = verdict.best
                if best:
                    print(f"        年化 {best.annualized_pct:+.1f}%"
                          f"（{rental_mod.MODE_CN[best.mode]} {best.horizon_days}天）"
                          f"  {verdict.headline}")
                else:
                    print("        无出租数据")
                ok += 1
                if index < len(names):
                    time.sleep(args.delay)

            print(f"\n完成：成功 {ok}，失败 {failed}")
            stats = store.rent_stats()
            print(f"租赁库：{stats['items_with_rent']} 个饰品有租价数据")
            return 0

        if args.action == "rank":
            rows = store.latest_rent_all(limit=2000)
            if not rows:
                print("租赁库为空。先采集：python -m csmon rent scan")
                return 0

            ranked: list[tuple[Any, Any]] = []
            for row in rows:
                snapshot = _snapshot_from_row(row)
                if snapshot is None:
                    continue
                yields = rental_mod.analyze_all(
                    snapshot, horizons=_parse_horizons(args.horizons))
                verdict = rental_mod.judge(snapshot, yields)
                ranked.append((snapshot, verdict))

            board = rental_mod.rank(ranked, limit=args.limit,
                                    min_liquidity=args.min_liquidity)
            if not board:
                print("没有满足条件的标的（可放宽 --min-liquidity）")
                return 0

            print(f"\n租赁收益排行（按最佳年化；手续费与出租率已计入）\n")
            print(f"{'年化':>8} {'风险调整':>9} {'模式':<5} {'天数':>4} "
                  f"{'日租':>7} {'出租率':>7} {'流动性':>6} 饰品")
            for row in board:
                ra = (f"{row['risk_adjusted_pct']:.2f}"
                      if row["risk_adjusted_pct"] is not None else "—")
                liq = (f"{row['liquidity_score']:.0f}"
                       if row["liquidity_score"] is not None else "—")
                print(f"{row['annualized_pct']:>7.1f}% {ra:>9} "
                      f"{row['mode_cn']:<5} {row['horizon_days']:>4} "
                      f"{row['daily_rent']:>7.2f} "
                      f"{row['occupancy']*100:>6.0f}% {liq:>6} "
                      f"{(row['display_name'] or row['market_hash_name'])[:34]}")
            print("\n说明：")
            print("  · 年化 =（净租金 + 价格变动 − 卖出成本）÷ 买入价，按持有天数年化")
            print("  · 出租率由「平台年化 ÷ 理论年化」推算，平台算法未公开")
            print("  · 风险调整 = 年化 ÷ 年化波动率，越高说明收益相对波动越划算")
            return 0

        if args.action == "ls":
            stats = store.rent_stats()
            print(f"租赁库：{stats['snapshots']} 条快照，"
                  f"{stats['items']} 个饰品，其中 {stats['items_with_rent']} 个有租价")
            rows = store.latest_rent_all(limit=args.limit)
            if not rows:
                print("（空）先采集：python -m csmon rent scan")
                return 0
            print(f"\n{'短租日租':>9} {'长租日租':>9} {'短租年化':>9} {'长租年化':>9} "
                  f"{'出租挂单':>8} 饰品")
            for row in rows:
                def _fmt(value: object, suffix: str = "") -> str:
                    if value is None:
                        return "—"
                    try:
                        return f"{float(value):.2f}{suffix}"
                    except (TypeError, ValueError):
                        return str(value)
                print(f"{_fmt(row['short_daily_rent']):>9} "
                      f"{_fmt(row['long_daily_rent']):>9} "
                      f"{_fmt(row['short_annual_pct'], '%'):>9} "
                      f"{_fmt(row['long_annual_pct'], '%'):>9} "
                      f"{row['lease_listings'] if row['lease_listings'] is not None else '—':>8} "
                      f"{resolver.resolve(row['market_hash_name'])[:34]}")
            return 0

        if args.action == "detail":
            if not args.name:
                print("需要饰品名", file=sys.stderr)
                return 2
            row = store.latest_rent_snapshot(args.name)
            if not row:
                print("本地没有该饰品的租赁数据。先执行：")
                print(f'  python -m csmon rent show "{args.name}"')
                return 1
            snapshot = _snapshot_from_row(row)
            if snapshot is None:
                print("快照解析失败")
                return 1
            _print_rent_report(snapshot, rental_mod, show_raw=_json)
            return 0

        print(f"未知操作：{args.action}", file=sys.stderr)
        return 2
    finally:
        if adapter is not None:
            adapter.close()
        store.close()


def _parse_horizons(spec: str | None) -> tuple[int, ...]:
    from .rental import DEFAULT_HORIZONS

    if not spec:
        return DEFAULT_HORIZONS
    out: list[int] = []
    for piece in spec.replace("，", ",").split(","):
        piece = piece.strip()
        if piece.isdigit() and int(piece) > 0:
            out.append(int(piece))
    return tuple(out) or DEFAULT_HORIZONS


def _snapshot_from_row(row: dict[str, Any]):
    """把库里的一行租赁快照还原成 RentSnapshot。"""
    import json as _json

    from .rental import PhaseInfo, RentSnapshot

    if not row:
        return None
    try:
        changes = {int(k): float(v)
                   for k, v in (_json.loads(row.get("price_change") or "{}")).items()}
    except (ValueError, TypeError):
        changes = {}

    phases: list[PhaseInfo] = []
    try:
        for entry in _json.loads(row.get("phases") or "[]"):
            phases.append(PhaseInfo(
                label=str(entry.get("label") or "?"),
                label_en=entry.get("label_en"),
                paint_index=entry.get("paint_index"),
                buff_sell_price=entry.get("buff_sell_price"),
                buff_buy_price=entry.get("buff_buy_price"),
            ))
    except (ValueError, TypeError):
        pass

    snapshot = RentSnapshot(
        market_hash_name=row["market_hash_name"],
        display_name=None,
        csqaq_good_id=row.get("csqaq_good_id"),
        buff_sell_price=row.get("buff_sell_price"),
        yyyp_sell_price=row.get("yyyp_sell_price"),
        steam_sell_price=row.get("steam_sell_price"),
        short_daily_rent=row.get("short_daily_rent"),
        long_daily_rent=row.get("long_daily_rent"),
        short_annual_pct=row.get("short_annual_pct"),
        long_annual_pct=row.get("long_annual_pct"),
        lease_listings=row.get("lease_listings"),
        transfer_price=row.get("transfer_price"),
        turnover_number=row.get("turnover_number"),
        turnover_avg_price=row.get("turnover_avg_price"),
        supply=row.get("supply"),
        sell_num=row.get("sell_num"),
        buy_num=row.get("buy_num"),
        price_change_pct=changes,
        phases=phases,
        source=row.get("source") or "csqaq",
    )
    # 必须赋值给变量再返回：直接 return 会让下面这行变成死代码，
    # 导致 market_price 永远还原不回去（收益率分母丢失口径）
    return _restore_market_price(snapshot, row)


def _restore_market_price(snapshot: Any, row: dict[str, Any]) -> Any:
    """兜底还原买入价。

    正常情况下分平台价格都存了、market_price 能自己算出来。这里只在
    **老库缺列**（迁移前写入的快照没有分平台价）时补一刀，
    否则那些历史快照的收益率分母会变成 None、整条分析失效。
    """
    if snapshot is None:
        return None
    has_any = any(p for p in (snapshot.buff_sell_price, snapshot.yyyp_sell_price,
                              snapshot.steam_sell_price))
    if has_any:
        return snapshot

    price = row.get("market_price")
    if price:
        try:
            snapshot.buff_sell_price = float(price)
        except (TypeError, ValueError):
            pass
    return snapshot


def _print_rent_report(snapshot: Any, rental_mod: Any, show_raw: Any = None) -> None:
    """打印单个饰品的租赁分析报告。"""
    print(f"\n{'=' * 68}")
    print(f"  {snapshot.display_name or snapshot.market_hash_name}")
    if snapshot.display_name:
        print(f"  {snapshot.market_hash_name}")
    print(f"{'=' * 68}\n")

    print("市场行情")
    print(f"  买入价（各平台最低）  ¥{snapshot.market_price:.2f}"
          if snapshot.market_price else "  买入价                无数据")
    if snapshot.buff_sell_price:
        print(f"    BUFF 在售           ¥{snapshot.buff_sell_price:.2f}")
    if snapshot.yyyp_sell_price:
        print(f"    悠悠有品 在售       ¥{snapshot.yyyp_sell_price:.2f}")
    if snapshot.steam_sell_price:
        print(f"    Steam 在售          ¥{snapshot.steam_sell_price:.2f}")
    if snapshot.supply is not None:
        print(f"  存世量                {snapshot.supply}")
    if snapshot.sell_num is not None:
        print(f"  在售量                {snapshot.sell_num}")
    if snapshot.turnover_number is not None:
        print(f"  成交量                {snapshot.turnover_number}"
              + (f"   成交均价 ¥{snapshot.turnover_avg_price:.2f}"
                 if snapshot.turnover_avg_price else ""))

    print("\n租赁行情")
    if snapshot.short_daily_rent:
        print(f"  短租日租金            ¥{snapshot.short_daily_rent:.2f}"
              + (f"   平台年化 {snapshot.short_annual_pct:.2f}%"
                 if snapshot.short_annual_pct is not None else ""))
    if snapshot.long_daily_rent:
        print(f"  长租日租金            ¥{snapshot.long_daily_rent:.2f}"
              + (f"   平台年化 {snapshot.long_annual_pct:.2f}%"
                 if snapshot.long_annual_pct is not None else ""))
    if snapshot.lease_listings is not None:
        print(f"  出租挂单数            {snapshot.lease_listings}")
    if snapshot.transfer_price:
        print(f"  转租价                ¥{snapshot.transfer_price:.2f}")
    if not (snapshot.short_daily_rent or snapshot.long_daily_rent):
        print("  （无出租挂单 —— 该饰品当前没有租赁市场）")

    if snapshot.price_change_pct:
        print("\n历史涨跌")
        for window in sorted(snapshot.price_change_pct):
            value = snapshot.price_change_pct[window]
            print(f"  {window:>4} 天  {value:>+8.2f}%")

    if snapshot.phases:
        print("\n图案档位（含各档价格）")
        print(f"  {'档位':<12} {'paint_index':>12} {'BUFF 在售':>12} {'BUFF 求购':>12}")
        for phase in snapshot.phases:
            pi = str(phase.paint_index) if phase.paint_index is not None else "—"
            sp = f"¥{phase.buff_sell_price:.0f}" if phase.buff_sell_price else "—"
            bp = f"¥{phase.buff_buy_price:.0f}" if phase.buff_buy_price else "—"
            print(f"  {phase.label:<12} {pi:>12} {sp:>12} {bp:>12}")

    yields = rental_mod.analyze_all(snapshot)
    verdict = rental_mod.judge(snapshot, yields)
    if not yields:
        print("\n结论：数据不足，无法评估\n")
        return

    print(f"\n{'=' * 68}")
    print(f"  收益分析（已计入租赁抽成、卖出抽成、提现费、推算出租率）")
    print(f"{'=' * 68}\n")
    print(f"{'模式':<5} {'天数':>5} {'日租':>7} {'出租率':>7} {'净租金':>9} "
          f"{'价格变动':>10} {'卖出成本':>9} {'总收益':>9} {'年化':>8}")
    for y in sorted(yields, key=lambda x: (x.mode, x.horizon_days)):
        print(f"{rental_mod.MODE_CN[y.mode]:<5} {y.horizon_days:>5} "
              f"{y.daily_rent:>7.2f} {y.occupancy*100:>6.0f}% "
              f"{y.net_rent:>9.2f} {y.price_move:>+10.2f} "
              f"{y.exit_cost:>9.2f} {y.total_return:>+9.2f} "
              f"{y.annualized_pct:>+7.1f}%")

    best = verdict.best
    if best:
        print(f"\n理论年化（满租、不含价格变动）  {best.theoretical_annual_pct:>+7.1f}%")
        if best.platform_annual_pct is not None:
            print(f"平台口径年化                {best.platform_annual_pct:>+7.1f}%")
        if best.implied_occupancy is not None:
            print(f"推算出租率                  {best.implied_occupancy:>7.0%}")
        if best.volatility_pct is not None:
            print(f"估计年化波动率              {best.volatility_pct:>7.1f}%")
        if best.risk_adjusted_pct is not None:
            print(f"风险调整（年化÷波动）       {best.risk_adjusted_pct:>7.2f}")
        if best.liquidity_score is not None:
            print(f"流动性评分                  {best.liquidity_score:>6.0f}/100")

    print(f"\n{'─' * 68}")
    print(f"  结论：{verdict.headline}")
    print(f"{'─' * 68}")
    for reason in verdict.reasons:
        print(f"  · {reason}")
    if verdict.caveats:
        print("\n  需要注意：")
        for caveat in verdict.caveats:
            print(f"  ! {caveat}")
    print()


def cmd_sources(args: argparse.Namespace, config: Config) -> int:
    print("支持的源：")
    for row in describe_registry():
        mark = "✓" if config.source(str(row["name"])).enabled else " "
        print(f"  [{mark}] {row['name']:<16} {', '.join(row['platforms']) or '-':<24} "
              f"在售={'Y' if row['sell'] else 'n'} 求购={'Y' if row['bid'] else 'n'}")
    print("\n启用的源（按优先级）：")
    from .sources import REGISTRY
    for s in config.enabled_sources():
        adapter_cls = REGISTRY.get(s.name)
        # 凭证状态按适配器声明的 required_credential 判断，而不是「有没有值」
        required = getattr(adapter_cls, "required_credential", None)
        env_name = getattr(adapter_cls, "credential_env", "") if adapter_cls else ""
        if required is None:
            cred = "免凭据"
        elif getattr(s, required, None):
            cred = "凭证已配置"
        else:
            cred = f"⚠ 缺少 {env_name}（该源当前不可用）"
        print(f"  {s.priority:>4}  {s.name:<16} 间隔 {s.min_interval:>5}s  "
              f"批量 {s.batch_size:<4} {cred}")
    return 0


def cmd_probe(args: argparse.Namespace, config: Config) -> int:
    """不带任何凭证地自检每个源的可达性。"""
    from .models import ItemRef
    from .sources import build_sources

    sample_name = args.name
    print(f"自检目标饰品：{sample_name}\n")
    ref = ItemRef(market_hash_name=sample_name,
                  buff_goods_id=args.buff_id,
                  youpin_template_id=args.youpin_id)

    for adapter in build_sources(config):
        try:
            adapter.preflight()
        except Exception as exc:  # noqa: BLE001
            print(f"  [跳过] {adapter.name:<16} {exc}")
            continue
        quotes, report = adapter.fetch_report([ref])
        if report.errors:
            print(f"  [失败] {adapter.name:<16} {report.errors[0][:100]}")
        elif quotes:
            for q in quotes:
                print(f"  [OK  ] {adapter.name:<16} {q.platform:<7} "
                      f"在售 {_f(q.sell_price):>9} 在售量 {_s(q.sell_count):>6} "
                      f"求购 {_f(q.bid_price):>9}")
        else:
            print(f"  [空  ] {adapter.name:<16} 无可采信数据"
                  f"（可能缺平台 ID 或该平台无挂单）")
        adapter.close()
    return 0


# ── 格式化助手 ─────────────────────────────────────────────

def _f(v: object) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_pct(v: object) -> str:
    """把比率格式化成百分比；None 显示为 —（区别于 0.00%）。"""
    if v is None:
        return "—"
    try:
        return f"{float(v):+.2%}"
    except (TypeError, ValueError):
        return str(v)


def _s(v: object) -> str:
    return "—" if v is None else str(v)


# ── 参数解析 ───────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csmon",
        description="CS 饰品多源行情监控（BUFF / 悠悠有品）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"csmon {__version__}")
    parser.add_argument("-c", "--config", default=None, help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="跑一轮采集")
    p_run.add_argument("--loop", action="store_true", help="常驻循环")
    p_run.add_argument("--interval", type=int, default=None, help="循环间隔秒（默认取配置）")
    p_run.add_argument("--source", action="append", default=None,
                       help="只跑指定源（可重复，如 --source csqaq）")
    p_run.set_defaults(func=cmd_run)

    p_serve = sub.add_parser("serve", help="启动 Web 看板")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=cmd_serve)

    p_watch = sub.add_parser("watch", help="管理监控清单")
    p_watch.add_argument("action", choices=["add", "rm", "ls"])
    p_watch.add_argument("name", nargs="?", help="Steam 官方饰品名")
    p_watch.add_argument("--below", type=float, help="在售价跌破该值告警")
    p_watch.add_argument("--above", type=float, help="在售价涨破该值告警")
    p_watch.add_argument("--drop", type=float, help="相对基准下跌百分比告警")
    p_watch.add_argument("--rise", type=float, help="相对基准上涨百分比告警")
    p_watch.add_argument("--cooldown", type=int, default=240, help="告警冷却分钟")
    p_watch.add_argument("--platform", action="append", default=None,
                         help="限定平台（BUFF / YOUPIN，可重复）")
    p_watch.add_argument("--buff-id", type=int, default=None, help="直接指定 BUFF goods_id")
    p_watch.add_argument("--youpin-id", type=int, default=None,
                         help="直接指定悠悠有品 templateId")
    p_watch.set_defaults(func=cmd_watch)

    p_index = sub.add_parser("index", help="BUFF goods_id 索引")
    p_index.add_argument("action", choices=["scan", "resolve", "status"])
    p_index.add_argument("--start", type=int, default=1)
    p_index.add_argument("--end", type=int, default=60000)
    p_index.add_argument("--step", type=int, default=1)
    p_index.add_argument("--no-resume", action="store_true", help="忽略已存游标，从头扫")
    p_index.add_argument("--max-seconds", type=float, default=None,
                         help="本轮时间预算，到点保存游标退出")
    p_index.set_defaults(func=cmd_index)

    p_seed = sub.add_parser("seed", help="导入参考项目的映射资源")
    p_seed.add_argument("extra", nargs="*", help="额外的映射文件（.json / .json.gz）")
    p_seed.set_defaults(func=cmd_seed)

    p_report = sub.add_parser("report", help="终端打印行情与告警")
    p_report.add_argument("--limit", type=int, default=80)
    p_report.add_argument("--alerts", type=int, default=20)
    p_report.set_defaults(func=cmd_report)

    p_src = sub.add_parser("sources", help="列出源与配置")
    p_src.set_defaults(func=cmd_sources)

    p_probe = sub.add_parser("probe", help="无凭证自检各源可达性")
    p_probe.add_argument("--name", default="AK-47 | Redline (Field-Tested)")
    p_probe.add_argument("--buff-id", type=int, default=None)
    p_probe.add_argument("--youpin-id", type=int, default=None)
    p_probe.set_defaults(func=cmd_probe)

    # ── 分析 ───────────────────────────────────────────────

    p_kline = sub.add_parser("kline", help="打印日线与技术指标")
    p_kline.add_argument("name", help="饰品名（Steam 官方 market_hash_name）")
    p_kline.add_argument("--platform", default="BUFF", help="BUFF / YOUPIN（默认 BUFF）")
    p_kline.add_argument("--days", type=int, default=90, help="取多少天的日线")
    p_kline.add_argument("--tail", type=int, default=30, help="打印最近多少根")
    p_kline.set_defaults(func=cmd_kline)

    p_spread = sub.add_parser("spread", help="跨平台价差雷达")
    p_spread.add_argument("--min-percent", type=float, default=0.03,
                          help="最低净收益率（默认 0.03 = 3%%）")
    p_spread.add_argument("--min-profit", type=float, default=1.0,
                          help="最低净收益金额（默认 1 元）")
    p_spread.add_argument("--limit", type=int, default=30)
    p_spread.set_defaults(func=cmd_spread)

    p_movers = sub.add_parser("movers", help="涨跌排行 / 流动性榜")
    p_movers.add_argument("--hours", type=int, default=168, help="对比窗口（小时）")
    p_movers.add_argument("--limit", type=int, default=15)
    p_movers.add_argument("--liquidity", action="store_true", help="附在售量榜")
    p_movers.set_defaults(func=cmd_movers)

    # ── 极致追踪 ───────────────────────────────────────────

    p_ext = sub.add_parser("extreme", help="极致追踪（单件高频轮询）")
    p_ext.add_argument("action", choices=["add", "rm", "ls", "run"])
    p_ext.add_argument("name", nargs="?", help="饰品名")
    p_ext.add_argument("--platform", default="BUFF", help="BUFF / YOUPIN")
    p_ext.add_argument("--interval", type=int, default=30, help="轮询间隔秒（默认 30）")
    p_ext.add_argument("--price-mode", choices=["any", "percent"], default="percent",
                       help="价格变动判定：any=任何变化，percent=超过百分比")
    p_ext.add_argument("--price-threshold", type=float, default=1.0,
                       help="价格变动阈值（percent 模式，单位 %%）")
    p_ext.add_argument("--quantity-mode", choices=["any", "percent"], default="percent",
                       help="在售量变动判定")
    p_ext.add_argument("--quantity-threshold", type=float, default=10.0,
                       help="在售量变动阈值（percent 模式，单位 %% ）")
    p_ext.add_argument("--cooldown", type=int, default=300, help="同任务告警冷却秒")
    p_ext.add_argument("--quiet-start", type=int, default=None, help="静默开始小时")
    p_ext.add_argument("--quiet-end", type=int, default=None, help="静默结束小时")
    p_ext.set_defaults(func=cmd_extreme)

    # ── 归档 ───────────────────────────────────────────────

    p_arch = sub.add_parser("archive", help="数据归档与日线回填")
    p_arch.add_argument("action", choices=["run", "stats", "ohlc", "prune-extreme"])
    p_arch.add_argument("--keep-days", type=int, default=90, help="明细保留天数")
    p_arch.add_argument("--days", type=int, default=365, help="OHLC 回填天数")
    p_arch.set_defaults(func=cmd_archive)

    # ── 其它 ───────────────────────────────────────────────

    p_search = sub.add_parser("search", help="搜索本地饰品库")
    p_search.add_argument("keyword")
    p_search.add_argument("--limit", type=int, default=30)
    p_search.set_defaults(func=cmd_search)

    p_doc = sub.add_parser("doctor", help="环境自检")
    p_doc.add_argument("--no-network", action="store_true", help="跳过网络连通性检查")
    p_doc.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_doc.set_defaults(func=cmd_doctor)

    # ── 中文名 ─────────────────────────────────────────────

    p_names = sub.add_parser("names", help="中文名（学习进度 / 手工设置 / 查看）")
    p_names.add_argument("action", choices=["stats", "show", "set", "learn"])
    p_names.add_argument("name", nargs="?", help="市场名（show/set 用）")
    p_names.add_argument("cn", nargs="?", help="中文名（set 用）")
    p_names.set_defaults(func=cmd_names)

    # ── 关注清单（带买卖意图）──────────────────────────────

    p_focus = sub.add_parser("focus", help="关注清单：我真正要买/要卖的")
    p_focus.add_argument("action", choices=["ls", "add", "rm", "detail"])
    p_focus.add_argument("name", nargs="?", help="饰品名或基础名")
    p_focus.add_argument("--intent", choices=["buy", "sell", "watch"], default="watch",
                         help="交易意图（默认 watch）")
    p_focus.add_argument("--target", type=float, default=None,
                         help="目标价：买入指「≤此价」；卖出指「≥此价」")
    p_focus.add_argument("--budget", type=float, default=None, help="预算上限（单件）")
    p_focus.add_argument("--quantity", type=int, default=1, help="计划数量")
    p_focus.add_argument("--priority", type=int, default=3, choices=[1, 2, 3, 4],
                         help="优先级 1 最高（默认 3）")
    p_focus.add_argument("--wears", default=None,
                         help="可接受磨损，逗号分隔：FN,MW,FT,WW,BS 或 崭新,略磨,久经")
    p_focus.add_argument("--qualities", default=None,
                         help="可接受品质：普通,暗金,纪念品")
    p_focus.add_argument("--note", default="", help="备注")
    p_focus.add_argument("--expand", action="store_true",
                         help="rm 时按基础名展开成所有变体一起移除")
    p_focus.set_defaults(func=cmd_focus)

    # ── 图案档位 ───────────────────────────────────────────

    p_pat = sub.add_parser("patterns", help="图案档位（多普勒相位/渐变/淬火/特殊模板）")
    p_pat.add_argument("action", choices=["init", "show", "learn", "stats"])
    p_pat.add_argument("name", nargs="?", help="饰品名（精确匹配本地库）")
    p_pat.add_argument("--rules", default="patterns.yaml", help="规则文件路径")
    p_pat.add_argument("--pages", type=int, default=2, help="学习时抓取的挂单页数")
    p_pat.add_argument("--force", action="store_true", help="init 时覆盖已有文件")
    p_pat.set_defaults(func=cmd_patterns)

    # ── LLM 建议 ───────────────────────────────────────────

    p_adv = sub.add_parser("advice", help="LLM 交易建议（第二意见，非投资建议）")
    p_adv.add_argument("action", choices=["ask", "ls", "show", "config", "probe", "presets"])
    p_adv.add_argument("name", nargs="?", help="饰品名（不填则用关注清单）")
    p_adv.add_argument("--rules", default="patterns.yaml", help="图案规则文件")
    p_adv.add_argument("--limit", type=int, default=30, help="ls 展示条数")
    p_adv.set_defaults(func=cmd_advice)

    # ── 档位词表 ───────────────────────────────────────────

    p_var = sub.add_parser("variants", help="档位词表（相位/宝石/渐变/Tier）")
    p_var.add_argument("action", choices=["show", "sync", "ls"])
    p_var.add_argument("name", nargs="?", help="饰品名（show/sync 用）")
    p_var.add_argument("--pages", type=int, default=3, help="每项抓取的求购页数")
    p_var.add_argument("--delay", type=float, default=1.3, help="请求间隔秒")
    p_var.add_argument("--limit", type=int, default=40, help="批量同步上限")
    p_var.set_defaults(func=cmd_variants)

    # ── 配置向导 ───────────────────────────────────────────

    p_setup = sub.add_parser("setup", help="配置向导（LLM / BUFF / CSQAQ）")
    p_setup.add_argument("target", nargs="?",
                         choices=["auto", "all", "llm", "buff", "csqaq"],
                         default="auto", help="只配置指定项（默认自动探测缺什么）")
    p_setup.add_argument("--csqaq-token", default=None, help="直接提供 CSQAQ Token")
    p_setup.add_argument("--buff-cookie", default=None, help="直接提供 BUFF Cookie")
    p_setup.add_argument("--llm-preset", default=None,
                         help="LLM 预设名（deepseek/openai/ollama/...）")
    p_setup.add_argument("--llm-key", default=None, help="LLM API Key")
    p_setup.add_argument("--llm-model", default=None, help="覆盖模型名")
    p_setup.add_argument("--no-verify", action="store_true",
                         help="跳过写入后的连通性验证")
    p_setup.set_defaults(func=cmd_setup)

    # ── 租赁收益 ───────────────────────────────────────────

    p_rent = sub.add_parser("rent", help="租赁收益分析（日租金 + 波动 + 手续费）")
    p_rent.add_argument("action", choices=["show", "scan", "rank", "ls", "detail"])
    p_rent.add_argument("name", nargs="?", help="饰品名（show 用）")
    p_rent.add_argument("--horizons", default=None,
                        help="持有周期（天），逗号分隔，默认 30,90,180")
    p_rent.add_argument("--limit", type=int, default=30, help="scan/rank 条数上限")
    p_rent.add_argument("--delay", type=float, default=1.1, help="采集间隔秒")
    p_rent.add_argument("--min-liquidity", type=float, default=0.0,
                        help="rank 时过滤掉流动性低于此值的标的")
    p_rent.set_defaults(func=cmd_rent)

    return parser


def main(argv: list[str] | None = None) -> int:
    # 控制台适配必须最先执行：Windows 中文版控制台默认 GBK，
    # 本工具大量输出中文与 ✓ ─ ★ 这类符号，不先切到 UTF-8 会在打印时崩掉。
    from .console import setup_console
    setup_console()

    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    _setup_logging(args.log_level or config.log_level)
    return int(args.func(args, config) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
