"""查询调度的时间策略（P0-2 的一部分，纯函数，无依赖，便于单测）。

规格书要求：
- 基础间隔默认 3~5 秒，实际间隔 = base ± random(0, base*0.3)
- 每个任务 / 日期 / 车站组合各自独立抖动，不能同步（用 DeterministicJitter 按 key 派生流）
- 开售前（T-60min 起）更激进，进入放票窗口后降速
- 熔断后指数级延长重试间隔（30s -> 5min -> 30min），带随机抖动
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Optional, Protocol


class RandomStream(Protocol):
    def uniform(self, a: float, b: float) -> float: ...

    def random(self) -> float: ...


@dataclass
class DeterministicJitter:
    """按 key 派生独立随机流：同一个 (任务,日期,车站) 稳定复现，不同组合互不相关。

    这样多个任务不会在同一毫秒一起发请求（同步抖动等于没有抖动）。
    """

    key: str
    seed: Optional[int] = None

    def __post_init__(self) -> None:
        digest = hashlib.blake2b(self.key.encode("utf-8"), digest_size=8).digest()
        base = int.from_bytes(digest, "big")
        if self.seed is not None:
            base ^= self.seed
        self._rng = random.Random(base)

    def uniform(self, a: float, b: float) -> float:
        return self._rng.uniform(a, b)

    def random(self) -> float:
        return self._rng.random()


class SystemJitter:
    """真随机流，用于没有 key 语义的场合。"""

    def __init__(self, seed: Optional[int] = None) -> None:
        self._rng = random.Random(seed)

    def uniform(self, a: float, b: float) -> float:
        return self._rng.uniform(a, b)

    def random(self) -> float:
        return self._rng.random()


@dataclass(frozen=True)
class QueryTimingConfig:
    normal_base_seconds: float = 4.0
    pre_sale_base_seconds: float = 1.2
    window_base_seconds: float = 5.0
    jitter_ratio: float = 0.3
    min_seconds: float = 0.2
    window_start_minutes: int = 6


def new_stream(key: str, seed: Optional[int] = None) -> RandomStream:
    """按 key 派生独立抖动流；key 为空时用系统随机源。"""
    if not key:
        return SystemJitter(seed)
    return DeterministicJitter(key, seed)


def jittered(base_seconds: float, ratio: float, stream: RandomStream) -> float:
    """base ± uniform(0, base*ratio)。base<=0 时退化为 ratio 秒内的正抖动。"""
    if base_seconds <= 0:
        return max(0.0, ratio) * stream.random()
    spread = base_seconds * max(0.0, ratio)
    low = max(0.0, base_seconds - spread)
    high = base_seconds + spread
    return stream.uniform(low, high)


def pre_sale_base(config: QueryTimingConfig, stream: RandomStream) -> float:
    """开售前（T-60min 起）更激进：在 window_base 与 pre_sale_base 之间抖动。

    注意这是有下限的：规格书要求默认不低于 pre_sale_base，避免「提前 1 小时就开始轰炸」。
    """
    low = min(config.pre_sale_base_seconds, config.window_base_seconds)
    high = max(config.pre_sale_base_seconds, config.window_base_seconds)
    return stream.uniform(low, high)


def next_query_delay(
    config: QueryTimingConfig,
    pre_sale: bool,
    stream: RandomStream,
    excited: bool = False,
    sold_out_busy: bool = False,
    override: Optional[float] = None,
) -> float:
    """返回「下一次查询前应等待的秒数」。

    pre_sale:          处于开售前激进窗口
    excited:           刚出现异常（退避中），此时用正常间隔
    sold_out_busy:     已知无票的空转轮次，用窗口间隔（降速）
    override:          显式覆盖基础间隔（配置里的 QUERY_INTERVAL）
    """
    if override is not None and override > 0:
        return jittered(override, config.jitter_ratio, stream)
    if pre_sale:
        return pre_sale_base(config, stream)
    if sold_out_busy:
        return jittered(config.window_base_seconds, config.jitter_ratio, stream)
    return jittered(config.normal_base_seconds, config.jitter_ratio, stream)


def clamp_delay(seconds: float, minimum: float, maximum: Optional[float] = None) -> float:
    out = max(seconds, minimum)
    if maximum is not None:
        out = min(out, maximum)
    return out


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """解析 Retry-After 响应头：只支持秒数形式（HTTP-date 形式省略，返回 None）。"""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


def exponential_backoff(
    attempt: int,
    base_seconds: float,
    factor: float,
    cap_seconds: float,
    *,
    jitter_ratio: float = 0.2,
    stream: Optional[RandomStream] = None,
) -> float:
    """带抖动的指数退避：base * factor^(attempt-1)，上限 cap。

    两层时间口径在项目里是固定的，见 risk.py：
      退避（连续 5xx/超时）  base=3s factor=2 cap=120s
      熔断（命中风控）      base=30s factor≈3.16 cap=1800s
    """
    if attempt < 1:
        attempt = 1
    spread = base_seconds * (factor ** (attempt - 1))
    value = min(spread, cap_seconds)
    if jitter_ratio <= 0:
        return value
    src = stream or SystemJitter()
    return src.uniform(value * (1.0 - jitter_ratio), value)
