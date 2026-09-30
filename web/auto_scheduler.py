"""定时点击调度器：只向 JobRunner 投递意图，撞车即停，绝不 cancel。

V9：自动 slot 跨进程防重复。同一 Profile 的已消费 logical slot 会持久化到
per-Profile scheduler_slots.json；进程重启后相同 slot 不再二次投递
（fail-closed：宁可漏跑，绝不重复远程）。手动 Job 不经过本状态。
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from src.app_logging import get_logger
from src.platform_risk import risk_code_of
from src.profile_writer_lease import _ProfileWriterLease
from src.restart_control import restart_control
from web.auto_config import (
    ACTION_LABELS,
    ALLOWED_CLICK_ACTIONS,
    AUTO_REMOTE_RISK_COOLDOWN_SECONDS,
    FOLLOWING_FEED_SCAN_HOUR_INTERVAL,
    FOLLOWING_FEED_SCAN_HOUR_OFFSET,
    FOLLOWING_FEED_SCAN_MINUTE,
    JOB_POLL_INTERVAL_SEC,
    JOB_POLL_TIMEOUT_SEC,
    MIN_REMOTE_STAGE_GAP_SECONDS,
    REFRESH_HOURS,
    TRIPLE_DELAY_MIN_SECONDS,
    TRIPLE_DELAY_MAX_SECONDS,
    WINNER_SCAN_HOURS,
    WINNER_SCAN_MINUTE,
)
from web.auto_remote_state import (
    clear_expired_risk_pause,
    matches_platform_risk,
    record_auto_remote_risk,
    risk_pause_state,
)
from web.event_hub import event_hub
from web.job_runner import JobRunner, runner
from web.user_messages import friendly_error

logger = get_logger("auto")
# 固定 UTC+8，与 lottery_time / forward_parser 一致；避免 Windows 打包缺 tzdata 时启动失败
CN_TZ = timezone(timedelta(hours=8))
SchedulerState = Literal["idle", "running", "stopped", "fatal"]
STATE_LABELS = {
    "idle": "尚未启动",
    "running": "调度运行中",
    "stopped": "已停止",
    "fatal": "已停机",
}
REFRESH_STEPS = (
    {"action": "refresh_all", "label": "一键更新活动链接"},
    {"action": "refresh_watch", "label": "更新监控用户动态"},
    {"action": "refresh_status", "label": "刷新任务状态"},
)
_AUTO_SNAPSHOT_MIN_INTERVAL_SEC = 0.5
_AUTO_SNAPSHOT_LOG_LIMIT = 100

_SLOT_STATE_FILE = "scheduler_slots.json"
_SLOT_STATE_MAX_TASKS = 8
_TRIPLE_NEXT_RUN = "triple_next_run"
# Kept in the retention set so pre-V67 state remains inert and readable during
# ordinary slot writes. No scheduler code reads or claims these legacy values.
_LEGACY_CLEANUP_RESUME_KEYS = frozenset(
    {"cleanup_budget_resume", "cleanup_budget_resume_claim"}
)
_slot_state_lock = threading.Lock()
_SLOT_STATE_LOCK_FILE = "scheduler_slots.lock"
_SLOT_STATE_LOCK_WAIT_SECONDS = 5.0


class SlotStateError(RuntimeError):
    """自动调度槽位状态不可用（损坏 / 无法写入）。

    Fail-closed：遇到本异常时调度器不得再启动任何自动远程 Job。
    """


def _slot_state_path() -> Any:
    from src.data_paths import get_profile_dir

    return get_profile_dir() / _SLOT_STATE_FILE


@contextmanager
def _slot_state_process_lock(path: Any):
    """持有 Profile-local OS 锁，仅覆盖 slot 状态的短暂 RMW 临界区。

    复用 ProfileWriterLease 的跨平台 msvcrt/fcntl 原语，但不取得业务
    ProfileWriterLease；锁文件独立于 ``binggo.writer.lock``，因此不会改变
    任何 JobRunner 的写入互斥语义。
    """
    lock_path = path.parent / _SLOT_STATE_LOCK_FILE
    lease = _ProfileWriterLease(lock_path, owner="scheduler-slot-state")
    deadline = time.monotonic() + _SLOT_STATE_LOCK_WAIT_SECONDS
    while not lease.acquire():
        if time.monotonic() >= deadline:
            raise SlotStateError("调度槽位跨进程锁获取失败，已停止自动远程任务")
        # 等待发生在锁尚未取得时；持锁临界区本身不包含 sleep 或远程工作。
        time.sleep(0.01)
    try:
        yield
    finally:
        lease.release()


def _read_slot_state(path: Any) -> dict[str, str]:
    """读取已消费槽位。文件不存在 = 首次运行（空历史）；存在但损坏 = 明确异常。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SlotStateError(f"调度槽位状态读取失败：{exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SlotStateError("调度槽位状态文件损坏，已停止自动远程任务（请人工修复后重启）") from exc
    if not isinstance(data, dict):
        raise SlotStateError("调度槽位状态文件结构非法，已停止自动远程任务")
    cleaned: dict[str, str] = {}
    if _TRIPLE_NEXT_RUN in data and not isinstance(data[_TRIPLE_NEXT_RUN], str):
        raise SlotStateError("自动参与 next_run 结构非法，已停止自动调度")
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str):
            cleaned[key] = value
    return cleaned


