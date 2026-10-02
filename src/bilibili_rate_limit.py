from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

DEFAULT_BILI_RPS = 3.0

_limiter_lock = threading.Lock()
_limiter: TokenBucketRateLimiter | None = None
_configured_rps: float | None = None


@dataclass(frozen=True)
class BilibiliRateLimitAcquisition:
    rps: float
    source: str
    waited: bool
    wait_ms: float


def _read_rps_from_env() -> float:
    raw = os.environ.get("BILI_RPS", "").strip()
    if not raw:
        return DEFAULT_BILI_RPS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_BILI_RPS


class TokenBucketRateLimiter:
    """进程内全局限流：令牌桶，线程安全。"""

    def __init__(
        self,
        rps: float,
        *,
        burst: float | None = None,
        source: str = "configured",
    ) -> None:
        self._rps = max(0.0, float(rps))
        self._capacity = max(1.0, float(burst if burst is not None else min(2.0, self._rps)))
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self._source = str(source or "configured")

    @property
    def rps(self) -> float:
        return self._rps

    @property
    def burst(self) -> float:
        return self._capacity

    @property
    def source(self) -> str:
        return self._source

    def acquire(self) -> tuple[bool, float]:
        if self._rps <= 0:
            return False, 0.0
        waited = False
        waited_seconds = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self._last_refill)
                self._last_refill = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rps)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited, waited_seconds * 1000
                wait_sec = (1.0 - self._tokens) / self._rps
            waited = True
            started = time.monotonic()
            time.sleep(wait_sec)
            waited_seconds += max(0.0, time.monotonic() - started)


def configure_bilibili_rate_limit(*, rps: float | None = None) -> TokenBucketRateLimiter:
    """配置全局限流器；rps=0 表示关闭。"""
    global _limiter, _configured_rps
    resolved = DEFAULT_BILI_RPS if rps is None else max(0.0, float(rps))
    with _limiter_lock:
        _configured_rps = resolved
        _limiter = TokenBucketRateLimiter(
            resolved, source="default" if rps is None else "configured"
        )
        return _limiter


def reset_bilibili_rate_limit() -> None:
    """测试用：恢复为环境变量/默认值。"""
    global _limiter, _configured_rps
    with _limiter_lock:
        _configured_rps = None
        _limiter = None


def get_bilibili_rate_limiter() -> TokenBucketRateLimiter:
    global _limiter, _configured_rps
    with _limiter_lock:
        if _limiter is None:
            if _configured_rps is None:
                raw = os.environ.get("BILI_RPS", "").strip()
                resolved = _read_rps_from_env()
                try:
                    float(raw)
                    source = "env" if raw else "default"
                except ValueError:
                    source = "default"
            else:
                resolved = _configured_rps
                source = "configured"
            _limiter = TokenBucketRateLimiter(resolved, source=source)
        return _limiter


def acquire_bilibili_request_slot() -> BilibiliRateLimitAcquisition:
    limiter = get_bilibili_rate_limiter()
    waited, wait_ms = limiter.acquire()
    return BilibiliRateLimitAcquisition(
        rps=limiter.rps,
        source=limiter.source,
        waited=waited,
        wait_ms=wait_ms,
    )
