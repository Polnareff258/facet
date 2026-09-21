"""租赁收益分析：把「日租金 / 市场波动 / 手续费 / 流动性」合成一个可比较的结论。

**为什么租赁要看的不只是日租金**

日租金高不代表收益高。一件饰品出租的实际回报由四项共同决定：

    实际回报 = 净租金收入 + 持有期价格变动 − 卖出成本 − 空置损失

其中任何一项都可能吃掉另外几项：
  · 日租金 5 元 / 价 2000 元，看着是 91% 年化；但若该品类全年只有 30% 时间租得出去，
    实际就是 27%；若同期价格跌了 40%，你是净亏的。
  · 长租日租金通常**低于**短租，但空置率也低得多 —— 实测 CSQAQ 数据里
    长租日租金 3.55 < 短租 4.14，可长租年化 14.05% > 短租 11.92%。
    只看日租金会选错。

**口径说明（重要，别把两个数搞混）**

本模块同时给出两个年化率：

  · `理论年化` = 日租金 × 365 ÷ 买入价 —— 这是**满租**假设下的上限，任何平台都达不到
  · `平台年化` = 数据源（CSQAQ）直接给出的 `lease_annual` —— 平台口径，
    推测已折算了空置与租期结构，但**其确切算法未公开**

两者的比值被本模块用作「隐含出租率」的估计。这是**推算值**，不是实测值 ——
代码里对应的字段带 `implied_` 前缀，展示时也会注明，避免被当成事实使用。
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from .models import PLATFORM_BUFF, PLATFORM_YOUPIN, utcnow

logger = logging.getLogger(__name__)

# ── 费率 ───────────────────────────────────────────────────
#
# 租赁收益对手续费极其敏感：日租金常常只有价格的 0.05–0.2%，
# 而一次卖出的手续费就是 2.5% —— 相当于吃掉十几天的租金。
# 所以费率必须按自己的实际档位覆盖，默认值只是常见档位。
DEFAULT_SELL_FEE = {          # 卖出时平台抽成
    PLATFORM_BUFF: 0.025,
    PLATFORM_YOUPIN: 0.020,
    "STEAM": 0.15,
}
DEFAULT_WITHDRAW_FEE = 0.01   # 提现费

#: 租赁平台对租金收入抽成（各平台规则不同，默认按常见档位）
DEFAULT_RENT_FEE = {PLATFORM_YOUPIN: 0.10, PLATFORM_BUFF: 0.10}

#: 平台未提供年化率时的保守出租率假设。
#:
#: 为什么不用 1.0（满租）：空置是租赁的常态而非例外 —— 一件饰品上架后要等租客、
#: 租期之间有空档、热门档期与淡季差异很大。按满租估算会系统性高估收益，
#: 让本该「勉强」的标的看起来「值得考虑」。宁可保守，也不能乐观。
OCCUPANCY_FALLBACK = 0.6


# ── 数据模型 ───────────────────────────────────────────────

@dataclass(slots=True)
class PhaseInfo:
    """一个图案相位/档位及其价格（来自 CSQAQ 的 dpl 数组）。"""

    label: str                  # 红宝石 / Phase2 / 黑珍珠 …
    label_en: str | None
    paint_index: int | None
    buff_sell_price: float | None
    buff_buy_price: float | None

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "label_en": self.label_en,
                "paint_index": self.paint_index,
                "buff_sell_price": self.buff_sell_price,
                "buff_buy_price": self.buff_buy_price}


@dataclass(slots=True)
class RentSnapshot:
    """一次租赁行情快照（来自 CSQAQ 单件详情）。"""

    market_hash_name: str
    display_name: str | None = None
    csqaq_good_id: int | None = None

    # ── 市场价 ──
    buff_sell_price: float | None = None
    yyyp_sell_price: float | None = None
    steam_sell_price: float | None = None
    buff_buy_price: float | None = None
    yyyp_buy_price: float | None = None

    # ── 租赁 ──
    short_daily_rent: float | None = None      # 短租日租金
    long_daily_rent: float | None = None       # 长租日租金
    short_annual_pct: float | None = None      # 平台口径短租年化（%）
    long_annual_pct: float | None = None       # 平台口径长租年化（%）
    lease_listings: int | None = None          # 出租挂单数（竞争程度）
    transfer_price: float | None = None        # 转租价

    # ── 市场健康度 ──
    turnover_number: int | None = None         # 成交量
    turnover_avg_price: float | None = None    # 成交均价
    supply: int | None = None                  # 存世量
    sell_num: int | None = None                # 在售量
    buy_num: int | None = None                 # 求购量
    min_float: float | None = None
    max_float: float | None = None

    #: 各周期涨跌（%），键为天数
    price_change_pct: dict[int, float] = field(default_factory=dict)
    #: 相位/档位及其价格
    phases: list[PhaseInfo] = field(default_factory=list)

    observed_at: datetime = field(default_factory=utcnow)
    source: str = "csqaq"

    @property
    def market_price(self) -> float | None:
        """用于计算收益率的「买入价」。

        取在各平台能买到的最低价 —— 收益率的分母应该是你**实际付出**的钱，
        用均价会把收益率算低、让不值得的标的看起来还行。
        """
        candidates = [p for p in (self.buff_sell_price, self.yyyp_sell_price,
                                  self.steam_sell_price) if p and p > 0]
        return min(candidates) if candidates else None

    @property
    def sell_platform(self) -> str:
        """最便宜的买入平台，决定卖出时的手续费口径。"""
        pairs = [(self.buff_sell_price, PLATFORM_BUFF),
                 (self.yyyp_sell_price, PLATFORM_YOUPIN)]
        pairs = [(p, name) for p, name in pairs if p and p > 0]
        if not pairs:
            return PLATFORM_BUFF
        return min(pairs, key=lambda x: x[0])[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_hash_name": self.market_hash_name,
            "display_name": self.display_name,
            "csqaq_good_id": self.csqaq_good_id,
            "market_price": self.market_price,
            "buff_sell_price": self.buff_sell_price,
            "yyyp_sell_price": self.yyyp_sell_price,
            "steam_sell_price": self.steam_sell_price,
            "short_daily_rent": self.short_daily_rent,
            "long_daily_rent": self.long_daily_rent,
            "short_annual_pct": self.short_annual_pct,
            "long_annual_pct": self.long_annual_pct,
            "lease_listings": self.lease_listings,
            "transfer_price": self.transfer_price,
            "turnover_number": self.turnover_number,
            "turnover_avg_price": self.turnover_avg_price,
            "supply": self.supply,
            "sell_num": self.sell_num,
            "buy_num": self.buy_num,
            "price_change_pct": {str(k): v for k, v in self.price_change_pct.items()},
            "phases": [p.to_dict() for p in self.phases],
            "observed_at": self.observed_at.isoformat(timespec="seconds"),
            "source": self.source,
        }


# ── 解析 ───────────────────────────────────────────────────

def _f(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _i(value: Any) -> int | None:
    out = _f(value)
    return int(out) if out is not None else None


def _positive(value: Any) -> float | None:
    out = _f(value)
    return out if out is not None and out > 0 else None


CHANGE_WINDOWS = (1, 7, 15, 30, 90, 180, 365)


def parse_detail(goods: dict[str, Any], display_name: str | None = None
                 ) -> RentSnapshot:
    """把 CSQAQ `info/good` 的 goods_info 解析成租金快照。

    字段名以官方文档示例为准；缺失字段一律留 None 而不是填 0 ——
    「没有这个数据」和「这个数据是 0」在收益计算里是完全不同的两件事。
    """
    snapshot = RentSnapshot(
        market_hash_name=str(goods.get("market_hash_name") or ""),
        display_name=display_name or goods.get("name"),
        csqaq_good_id=_i(goods.get("id")),

        buff_sell_price=_positive(goods.get("buff_sell_price")),
        yyyp_sell_price=_positive(goods.get("yyyp_sell_price")),
        steam_sell_price=_positive(goods.get("steam_sell_price")),
        buff_buy_price=_positive(goods.get("buff_buy_price")),
        yyyp_buy_price=_positive(goods.get("yyyp_buy_price")),

        short_daily_rent=_positive(goods.get("yyyp_lease_price")),
        long_daily_rent=_positive(goods.get("yyyp_long_lease_price")),
        short_annual_pct=_f(goods.get("yyyp_lease_annual")),
        long_annual_pct=_f(goods.get("yyyp_long_lease_annual")),
        lease_listings=_i(goods.get("yyyp_lease_num")),
        transfer_price=_positive(goods.get("yyyp_transfer_price")),

        turnover_number=_i(goods.get("turnover_number")),
        turnover_avg_price=_positive(goods.get("turnover_avg_price")),
        supply=_i(goods.get("statistic")),
        sell_num=_i(goods.get("yyyp_sell_num")),
        buy_num=_i(goods.get("yyyp_buy_num")),
        min_float=_f(goods.get("min_float")),
        max_float=_f(goods.get("max_float")),
    )

    for window in CHANGE_WINDOWS:
        value = _f(goods.get(f"sell_price_rate_{window}"))
        if value is not None:
            snapshot.price_change_pct[window] = value

    for entry in (goods.get("_dpl") or []):
        if not isinstance(entry, dict):
            continue
        snapshot.phases.append(PhaseInfo(
            label=str(entry.get("label") or entry.get("key") or "?"),
            label_en=entry.get("value"),
            paint_index=_i(entry.get("paint_index")),
            buff_sell_price=_positive(entry.get("buff_sell_price")),
            buff_buy_price=_positive(entry.get("buff_buy_price")),
        ))

    return snapshot


# ── 收益分析 ───────────────────────────────────────────────

MODE_SHORT = "short"
MODE_LONG = "long"
MODE_CN = {MODE_SHORT: "短租", MODE_LONG: "长租"}

#: 各周期的参考持有天数（把平台涨跌窗口映射到持仓周期）
DEFAULT_HORIZONS = (30, 90, 180)


@dataclass(slots=True)
class RentYield:
    """一个「持有 N 天并出租」方案的收益分解。"""

    mode: str
    horizon_days: int
    buy_price: float
    daily_rent: float
    occupancy: float             # 出租率（推算）

    gross_rent: float
    net_rent: float
    price_move: float
    exit_cost: float
    total_return: float
    total_return_pct: float
    annualized_pct: float

    theoretical_annual_pct: float
    platform_annual_pct: float | None
    implied_occupancy: float | None
    #: 出租率是否为保守假设（平台未给年化率时用了 OCCUPANCY_FALLBACK）
    occupancy_assumed: bool = False

    volatility_pct: float | None = None
    liquidity_score: float | None = None
    risk_adjusted_pct: float | None = None

    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "mode_cn": MODE_CN.get(self.mode, self.mode),
            "horizon_days": self.horizon_days,
            "buy_price": round(self.buy_price, 2),
            "daily_rent": round(self.daily_rent, 4),
            "occupancy": round(self.occupancy, 4),
            "gross_rent": round(self.gross_rent, 2),
            "net_rent": round(self.net_rent, 2),
            "price_move": round(self.price_move, 2),
            "exit_cost": round(self.exit_cost, 2),
            "total_return": round(self.total_return, 2),
            "total_return_pct": round(self.total_return_pct, 6),
            "annualized_pct": round(self.annualized_pct, 4),
            "theoretical_annual_pct": round(self.theoretical_annual_pct, 4),
            "platform_annual_pct": (round(self.platform_annual_pct, 4)
                                    if self.platform_annual_pct is not None else None),
            "implied_occupancy": (round(self.implied_occupancy, 4)
                                  if self.implied_occupancy is not None else None),
            "volatility_pct": (round(self.volatility_pct, 4)
                               if self.volatility_pct is not None else None),
            "liquidity_score": (round(self.liquidity_score, 2)
                                if self.liquidity_score is not None else None),
            "risk_adjusted_pct": (round(self.risk_adjusted_pct, 4)
                                  if self.risk_adjusted_pct is not None else None),
            "note": self.note,
        }


def implied_occupancy(platform_annual_pct: float | None,
                      theoretical_annual_pct: float | None) -> float | None:
    """由「平台年化 / 理论年化」推算隐含出租率。

    ⚠ 这是**推算值**：平台年化的确切算法未公开，此处假设它的差异主要来自
    空置与租期结构。结果被截断在 [0.05, 1.0] 区间 —— 超出这个范围说明
    假设不成立（例如平台年化用了不同的价格基准），此时宁可不给数字。
    """
    if not platform_annual_pct or not theoretical_annual_pct:
        return None
    if theoretical_annual_pct <= 0:
        return None
    ratio = platform_annual_pct / theoretical_annual_pct
    if ratio <= 0:
        return None
    if ratio > 1.0:
        # 平台年化高于满租理论值 → 口径不同（可能用了求购价或不同币种），不猜
        return None
    return max(0.05, min(1.0, ratio))


def estimate_volatility(snapshot: RentSnapshot) -> float | None:
    """用平台给出的多周期涨跌幅估计波动率（年化，%）。

    只有 7 个样本点，所以这只是**量级估计**而非精确统计量。
    用途是横向比较（哪个标的更颠簸），不是预测。
    """
    values = [abs(v) for k, v in snapshot.price_change_pct.items()
              if k >= 7 and v is not None]
    if len(values) < 3:
        return None
    # 各窗口涨跌幅都归一到「日均绝对变动」再年化
    daily = []
    for window, value in snapshot.price_change_pct.items():
        if window >= 7 and value is not None:
            daily.append(abs(value) / 100.0 / window)
    if len(daily) < 3:
        return None
    return statistics.median(daily) * 365 ** 0.5 * 100


def estimate_price_move(snapshot: RentSnapshot, horizon_days: int) -> tuple[float, str]:
    """估计持有期的价格变动率（小数，如 -0.08 表示跌 8%）。

    优先用最接近该周期的平台实测涨跌；没有就直接用 90 天数据按天数线性外推，
    并**在 note 里标明这是外推**。价格趋势是租赁收益里最不可预测的一项，
    所以这里只做保守外推，不做趋势加速之类的假设。
    """
    windows = sorted(snapshot.price_change_pct)
    if not windows:
        return 0.0, "无历史涨跌数据，按价格不变估算"

    # 找最接近的窗口
    nearest = min(windows, key=lambda w: abs(w - horizon_days))
    value = snapshot.price_change_pct[nearest]
    if nearest == horizon_days:
        return value / 100.0, f"采用平台 {nearest} 天实测涨跌 {value:+.2f}%"

    # 线性外推（保守：不放大趋势斜率）
    scaled = value * (horizon_days / nearest)
    return (scaled / 100.0,
            f"由 {nearest} 天实测涨跌 {value:+.2f}% 线性外推到 {horizon_days} 天 "
            f"（外推值，仅供参考）")


def liquidity_score(snapshot: RentSnapshot) -> float | None:
    """流动性评分 0~100：能不能顺利买进、顺利卖出。

    三个维度：存世量（盘子大小）、在售量（供给厚度）、成交量（真实需求）。
    任一项缺失就不给分 —— 缺数据的「高分」会误导决策。
    """
    parts: list[float] = []
    if snapshot.supply:
        # 存世量 1 万以上算很充裕
        parts.append(min(1.0, snapshot.supply / 10_000) * 40)
    if snapshot.sell_num:
        parts.append(min(1.0, snapshot.sell_num / 500) * 30)
    if snapshot.turnover_number:
        parts.append(min(1.0, snapshot.turnover_number / 100) * 30)
    if len(parts) < 2:
        return None
    return sum(parts)


def analyze(snapshot: RentSnapshot, horizon_days: int = 90,
            mode: str = MODE_LONG,
            rent_fee: dict[str, float] | None = None,
            sell_fee: dict[str, float] | None = None,
            withdraw_fee: float = DEFAULT_WITHDRAW_FEE) -> RentYield | None:
    """分析「买入并出租 N 天再卖出」的收益。

    收益构成（全部按买入价折算）：

        毛租金   = 日租金 × N × 出租率
        净租金   = 毛租金 × (1 − 租赁平台抽成)
        价格变动 = 买入价 × 持有期涨跌
        卖出成本 = (买入价 + 价格变动) × (卖出抽成 + 提现费)
        总收益   = 净租金 + 价格变动 − 卖出成本

    出租率用「平台年化 / 理论年化」推算（见 implied_occupancy）。
    """
    price = snapshot.market_price
    if not price or price <= 0:
        return None

    daily = (snapshot.long_daily_rent if mode == MODE_LONG
             else snapshot.short_daily_rent)
    if not daily or daily <= 0:
        return None

    platform_annual = (snapshot.long_annual_pct if mode == MODE_LONG
                       else snapshot.short_annual_pct)
    theoretical = daily * 365.0 / price * 100.0
    occupancy = implied_occupancy(platform_annual, theoretical)
    occupancy_assumed = occupancy is None
    if occupancy is None:
        occupancy = OCCUPANCY_FALLBACK

    horizon = max(1, int(horizon_days))
    gross_rent = daily * horizon * occupancy

    rent_fee_table = rent_fee or DEFAULT_RENT_FEE
    platform = snapshot.sell_platform
    net_rent = gross_rent * (1 - rent_fee_table.get(platform, 0.10))

    change_rate, change_note = estimate_price_move(snapshot, horizon)
    price_move = price * change_rate

    fee_table = sell_fee or DEFAULT_SELL_FEE
    exit_cost = abs(price + price_move) * (
        fee_table.get(platform, DEFAULT_SELL_FEE.get(platform, 0.025)) + withdraw_fee)

    total = net_rent + price_move - exit_cost
    total_pct = total / price
    annualized = total_pct * 365.0 / horizon * 100.0

    volatility = estimate_volatility(snapshot)
    liquidity = liquidity_score(snapshot)

    # 风险调整：用波动率把收益打折。波动率缺失时不做这个调整（而不是当成 0）
    risk_adjusted = None
    if volatility and volatility > 0:
        risk_adjusted = annualized / volatility
    elif volatility == 0:
        risk_adjusted = None      # 无波动数据不代表无风险，不给分

    note = (f"出租率 {occupancy:.0%}"
            + ("（保守假设，平台未提供年化率）" if occupancy_assumed else "（由平台年化推算）")
            + f"；{change_note}；"
            f"卖出成本按 {platform} {fee_table.get(platform, 0.025):.1%}"
            f" + 提现 {withdraw_fee:.1%}")

    return RentYield(
        mode=mode, horizon_days=horizon, buy_price=price, daily_rent=daily,
        occupancy=occupancy, gross_rent=gross_rent, net_rent=net_rent,
        price_move=price_move, exit_cost=exit_cost, total_return=total,
        total_return_pct=total_pct, annualized_pct=annualized,
        theoretical_annual_pct=theoretical, platform_annual_pct=platform_annual,
        implied_occupancy=occupancy, volatility_pct=volatility,
        liquidity_score=liquidity, risk_adjusted_pct=risk_adjusted,
        occupancy_assumed=occupancy_assumed,
        note=note,
    )


def analyze_all(snapshot: RentSnapshot,
                horizons: Sequence[int] = DEFAULT_HORIZONS,
                **kwargs: Any) -> list[RentYield]:
    """对长短租 × 多个持有周期做全组合分析。"""
    results: list[RentYield] = []
    for mode in (MODE_LONG, MODE_SHORT):
        for horizon in horizons:
            outcome = analyze(snapshot, horizon_days=horizon, mode=mode, **kwargs)
            if outcome:
                results.append(outcome)
    return results


# ── 结论 ───────────────────────────────────────────────────

VERDICT_GOOD = "good"        # 值得做
VERDICT_MARGINAL = "marginal"  # 勉强
VERDICT_POOR = "poor"        # 不值得
VERDICT_UNKNOWN = "unknown"  # 数据不足

VERDICT_CN = {VERDICT_GOOD: "值得考虑", VERDICT_MARGINAL: "勉强可行",
              VERDICT_POOR: "不建议", VERDICT_UNKNOWN: "数据不足"}


@dataclass
class RentVerdict:
    verdict: str
    headline: str
    best: RentYield | None
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "verdict_cn": VERDICT_CN.get(self.verdict, self.verdict),
            "headline": self.headline,
            "best": self.best.to_dict() if self.best else None,
            "reasons": self.reasons,
            "caveats": self.caveats,
        }


def judge(snapshot: RentSnapshot, yields: Sequence[RentYield]) -> RentVerdict:
    """给出可执行的结论与理由。

    判定顺序刻意是「先排除、再优选」：先看有没有致命问题（负收益、流动性枯竭），
    再看哪个方案最好。反过来会把一个流动性枯竭的标的评为「收益高」。
    """
    reasons: list[str] = []
    caveats: list[str] = []

    if not yields:
        return RentVerdict(VERDICT_UNKNOWN,
                           "缺少租金或价格数据，无法评估",
                           None, caveats=["该饰品可能没有出租挂单，或数据源未覆盖"])

    best = max(yields, key=lambda y: y.annualized_pct)
    annual = best.annualized_pct

    # ── 致命问题优先 ──
    if annual <= 0:
        reasons.append(f"持有 {best.horizon_days} 天的最佳年化为 {annual:+.1f}%，"
                       "已被价格下跌与手续费吃掉")
        return RentVerdict(VERDICT_POOR, "不建议：租金覆盖不了价格下跌与手续费",
                           best, reasons, caveats)

    if best.liquidity_score is not None and best.liquidity_score < 20:
        caveats.append(f"流动性偏低（评分 {best.liquidity_score:.0f}/100）："
                       "买入容易但卖出可能要压价，实际退出成本高于模型估计")

    if snapshot.lease_listings and snapshot.sell_num:
        ratio = snapshot.lease_listings / max(1, snapshot.sell_num)
        if ratio > 0.5:
            caveats.append(f"出租挂单 {snapshot.lease_listings} 对在售 "
                           f"{snapshot.sell_num}（{ratio:.0%}）：出租竞争激烈，"
                           "空置率可能高于推算值")

    # ── 收益水平 ──
    if annual >= 25:
        verdict = VERDICT_GOOD
    elif annual >= 10:
        verdict = VERDICT_MARGINAL
    else:
        verdict = VERDICT_POOR

    reasons.insert(0, f"最佳方案：{MODE_CN.get(best.mode, best.mode)}持有 "
                      f"{best.horizon_days} 天，年化 {annual:+.1f}%"
                      f"（净租金 ¥{best.net_rent:.2f}，"
                      f"价格变动 ¥{best.price_move:+.2f}，"
                      f"卖出成本 ¥{best.exit_cost:.2f}）")

    if best.risk_adjusted_pct is not None:
        if best.risk_adjusted_pct < 0.5:
            caveats.append(f"风险调整后收益仅 {best.risk_adjusted_pct:.2f}"
                           f"（年化 ÷ 年化波动 {best.volatility_pct:.0f}%）："
                           "波动相对收益偏高")
        else:
            reasons.append(f"风险调整后收益 {best.risk_adjusted_pct:.2f}，"
                           f"波动率 {best.volatility_pct:.0f}%")

    # ── 长租 vs 短租 ──
    longs = [y for y in yields if y.mode == MODE_LONG]
    shorts = [y for y in yields if y.mode == MODE_SHORT]
    if longs and shorts and snapshot.short_daily_rent and snapshot.long_daily_rent:
        if snapshot.short_daily_rent > snapshot.long_daily_rent:
            reasons.append(
                f"短租日租金 {snapshot.short_daily_rent:.2f} 高于长租 "
                f"{snapshot.long_daily_rent:.2f}，"
                + ("但折算年化后长租更优（空置率更低）"
                   if best.mode == MODE_LONG else "且折算年化也是短租更优"))

    # ── 必带的提醒 ──
    if best.occupancy_assumed:
        caveats.append(f"平台未提供租赁年化率，出租率按保守值 "
                       f"{best.occupancy:.0%} 假设 —— 实际可能更低，"
                       "收益也随之更低")
    else:
        caveats.append("出租率由平台年化率推算，平台算法未公开；实际空置可能更高")
    caveats.append("价格趋势为历史外推，赛事/版本更新等外部事件无法预判")

    headline = {
        VERDICT_GOOD: f"值得考虑：{MODE_CN.get(best.mode, best.mode)}年化约 {annual:.0f}%",
        VERDICT_MARGINAL: f"勉强可行：年化约 {annual:.0f}%，但余量不大",
        VERDICT_POOR: f"不建议：年化仅 {annual:.0f}%",
    }[verdict]

    return RentVerdict(verdict, headline, best, reasons, caveats)


def rank(snapshots: Sequence[tuple[RentSnapshot, RentVerdict]],
         limit: int = 30, min_liquidity: float = 0.0
         ) -> list[dict[str, Any]]:
    """把多个标的按最佳年化排序，供排行榜使用。"""
    rows: list[dict[str, Any]] = []
    for snapshot, verdict in snapshots:
        best = verdict.best
        if not best:
            continue
        if (best.liquidity_score or 0) < min_liquidity:
            continue
        rows.append({
            "market_hash_name": snapshot.market_hash_name,
            "display_name": snapshot.display_name,
            "market_price": best.buy_price,
            "mode_cn": MODE_CN.get(best.mode, best.mode),
            "daily_rent": best.daily_rent,
            "horizon_days": best.horizon_days,
            "annualized_pct": round(best.annualized_pct, 2),
            "risk_adjusted_pct": (round(best.risk_adjusted_pct, 2)
                                  if best.risk_adjusted_pct is not None else None),
            "liquidity_score": (round(best.liquidity_score, 1)
                                if best.liquidity_score is not None else None),
            "occupancy": round(best.occupancy, 3),
            "volatility_pct": (round(best.volatility_pct, 1)
                               if best.volatility_pct is not None else None),
            "verdict": verdict.verdict,
            "verdict_cn": VERDICT_CN.get(verdict.verdict, verdict.verdict),
            "lease_listings": snapshot.lease_listings,
            "supply": snapshot.supply,
        })
    rows.sort(key=lambda r: r["annualized_pct"], reverse=True)
    return rows[:limit]