def _write_slot_state_atomic(path: Any, state: dict[str, str]) -> None:
    """同目录唯一临时文件 → write → flush → fsync → close → os.replace（Windows 兼容）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(
        prefix=f".{_SLOT_STATE_FILE}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, ensure_ascii=False, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _persist_slot_claimed(task: str, slot: str) -> bool:
    """原子 check+claim：返回 True 表示本次成功消费该 slot（可继续自动投递）。

    返回 False 表示该 (task, slot) 已被（本进程或历史进程）消费，不得再次投递。
    文件损坏/不可写时抛 SlotStateError，由调用方 fail closed（0 remote）。
    """
    path = _slot_state_path()
    with _slot_state_lock:
        with _slot_state_process_lock(path):
            state = _read_slot_state(path)
            if state.get(task) == slot:
                return False
            state[task] = slot
            if len(state) > _SLOT_STATE_MAX_TASKS:
                # 有界：只保留最近消费的少量 task；key 天然含 task 身份，不会错判当前 slot。
                for key in sorted(state):
                    if key in {
                        _TRIPLE_NEXT_RUN,
                        *_LEGACY_CLEANUP_RESUME_KEYS,
                    }:
                        continue
                    del state[key]
                    if len(state) <= _SLOT_STATE_MAX_TASKS:
                        break
            try:
                _write_slot_state_atomic(path, state)
            except OSError as exc:
                raise SlotStateError(f'调度槽位状态写入失败：{exc}') from exc
    return True


def _reset_slot_state_for_tests() -> None:
    """仅测试用：清除 slot 状态文件，避免跨用例污染。"""
    with _slot_state_lock:
        path = _slot_state_path()
        with _slot_state_process_lock(path):
            try:
                path.unlink()
            except OSError:
                pass


def _log_slot_skip(scheduler: "AutoScheduler", task: str, slot: str) -> None:
    scheduler._log("info", f"自动任务槽位 {slot}（{task}）已被消费（含历史进程），本轮跳过")


def _stale_or_slot_guard(scheduler: "AutoScheduler", task: str, slot: str) -> bool:
    """阶段入口：stale 或槽位已消费时返回 False（调用方不得投递任何远程任务）。"""
    if scheduler._is_stale():
        return False
    try:
        return _persist_slot_claimed(task, slot)
    except SlotStateError as exc:
        scheduler._log("error", f"调度槽位状态不可用，自动任务（{slot}）fail-closed 跳过：{exc}")
        return False


class CollisionError(RuntimeError):
    """抽奖端已有任务在跑，自动调度必须立即停机。"""


@dataclass(frozen=True)
class RemoteGateDecision:
    """自动远程任务门禁的结构化结果（V24）。

    kind: ok / cooldown / invalid_state / gap
    普通业务任务异常永远不进入 fatal；只有门禁跳过的结构化原因用于状态与日志。
    """

    allowed: bool
    kind: str
    message: str
    remaining_minutes: int = 0
    resume_at: int | None = None


@dataclass
class LogEntry:
    ts: str
    level: str
    message: str


@dataclass
class SchedulerStatus:
    state: SchedulerState = "idle"
    message: str = "尚未启动"
    started_at: str | None = None
    stopped_at: str | None = None
    fatal_error: str | None = None
    last_tick_at: str | None = None
    current_phase: str = ""
    next_hint: str = ""
    last_click: dict[str, Any] | None = None
    refresh_batch_key: str | None = None
    triple_slot_key: str | None = None
    refresh_pipeline: dict[str, Any] = field(default_factory=dict)
    next_slot: dict[str, Any] | None = None
    job_probe: dict[str, Any] | None = None
    server_now: str = ""
    server_now_unix: int = 0
    logs: list[dict[str, str]] = field(default_factory=list)
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "state_label": STATE_LABELS.get(self.state, self.state),
            "message": self.message,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "fatal_error": self.fatal_error,
            "last_tick_at": self.last_tick_at,
            "current_phase": self.current_phase,
            "next_hint": self.next_hint,
            "next_slot": self.next_slot,
            "last_click": self.last_click,
            "refresh_batch_key": self.refresh_batch_key,
            "triple_slot_key": self.triple_slot_key,
            "refresh_pipeline": self.refresh_pipeline or _idle_pipeline(),
            "job_probe": self.job_probe,
            "server_now": self.server_now,
            "server_now_unix": self.server_now_unix,
            "logs": list(self.logs),
            "tasks": {key: dict(value) for key, value in self.tasks.items()},
            "next_task": next_auto_task(),
            "schedule": {
                "refresh_hours": sorted(REFRESH_HOURS),
                "triple_minutes": [],  # 兼容旧状态消费者；不再有固定分钟槽。
                "triple_delay_seconds": [TRIPLE_DELAY_MIN_SECONDS, TRIPLE_DELAY_MAX_SECONDS],
                "actions": [
                    {"action": key, "label": ACTION_LABELS[key]}
                    for key in ("refresh_all", "refresh_watch", "refresh_status", "participate_triple")
                ],
            },
        }


class AutoScheduler:
    def __init__(
        self, job_runner: JobRunner | None = None, *,
        clock: Callable[[], datetime] | None = None,
        delay_sampler: Callable[[int, int], int] | None = None,
    ) -> None:
        self._runner = job_runner or runner
        self._clock = clock or (lambda: datetime.now(CN_TZ))
        self._delay_sampler = delay_sampler or random.randint
        self._triple_tick_lock = threading.Lock()
        self._lock = threading.Lock()
        # 每代调度器拥有自己独立的 stop Event；新代 start 绝不 clear 旧代 Event。
        # 线程运行期间所有停止判定都绑定“自己的代际上下文”，避免旧代被唤醒后继续调度。
        self._generation = 0
        self._stop_event = threading.Event()
        self._ctx = threading.local()
        self._thread: threading.Thread | None = None
        self._logs: deque[LogEntry] = deque(maxlen=200)
        self._status = SchedulerStatus(
            refresh_pipeline=_idle_pipeline(),
            tasks={
                key: {
                    "label": label,
                    "status": "waiting",
                    "last_started_at": None,
                    "last_finished_at": None,
                    "last_result": "尚未执行",
                    "next_run_at": None,
                }
                for key, label in {
                    "refresh": "刷新批次",
                    "winner": "中奖深探",
                    "following": "关注补漏",
                    "participate": "自动参与",
                }.items()
            },
        )
        self._done_refresh: set[str] = set()
        self._done_triple: set[str] = set()
        self._done_winner: set[str] = set()
        self._done_following: set[str] = set()
        self._snapshot_timer: threading.Timer | None = None
        self._last_snapshot_mono = 0.0
        self._snapshot_pending = False
        # 上一次“自动远程大任务”结束的单调时间；用于不同大任务之间 60s 错峰。
        self._last_remote_stage_finished_mono = 0.0

    def _active_stop_event(self) -> threading.Event:
        """当前线程绑定的 stop Event；直接调用（无线程上下文，如单测）回退到实例当前 Event。"""
        event = getattr(self._ctx, "stop_event", None)
        return event if event is not None else self._stop_event

    def _context_generation(self) -> int:
        gen = getattr(self._ctx, "generation", None)
        return gen if gen is not None else self._generation

    def _is_stale(self) -> bool:
        """本线程（代际上下文）是否已失效：自己被 stop，或已被新一代 start 取代。"""
        generation = self._context_generation()
        with self._lock:
            if generation != self._generation:
                return True
        return self._active_stop_event().is_set()

    def get_status(self) -> dict[str, Any]:
        now = datetime.now(CN_TZ)
        slot = _next_slot(now)
        probe = _probe_job(self._runner)
        next_runs = _next_runs_by_task(now)
        risk_view = risk_pause_state()
        with self._lock:
            self._status.logs = [
                {"ts": e.ts, "level": e.level, "message": e.message} for e in list(self._logs)[-100:]
            ]
            self._status.next_slot = slot
            self._status.next_hint = slot.get("hint") or ""
            self._status.job_probe = probe
            self._status.server_now = _now_iso()
            self._status.server_now_unix = int(now.timestamp())
            for key, task in self._status.tasks.items():
                task["next_run_at"] = next_runs.get(key)
                if risk_view.get("paused") and task.get("status") != "running":
                    task["status"] = "risk_paused"
                    task["last_result"] = str(
                        risk_view.get("reason") or risk_view.get("code") or "平台风控冷却中"
                    )
                    task["resume_at"] = risk_view.get("paused_until")
                elif task.get("status") == "risk_paused" and not risk_view.get("paused"):
                    task["status"] = "waiting"
                    task.pop("resume_at", None)
            if not self._status.refresh_pipeline:
                self._status.refresh_pipeline = _idle_pipeline()
            return self._status.to_dict()

    def start(self) -> dict[str, Any]:
        # 在 restart gate 内先把调度器状态变为 running；Profile 切换随后
        # 取得同一 gate 时一定能看到该状态，不能在 idle 检查后抢跑。
        with restart_control.new_work_guard():
            with self._lock:
                if self._thread and self._thread.is_alive() and self._status.state == "running":
                    raise RuntimeError("调度器已在运行")
                if self._status.state == "fatal":
                    self._status.fatal_error = None
                # 新一代：自建独立 stop Event + generation 递增。旧代线程持有自己的
                # Event 引用与代际，start 永不 clear 旧 Event，旧代不会被再次唤醒。
                generation = self._generation + 1
                self._generation = generation
                stop_event = threading.Event()
                self._stop_event = stop_event
                self._status.state = "running"
                self._status.message = "调度器运行中"
                self._status.started_at = _now_iso()
                self._status.stopped_at = None
                self._status.fatal_error = None
                self._status.current_phase = "等待下一刻度"
                self._status.refresh_pipeline = _idle_pipeline()
                self._thread = threading.Thread(
                    target=self._loop,
                    args=(generation, stop_event),
                    name="binggo-auto-scheduler",
                    daemon=True,
                )
                self._thread.start()
        self._log("info", "调度器已启动（按计划投递自动任务：刷新 / 参与 / 关注补漏 / 中奖深探）")
        self._schedule_auto_snapshot(force=True)
        return self.get_status()

    def stop(self, *, reason: str = "用户停止") -> dict[str, Any]:
        """只停止本调度器，绝不取消抽奖端任务；不 join，立即返回。"""
        with self._lock:
            # 先 set 当前代的 stop Event，再落状态；旧代线程会因自己绑定的
            # Event 被 set 而退出，即使此刻已被新一代取代也不影响新代。
            self._active_stop_event().set()
            if self._status.state == "running":
                self._status.state = "stopped"
                self._status.message = reason
                self._status.stopped_at = _now_iso()
                self._status.current_phase = ""
                self._status.refresh_pipeline = _idle_pipeline()
                for task in self._status.tasks.values():
                    if task.get("status") == "running":
                        task["status"] = "waiting"
                        task["last_finished_at"] = self._status.stopped_at
                        task["last_result"] = "调度已停止；已投递 Job 不会被取消"
        self._log("warn", f"调度器已停止：{reason}")
        self._schedule_auto_snapshot(force=True)
        return self.get_status()

    def _fatal(self, message: str) -> None:
        # 旧代线程晚到的致命错误不得停机/污染新一代调度器。
        if self._is_stale():
            self._log("warn", f"调度器已换代，忽略旧代致命信号：{message}")
            return
        self._active_stop_event().set()
        with self._lock:
            self._status.state = "fatal"
            self._status.message = "因任务撞车或严重错误已停机"
            self._status.fatal_error = message
            self._status.stopped_at = _now_iso()
            self._status.current_phase = "已停机"
            self._status.refresh_pipeline = {
                **(self._status.refresh_pipeline or _idle_pipeline()),
                "active": False,
            }
        self._log("error", f"致命停机：{message}")
        self._schedule_auto_snapshot(force=True)

    def _log(self, level: str, message: str) -> None:
        from web.user_messages import sanitize_log

        safe_message = sanitize_log(str(message or "")) or "状态已更新"
        safe_message = re.sub(
            r"(https?://[^\s?]+)\?[^\s]+",
            r"\1?[查询参数已隐藏]",
            safe_message,
            flags=re.IGNORECASE,
        )
        entry = LogEntry(ts=_now_iso(), level=level, message=safe_message)
        with self._lock:
            self._logs.append(entry)
        self._publish_auto_log(entry)

    def _set_task_status(self, task: str, status: str, result: object = "") -> None:
        """只更新现有 Scheduler 的观察状态；不投递 Job、不触发远程请求。"""
        if self._is_stale():
            return
        now = _now_iso()
        with self._lock:
            entry = self._status.tasks.setdefault(
                task,
                {
                    "label": task,
                    "status": "waiting",
                    "last_started_at": None,
                    "last_finished_at": None,
                    "last_result": "尚未执行",
                    "next_run_at": None,
                },
            )
            entry["status"] = status
            if status == "running":
                entry["last_started_at"] = now
                entry["last_finished_at"] = None
            elif status in {
                "completed",
                "skipped_busy",
                "risk_paused",
                "error",
                "skipped_by_platform_cooldown",
            }:
                entry["last_finished_at"] = now
            if result:
                from web.user_messages import sanitize_log

                entry["last_result"] = sanitize_log(str(result)) or status
        self._schedule_auto_snapshot(force=True)

    def _set_phase(self, phase: str, message: str | None = None) -> None:
        # 旧代线程的过期阶段更新不得污染新一代调度器的状态展示。
        if self._is_stale():
            return
        with self._lock:
            prev = (
                self._status.current_phase,
                self._status.message,
                (self._status.next_slot or {}).get("due_at"),
            )
            self._status.current_phase = phase
            if message is not None:
                self._status.message = message
            slot = _next_slot(datetime.now(CN_TZ))
            self._status.next_slot = slot
            self._status.next_hint = slot.get("hint") or ""
            changed = prev != (
                self._status.current_phase,
                self._status.message,
                (self._status.next_slot or {}).get("due_at"),
            )
        if changed:
            self._schedule_auto_snapshot(force=False)

    def _pause_all_auto_remote(self, stage_label: str, message: object) -> None:
        # 旧代线程晚到的风控判断不得把新一代调度器打进全局冷却。
        if self._is_stale():
            self._log("warn", f"调度器已换代，忽略旧代风控信号：{stage_label}")
            return
        state = record_auto_remote_risk(
            trigger_stage=stage_label,
            reason=str(message or "") or type(message).__name__,
            code=str(risk_code_of(message) or ""),
        )
        until = state.get("paused_until")
        until_text = (
            datetime.fromtimestamp(int(until), CN_TZ).strftime("%H:%M")
            if until
            else "稍后"
        )
        minutes = AUTO_REMOTE_RISK_COOLDOWN_SECONDS // 60
        code = str(state.get("code") or "").strip()
        code_text = f"：{code}" if code else ""
        self._log(
            "error",
            f"检测到 Bilibili 平台风控{code_text}；当前远程任务已停止，Scheduler 继续运行；"
            f"进入 {minutes} 分钟冷却，预计 {until_text} 恢复：{message}",
        )
        self._set_phase(
            "等待下一刻度",
            f"自动远程任务因平台风控进入 {minutes} 分钟冷却（预计 {until_text} 恢复），"
            "冷却结束后等下一次正常调度。",
        )

    def _remote_gate(self) -> RemoteGateDecision:
        """自动远程任务门禁（0 远程只读判断，结构化原因）。

        仅禁止新的自动远程任务：风控冷却（含状态文件损坏的 Fail-Closed）或最小错峰间隔。
        冷却到期后本地清除，只恢复“等待下一次正常调度”的资格，绝不主动探测或补跑。
        """
        view = risk_pause_state()
        if view.get("paused"):
            status = str(view.get("state_status") or "ok")
            if status in ("invalid", "unreadable"):
                return RemoteGateDecision(
                    False,
                    "invalid_state",
                    "自动远程风控状态文件损坏/不可读，fail-closed：本轮远程任务跳过且不联网，"
                    "请检查 auto_remote_state.json。",
                )
            reason = view.get("reason") or view.get("code") or "平台风控"
            until = view.get("paused_until")
            resume_at = int(until) if until else None
            remaining = max(0, (resume_at or 0) - int(time.time())) if resume_at else 0
            minutes = (remaining + 59) // 60
            until_text = (
                datetime.fromtimestamp(resume_at, CN_TZ).strftime("%H:%M")
                if resume_at
                else "稍后"
            )
            return RemoteGateDecision(
                False,
                "cooldown",
                f"平台风控冷却中（{reason}）：本轮远程任务跳过；"
                f"剩余约 {minutes} 分钟，预计 {until_text} 恢复。",
                remaining_minutes=minutes,
                resume_at=resume_at,
            )
        clear_expired_risk_pause()
        now_mono = time.monotonic()
        if (
            self._last_remote_stage_finished_mono > 0
            and now_mono - self._last_remote_stage_finished_mono
            < MIN_REMOTE_STAGE_GAP_SECONDS
        ):
            return RemoteGateDecision(
                False,
                "gap",
                "上一个自动远程大任务刚结束，尚未达到最小错峰间隔，本轮远程任务跳过。",
            )
        return RemoteGateDecision(True, "ok", "")

    def _remote_stage_allowed(self) -> bool:
        """兼容入口：是否允许启动下一个自动远程大任务（0 远程只读判断）。"""
        return self._remote_gate().allowed

    def _log_remote_gate_skip(self, task: str, gate: RemoteGateDecision) -> None:
        """同一 slot 只记录一次主要跳过日志。"""
        if gate.kind in ("cooldown", "invalid_state"):
            self._log("warn", f"{gate.message}（{task}）")
        elif gate.kind == "gap":
            self._log("info", gate.message)

    def _skip_remote_slot(
        self,
        task: str,
        gate: RemoteGateDecision,
        *,
        done: set[str],
        key: str,
        phase_message: str | None = None,
    ) -> None:
        """cooldown/gap 时消费 slot 并写入结构化任务状态；Scheduler 保持运行。"""
        self._log_remote_gate_skip(task, gate)
        done.add(key)
        status = (
            "skipped_by_platform_cooldown"
            if gate.kind in ("cooldown", "invalid_state")
            else "skipped_busy"
        )
        self._set_task_status(task, status, gate.message)
        if phase_message:
            self._set_phase("等待下一刻度", phase_message)
        else:
            self._set_phase("等待下一刻度", gate.message)

    def _mark_remote_stage_finished(self) -> None:
        self._last_remote_stage_finished_mono = time.monotonic()

    def _publish_auto_log(self, entry: LogEntry) -> None:
        try:
            event_hub.publish(
                "auto.log",
                {
                    "level": entry.level,
                    "message": entry.message,
                    "log_ts": entry.ts,
                },
            )
        except Exception:
            logger.exception("发布 auto.log 失败")

    def _schedule_auto_snapshot(self, *, force: bool = False) -> None:
        """变更合并推送；fatal/stopped/启停 force 立即发。"""
        with self._lock:
            if force:
                if self._snapshot_timer is not None:
                    self._snapshot_timer.cancel()
                    self._snapshot_timer = None
                self._snapshot_pending = False
                should_emit_now = True
            else:
                now = time.monotonic()
                elapsed = now - self._last_snapshot_mono
                if elapsed >= _AUTO_SNAPSHOT_MIN_INTERVAL_SEC:
                    should_emit_now = True
                    if self._snapshot_timer is not None:
                        self._snapshot_timer.cancel()
                        self._snapshot_timer = None
                    self._snapshot_pending = False
                else:
                    should_emit_now = False
                    self._snapshot_pending = True
                    if self._snapshot_timer is None:
                        delay = max(0.05, _AUTO_SNAPSHOT_MIN_INTERVAL_SEC - elapsed)

                        def _fire() -> None:
                            with self._lock:
                                self._snapshot_timer = None
                                if not self._snapshot_pending:
                                    return
                                self._snapshot_pending = False
                            self._emit_auto_snapshot()

                        self._snapshot_timer = threading.Timer(delay, _fire)
                        self._snapshot_timer.daemon = True
                        self._snapshot_timer.start()
        if should_emit_now:
            self._emit_auto_snapshot()

    def _emit_auto_snapshot(self) -> None:
        try:
            payload = self.get_status()
            logs = payload.get("logs")
            if isinstance(logs, list) and len(logs) > _AUTO_SNAPSHOT_LOG_LIMIT:
                payload = dict(payload)
                payload["logs"] = logs[-_AUTO_SNAPSHOT_LOG_LIMIT:]
            event_hub.publish("auto.snapshot", payload)
            with self._lock:
                self._last_snapshot_mono = time.monotonic()
        except Exception:
            logger.exception("发布 auto.snapshot 失败")

    def _set_pipeline(self, *, active: bool, step_index: int = -1, waiting: bool = False) -> None:
        # 旧代线程的过期流水线更新不得污染新一代调度器的展示。
        if self._is_stale():
            return
        steps = []
        for i, item in enumerate(REFRESH_STEPS):
            if not active or step_index < 0:
                status = "pending"
            elif i < step_index:
                status = "done"
            elif i == step_index:
                status = "waiting" if waiting else "active"
            else:
                status = "pending"
            steps.append({**item, "status": status, "index": i})
        pipeline = {
            "active": active,
            "step_index": step_index,
            "waiting": waiting,
            "steps": steps,
        }
        with self._lock:
            prev = self._status.refresh_pipeline
            if (
                prev
                and prev.get("active") == pipeline["active"]
                and prev.get("step_index") == pipeline["step_index"]
                and prev.get("waiting") == pipeline["waiting"]
            ):
                return
            self._status.refresh_pipeline = pipeline
        self._schedule_auto_snapshot(force=False)

    def _loop(self, generation: int, stop_event: threading.Event) -> None:
        # 绑定本线程所属代际：此后所有停止/换代判定都以这对上下文为准，
        # 旧代线程即使在新代启动后晚醒，也只会退出而不再调度。
        self._ctx.generation = generation
        self._ctx.stop_event = stop_event
        try:
            while not self._is_stale():
                now = self._clock()
                with self._lock:
                    self._status.last_tick_at = _now_iso()
                    slot = _next_slot(now)
                    self._status.next_slot = slot
                    self._status.next_hint = slot.get("hint") or ""

                if now.hour in REFRESH_HOURS and now.minute == 0:
                    key = f"{now:%Y-%m-%d-%H}"
                    if key not in self._done_refresh:
                        self._run_refresh_batch(key)
                        continue

                # V38: 中奖深检每天 23:55；仍仅当前分钟触发，错过不补跑。
                if now.minute == WINNER_SCAN_MINUTE and now.hour in WINNER_SCAN_HOURS:
                    key = f"winner-{now:%Y-%m-%d-%H}"
                    if key not in self._done_winner:
                        self._run_winner_scan(key)
                        continue

                if (
                    now.minute == FOLLOWING_FEED_SCAN_MINUTE
                    and now.hour % FOLLOWING_FEED_SCAN_HOUR_INTERVAL
                    == FOLLOWING_FEED_SCAN_HOUR_OFFSET
                ):
                    key = f"following-{now:%Y-%m-%d-%H}"
                    if key not in self._done_following:
                        self._run_following_scan(key)
                        continue

                self._tick_triple(now)

                if self._status.state == "running":
                    self._set_phase("等待下一刻度", "调度器运行中")
                stop_event.wait(1.0)
        except CollisionError as exc:
            # 兜底：业务 wrapper 已各自处理撞车；loop 级撞车说明调度器不变量被破坏。
            self._fatal(f"调度器内部任务撞车：{exc}")
        except Exception as exc:
            logger.exception("调度器内部未预期错误")
            self._fatal(f"调度器内部不可恢复错误：{friendly_error(exc)}")

    def _plan_triple(self, now: datetime, expected: str | None) -> None:
        """CAS 保存唯一下一轮；旧代无写权，状态读取绝不抽签。"""
        with self._lock:
            if self._context_generation() != self._generation or self._active_stop_event().is_set():
                return
            with _slot_state_lock:
                path = _slot_state_path()
                with _slot_state_process_lock(path):
                    state = _read_slot_state(path)
                    if state.get(_TRIPLE_NEXT_RUN) != expected:
                        return
                    delay = self._delay_sampler(TRIPLE_DELAY_MIN_SECONDS, TRIPLE_DELAY_MAX_SECONDS)
                    if type(delay) is not int or not TRIPLE_DELAY_MIN_SECONDS <= delay <= TRIPLE_DELAY_MAX_SECONDS:
                        raise SlotStateError("自动参与随机等待时间必须在 240～420 秒之间")
                    state[_TRIPLE_NEXT_RUN] = (now + timedelta(seconds=delay)).isoformat()
                    try:
                        _write_slot_state_atomic(path, state)
                    except OSError as exc:
                        raise SlotStateError(f"自动参与 next_run 写入失败：{exc}") from exc

    def _tick_triple(self, now: datetime) -> None:
        """仅执行已保存计划的到期分钟；错过/已消费只规划未来，绝不补跑。

        复用单线程等待、JobRunner 和 WriterLease。停止/等待超时留下已消费槽；
        新代等待仍在运行的 Job 结束后再规划，不从未完成的轮次起算间隔。
        """
        if self._is_stale() or not self._triple_tick_lock.acquire(blocking=False):
            return
        try:
            state = _read_slot_state(_slot_state_path())
            raw = state.get(_TRIPLE_NEXT_RUN)
            due = _triple_due(state)
            if due is None or state.get("triple") == f"jitter-{raw}":
                if not self._runner.is_running():
                    self._plan_triple(now, raw)
                return
            if now < due:
                return
            if now.replace(second=0, microsecond=0) > due.replace(second=0, microsecond=0):
                self._log("info", f"自动参与计划 {raw} 已错过，跳过并等待下一轮")
                self._plan_triple(now, raw)
                return
            key = f"jitter-{raw}"
            if _dedicated_minute(now):
                # 不抢占原有固定任务；本轮不补跑。
                if _stale_or_slot_guard(self, "triple", key):
                    self._log("info", "自动参与与固定任务同分钟，本轮跳过")
            else:
                self._run_triple_slot(key)
            if not self._is_stale() and not self._runner.is_running():
                self._plan_triple(self._clock(), raw)
        finally:
            self._triple_tick_lock.release()

    def _run_refresh_batch(self, key: str) -> None:
        if not _stale_or_slot_guard(self, "refresh", key):
            self._done_refresh.add(key)
            return
        # V24：远程门禁只拦截需要 Bilibili 的动作；本地 refresh_status 始终运行。
        gate = self._remote_gate()
        self._set_phase("刷新批次", f"开始刷新批次 {key}")
        self._set_task_status("refresh", "running", "刷新批次开始")
        self._log("info", f"刷新批次开始 {key}：一键更新 → 监控动态 → 刷新状态")
        self._set_pipeline(active=True, step_index=0, waiting=False)
        remote_actions = {"refresh_all", "refresh_watch"}
        actions = ("refresh_all", "refresh_watch", "refresh_status")
        remote_attempted = False
        remote_skipped = False
        failures: list[str] = []
        try:
            for index, action in enumerate(actions):
                # 停止/换代后：不再安排后续任何自动任务。
                if self._is_stale():
                    self._set_pipeline(active=False)
                    return
                if action in remote_actions and not gate.allowed:
                    remote_skipped = True
                    continue
                self._set_pipeline(active=True, step_index=index, waiting=False)
                try:
                    self._click_and_wait(action, pipeline_index=index)
                    if action in remote_actions:
                        remote_attempted = True
                except CollisionError:
                    # 已有 Job 在运行：本轮直接跳过，不 fatal、不补跑。
                    self._log(
                        "warn",
                        f"「{ACTION_LABELS.get(action, action)}」发现已有任务在运行，本轮跳过。",
                    )
                    self._done_refresh.add(key)
                    self._set_pipeline(active=False)
                    self._set_task_status("refresh", "skipped_busy", "本轮跳过（任务占用）")
                    return
                except Exception as exc:
                    if matches_platform_risk(exc):
                        self._pause_all_auto_remote(
                            f"refresh:{action}", exc
                        )
                        self._done_refresh.add(key)
                        self._set_pipeline(active=False)
                        self._set_task_status("refresh", "risk_paused", str(exc))
                        return
                    # V24：普通业务失败（网络/LLM/登录/timeout/预算）只结束当前动作，
                    # 绝不 fatal；Scheduler 继续运行，本地动作继续执行。
                    if action in remote_actions:
                        remote_attempted = True
                    label = ACTION_LABELS.get(action, action)
                    failures.append(f"{label}：{exc}")
                    self._log(
                        "error",
                        f"「{label}」当前任务失败：{exc}；"
                        "Scheduler 继续运行，将在下一正常调度时重试。",
                    )
            self._done_refresh.add(key)
            if remote_attempted:
                self._mark_remote_stage_finished()
            if failures:
                message = "；".join(failures)[:300]
                self._set_task_status("refresh", "error", message)
                self._set_phase("等待下一刻度", f"刷新批次结束（当前任务失败）：{message}")
                self._log("warn", f"刷新批次结束（当前任务失败）{key}：{message}")
            elif remote_skipped:
                # 远程动作被门禁拦截：记录一次结构化跳过；本地 refresh_status 已运行。
                self._log("warn", f"{gate.message}（refresh；本地状态已刷新）")
                self._set_task_status(
                    "refresh",
                    "skipped_by_platform_cooldown"
                    if gate.kind in ("cooldown", "invalid_state")
                    else "skipped_busy",
                    f"远程任务跳过，本地状态已刷新；{gate.message}",
                )
                self._set_phase(
                    "等待下一刻度",
                    f"刷新批次：远程任务跳过，本地状态已刷新（{gate.message}）",
                )
            else:
                with self._lock:
                    self._status.refresh_batch_key = key
                self._log("info", f"刷新批次完成 {key}")
                self._set_task_status("refresh", "completed", "刷新批次完成")
                self._set_phase("等待下一刻度", "刷新批次已完成")
        finally:
            self._set_pipeline(active=False)

    def _run_following_scan(self, key: str) -> None:
        if not _stale_or_slot_guard(self, "following", key):
            self._done_following.add(key)
            return
        gate = self._remote_gate()
        if not gate.allowed:
            self._skip_remote_slot("following", gate, done=self._done_following, key=key)
            return
        self._set_pipeline(active=False)
        self._set_phase("关注动态补漏", f"关注动态补漏 {key}")
        self._set_task_status("following", "running", "关注动态补漏开始")
        self._log("info", f"关注动态补漏刻度 {key}")
        try:
            self._click_and_wait("following_feed_scan")
            self._done_following.add(key)
            if self._is_stale():
                # 停止/换代：Job 自行自然结束，旧代不再推进阶段状态。
                return
            with self._lock:
                self._status.message = "关注动态补漏完成"
            self._mark_remote_stage_finished()
            self._set_task_status("following", "completed", "关注动态补漏完成")
            self._set_phase("等待下一刻度", "关注动态补漏完成")
        except CollisionError:
            self._log("warn", "关注动态补漏发现已有任务在运行，本轮跳过。")
            self._set_task_status("following", "skipped_busy", "本轮跳过（任务占用）")
            self._done_following.add(key)
        except Exception as exc:
            if matches_platform_risk(exc):
                self._pause_all_auto_remote("following_feed_scan", exc)
                self._set_task_status("following", "risk_paused", str(exc))
            else:
                # V24：普通业务失败（网络/LLM/登录/timeout/预算）只结束当前 Job。
                self._log(
                    "error",
                    f"关注动态补漏当前任务失败：{exc}；"
                    "Scheduler 继续运行，将在下一正常调度时重试。",
                )
                self._set_task_status("following", "error", str(exc))
            self._done_following.add(key)
            self._set_phase("等待下一刻度", f"关注动态补漏已跳过：{exc}")

    def _run_triple_slot(self, key: str) -> None:
        if not _stale_or_slot_guard(self, "triple", key):
            self._done_triple.add(key)
            return
        gate = self._remote_gate()
        if not gate.allowed:
            self._skip_remote_slot("triple", gate, done=self._done_triple, key=key)
            return
        self._set_pipeline(active=False)
        self._set_phase("三连参与", f"触发三连参与 {key}")
        self._set_task_status("participate", "running", "三连参与开始")
        self._log("info", f"三连参与刻度 {key}")
        try:
            from src.comment_probe import run_comment_recovery_probe

            probe = run_comment_recovery_probe()
            if probe.get("claimed"):
                self._done_triple.add(key)
                if self._is_stale():
                    return
                with self._lock:
                    self._status.triple_slot_key = key
                self._mark_remote_stage_finished()
                self._set_task_status("participate", "completed", "评论恢复探测完成")
                self._set_phase("等待下一刻度", "评论恢复探测完成，等待下一轮参与")
                return
            outcome = self._click_and_wait("participate_triple")
            self._done_triple.add(key)
            if self._is_stale():
                # 停止/换代：Job 自行自然结束，旧代不再推进阶段状态。
                return
            with self._lock:
                self._status.triple_slot_key = key
            self._mark_remote_stage_finished()
            if outcome and outcome.get("skipped"):
                msg = str(outcome.get("message") or "当前没有可参与活动，已跳过")
                self._log("info", f"三连参与已跳过：{msg}")
                self._set_task_status("participate", "completed", msg)
                self._set_phase("等待下一刻度", msg)
            else:
                self._set_task_status("participate", "completed", "三连参与完成")
                self._set_phase("等待下一刻度", "三连参与已完成")
        except CollisionError:
            self._log("warn", "三连参与发现已有任务在运行，本轮跳过。")
            self._set_task_status("participate", "skipped_busy", "本轮跳过（任务占用）")
            self._done_triple.add(key)
        except Exception as exc:
            if matches_platform_risk(exc):
                self._pause_all_auto_remote("participate_triple", exc)
                self._set_task_status("participate", "risk_paused", str(exc))
            else:
                # V24：普通业务失败只结束当前 Job，Scheduler 继续运行。
                self._log(
                    "error",
                    f"三连参与当前任务失败：{exc}；"
                    "Scheduler 继续运行，将在下一正常调度时重试。",
                )
                self._set_task_status("participate", "error", str(exc))
            self._done_triple.add(key)
            self._set_phase("等待下一刻度", f"已跳过：{exc}")

    def _run_winner_scan(self, key: str) -> None:
        if not _stale_or_slot_guard(self, "winner_scan", key):
            self._done_winner.add(key)
            return
        gate = self._remote_gate()
        if not gate.allowed:
            self._skip_remote_slot("winner_scan", gate, done=self._done_winner, key=key)
            return
        self._set_pipeline(active=False)
        self._set_phase("中奖深检", f"中奖深检刻度 {key}")
        self._set_task_status("winner", "running", "中奖深探开始")
        self._log("info", "中奖深探开始")
        try:
            outcome = self._click_and_wait("winner_deep_scan")
            self._done_winner.add(key)
            if self._is_stale():
                return
            with self._lock:
                self._status.message = "中奖深检完成"
            self._mark_remote_stage_finished()
            final_message = str((outcome or {}).get("message") or "中奖深探完成")
            self._set_task_status("winner", "completed", final_message)
            self._log("info", final_message)
            self._set_phase("等待下一刻度", "中奖深检完成")
        except CollisionError:
            self._log("warn", "中奖深检发现已有任务在运行，本轮跳过。")
            self._set_task_status("winner", "skipped_busy", "本轮跳过（任务占用）")
            self._done_winner.add(key)
        except Exception as exc:
            if matches_platform_risk(exc):
                self._pause_all_auto_remote("winner_deep_scan", exc)
                self._set_task_status("winner", "risk_paused", str(exc))
            else:
                # V24：普通业务失败只结束当前 Job，Scheduler 继续运行。
                self._log(
                    "error",
                    f"中奖深检当前任务失败：{exc}；"
                    "Scheduler 继续运行，将在下一正常调度时重试。",
                )
                self._set_task_status("winner", "error", str(exc))
            self._done_winner.add(key)
            self._set_phase("等待下一刻度", f"中奖深检已跳过：{exc}")

    def _click_and_wait(self, action: str, *, pipeline_index: int | None = None) -> dict[str, Any]:
        if action not in ALLOWED_CLICK_ACTIONS:
            raise ValueError(f"禁止的操作：{action}")

        label = ACTION_LABELS.get(action, action)
        # 停止/换代后本代不得再启动任何新 Job（JobRunner 端既有 Job 不受影响）。
        if self._is_stale():
            raise CollisionError(
                f"调度已停止或被新一代接管，放弃自动点击「{label}」（{action}）"
            )
        self._set_phase(f"点击：{label}", f"正在点击「{label}」")
        self._log("info", f"点击按钮：{label} ({action})")

        if self._runner.is_running():
            current = self._runner.get_status().to_dict()
            raise CollisionError(
                f"准备点击「{label}」时发现抽奖端仍有任务在运行"
                f"（action={current.get('action')}, message={current.get('message')}）"
            )

        # 二次确认：is_running 检查与 try_start 之间可能已 stop/换代，绝不能启动新 Job。
        if self._is_stale():
            raise CollisionError(
                f"调度已停止或被新一代接管，放弃启动「{label}」（{action}）"
            )

        params = {"from_auto": True} if action == "participate_triple" else {}
        job_id = self._runner.try_start(action, params, source="auto")
        if job_id is None:
            raise CollisionError(f"点击「{label}」失败：已有任务正在运行")

        with self._lock:
            self._status.last_click = {
                "action": action,
                "label": label,
                "at": _now_iso(),
                "response_ok": True,
                "job_id": job_id,
            }
        self._schedule_auto_snapshot(force=False)

        if pipeline_index is not None:
            self._set_pipeline(active=True, step_index=pipeline_index, waiting=True)

        final = self._wait_until_terminal(job_id, label)
        if self._is_stale():
            # 停止/换代：立即停止等待；JobRunner 中已启动的 Job 继续自然完成，绝不 cancel。
            # 该 Job 若后来命中平台风控，由 JobRunner（source=auto）负责记录 global cooldown，
            # 与调度代际是否 stale 无关。
            return {"stopped": True, "message": "", "job": final}
        state = str(final.get("state") or "")
        msg = str(final.get("message") or "")
        result = final.get("result") if isinstance(final.get("result"), dict) else {}
        risk_code = result.get("risk_code")
        if result.get("skipped") or (
            action == "participate_triple" and state == "error" and _is_triple_empty_skip(msg)
        ):
            return {"skipped": True, "message": msg, "job": final}
        if state == "cancelled":
            raise RuntimeError(msg or f"「{label}」已取消")
        if state in {"error", "interrupted"}:
            if risk_code is not None:
                # 保留结构化风控语义，供上层统一判定（停止后续 stage / 冷却记录）。
                raise RuntimeError(f"API error {risk_code}: {msg or '平台风控'}")
            raise RuntimeError(msg or f"「{label}」以 {state} 结束")
        if state != "success":
            raise RuntimeError(msg or f"「{label}」异常结束：state={state}")
        return {"skipped": False, "message": msg, "job": final}

    def _wait_until_terminal(self, job_id: int, label: str) -> dict[str, Any]:
        """按 job id 只读轮询至终态。绝不 cancel。停止/换代立即退出等待。"""
        deadline = time.monotonic() + JOB_POLL_TIMEOUT_SEC
        self._set_phase(f"等待结束：{label}", f"已点击「{label}」，等待抽奖端自行结束…")
        time.sleep(0.8)
        last: dict[str, Any] = {}
        last_detail = ""
        while not self._is_stale():
            if time.monotonic() > deadline:
                raise RuntimeError(f"等待「{label}」超时（超过 {int(JOB_POLL_TIMEOUT_SEC)} 秒）")
            job = self._runner.resolve_job_status(job_id).to_dict()
            last = job
            state = str(job.get("state") or "")
            if state != "running":
                msg = str(job.get("message") or state or "done")
                self._log("info", f"「{label}」已结束：state={state} · {msg}")
                return job
            detail = str(job.get("progress_message") or job.get("message") or "")
            if detail:
                self._set_phase(f"等待结束：{label}", detail)
                if detail != last_detail:
                    self._log("info", f"{label} · {detail}")
                    last_detail = detail
            self._active_stop_event().wait(JOB_POLL_INTERVAL_SEC)
        return last


def _probe_job(job_runner: JobRunner) -> dict[str, Any]:
    job = job_runner.get_status().to_dict()
    state = str(job.get("state") or "idle")
    return {
        "ok": True,
        "reachable": True,
        "job_state": state,
        "job_id": job.get("id"),
        "job_action": str(job.get("action") or ""),
        "job_message": str(job.get("message") or ""),
        "job_label": str(job.get("label") or ""),
        "checked_at": _now_iso(),
    }


def _idle_pipeline() -> dict[str, Any]:
    return {
        "active": False,
        "step_index": -1,
        "waiting": False,
        "steps": [{**item, "status": "pending", "index": i} for i, item in enumerate(REFRESH_STEPS)],
    }


def _is_triple_empty_skip(message: str) -> bool:
    text = str(message or "")
    markers = ("没有可参与", "当前列表没有", "无可参与", "已跳过")
    return any(marker in text for marker in markers)


def _now_iso() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _triple_due(state: dict[str, str]) -> datetime | None:
    raw = state.get(_TRIPLE_NEXT_RUN)
    if raw is None:
        return None
    try:
        due = datetime.fromisoformat(raw)
        if due.tzinfo is None:
            raise ValueError("missing timezone")
        return due.astimezone(CN_TZ)
    except (ValueError, OverflowError) as exc:
        raise SlotStateError("自动参与 next_run 损坏，已停止自动调度") from exc


def _dedicated_minute(now: datetime) -> bool:
    return (
        (now.hour in REFRESH_HOURS and now.minute == 0)
        or (now.hour in WINNER_SCAN_HOURS and now.minute == WINNER_SCAN_MINUTE)
        or (now.hour % FOLLOWING_FEED_SCAN_HOUR_INTERVAL == FOLLOWING_FEED_SCAN_HOUR_OFFSET
            and now.minute == FOLLOWING_FEED_SCAN_MINUTE)
    )


def _next_occurrence_candidates(now: datetime) -> list[tuple[str, str, datetime]]:
    """按 ``_loop`` 的真实 cadence 枚举未来 24 小时任务（纯只读）。"""
    candidates: list[tuple[str, str, datetime]] = []
    base = now.replace(second=0, microsecond=0)
    for offset_min in range(1, 24 * 60 + 1):
        candidate = base + timedelta(minutes=offset_min)
        h, m = candidate.hour, candidate.minute
        if h in REFRESH_HOURS and m == 0:
            candidates.append(("refresh", "刷新批次", candidate))
        if m == WINNER_SCAN_MINUTE and h in WINNER_SCAN_HOURS:
            candidates.append(("winner", "中奖深探", candidate))
        if (
            m == FOLLOWING_FEED_SCAN_MINUTE
            and h % FOLLOWING_FEED_SCAN_HOUR_INTERVAL == FOLLOWING_FEED_SCAN_HOUR_OFFSET
        ):
            candidates.append(("following", "关注补漏", candidate))
    # 只读真实已保存的下一轮，不虚构后续随机时间，也不在页面读取时写文件。
    try:
        state = _read_slot_state(_slot_state_path())
        due = _triple_due(state)
        if (due is not None and due >= now
                and state.get("triple") != f"jitter-{state[_TRIPLE_NEXT_RUN]}"):
            candidates.append(("participate", "自动参与", due))
    except SlotStateError:
        pass  # 状态页仍可读；执行入口遇到损坏会 fail-closed。
    return candidates


def next_auto_task(now: datetime | None = None) -> dict[str, Any]:
    """返回下一项自动任务（人话分钟数；纯本地，不改任何调度状态）。"""
    current = now or datetime.now(CN_TZ)
    candidates = _next_occurrence_candidates(current)
    if not candidates:
        return {"label": "", "action": "", "minutes": None, "at_unix": None}
    # 按到期时间排序；同分保持循环判定顺序（刷新→中奖→关注→参与）。
    candidates.sort(key=lambda item: (item[2], _AUTO_TASK_ORDER.get(item[0], 99)))
    action, label, due = candidates[0]
    seconds = max(0, int((due - current).total_seconds()))
    minutes = (seconds + 59) // 60
    return {
        "label": label,
        "action": action,
        "minutes": minutes,
        "at_unix": int(due.timestamp()),
    }


_AUTO_TASK_ORDER = {
    "refresh": 0,
    "winner": 1,
    "following": 2,
    "participate": 3,
}


def _next_runs_by_task(now: datetime) -> dict[str, int]:
    result: dict[str, int] = {}
    for task, _label, due in _next_occurrence_candidates(now):
        result.setdefault(task, int(due.timestamp()))
        if len(result) == len(_AUTO_TASK_ORDER):
            break
    return result


def _next_slot(now: datetime) -> dict[str, Any]:
    task = next_auto_task(now)
    at_unix = task.get("at_unix")
    if at_unix:
        candidate = datetime.fromtimestamp(int(at_unix), CN_TZ)
        kind = str(task.get("action") or "")
        action_by_kind = {
            "refresh": "refresh_all",
            "winner": "winner_deep_scan",
            "following": "following_feed_scan",
            "participate": "participate_triple",
        }
        action = action_by_kind[kind]
        actions = (
            [{"action": item["action"], "label": item["label"]} for item in REFRESH_STEPS]
            if kind == "refresh"
            else [{"action": action, "label": ACTION_LABELS[action]}]
        )
        return {
            "kind": kind,
            "label": task.get("label"),
            "action": action,
            "action_label": ACTION_LABELS[action],
            "actions": actions,
            "at": candidate.strftime("%Y-%m-%d %H:%M:%S"),
            "at_unix": int(at_unix),
            "hour": candidate.hour,
            "minute": candidate.minute,
            "hint": f"下次{task.get('label')}约 {candidate:%H:%M}",
        }
    return {
        "kind": "none",
        "label": "暂无",
        "action": None,
        "action_label": "暂无",
        "actions": [],
        "at": None,
        "at_unix": None,
        "hint": "暂无下一刻度",
    }


auto_scheduler = AutoScheduler()
