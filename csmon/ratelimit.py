"""限速与退避：每个 (源, 端点) 独立闸门。

借鉴参考实现 YouPinMobileApiClient.EndpointGate 的经验：
  - 端点级最小间隔（不是全局），避免一个慢端点拖累其它端点
  - 命中 429/风控时进入冷却，冷却按失败次数指数放大
  - 成功一次即清零，防止偶发抖动把冷却永久拉长
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field


class RateLimitExceeded(RuntimeError):
    """闸门处于冷却中，调用方应跳过一次而不是硬重试。"""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class _GateState:
    last_call_at: float = 0.0
    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    failures: int = 0


@dataclass
class EndpointGate:
    """单端点闸门：最小间隔 + 指数冷却。线程安全。"""

    name: str
    min_interval: float = 1.0          # 秒
    max_cooldown: float = 1800.0       # 上限 30 分钟
    base_cooldown: float = 60.0
    jitter: float = 0.25               # 间隔抖动比例，避免固定节奏被识别

    _state: _GateState = field(default_factory=_GateState)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self, blocking: bool = True) -> float:
        """申请一次调用许可。

        返回实际等待秒数。冷却中且 blocking=False 时抛 RateLimitExceeded。
        """
        while True:
            with self._lock:
                now = time.monotonic()
                if now < self._state.cooldown_until:
                    remaining = self._state.cooldown_until - now
                    if not blocking:
                        raise RateLimitExceeded(
                            f"{self.name} 冷却中：{self._state.cooldown_reason}", remaining)
                    wait = remaining
                else:
                    interval = self.min_interval * (1 + random.uniform(-self.jitter, self.jitter))
                    wait = (self._state.last_call_at + interval) - now
                    if wait <= 0:
                        self._state.last_call_at = now
                        return 0.0
            time.sleep(min(wait, 5.0))

    def report_success(self) -> None:
        with self._lock:
            self._state.failures = 0
            self._state.cooldown_until = 0.0
            self._state.cooldown_reason = ""

    def report_rate_limit(self, reason: str = "", retry_after: float | None = None) -> float:
        """记录一次限流/风控，返回本次冷却秒数。"""
        with self._lock:
            self._state.failures = min(self._state.failures + 1, 6)
            cooldown = min(self.max_cooldown, self.base_cooldown * (2 ** (self._state.failures - 1)))
            if retry_after and retry_after > cooldown:
                cooldown = min(self.max_cooldown * 2, retry_after)
            self._state.cooldown_until = time.monotonic() + cooldown
            self._state.cooldown_reason = reason or "请求过于频繁"
            return cooldown

    def report_transient_failure(self, reason: str = "", cooldown: float = 30.0) -> float:
        with self._lock:
            self._state.failures = min(self._state.failures + 1, 4)
            self._state.cooldown_until = time.monotonic() + cooldown
            self._state.cooldown_reason = reason or "临时服务异常"
            return cooldown

    @property
    def cooldown_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._state.cooldown_until - time.monotonic())

    @property
    def cooldown_reason(self) -> str:
        with self._lock:
            return self._state.cooldown_reason

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "name": self.name,
                "failures": self._state.failures,
                "cooldown_remaining": round(max(0.0, self._state.cooldown_until - time.monotonic()), 1),
                "cooldown_reason": self._state.cooldown_reason,
            }


class GateRegistry:
    """按 (source, endpoint) 缓存闸门，供调度器与适配器共享。"""

    def __init__(self) -> None:
        self._gates: dict[str, EndpointGate] = {}
        self._lock = threading.Lock()

    def gate(self, source: str, endpoint: str, min_interval: float = 1.0,
             **kwargs: object) -> EndpointGate:
        key = f"{source}:{endpoint}"
        with self._lock:
            gate = self._gates.get(key)
            if gate is None:
                gate = EndpointGate(name=key, min_interval=min_interval, **kwargs)  # type: ignore[arg-type]
                self._gates[key] = gate
            return gate

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [g.snapshot() for g in self._gates.values()]


# 各源已知/实测的频率约束（秒），供配置默认值使用
KNOWN_INTERVALS: dict[str, float] = {
    "csqaq:price": 1.05,          # 文档：单 IP 1 次/秒
    "steamdt:price_batch": 60.5,  # 文档：批量 1 次/分钟
    "steamdt:price_single": 1.05,  # 文档：单件 60 次/分钟
    "buff_direct:goods_info": 0.6,
    "buff_direct:sell_order": 0.8,
    "youpin_direct:purchase_page": 1.0,   # 参考实现默认 2s，此处保守取 1s 并留抖动
}
