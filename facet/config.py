"""配置：YAML 主配置 + 环境变量覆盖（密钥只走环境变量/密钥文件）。

优先级：环境变量 > YAML > 代码默认值。
密钥永不写入 YAML，避免误提交。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import WatchRule

DEFAULT_CONFIG_PATH = "config.yaml"

#: 环境变量前缀。历史版本用过 CSMON_，_migrate_legacy_env() 会自动搬过来。
ENV_PREFIX = "FACET_"
LEGACY_ENV_PREFIX = "CSMON_"


def _migrate_legacy_env() -> int:
    """把旧的 CSMON_* 环境变量搬到 FACET_*。

    改名不能顺带把别人已配置好的环境弄失效 —— 用户可能已经把 Token 写进
    系统环境变量或 CI secret，改个前缀就要求重配是不合理的。
    只在 FACET_* 未设置时搬运，新名字优先。
    """
    moved = 0
    for key, value in list(os.environ.items()):
        if not key.startswith(LEGACY_ENV_PREFIX):
            continue
        new_key = ENV_PREFIX + key[len(LEGACY_ENV_PREFIX):]
        if new_key not in os.environ:
            os.environ[new_key] = value
            moved += 1
    return moved

# 支持的源名称
SOURCE_CSQAQ = "csqaq"
SOURCE_STEAMDT = "steamdt"
SOURCE_BUFF = "buff"                 # BUFF 数据源（规范名）
SOURCE_BUFF_DIRECT = "buff_direct"   # 旧名，保留兼容
SOURCE_YOUPIN_DIRECT = "youpin_direct"
SOURCE_MOCK = "mock"

ALL_SOURCES = (SOURCE_CSQAQ, SOURCE_STEAMDT, SOURCE_BUFF,
               SOURCE_BUFF_DIRECT, SOURCE_YOUPIN_DIRECT, SOURCE_MOCK)

#: 本地私有配置文件（不入库）。所有个人化内容放这里：
#: 关注清单、LLM 预设与模型选择、通知渠道、端口等。
LOCAL_CONFIG_NAME = "config.local.yaml"


@dataclass
class SourceConfig:
    """单个源的行为配置。"""

    name: str
    enabled: bool = True
    priority: int = 100          # 数字越小越优先；同平台冲突时低优先级源补齐
    min_interval: float = 1.0    # 端点最小间隔（秒）
    batch_size: int = 50         # 单请求最多携带多少个饰品
    timeout: float = 20.0
    # 凭证（从环境变量注入，不落 YAML）
    api_token: str | None = None
    api_key: str | None = None
    cookie: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # 该源覆盖的平台（用于决定是否采信其价格）
    platforms: tuple[str, ...] = ()


@dataclass
class NotifyConfig:
    """通知渠道。未启用的渠道不会被打扰。"""

    enabled: bool = False
    channels: list[dict[str, Any]] = field(default_factory=list)
    min_severity: str = "warning"   # info | warning | critical
    quiet_hours: tuple[int, int] | None = None   # (start_hour, end_hour) 静默时段
    dry_run: bool = False


@dataclass
class LLMSettings:
    """LLM 接入设置（**非密钥部分**，放 config.local.yaml）。

    密钥本身走 .env 的 FACET_LLM_API_KEY，不进这里 —— 这样个人配置文件
    即使被误传到别处，也不会泄露凭据。
    """

    preset: str = ""            # 预设名：deepseek / openai / ollama ...
    provider: str = ""          # 留空则由预设决定
    model: str = ""             # 留空用预设默认
    base_url: str = ""          # 留空用预设默认
    temperature: float = 0.2
    max_tokens: int = 2000
    enabled: bool = True

    def describe(self) -> dict[str, Any]:
        return {"preset": self.preset or "(未设置)", "provider": self.provider or "(自动)",
                "model": self.model or "(预设默认)", "enabled": self.enabled}


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    # 看板默认只绑回环；如需外网访问自行改 host 并配置反代


@dataclass
class Config:
    database: str = "data/facet.db"
    poll_interval: int = 1800          # 普通监控轮询间隔（秒）
    baseline_window_hours: int = 168   # 波动基准窗口（7 天）
    sources: dict[str, SourceConfig] = field(default_factory=dict)
    watchlist: list[WatchRule] = field(default_factory=list)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    web: WebConfig = field(default_factory=WebConfig)
    llm: LLMSettings = field(default_factory=LLMSettings)
    log_level: str = "INFO"
    config_path: Path | None = None
    local_path: Path | None = None     # 实际加载到的本地私有配置（若有）

    def enabled_sources(self) -> list[SourceConfig]:
        return sorted(
            (s for s in self.sources.values() if s.enabled),
            key=lambda s: s.priority,
        )

    def source(self, name: str) -> SourceConfig:
        return self.sources.get(name) or SourceConfig(name=name, enabled=False)


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v.strip()
    return None


def load_dotenv(path: str | Path = ".env", override: bool = False) -> int:
    """读取 .env 到环境变量（零依赖，避免为这点功能引入 python-dotenv）。

    支持：KEY=VALUE、# 注释、引号包裹、行内注释、export 前缀。
    已存在的环境变量默认不覆盖（真实环境变量优先于文件）。
    返回成功导入的变量个数。
    """
    env_path = Path(path)
    if not env_path.exists():
        return 0

    loaded = 0
    try:
        content = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0

    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        # 去掉成对引号；未加引号时剥掉 " #" 之后的行内注释
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()

        if not override and os.environ.get(key):
            continue
        os.environ[key] = value
        loaded += 1
    return loaded


def _apply_env_secrets(sources: dict[str, SourceConfig]) -> None:
    """凭证只从环境变量 / .env 读取，绝不进 YAML。

    BUFF_COOKIE 会同时作用于 `buff` 与旧名 `buff_direct`，
    这样无论配置里写的是哪个名字都能生效。
    """
    if SOURCE_CSQAQ in sources:
        sources[SOURCE_CSQAQ].api_token = _env("CSQAQ_TOKEN", "CSQAQ_API_TOKEN")
    if SOURCE_STEAMDT in sources:
        sources[SOURCE_STEAMDT].api_key = _env("STEAMDT_API_KEY", "STEAMDT_KEY")
    buff_cookie = _env("BUFF_COOKIE", "BUFF_SESSION")
    for name in (SOURCE_BUFF, SOURCE_BUFF_DIRECT):
        if name in sources:
            sources[name].cookie = buff_cookie
    if SOURCE_YOUPIN_DIRECT in sources:
        sources[SOURCE_YOUPIN_DIRECT].api_token = _env("YOUPIN_TOKEN")
        sources[SOURCE_YOUPIN_DIRECT].cookie = _env("YOUPIN_DEVICE_UK")


def default_sources() -> dict[str, SourceConfig]:
    """默认源拓扑：优先官方授权源，直连源作为补充与兜底。

    默认只启用 csqaq：
      - csqaq 一次调用同时给出 BUFF 与悠悠有品的在售价/在售量，覆盖两个目标平台
      - buff_direct / youpin_direct 是免登录直连，但都是「机会型」通道：
        BUFF 的匿名读会被 IP 级风控整体关闭（实测），
        悠悠有品只开放求购价、在售价通道对匿名请求返回风控话术。
        因此默认关闭，需要时在 config.yaml 里显式打开。
    详见 docs/DATA_SOURCES.md。
    """
    from .models import PLATFORM_BUFF, PLATFORM_STEAM, PLATFORM_YOUPIN

    return {
        SOURCE_CSQAQ: SourceConfig(
            name=SOURCE_CSQAQ, enabled=True, priority=10, min_interval=1.05,
            batch_size=50, platforms=(PLATFORM_BUFF, PLATFORM_YOUPIN, PLATFORM_STEAM),
        ),
        SOURCE_BUFF: SourceConfig(
            name=SOURCE_BUFF, enabled=False, priority=20, min_interval=5.0,
            batch_size=1, platforms=(PLATFORM_BUFF,),
        ),
        SOURCE_YOUPIN_DIRECT: SourceConfig(
            name=SOURCE_YOUPIN_DIRECT, enabled=True, priority=30, min_interval=1.5,
            batch_size=1, platforms=(PLATFORM_YOUPIN,),
        ),
        SOURCE_STEAMDT: SourceConfig(
            name=SOURCE_STEAMDT, enabled=False, priority=40, min_interval=60.5,
            batch_size=100, platforms=(PLATFORM_BUFF, PLATFORM_YOUPIN, PLATFORM_STEAM),
        ),
        SOURCE_MOCK: SourceConfig(
            name=SOURCE_MOCK, enabled=False, priority=999, min_interval=0.0,
            batch_size=1000,
        ),
    }


def load_config(path: str | Path | None = None) -> Config:
    """读取配置；文件不存在时使用全默认值（保证开箱可跑）。

    会先自动载入同目录的 .env（若存在），这样「把凭证写进 .env」就能直接生效，
    不必手动 export 环境变量。
    """
    load_dotenv(Path(path).parent / ".env" if path else ".env")
    _migrate_legacy_env()
    cfg_path = Path(path or os.environ.get(f"{ENV_PREFIX}CONFIG") or DEFAULT_CONFIG_PATH)
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    # 叠加本地私有配置：个人化的东西（关注清单、LLM 预设、通知渠道、端口）
    # 都放 config.local.yaml，它不入库，所以每次 git pull 都不会冲突。
    local_path = local_config_path(cfg_path)
    local_raw: dict[str, Any] = {}
    env_local = os.environ.get("FACET_LOCAL_CONFIG")
    if env_local:
        local_path = Path(env_local)
    if local_path.exists():
        try:
            local_raw = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
            if not isinstance(local_raw, dict):
                local_raw = {}
            else:
                raw = _deep_merge(raw, local_raw)
        except (OSError, yaml.YAMLError):
            local_raw = {}

    sources = default_sources()
    for name, scfg in (raw.get("sources") or {}).items():
        base = sources.get(name) or SourceConfig(name=name)
        if isinstance(scfg, dict):
            for k, v in scfg.items():
                if hasattr(base, k):
                    if k == "platforms":
                        v = tuple(v)
                    setattr(base, k, v)
                else:
                    base.extra[k] = v
        sources[name] = base
    _apply_env_secrets(sources)

    watchlist: list[WatchRule] = []
    for entry in (raw.get("watchlist") or []):
        if isinstance(entry, str):
            watchlist.append(WatchRule(market_hash_name=entry))
        elif isinstance(entry, dict):
            item = entry.get("market_hash_name") or entry.get("name")
            if not item:
                continue
            payload = {k: v for k, v in entry.items()
                       if k not in ("market_hash_name", "name")}
            watchlist.append(WatchRule.from_dict(item, payload))

    notify_raw = raw.get("notify") or {}
    notify = NotifyConfig(
        enabled=bool(notify_raw.get("enabled", False)),
        channels=list(notify_raw.get("channels") or []),
        min_severity=notify_raw.get("min_severity", "warning"),
        dry_run=bool(notify_raw.get("dry_run", False)),
    )
    qh = notify_raw.get("quiet_hours")
    if isinstance(qh, (list, tuple)) and len(qh) == 2:
        notify.quiet_hours = (int(qh[0]), int(qh[1]))

    web_raw = raw.get("web") or {}

    llm_raw = raw.get("llm") or {}

    cfg = Config(
        database=raw.get("database") or os.environ.get("FACET_DB") or "data/facet.db",
        poll_interval=int(raw.get("poll_interval") or 1800),
        baseline_window_hours=int(raw.get("baseline_window_hours") or 168),
        sources=sources,
        watchlist=watchlist,
        notify=notify,
        web=WebConfig(
            host=web_raw.get("host", "127.0.0.1"),
            port=int(web_raw.get("port", 8787)),
        ),
        llm=LLMSettings(
            preset=str(llm_raw.get("preset") or "").strip().lower(),
            provider=str(llm_raw.get("provider") or "").strip().lower(),
            model=str(llm_raw.get("model") or "").strip(),
            base_url=str(llm_raw.get("base_url") or "").strip(),
            temperature=float(llm_raw.get("temperature", 0.2)),
            max_tokens=int(llm_raw.get("max_tokens", 2000)),
            enabled=bool(llm_raw.get("enabled", True)),
        ),
        log_level=raw.get("log_level", "INFO"),
        config_path=cfg_path,
        local_path=local_path if local_raw else None,
    )

    # 环境变量覆盖轮询间隔，便于临时加速调试
    if os.environ.get("FACET_POLL_INTERVAL"):
        cfg.poll_interval = int(os.environ["FACET_POLL_INTERVAL"])
    return cfg


# ── 本地私有配置覆盖 ───────────────────────────────────────

def local_config_path(base_path: Path | None = None) -> Path:
    """返回本地私有配置路径（与主配置同目录）。"""
    base = base_path or Path(DEFAULT_CONFIG_PATH)
    return base.parent / LOCAL_CONFIG_NAME


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """递归合并两份配置，overlay 优先。

    - 字典：逐键递归合并（这样本地配置只写 `sources.buff.enabled: true`
      就能改主配置里的一整段，不必整段抄一遍）
    - 列表：**整体替换**（关注清单、通知渠道这类，追加语义容易产生意外的重复项）
    - 标量：覆盖
    """
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def write_local_config(updates: dict[str, Any], path: str | Path | None = None) -> Path:
    """把更新合并进本地私有配置并写回。

    只写 config.local.yaml（.gitignore 已排除），绝不改主配置 ——
    主配置是要提交进仓库的模板，本地调整不该污染它。
    """
    target = Path(path) if path else local_config_path()
    existing: dict[str, Any] = {}
    if target.exists():
        try:
            existing = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            existing = {}
    merged = _deep_merge(existing, updates)

    header = (
        "# facet 本地私有配置（不入库）\n"
        "#\n"
        "# 这个文件放你个人的东西：关注清单、LLM 预设选择、通知渠道、端口等。\n"
        "# 密钥不写这里 —— 密钥走 .env（同样不入库）。\n"
        "#\n"
        "# 它覆盖 config.yaml 里的同名项，且只需写要改的部分。\n"
        "# 例如只改 BUFF 源：\n"
        "#   sources:\n"
        "#     buff:\n"
        "#       enabled: true\n"
        "\n"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        header + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return target


LOCAL_CONFIG_EXAMPLE = """\
# 本地私有配置模板 —— 复制为 config.local.yaml 后按需修改
#
# config.local.yaml 已被 .gitignore 排除，不会上传。
# 只写你要覆盖的项即可，其余自动继承 config.yaml。

