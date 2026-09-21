"""技术指标：纯 Python 实现，不依赖 numpy/pandas。

为什么不用 numpy：目标是能在树莓派 Zero/3 上跑（512MB 内存、无编译工具链）。
这些指标都是 O(n) 单遍算法，纯 Python 对几千个数据点完全够用，
而引入 numpy 会让 ARM 上的安装体积和失败概率都上一个台阶。

所有函数约定：
  - 输入是「按时间升序」的价格序列
  - 数据不足时返回 None（而不是抛异常或返回 0）
    —— 指标算出 0 和「算不出来」是两件完全不同的事，混在一起会让告警误判
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


def _clean(prices: Sequence[float | None]) -> list[float]:
    """剔除空值与非正值（挂单价格为 0 通常是脏数据，不是「免费」）。"""
    return [float(p) for p in prices if p is not None and float(p) > 0]


# ── 均线 ───────────────────────────────────────────────────

def sma(prices: Sequence[float], period: int) -> float | None:
    """简单移动平均（取最后 period 个）。"""
    if period <= 0:
        return None
    values = _clean(prices)
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def ema_series(prices: Sequence[float], period: int) -> list[float]:
    """指数移动平均全序列。首值用首个价格初始化，避免前几项失真。"""
    values = _clean(prices)
    if not values or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for price in values[1:]:
        out.append(price * k + out[-1] * (1 - k))
    return out


def ema(prices: Sequence[float], period: int) -> float | None:
    series = ema_series(prices, period)
    return series[-1] if series else None


def ma_bundle(prices: Sequence[float],
              periods: Sequence[int] = (7, 30, 90)) -> dict[str, float | None]:
    """一组常用均线，用于看板与「均线多头/空头排列」判断。"""
    return {f"ma{p}": sma(prices, p) for p in periods}


# ── 波动与超买超卖 ─────────────────────────────────────────

def rsi(prices: Sequence[float], period: int = 14) -> float | None:
    """Wilder RSI。数据不足 period+1 时返回 None。"""
    values = _clean(prices)
    if len(values) < period + 1:
        return None

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period

    # Wilder 平滑（不是简单平均），后续项按 1/period 权重递推
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def stddev(prices: Sequence[float], period: int | None = None) -> float | None:
    """样本标准差（n-1 分母）。period 为空时用全序列。"""
    values = _clean(prices)
    if period:
        values = values[-period:]
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(variance)


@dataclass(frozen=True)
class BollingerBands:
    upper: float
    middle: float
    lower: float
    #: 带宽 = (上轨 - 下轨) / 中轨，衡量波动剧烈程度
    width: float

    def position(self, price: float) -> float | None:
        """价格在通道内的相对位置：0=下轨，1=上轨。用于「贴近下轨」这类判断。"""
        span = self.upper - self.lower
        if span <= 0:
            return None
        return (price - self.lower) / span


def bollinger(prices: Sequence[float], period: int = 20,
              mult: float = 2.0) -> BollingerBands | None:
    values = _clean(prices)
    if len(values) < period:
        return None
    window = values[-period:]
    middle = sum(window) / period
    sd = stddev(window)
    if sd is None:
        return None
    upper = middle + mult * sd
    lower = middle - mult * sd
    width = (upper - lower) / middle if middle else 0.0
    return BollingerBands(upper=upper, middle=middle, lower=lower, width=width)


def volatility(prices: Sequence[float], period: int = 30) -> float | None:
    """对数收益率的标准差（日波动率）。"""
    values = _clean(prices)
    if len(values) < period + 1:
        return None
    window = values[-(period + 1):]
    returns: list[float] = []
    for i in range(1, len(window)):
        if window[i - 1] > 0 and window[i] > 0:
            returns.append(math.log(window[i] / window[i - 1]))
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance)


def annualized_volatility(prices: Sequence[float], period: int = 30,
                          periods_per_year: int = 365) -> float | None:
    vol = volatility(prices, period)
    return vol * math.sqrt(periods_per_year) if vol is not None else None


# ── 收益与动量 ─────────────────────────────────────────────

def momentum(prices: Sequence[float], period: int = 7) -> float | None:
    """动量 = 现价 / period 前价格 - 1。"""
    values = _clean(prices)
    if len(values) < period + 1:
        return None
    past = values[-(period + 1)]
    if past <= 0:
        return None
    return values[-1] / past - 1.0


def annualized_return(prices: Sequence[float],
                      periods_per_year: int = 365) -> float | None:
    """年化收益率（按复利外推）。数据跨度不足 2 个点时返回 None。"""
    values = _clean(prices)
    if len(values) < 2 or values[0] <= 0 or values[-1] <= 0:
        return None
    periods = len(values) - 1
    if periods <= 0:
        return None
    total = values[-1] / values[0]
    try:
        return total ** (periods_per_year / periods) - 1.0
    except (OverflowError, ValueError):
        return None


def max_drawdown(prices: Sequence[float]) -> float | None:
    """最大回撤（负数，如 -0.23 表示最深跌 23%）。"""
    values = _clean(prices)
    if len(values) < 2:
        return None
    peak = values[0]
    worst = 0.0
    for price in values:
        if price > peak:
            peak = price
        if peak > 0:
            drawdown = price / peak - 1.0
            worst = min(worst, drawdown)
    return worst


def zscore(prices: Sequence[float], period: int = 30) -> float | None:
    """现价相对近 period 个样本的 Z 值。极端值提示偏离常态。"""
    values = _clean(prices)
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / len(window)
    sd = stddev(window)
    if not sd:
        return None
    return (values[-1] - mean) / sd


# ── 综合快照 ───────────────────────────────────────────────

@dataclass(frozen=True)
class IndicatorSnapshot:
    """一次性算全套，供看板与信号评分使用。"""

    count: int
    last: float | None
    ma: dict[str, float | None]
    rsi14: float | None
    boll: BollingerBands | None
    vol30: float | None
    ann_vol: float | None
    ann_return: float | None
    momentum7: float | None
    drawdown: float | None
    z30: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "count": self.count,
            "last": self.last,
            "ma": self.ma,
            "rsi14": self.rsi14,
            "bollinger": (None if self.boll is None else {
                "upper": self.boll.upper, "middle": self.boll.middle,
                "lower": self.boll.lower, "width": self.boll.width}),
            "volatility_30": self.vol30,
            "annualized_volatility": self.ann_vol,
            "annualized_return": self.ann_return,
            "momentum_7": self.momentum7,
            "max_drawdown": self.drawdown,
            "zscore_30": self.z30,
        }


def snapshot(prices: Sequence[float],
             ma_periods: Sequence[int] = (7, 30, 90)) -> IndicatorSnapshot:
    values = _clean(prices)
    return IndicatorSnapshot(
        count=len(values),
        last=values[-1] if values else None,
        ma=ma_bundle(values, ma_periods),
        rsi14=rsi(values, 14),
        boll=bollinger(values, 20, 2.0),
        vol30=volatility(values, 30),
        ann_vol=annualized_volatility(values, 30),
        ann_return=annualized_return(values),
        momentum7=momentum(values, 7),
        drawdown=max_drawdown(values),
        z30=zscore(values, 30),
    )


# ── 信号解释（把数字翻译成人话，避免看板只有裸指标）──────────

def interpret(snap: IndicatorSnapshot) -> list[str]:
    """给出可读的指标解读。只做描述，不做买卖建议。"""
    notes: list[str] = []
    if snap.rsi14 is not None:
        if snap.rsi14 >= 70:
            notes.append(f"RSI {snap.rsi14:.0f}：超买区间")
        elif snap.rsi14 <= 30:
            notes.append(f"RSI {snap.rsi14:.0f}：超卖区间")
        else:
            notes.append(f"RSI {snap.rsi14:.0f}：中性")

    if snap.boll is not None and snap.last is not None:
        pos = snap.boll.position(snap.last)
        if pos is not None:
            if pos <= 0.05:
                notes.append("价格贴近布林下轨（短期偏弱）")
            elif pos >= 0.95:
                notes.append("价格贴近布林上轨（短期偏强）")
        # 收敛提示不能塞进上面的 pos 判断里：价格几乎不动时带宽趋近 0，
        # 此时 position() 返回 None，而「极度收敛」恰恰是这时最该说的话
        if snap.boll.width < 0.05:
            notes.append(f"布林带宽 {snap.boll.width:.1%}：极度收敛，可能酝酿方向选择")

    if snap.ann_vol is not None:
        notes.append(f"年化波动率 {snap.ann_vol:.0%}")

    if snap.drawdown is not None and snap.drawdown < -0.15:
        notes.append(f"区间最大回撤 {snap.drawdown:.1%}")

    if snap.z30 is not None and abs(snap.z30) >= 2:
        direction = "高于" if snap.z30 > 0 else "低于"
        notes.append(f"Z 值 {snap.z30:+.1f}：现价显著{direction}近期均值")

    ma = snap.ma
    fast, mid = ma.get("ma7"), ma.get("ma30")
    if fast and mid and snap.last:
        if fast > mid and snap.last > fast:
            notes.append("均线多头排列（MA7 > MA30，现价在 MA7 上方）")
        elif fast < mid and snap.last < fast:
            notes.append("均线空头排列（MA7 < MA30，现价在 MA7 下方）")
    return notes
