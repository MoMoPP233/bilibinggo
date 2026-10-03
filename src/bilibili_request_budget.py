"""V22 per-Profile Bilibili 真实 HTTP attempt 预算窗口。

目标：冷状态首次「同步转发历史 + 连续智能评估」按真实 HTTP attempt 计数，
在请求发出前 reserve；预算耗尽时安全暂停，剩余记录在下一窗口自动续扫。

设计要点：
- 计数层：BilibiliClient._http_get/_http_post 的统一发送点（retry 每次 +1）。
- 作用域：contextvar，仅 sync/assessment/auto_maintain 等受保护只读任务启用；
  Winner/Following/普通 DS/登录/DELETE mutation 不受影响。
- 持久化：per-Profile JSON（只保存窗口时间戳/计数/上限，绝无 Cookie/Key/正文）。
- 并发：复用 data_paths.get_atomic_path_lock 的 per-path RLock 串行化
  read-modify-write + 原子替换，同进程并发不超卖；Profile 之间路径独立。
- 重启不重置：状态在文件里，进程重启后窗口仍生效。
- 与 V20/V21 隔离：RequestBudgetExhausted 是本地主动保护，不是平台风控、
  不是 LLM 故障，绝不写 cooldown、绝不污染 provider breaker。
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 默认预算选择依据（报告需说明）：
# - 真实事故：198 条评估 ≈ 413 次 Bilibili 请求后出现 -352；
# - 本默认值取 120 ≈ 事故量级的 29%，安全优先，明显低于危险区；
# - 6 小时窗口与既有 auto remote risk cooldown 对齐，冷 backlog 分窗自动续扫；
# - 单一常量，测试可注入更小值。
DEFAULT_BILIBILI_REQUEST_BUDGET = 120
DEFAULT_BILIBILI_REQUEST_BUDGET_WINDOW_SECONDS = 6 * 60 * 60

BUDGET_STATE_FILENAME = "bilibili_request_budget.json"

# 时钟偏差容忍：超出视为异常时间戳（回拨/未来），按 fail-safe 重建有界窗口。
CLOCK_SKEW_SECONDS = 60


class RequestBudgetExhausted(RuntimeError):
    """本地主动保护：本窗口 Bilibili 真实 HTTP 预算已用完。

    不是平台风控（-352/-509/429），不是 LLM 故障；不得写入任何 cooldown。
    """

    def __init__(self, snapshot: "RequestBudgetSnapshot", *, message: str | None = None) -> None:
        self.snapshot = snapshot
        super().__init__(
            message
            or (
                f"本轮 Bilibili 安全请求预算已用完：已使用 "
                f"{snapshot.used_requests} / {snapshot.budget_limit} 次"
            )
        )


@dataclass(frozen=True)
class RequestBudgetSnapshot:
    window_started_at: int
    window_expires_at: int
    used_requests: int
    budget_limit: int
    window_seconds: int
    observed_at: int

    @property
    def remaining_requests(self) -> int:
        return max(0, self.budget_limit - self.used_requests)

    @property
    def remaining_seconds(self) -> int:
        return max(0, self.window_expires_at - self.observed_at)

    @property
    def exhausted(self) -> bool:
        return self.used_requests >= self.budget_limit

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_limit": self.budget_limit,
            "used_requests": self.used_requests,
            "remaining_requests": self.remaining_requests,
            "window_started_at": self.window_started_at,
            "window_expires_at": self.window_expires_at,
            "resume_after": self.window_expires_at,
            "remaining_seconds": self.remaining_seconds,
        }


@dataclass
class BilibiliRequestBudgetScope:
    """单次受保护任务的作用域：共享持久窗口 + 本任务脱敏计数。"""

    profile_dir: Path
    limit: int
    window_seconds: int
    now_fn: Callable[[], int]
    enforce_limit: bool = True
    window_started_at: int = 0
    rolled: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _total_attempts: int = 0
    _category_counts: dict[str, int] = field(default_factory=dict)
    _cache_hits: int = 0

    def note_attempt(self, category: str) -> None:
        with self._lock:
            self._total_attempts += 1
            self._category_counts[category] = self._category_counts.get(category, 0) + 1

    def note_cache_hit(self) -> None:
        with self._lock:
            self._cache_hits += 1

    @property
    def total_attempts(self) -> int:
        with self._lock:
            return self._total_attempts

    @property
    def category_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._category_counts)

    @property
    def cache_hits(self) -> int:
        with self._lock:
            return self._cache_hits

    def snapshot(self) -> RequestBudgetSnapshot:
        return get_bilibili_request_budget(
            now=self.now_fn(),
            limit=self.limit,
            window_seconds=self.window_seconds,
            profile_dir=self.profile_dir,
        )

    def result_fields(self, *, exhausted: bool, partial: bool) -> dict[str, Any]:
        if not self.enforce_limit:
            return {
                "budget_exhausted": False,
                "partial": bool(partial),
                "request_attempts": self.total_attempts,
                "request_categories": self.category_counts,
                "cache_hits": self.cache_hits,
            }
        snap = self.snapshot()
        return {
            "budget_exhausted": bool(exhausted),
            "partial": bool(partial or exhausted),
            **snap.to_dict(),
            "request_attempts": self.total_attempts,
            "request_categories": self.category_counts,
            "cache_hits": self.cache_hits,
        }


_scope_ctx: contextvars.ContextVar[BilibiliRequestBudgetScope | None] = (
    contextvars.ContextVar("bilibili_request_budget_scope", default=None)
)


def active_bilibili_request_budget_scope() -> BilibiliRequestBudgetScope | None:
    return _scope_ctx.get()


def clear_bilibili_request_budget_scope_for_tests() -> None:
    _scope_ctx.set(None)


@contextmanager
def bilibili_request_budget_scope(
    *,
    limit: int | None = None,
    window_seconds: int | None = None,
    now_fn: Callable[[], int] | None = None,
    profile_dir: Path | None = None,
    enforce_limit: bool = True,
) -> Iterator[BilibiliRequestBudgetScope]:
    """请求统计作用域；默认执行持久预算，人工清理可只统计不截断。"""
    existing = _scope_ctx.get()
    if existing is not None:
        yield existing
        return
    resolved_now_fn = now_fn or (lambda: int(time.time()))
    scope = BilibiliRequestBudgetScope(
        profile_dir=profile_dir or _default_profile_dir(),
        limit=_resolve_limit(limit),
        window_seconds=_resolve_window(window_seconds),
        now_fn=resolved_now_fn,
        enforce_limit=enforce_limit,
    )
    token = _scope_ctx.set(scope)
    try:
        yield scope
    finally:
        _scope_ctx.reset(token)


def _default_profile_dir() -> Path:
    from src.data_paths import get_profile_dir

    return get_profile_dir()


def _resolve_limit(limit: int | None) -> int:
    if limit is None:
        return int(DEFAULT_BILIBILI_REQUEST_BUDGET)
    return max(1, int(limit))


def _resolve_window(window_seconds: int | None) -> int:
    if window_seconds is None:
        return int(DEFAULT_BILIBILI_REQUEST_BUDGET_WINDOW_SECONDS)
    return max(1, int(window_seconds))


def _state_path(profile_dir: Path | None = None) -> Path:
    return (profile_dir or _default_profile_dir()) / BUDGET_STATE_FILENAME


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _load_raw(path: Path) -> dict[str, Any] | None:
    import json

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _new_window(now: int, limit: int, window_seconds: int) -> dict[str, int]:
    return {
        "window_started_at": int(now),
        "window_expires_at": int(now) + int(window_seconds),
        "used_requests": 0,
        "budget_limit": int(limit),
    }


def _effective_window(
    raw: dict[str, Any] | None,
    *,
    now: int,
    limit: int,
    window_seconds: int,
) -> tuple[dict[str, int], bool]:
    """校验并返回 (有效窗口, 是否重建)。

    异常时间戳 fail-safe：不永久锁死、不无限放大；始终落在有界窗口内。
    """
    if not isinstance(raw, dict):
        return _new_window(now, limit, window_seconds), True
    started = _as_int(raw.get("window_started_at"))
    expires = _as_int(raw.get("window_expires_at"))
    used = _as_int(raw.get("used_requests"))
    stored_limit = _as_int(raw.get("budget_limit"))
    if (
        started is None
        or expires is None
        or used is None
        or stored_limit is None
        or used < 0
        or stored_limit <= 0
        or expires <= started
    ):
        return _new_window(now, limit, window_seconds), True
    # 回拨：窗口起点明显晚于当前时钟 → 不信任旧时间戳，重建有界窗口（不永久锁死）。
    if now + CLOCK_SKEW_SECONDS < started:
        return _new_window(now, limit, window_seconds), True
    # 未来异常到期时间：夹到合法窗口长度，避免被篡改成超长额度。
    max_expires = started + max(1, int(window_seconds))
    if expires > max_expires:
        expires = max_expires
    effective_limit = min(int(stored_limit), int(limit))
    if now >= expires:
        return _new_window(now, limit, window_seconds), True
    return (
        {
            "window_started_at": started,
            "window_expires_at": expires,
            "used_requests": used,
            "budget_limit": effective_limit,
        },
        False,
    )


def _snapshot(state: dict[str, int], *, now: int, window_seconds: int) -> RequestBudgetSnapshot:
    return RequestBudgetSnapshot(
        window_started_at=state["window_started_at"],
        window_expires_at=state["window_expires_at"],
        used_requests=state["used_requests"],
        budget_limit=state["budget_limit"],
        window_seconds=int(window_seconds),
        observed_at=int(now),
    )


def _write_state(path: Path, state: dict[str, int]) -> None:
    from src.data_paths import _write_json_atomic

    _write_json_atomic(path, state)


def get_bilibili_request_budget(
    *,
    now: int | None = None,
    limit: int | None = None,
    window_seconds: int | None = None,
    profile_dir: Path | None = None,
) -> RequestBudgetSnapshot:
    """只读视图：不创建/不推进窗口（0 远程、0 写）。"""
    resolved_now = int(now if now is not None else time.time())
    resolved_limit = _resolve_limit(limit)
    resolved_window = _resolve_window(window_seconds)
    path = _state_path(profile_dir)
    raw = _load_raw(path)
    state, _rolled = _effective_window(
        raw,
        now=resolved_now,
        limit=resolved_limit,
        window_seconds=resolved_window,
    )
    return _snapshot(state, now=resolved_now, window_seconds=resolved_window)


def reserve_bilibili_request(
    *,
    now: int | None = None,
    limit: int | None = None,
    window_seconds: int | None = None,
    profile_dir: Path | None = None,
) -> RequestBudgetSnapshot:
    """reserve-before-request：成功返回后才允许真正发送 HTTP；无余额抛异常。"""
    from src.data_paths import get_atomic_path_lock

    resolved_now = int(now if now is not None else time.time())
    resolved_limit = _resolve_limit(limit)
    resolved_window = _resolve_window(window_seconds)
    path = _state_path(profile_dir)
    with get_atomic_path_lock(path):
        raw = _load_raw(path)
        state, _rolled = _effective_window(
            raw,
            now=resolved_now,
            limit=resolved_limit,
            window_seconds=resolved_window,
        )
        if state["used_requests"] >= state["budget_limit"]:
            raise RequestBudgetExhausted(
                _snapshot(state, now=resolved_now, window_seconds=resolved_window)
            )
        state = dict(state)
        state["used_requests"] = int(state["used_requests"]) + 1
        _write_state(path, state)
    return _snapshot(state, now=resolved_now, window_seconds=resolved_window)


def _categorize_endpoint(url: str) -> str:
    text = str(url or "")
    if "/x/web-interface/nav" in text:
        return "nav"
    if "feed/space" in text or ("/x/polymer/" in text and "space" in text):
        return "feed"
    if "web-dynamic/v1/opus/detail" in text or "bilibili.com/opus/" in text:
        return "opus"
    if "web-dynamic/v1/detail" in text:
        return "detail"
    if "lottery_notice" in text or "lottery_svr" in text:
        return "notice"
    return "other"


def reserve_bilibili_http_attempt(url: str, *, now: int | None = None) -> None:
    """BilibiliClient 统一发送点调用；未处于受保护任务时 0 副作用。

    reserve 成功（额度已扣）后才计入本任务 attempt 统计；耗尽时该请求不发送，
    也不计入 attempt。
    """
    scope = _scope_ctx.get()
    if scope is None:
        return
    if not scope.enforce_limit:
        scope.note_attempt(_categorize_endpoint(url))
        return
    snapshot = reserve_bilibili_request(
        now=scope.now_fn() if now is None else int(now),
        limit=scope.limit,
        window_seconds=scope.window_seconds,
        profile_dir=scope.profile_dir,
    )
    scope.note_attempt(_categorize_endpoint(url))
    if not scope.window_started_at:
        scope.window_started_at = snapshot.window_started_at
    elif scope.window_started_at != snapshot.window_started_at and not scope.rolled:
        # 新窗口自动建立（到期恢复）：记录一次，供任务层输出恢复日志。
        scope.rolled = True