# 数据源开关与参数（完整说明见 config.yaml）
sources:
  buff:
    enabled: true
    min_interval: 5.0
    fetch_bid: true        # 同时取求购价（多一次请求）

# 我真正要买/要卖的（用 CLI 添加更省事：facet focus add）
# watchlist:
#   - market_hash_name: "AK-47 | Redline (Field-Tested)"
#     below: 95
#     drop_percent: 8

# LLM：只需选预设，模型与端点自动带出
llm:
  preset: deepseek         # openai / deepseek / moonshot / dashscope / zhipu /
                           # siliconflow / ollama / lmstudio / vllm
  # model: deepseek-chat   # 留空用预设默认
  temperature: 0.2

# 通知（先用 dry_run 验证规则再开）
notify:
  enabled: false
  dry_run: true
  quiet_hours: [23, 8]

# 看板端口
web:
  port: 8787
"""


ENV_TEMPLATE = """# facet 凭证（复制为 .env 并填入；不要提交到版本库）

# CSQAQ 数据开放 API：注册即送 Token，需在官网绑定本机白名单 IP
# 一次调用即返回 BUFF + 悠悠有品 + Steam 的在售价与在售量
CSQAQ_TOKEN=

# SteamDT 开放平台 API Key（个人中心-API管理，限时免费）
# 批量查价 1 次/分钟、单件 60 次/分钟
STEAMDT_API_KEY=

# 悠悠有品：匿名通道只开放求购价；如需在售/租赁行情，把已登录态的设备标识放这里
# （浏览器/抓包获得，注意其用户协议限制，见 docs/DATA_SOURCES.md）
YOUPIN_DEVICE_UK=

# BUFF：仅当需要使用带登录态的接口时填写 Cookie；匿名通道无需配置
BUFF_COOKIE=

# 可选：覆盖配置
# FACET_DB=data/facet.db
# FACET_POLL_INTERVAL=1800
# FACET_CONFIG=config.yaml
"""
