"""V21 LLM Provider 韧性：单候选异常隔离 + 连续故障 breaker。

作用域：当前 pipeline execution（contextvar 绑定），不写全局状态、
不触发 Bilibili 风险/冷却、不影响下一次正常刷新。
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field

from src.llm_client import LlmConfigError, LlmProtocolError, LlmTemporaryError

PROVIDER_FAILURE_BREAKER_THRESHOLD = 3


class LlmProviderUnavailable(RuntimeError):
    """Provider 已不可用（连续失败达阈值/配置错误）：当前执行禁止继续 LLM。"""


@dataclass
class LlmProviderBreaker:
    """连续 provider-level failure 熔断器（temporary/protocol 计入，业务解析失败不计入）。

    enrich 阶段可能由线程池并发上报，计数用锁保证一致性。
    """

    threshold: int = PROVIDER_FAILURE_BREAKER_THRESHOLD
    consecutive_failures: int = 0
    failure_count: int = 0
    success_count: int = 0
    opened: bool = False
    config_unavailable: bool = False
    stop_reason: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def check(self) -> None:
        """任何新的 LLM 请求前调用：breaker 已打开则本地快速失败（0 远程请求）。"""
        with self._lock:
            opened = self.opened
            stop_reason = self.stop_reason
        if opened:
            raise LlmProviderUnavailable(stop_reason or "LLM provider unavailable")

    def note_success(self) -> None:
        """任何一次真正成功的 provider 调用都重置连续失败计数。"""
        with self._lock:
            self.consecutive_failures = 0
            self.success_count += 1

    def note_provider_failure(self, exc: BaseException | None = None) -> None:
        with self._lock:
            self.failure_count += 1
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.threshold and not self.opened:
                self.opened = True
                self.stop_reason = (
                    f"LLM 服务连续异常（{self.consecutive_failures} 次），当前源停止继续分类；"
                    "已成功结果已保存，剩余候选将在后续刷新继续"
                )

    def note_config_failure(self, exc: BaseException | None = None) -> None:
        """Config/Auth 第一次明确失败即停止当前源（不等连续 3 次，不写 Bilibili 冷却）。"""
        with self._lock:
            self.config_unavailable = True
            self.opened = True
            if not self.stop_reason:
                self.stop_reason = (
                    "LLM 配置/鉴权不可用，当前源停止继续分类；"
                    "已成功结果已保存，修复配置后自动恢复"
                )


_breaker_ctx: contextvars.ContextVar[LlmProviderBreaker | None] = contextvars.ContextVar(
    "llm_provider_breaker", default=None
)


def bind_llm_provider_breaker(breaker: LlmProviderBreaker) -> contextvars.Token:
    return _breaker_ctx.set(breaker)


def reset_llm_provider_breaker(token: contextvars.Token) -> None:
    _breaker_ctx.reset(token)


def current_llm_provider_breaker() -> LlmProviderBreaker | None:
    return _breaker_ctx.get()


def clear_llm_provider_breaker_for_tests() -> None:
    _breaker_ctx.set(None)


def is_provider_failure(exc: BaseException) -> bool:
    """temporary transport/provider failure 或 protocol incompatibility。"""
    return isinstance(exc, (LlmTemporaryError, LlmProtocolError))


def is_config_failure(exc: BaseException) -> bool:
    """Config/Auth/endpoint/model 配置错误（含 fingerprint 封锁的快速失败）。"""
    return isinstance(exc, LlmConfigError)
