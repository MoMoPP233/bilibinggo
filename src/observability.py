"""轻量任务观测上下文。

只记录计数与耗时，不保存 URL 参数、响应正文、Cookie 或任何凭据。
ContextVar 让主线程与显式绑定的流水线 worker 共享同一份 Profile-local 指标。
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from typing import Callable, Iterator


_CURRENT: contextvars.ContextVar["JobMetrics | None"] = contextvars.ContextVar(
    "binggo_job_metrics", default=None
)


@dataclass
class JobMetrics:
    """单次任务内存指标；仅在任务结果/结构化日志中输出聚合值。"""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stage_started: dict[str, float] = field(default_factory=dict, repr=False)
    _stage_ms: Counter[str] = field(default_factory=Counter, repr=False)
    _request_count: int = 0
    _request_ms: int = 0
    _request_by_endpoint: Counter[str] = field(default_factory=Counter, repr=False)
    _request_by_kind: dict[str, list[int]] = field(default_factory=dict, repr=False)
    _bili_rps_effective: float = 0.0
    _bili_rps_source: str = ""
    _bili_rate_limit_wait_count: int = 0
    _bili_rate_limit_wait_ms: float = 0.0
    _bili_rate_limit_wait_max_ms: float = 0.0
    _bili_client_lock_wait_count: int = 0
    _bili_client_lock_wait_ms: float = 0.0
    _bili_client_lock_wait_max_ms: float = 0.0
    _bili_retry_wait_count: int = 0
    _bili_retry_wait_ms: float = 0.0
    _llm_calls: int = 0
    _llm_ms: int = 0
    _llm_cache_hits: int = 0
    _llm_wait_count: int = 0
    _llm_wait_ms: float = 0.0

    def stage_start(self, stage: str) -> None:
        name = str(stage or "").strip()
        if name:
            with self._lock:
                self._stage_started[name] = time.perf_counter()

    def stage_end(self, stage: str) -> int:
        name = str(stage or "").strip()
        if not name:
            return 0
        with self._lock:
            started = self._stage_started.pop(name, None)
            if started is None:
                return 0
            elapsed = max(0, int((time.perf_counter() - started) * 1000))
            self._stage_ms[name] += elapsed
            return elapsed

    def record_stage_duration(self, stage: str, duration_ms: int | float) -> None:
        name = str(stage or "").strip()
        if name:
            with self._lock:
                self._stage_ms[name] += max(0, int(duration_ms))

    def record_bilibili_request(
        self,
        url: str,
        duration_ms: int | float,
        *,
        kind: str | None = None,
    ) -> None:
        parsed = urlsplit(str(url))
        endpoint = parsed.path or "/"
        safe_kind = str(kind or "").strip().lower()
        if safe_kind not in {"detail", "notice", "body"}:
            safe_kind = "other"
        elapsed = max(0, int(duration_ms))
        with self._lock:
            self._request_count += 1
            self._request_ms += elapsed
            self._request_by_endpoint[endpoint] += 1
            bucket = self._request_by_kind.setdefault(safe_kind, [0, 0])
            bucket[0] += 1
            bucket[1] += elapsed

    def record_llm_call(self, duration_ms: int) -> None:
        with self._lock:
            self._llm_calls += 1
            self._llm_ms += max(0, int(duration_ms))

    def record_llm_cache_hit(self) -> None:
        with self._lock:
            self._llm_cache_hits += 1

    def record_bilibili_rate_limit_config(self, rps: float, source: str) -> None:
        with self._lock:
            self._bili_rps_effective = max(0.0, float(rps))
            self._bili_rps_source = str(source or "default")

    def record_bilibili_rate_limit_wait(self, duration_ms: int | float) -> None:
        elapsed = max(0.0, float(duration_ms))
        with self._lock:
            self._bili_rate_limit_wait_count += 1
            self._bili_rate_limit_wait_ms += elapsed
            self._bili_rate_limit_wait_max_ms = max(
                self._bili_rate_limit_wait_max_ms, elapsed
            )

    def record_bilibili_client_lock_wait(self, duration_ms: int | float) -> None:
        elapsed = max(0.0, float(duration_ms))
        with self._lock:
            self._bili_client_lock_wait_count += 1
            self._bili_client_lock_wait_ms += elapsed
            self._bili_client_lock_wait_max_ms = max(
                self._bili_client_lock_wait_max_ms, elapsed
            )

    def record_bilibili_retry_wait(self, duration_ms: int | float) -> None:
        elapsed = max(0.0, float(duration_ms))
        with self._lock:
            self._bili_retry_wait_count += 1
            self._bili_retry_wait_ms += elapsed

    def record_llm_wait(self, duration_ms: int | float) -> None:
        elapsed = max(0.0, float(duration_ms))
        with self._lock:
            self._llm_wait_count += 1
            self._llm_wait_ms += elapsed

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            stage_ms = dict(self._stage_ms)
            endpoints = dict(self._request_by_endpoint)
            request_by_kind = {
                kind: {"count": values[0], "ms": values[1]}
                for kind, values in self._request_by_kind.items()
            }
            detail = request_by_kind.get("detail", {"count": 0, "ms": 0})
            notice = request_by_kind.get("notice", {"count": 0, "ms": 0})
            body = request_by_kind.get("body", {"count": 0, "ms": 0})
            return {
                "stage_ms": stage_ms,
                "feed_fetch_ms": int(stage_ms.get("Feed获取", 0)),
                "classification_ms": int(stage_ms.get("分类中", 0)),
                "classification_stage_ms": int(stage_ms.get("分类阶段", 0)),
                "pipeline_ms": int(stage_ms.get("Pipeline总耗时", 0)),
                "database_save_ms": int(stage_ms.get("数据库保存", 0)),
                "bilibili_request_count": self._request_count,
                "bilibili_request_ms": self._request_ms,
                "bilibili_request_by_endpoint": endpoints,
                "bili_rps_effective": self._bili_rps_effective,
                "bili_rps_source": self._bili_rps_source,
                "bili_rate_limit_wait_count": self._bili_rate_limit_wait_count,
                "bili_rate_limit_wait_ms": round(self._bili_rate_limit_wait_ms),
                "bili_rate_limit_wait_max_ms": round(
                    self._bili_rate_limit_wait_max_ms
                ),
                "bili_client_lock_wait_count": self._bili_client_lock_wait_count,
                "bili_client_lock_wait_ms": round(self._bili_client_lock_wait_ms),
                "bili_client_lock_wait_max_ms": round(
                    self._bili_client_lock_wait_max_ms
                ),
                "bili_retry_wait_count": self._bili_retry_wait_count,
                "bili_retry_wait_ms": round(self._bili_retry_wait_ms),
                "requests": {"detail": detail, "notice": notice, "body": body},
                "detail_request_count": int(detail["count"]),
                "detail_request_ms": int(detail["ms"]),
                "notice_request_count": int(notice["count"]),
                "notice_request_ms": int(notice["ms"]),
                "body_request_count": int(body["count"]),
                "body_request_ms": int(body["ms"]),
                "llm_call_count": self._llm_calls,
                "llm_call_ms": self._llm_ms,
                "llm_cache_hits": self._llm_cache_hits,
                "llm_cache_hit_count": self._llm_cache_hits,
                "llm_wait_count": self._llm_wait_count,
                "llm_wait_ms": round(self._llm_wait_ms),
                "llm": {
                    "calls": self._llm_calls,
                    "cache_hits": self._llm_cache_hits,
                    "ms": self._llm_ms,
                },
            }


def request_kind(url: str) -> str:
    """将安全的 endpoint path 归入公共性能阶段，不保留查询参数。"""
    parsed = urlsplit(str(url))
    path = parsed.path or "/"
    if path == "/lottery_svr/v1/lottery_svr/lottery_notice":
        return "notice"
    if path in {
        "/x/polymer/web-dynamic/v1/detail",
        "/x/polymer/web-dynamic/v1/opus/detail",
    }:
        return "detail"
    if parsed.netloc == "www.bilibili.com" and (
        path.startswith("/opus/") or path.startswith("/dynamic/")
    ):
        return "body"
    return "other"


def performance_metrics(snapshot: dict[str, object]) -> dict[str, int | float | str]:
    """将内部观测快照转换成稳定、面向 Job 结果的扁平契约。"""
    requests = snapshot.get("requests")
    request_groups = requests if isinstance(requests, dict) else {}
    detail = request_groups.get("detail")
    notice = request_groups.get("notice")
    body = request_groups.get("body")
    detail_group = detail if isinstance(detail, dict) else {}
    notice_group = notice if isinstance(notice, dict) else {}
    body_group = body if isinstance(body, dict) else {}
    llm = snapshot.get("llm")
    llm_group = llm if isinstance(llm, dict) else {}

    def value(source: dict, key: str, fallback: object = 0) -> int:
        try:
            return max(0, int(source.get(key, fallback) or 0))
        except (TypeError, ValueError):
            return 0

    def float_value(source: dict, key: str, fallback: object = 0.0) -> float:
        try:
            return max(0.0, float(source.get(key, fallback) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    return {
        "pipeline_duration_ms": value(snapshot, "pipeline_ms"),
        "feed_fetch_duration_ms": value(snapshot, "feed_fetch_ms"),
        "classify_duration_ms": value(
            snapshot,
            "classification_stage_ms",
            snapshot.get("classification_ms", 0),
        ),
        "bili_request_count": value(snapshot, "bilibili_request_count"),
        "bili_request_duration_ms": value(snapshot, "bilibili_request_ms"),
        "bili_rps_effective": float_value(snapshot, "bili_rps_effective"),
        "bili_rps_source": str(snapshot.get("bili_rps_source") or ""),
        "bili_rate_limit_wait_count": value(
            snapshot, "bili_rate_limit_wait_count"
        ),
        "bili_rate_limit_wait_ms": value(snapshot, "bili_rate_limit_wait_ms"),
        "bili_rate_limit_wait_max_ms": value(
            snapshot, "bili_rate_limit_wait_max_ms"
        ),
        "bili_client_lock_wait_count": value(
            snapshot, "bili_client_lock_wait_count"
        ),
        "bili_client_lock_wait_ms": value(snapshot, "bili_client_lock_wait_ms"),
        "bili_client_lock_wait_max_ms": value(
            snapshot, "bili_client_lock_wait_max_ms"
        ),
        "bili_retry_wait_count": value(snapshot, "bili_retry_wait_count"),
        "bili_retry_wait_ms": value(snapshot, "bili_retry_wait_ms"),
        "detail_requests": value(
            detail_group, "count", snapshot.get("detail_request_count", 0)
        ),
        "detail_duration_ms": value(
            detail_group, "ms", snapshot.get("detail_request_ms", 0)
        ),
        "notice_requests": value(
            notice_group, "count", snapshot.get("notice_request_count", 0)
        ),
        "notice_duration_ms": value(
            notice_group, "ms", snapshot.get("notice_request_ms", 0)
        ),
        "body_requests": value(
            body_group, "count", snapshot.get("body_request_count", 0)
        ),
        "body_duration_ms": value(
            body_group, "ms", snapshot.get("body_request_ms", 0)
        ),
        "llm_calls": value(
            llm_group, "calls", snapshot.get("llm_call_count", 0)
        ),
        "llm_cache_hits": value(
            llm_group, "cache_hits", snapshot.get("llm_cache_hits", 0)
        ),
        "llm_duration_ms": value(
            llm_group, "ms", snapshot.get("llm_call_ms", 0)
        ),
        "llm_wait_count": value(snapshot, "llm_wait_count"),
        "llm_wait_ms": value(snapshot, "llm_wait_ms"),
        "db_save_duration_ms": value(snapshot, "database_save_ms"),
    }


def format_performance_summary(metrics: dict[str, int | float | str]) -> str:
    """生成单行人类可读摘要；只在任务终态输出一次。"""

    def duration(milliseconds: int) -> str:
        value = max(0, int(milliseconds or 0))
        if value < 1000:
            return f"{value}ms"
        seconds = value // 1000
        if seconds < 60:
            return f"{seconds}s"
        minutes, remaining = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes}m{remaining:02d}s"
        hours, remaining_minutes = divmod(minutes, 60)
        return f"{hours}h{remaining_minutes:02d}m"

    return (
        "性能："
        f"Pipeline {duration(metrics.get('pipeline_duration_ms', 0))}；"
        f"Feed获取 {duration(metrics.get('feed_fetch_duration_ms', 0))}；"
        f"分类 {duration(metrics.get('classify_duration_ms', 0))}；"
        f"B站请求 {metrics.get('bili_request_count', 0)}次/"
        f"{duration(metrics.get('bili_request_duration_ms', 0))}；"
        f"限流 {float(metrics.get('bili_rps_effective', 0) or 0):g} RPS"
        f"（{metrics.get('bili_rps_source', '') or 'unknown'}），"
        f"令牌等待 {metrics.get('bili_rate_limit_wait_count', 0)}次/"
        f"{duration(metrics.get('bili_rate_limit_wait_ms', 0))}，最长 "
        f"{duration(metrics.get('bili_rate_limit_wait_max_ms', 0))}；"
        f"Client Lock {metrics.get('bili_client_lock_wait_count', 0)}次/"
        f"{duration(metrics.get('bili_client_lock_wait_ms', 0))}，最长 "
        f"{duration(metrics.get('bili_client_lock_wait_max_ms', 0))}；"
        f"Retry等待 {metrics.get('bili_retry_wait_count', 0)}次/"
        f"{duration(metrics.get('bili_retry_wait_ms', 0))}；"
        f"Detail {metrics.get('detail_requests', 0)}次/"
        f"{duration(metrics.get('detail_duration_ms', 0))}；"
        f"Notice {metrics.get('notice_requests', 0)}次/"
        f"{duration(metrics.get('notice_duration_ms', 0))}；"
        f"正文 {metrics.get('body_requests', 0)}次/"
        f"{duration(metrics.get('body_duration_ms', 0))}；"
        f"LLM {metrics.get('llm_calls', 0)}次/"
        f"{duration(metrics.get('llm_duration_ms', 0))}"
        f"（缓存命中 {metrics.get('llm_cache_hits', 0)}，等待 "
        f"{metrics.get('llm_wait_count', 0)}次/"
        f"{duration(metrics.get('llm_wait_ms', 0))}）；"
        f"DB {duration(metrics.get('db_save_duration_ms', 0))}"
    )


def sleep_with_bili_retry_metrics(
    delay_seconds: float,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """执行既有 B 站退避，并把真实 wall time 聚合到当前 Job。"""
    started = time.perf_counter()
    try:
        sleeper(delay_seconds)
    finally:
        metrics = current_metrics()
        if metrics is not None:
            metrics.record_bilibili_retry_wait(
                (time.perf_counter() - started) * 1000
            )


def current_metrics() -> JobMetrics | None:
    return _CURRENT.get()


@contextlib.contextmanager
def metrics_context(metrics: JobMetrics | None = None) -> Iterator[JobMetrics]:
    active = metrics or JobMetrics()
    token = _CURRENT.set(active)
    try:
        yield active
    finally:
        _CURRENT.reset(token)


@contextlib.contextmanager
def bind_metrics(metrics: JobMetrics | None) -> Iterator[None]:
    token = _CURRENT.set(metrics)
    try:
        yield
    finally:
        _CURRENT.reset(token)
