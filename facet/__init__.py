"""facet — CS 饰品市场情报。

名字取「切面」之意：同一个皮肤名之下有多个决定价格的切面 ——
磨损、暗金/纪念品、相位与档位、图案模板，以及「出租收租」这条
低买高卖之外的收益路径。这个工具做的就是把这些切面拆开、
分别定价、并给出可比较的结论。

设计目标：把「多平台行情采集」与「告警/看板/分析」解耦，
数据源以适配器注册，任何一个源失效都不影响其它源与历史数据。
"""

__version__ = "0.2.0"

__all__ = ["__version__"]


def _bootstrap_env() -> int:
    """导入即生效的环境变量兼容迁移。

    放在包入口而不是各调用点：`facet.llm`、`facet.platform` 等模块
    都可能被单独导入（测试、脚本、第三方调用），逐个加调用容易漏。
    """
    try:
        from .config import _migrate_legacy_env

        return _migrate_legacy_env()
    except Exception:  # noqa: BLE001 — 迁移失败不能阻止包被导入
        return 0


_bootstrap_env()
