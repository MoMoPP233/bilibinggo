"""定时点击调度器：只向 JobRunner 投递意图，撞车即停，绝不 cancel。"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from src.app_logging import get_logger
from src.restart_control import restart_control
from web.auto_config import (
    ACTION_LABELS,
    ALLOWED_CLICK_ACTIONS,
    AUTO_REMOTE_RISK_COOLDOWN_SECONDS,
    CLEANUP_MAINTAIN_INTERVAL_HOURS,
    FOLLOWING_FEED_SCAN_HOUR_INTERVAL,
    FOLLOWING_FEED_SCAN_HOUR_OFFSET,
    FOLLOWING_FEED_SCAN_MINUTE,
    JOB_POLL_INTERVAL_SEC,
    JOB_POLL_TIMEOUT_SEC,
    MIN_REMOTE_STAGE_GAP_SECONDS,
    REFRESH_HOURS,
    TRIPLE_MINUTES,
)
from web.auto_remote_state import (
    clear_expired_risk_pause,
    is_risk_paused,
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
_AUTO_SNAPSHOT_LOG_LIMIT = 30


class CollisionError(RuntimeError):
    """抽奖端已有任务在跑，自动调度必须立即停机。"""


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
            "next_task": next_auto_task(),
            "schedule": {
                "refresh_hours": sorted(REFRESH_HOURS),
                "triple_minutes": sorted(TRIPLE_MINUTES),
                "actions": [
                    {"action": key, "label": ACTION_LABELS[key]}
                    for key in ("refresh_all", "refresh_watch", "refresh_status", "participate_triple")
                ],
            },
        }


class AutoScheduler:
    def __init__(self, job_runner: JobRunner | None = None) -> None:
        self._runner = job_runner or runner
        self._lock = threading.Lock()
        # 每代调度器拥有自己独立的 stop Event；新代 start 绝不 clear 旧代 Event。
        # 线程运行期间所有停止判定都绑定“自己的代际上下文”，避免旧代被唤醒后继续调度。
        self._generation = 0
        self._stop_event = threading.Event()
        self._ctx = threading.local()
        self._thread: threading.Thread | None = None
        self._logs: deque[LogEntry] = deque(maxlen=200)
        self._status = SchedulerStatus(refresh_pipeline=_idle_pipeline())
        self._done_refresh: set[str] = set()
        self._done_triple: set[str] = set()
        self._done_maintain: set[str] = set()
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
        with self._lock:
            self._status.logs = [
                {"ts": e.ts, "level": e.level, "message": e.message} for e in list(self._logs)[-80:]
            ]
            self._status.next_slot = slot
            self._status.next_hint = slot.get("hint") or ""
            self._status.job_probe = probe
            self._status.server_now = _now_iso()
            self._status.server_now_unix = int(now.timestamp())
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
        self._log("info", "调度器已启动（仅点击 4 个按钮，不干涉抽奖程序其它功能）")
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
        entry = LogEntry(ts=_now_iso(), level=level, message=message)
        with self._lock:
            self._logs.append(entry)
        self._publish_auto_log(entry)

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
            trigger_stage=stage_label, reason=str(message or "")
        )
        until = state.get("paused_until")
        until_text = (
            datetime.fromtimestamp(int(until), CN_TZ).strftime("%H:%M")
            if until
            else "稍后"
        )
        hours = AUTO_REMOTE_RISK_COOLDOWN_SECONDS // 3600
        self._log(
            "error",
            f"检测到平台风控，自动远程任务进入 {hours} 小时冷却（至 {until_text}）：{message}",
        )
        self._set_phase(
            "等待下一刻度",
            f"自动远程任务已因平台风控进入 {hours} 小时冷却，冷却结束后等下一次正常调度。",
        )

    def _remote_stage_allowed(self) -> bool:
        """返回当前是否允许启动下一个自动远程大任务（0 远程只读判断）。

        仅在风控冷却期内禁止新的自动远程任务；冷却到期后本地清除，
        只恢复“等待下一次正常调度”的资格，绝不主动探测或补跑。
        """
        if is_risk_paused():
            state = risk_pause_state()
            reason = state.get("reason") or state.get("code") or "平台风控"
            until = state.get("paused_until")
            until_text = (
                datetime.fromtimestamp(int(until), CN_TZ).strftime("%H:%M")
                if until
                else ""
            )
            self._log(
                "warn",
                f"自动远程任务风控冷却中（{state.get('trigger_stage') or '未知'}）："
                f"{reason}，至 {until_text}。本轮跳过且不联网。",
            )
            return False
        clear_expired_risk_pause()
        now_mono = time.monotonic()
        if (
            self._last_remote_stage_finished_mono > 0
            and now_mono - self._last_remote_stage_finished_mono
            < MIN_REMOTE_STAGE_GAP_SECONDS
        ):
            self._log(
                "info",
                "上一个自动远程大任务刚结束，尚未达到最小错峰间隔，本轮跳过。",
            )
            return False
        return True

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
                now = datetime.now(CN_TZ)
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

                # 清理数据自动维护：Scheduler 启动即自动拥有每 2 小时维护资格
                # （无需用户单独开关；旧 enabled 配置不影响统一 Scheduler）。
                if now.minute == 0 and now.hour % CLEANUP_MAINTAIN_INTERVAL_HOURS == 0:
                    key = f"maint-{now:%Y-%m-%d-%H}"
                    if key not in self._done_maintain:
                        self._run_maintenance(key)
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

                if now.hour not in REFRESH_HOURS and now.minute in TRIPLE_MINUTES:
                    key = f"{now:%Y-%m-%d-%H-%M}"
                    if key not in self._done_triple:
                        self._run_triple_slot(key)
                        continue

                if self._status.state == "running":
                    self._set_phase("等待下一刻度", "调度器运行中")
                stop_event.wait(1.0)
        except CollisionError as exc:
            self._fatal(f"任务撞车：{exc}")
        except Exception as exc:
            logger.exception("定时调度未预期错误")
            self._fatal(f"未预期错误：{friendly_error(exc)}")

    def _run_refresh_batch(self, key: str) -> None:
        if not self._remote_stage_allowed():
            # 全局暂停或 60s 错峰期内：本轮整批跳过，不致命、不补跑。
            self._done_refresh.add(key)
            return
        self._set_phase("刷新批次", f"开始刷新批次 {key}")
        self._log("info", f"刷新批次开始 {key}：一键更新 → 监控动态 → 刷新状态")
        self._set_pipeline(active=True, step_index=0, waiting=False)
        actions = ("refresh_all", "refresh_watch", "refresh_status")
        try:
            for index, action in enumerate(actions):
                # 停止/换代后：不再安排后续任何自动任务。
                if self._is_stale():
                    self._set_pipeline(active=False)
                    return
                self._set_pipeline(active=True, step_index=index, waiting=False)
                try:
                    self._click_and_wait(action, pipeline_index=index)
                except CollisionError:
                    # 已有 Job 在运行：本轮直接跳过，不 fatal、不补跑。
                    self._log(
                        "warn",
                        f"「{ACTION_LABELS.get(action, action)}」发现已有任务在运行，本轮跳过。",
                    )
                    self._done_refresh.add(key)
                    self._set_pipeline(active=False)
                    return
                except Exception as exc:
                    if matches_platform_risk(exc):
                        self._pause_all_auto_remote(
                            f"refresh:{action}", str(exc) or type(exc).__name__
                        )
                        self._done_refresh.add(key)
                        self._set_pipeline(active=False)
                        return
                    if _is_hard_failure(exc):
                        raise
                    self._log("warn", f"「{ACTION_LABELS.get(action, action)}」业务结束：{exc}，继续下一项")
            self._done_refresh.add(key)
            with self._lock:
                self._status.refresh_batch_key = key
            self._mark_remote_stage_finished()
            self._log("info", f"刷新批次完成 {key}")
            self._set_pipeline(active=False)
            self._set_phase("等待下一刻度", "刷新批次已完成")
        except CollisionError:
            # 兜底：任何时刻发现撞车都以“跳过本轮”处理，不致命。
            self._log("warn", f"刷新批次检测到任务撞车，本轮已跳过：{key}")
            self._done_refresh.add(key)
            self._set_pipeline(active=False)
        except Exception as exc:
            if matches_platform_risk(exc):
                self._pause_all_auto_remote("refresh_batch", str(exc) or type(exc).__name__)
            elif _is_hard_failure(exc):
                self._fatal(str(exc))
                return
            else:
                self._log("error", f"刷新批次中断 {key}：{exc}")
            self._done_refresh.add(key)
            self._set_pipeline(active=False)
            self._set_phase("等待下一刻度", f"刷新批次异常已跳过：{exc}")

    def _run_following_scan(self, key: str) -> None:
        if not self._remote_stage_allowed():
            self._done_following.add(key)
            return
        self._set_pipeline(active=False)
        self._set_phase("关注动态补漏", f"关注动态补漏 {key}")
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
            self._set_phase("等待下一刻度", "关注动态补漏完成")
        except CollisionError:
            self._log("warn", "关注动态补漏发现已有任务在运行，本轮跳过。")
            self._done_following.add(key)
        except Exception as exc:
            if matches_platform_risk(exc):
                self._pause_all_auto_remote("following_feed_scan", str(exc) or type(exc).__name__)
            elif _is_hard_failure(exc):
                self._fatal(str(exc))
                return
            else:
                self._log("warn", f"关注动态补漏跳过：{exc}")
            self._done_following.add(key)
            self._set_phase("等待下一刻度", f"关注动态补漏已跳过：{exc}")

    def _run_triple_slot(self, key: str) -> None:
        if not self._remote_stage_allowed():
            self._done_triple.add(key)
            return
        self._set_pipeline(active=False)
        self._set_phase("三连参与", f"触发三连参与 {key}")
        self._log("info", f"三连参与刻度 {key}")
        try:
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
                self._set_phase("等待下一刻度", msg)
            else:
                self._set_phase("等待下一刻度", "三连参与已完成")
        except CollisionError:
            self._log("warn", "三连参与发现已有任务在运行，本轮跳过。")
            self._done_triple.add(key)
        except Exception as exc:
            if matches_platform_risk(exc):
                self._pause_all_auto_remote("participate_triple", str(exc) or type(exc).__name__)
            elif _is_hard_failure(exc):
                self._fatal(str(exc))
                return
            else:
                self._log("info", f"三连参与已跳过：{exc}")
            self._done_triple.add(key)
            self._set_phase("等待下一刻度", f"已跳过：{exc}")

    def _run_maintenance(self, key: str) -> None:
        if not self._remote_stage_allowed():
            self._done_maintain.add(key)
            return
        self._set_pipeline(active=False)
        self._set_phase("清理维护", f"自动维护刻度 {key}")
        from src.repost_cleanup import auto_maintenance_paused_state

        paused, pause_reason = auto_maintenance_paused_state()
        if paused:
            from src.repost_cleanup import auto_clear_expired_maintenance_risk

            cleared = auto_clear_expired_maintenance_risk(
                cooldown_seconds=AUTO_REMOTE_RISK_COOLDOWN_SECONDS
            )
            if not cleared:
                self._log(
                    "warn",
                    f"自动维护风控冷却中，本轮跳过且不联网：{pause_reason}",
                )
                self._done_maintain.add(key)
                with self._lock:
                    self._status.message = "自动维护风控冷却中，冷却结束后等下一次正常调度。"
                self._set_phase(
                    "等待下一刻度",
                    "自动维护风控冷却中，冷却结束后等下一次正常调度。",
                )
                return
            self._log(
                "info",
                "自动维护风控冷却已到期，已本地恢复调度资格（不立即联网）。",
            )
        self._log("info", f"清理数据自动维护刻度 {key}")
        try:
            self._click_and_wait("cleanup_auto_maintain")
            self._done_maintain.add(key)
            if self._is_stale():
                # 停止/换代：Job 自行自然结束，旧代不再推进阶段状态。
                return
            with self._lock:
                self._status.message = "自动维护完成"
            self._mark_remote_stage_finished()
            self._set_phase("等待下一刻度", "自动维护完成")
            # cleanup 若在运行中命中风控，会把自己 maintenance_risk_paused 置 ON；
            # 此处把同一风险同时提升为“全局自动远程暂停”。
            newly_paused, new_reason = auto_maintenance_paused_state()
            if newly_paused:
                self._pause_all_auto_remote("cleanup_auto_maintain", new_reason or "cleanup 风控暂停")
        except CollisionError:
            self._log("warn", "自动维护发现已有任务在运行，本轮跳过。")
            self._done_maintain.add(key)
        except Exception as exc:
            if matches_platform_risk(exc):
                self._pause_all_auto_remote("cleanup_auto_maintain", str(exc) or type(exc).__name__)
            elif _is_hard_failure(exc):
                self._fatal(str(exc))
                return
            else:
                self._log("warn", f"自动维护跳过：{exc}")
            self._done_maintain.add(key)
            self._set_phase("等待下一刻度", f"自动维护已跳过：{exc}")

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


def _is_hard_failure(exc: BaseException) -> bool:
    if isinstance(exc, CollisionError):
        return True
    if _is_triple_empty_skip(str(exc)):
        return False
    text = str(exc)
    soft_markers = ("没有可参与", "当前列表没有", "无可参与", "已跳过")
    if any(m in text for m in soft_markers):
        return False
    hard_markers = (
        "连接",
        "timeout",
        "Timeout",
        "ConnectError",
        "等待「",
        "HTTP 5",
        "扫码登录",
        "LLM",
        "Cookie",
        "401",
    )
    return any(m in text for m in hard_markers)


def _now_iso() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _next_occurrence_candidates(now: datetime) -> list[tuple[str, str, datetime]]:
    """按 AutoScheduler 真实 cadence 计算最近各候选任务的下一到期时刻。

    优先级 = 顺序（与 _loop 判定顺序一致）：刷新批次 → 清理维护 → 关注补漏 → 自动参与。
    """
    candidates: list[tuple[str, str, datetime]] = []
    for day_offset in range(0, 2):
        base_day = (now + timedelta(days=day_offset)).replace(second=0, microsecond=0)

        for hour in sorted(REFRESH_HOURS):
            cand = base_day.replace(hour=hour, minute=0)
            if cand > now:
                candidates.append(("refresh", "刷新批次", cand))

        for hour in range(0, 24, CLEANUP_MAINTAIN_INTERVAL_HOURS):
            cand = base_day.replace(hour=hour, minute=0)
            if cand > now:
                candidates.append(("cleanup", "清理维护", cand))

        for hour in range(0, 24):
            if hour % FOLLOWING_FEED_SCAN_HOUR_INTERVAL != FOLLOWING_FEED_SCAN_HOUR_OFFSET:
                continue
            cand = base_day.replace(hour=hour, minute=FOLLOWING_FEED_SCAN_MINUTE)
            if cand > now:
                candidates.append(("following", "关注补漏", cand))

        for hour in range(0, 24):
            if hour in REFRESH_HOURS:
                continue
            for minute in sorted(TRIPLE_MINUTES):
                cand = base_day.replace(hour=hour, minute=minute)
                if cand > now:
                    candidates.append(("participate", "自动参与", cand))
        if candidates:
            break
    return candidates


def next_auto_task(now: datetime | None = None) -> dict[str, Any]:
    """返回下一项自动任务（人话分钟数；纯本地，不改任何调度状态）。"""
    current = now or datetime.now(CN_TZ)
    candidates = _next_occurrence_candidates(current)
    if not candidates:
        return {"label": "", "action": "", "minutes": None, "at_unix": None}
    # 按到期时间排序；同分保持循环判定顺序（刷新→清理→关注→参与）。
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


_AUTO_TASK_ORDER = {"refresh": 0, "cleanup": 1, "following": 2, "participate": 3}


def _next_slot(now: datetime) -> dict[str, Any]:
    for offset_min in range(1, 24 * 60 + 1):
        candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=offset_min)
        h, m = candidate.hour, candidate.minute
        if h in REFRESH_HOURS and m == 0:
            return {
                "kind": "refresh",
                "label": "刷新批次",
                "action": "refresh_all",
                "action_label": ACTION_LABELS["refresh_all"],
                "actions": [
                    {"action": item["action"], "label": item["label"]}
                    for item in REFRESH_STEPS
                ],
                "at": candidate.strftime("%Y-%m-%d %H:%M:%S"),
                "at_unix": int(candidate.timestamp()),
                "hour": h,
                "minute": m,
                "hint": f"下次刷新批次约 {h:02d}:00（一键更新→监控→状态）",
            }
        if h not in REFRESH_HOURS and m in TRIPLE_MINUTES:
            return {
                "kind": "triple",
                "label": "三连参与",
                "action": "participate_triple",
                "action_label": ACTION_LABELS["participate_triple"],
                "actions": [
                    {"action": "participate_triple", "label": ACTION_LABELS["participate_triple"]}
                ],
                "at": candidate.strftime("%Y-%m-%d %H:%M:%S"),
                "at_unix": int(candidate.timestamp()),
                "hour": h,
                "minute": m,
                "hint": f"下次三连参与约 {h:02d}:{m:02d}",
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
