from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from datetime import timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _isolate_scheduler_slots(tmp_path, monkeypatch):
    """V9：把 Scheduler slot 持久化文件隔离到每个用例的临时目录。"""
    from src import data_paths as _data_paths

    monkeypatch.setattr(_data_paths, "get_profile_dir", lambda profile_id=None: tmp_path)


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.auto_config import ALLOWED_CLICK_ACTIONS
from web.auto_scheduler import AutoScheduler, CollisionError, _next_slot, _probe_job, next_auto_task
from web.job_runner import JobRunner, JobStatus


def _bind_resolve(runner: MagicMock, status_factory) -> None:
    """让 resolve_job_status 与 get_status 行为一致，供调度等待循环使用。"""

    def resolve(_job_id: int) -> JobStatus:
        value = status_factory()
        return value if isinstance(value, JobStatus) else JobStatus(**value)

    runner.resolve_job_status.side_effect = resolve


def test_allowed_actions_include_cleanup_maintain() -> None:
    assert ALLOWED_CLICK_ACTIONS == frozenset(
        {
            "refresh_all",
            "refresh_watch",
            "refresh_status",
            "participate_triple",
            "cleanup_auto_maintain",
            "cleanup_maintenance_cycle",
            "following_feed_scan",
            "winner_deep_scan",
        }
    )


def test_cleanup_maintain_defaults_off() -> None:
    from web.auto_config import cleanup_maintain_enabled

    assert cleanup_maintain_enabled() is False


def test_cleanup_maintenance_paused_tick_never_starts_remote_job(monkeypatch) -> None:
    runner = MagicMock()
    scheduler = AutoScheduler(job_runner=runner)
    monkeypatch.setattr(
        "src.repost_cleanup.auto_maintenance_paused_state",
        lambda: (True, "-352 风控暂停"),
    )

    scheduler._run_cleanup_cycle("cleanup-2026-09-02")

    runner.try_start.assert_not_called()
    assert "cleanup-2026-09-02" in scheduler._done_cleanup


def test_task_status_rejects_stale_scheduler_generation() -> None:
    scheduler = AutoScheduler(job_runner=JobRunner())
    scheduler._generation = 2
    scheduler._ctx.generation = 1

    scheduler._set_task_status("winner", "running", "旧代不应写入")

    assert scheduler._status.tasks["winner"]["status"] == "waiting"
    assert scheduler._status.tasks["winner"]["last_result"] == "尚未执行"


def test_stop_releases_visible_running_task_without_cancelling_job() -> None:
    runner = MagicMock()
    runner.get_status.return_value.to_dict.return_value = {
        "state": "running",
        "action": "cleanup_maintenance_cycle",
        "message": "仍由 JobRunner 执行",
    }
    scheduler = AutoScheduler(job_runner=runner)
    scheduler._status.state = "running"
    scheduler._status.tasks["cleanup"]["status"] = "running"

    status = scheduler.stop()

    assert status["tasks"]["cleanup"]["status"] == "waiting"
    assert "不会被取消" in status["tasks"]["cleanup"]["last_result"]
    runner.cancel.assert_not_called()


def test_scheduler_log_is_sanitized_before_sse_or_snapshot() -> None:
    scheduler = AutoScheduler(job_runner=JobRunner())

    scheduler._log(
        "info",
        "GET https://api.example.test/feed?topic_id=123&csrf=topsecret SESSDATA=cookie-secret",
    )

    message = scheduler._logs[-1].message
    assert "topic_id=123" not in message
    assert "topsecret" not in message
    assert "cookie-secret" not in message
    assert "[查询参数已隐藏]" in message


def test_click_rejects_forbidden_action() -> None:
    scheduler = AutoScheduler(job_runner=JobRunner())
    with pytest.raises(ValueError, match="禁止的操作"):
        scheduler._click_and_wait("cancel")


def test_collision_when_runner_already_running() -> None:
    runner = MagicMock()
    runner.is_running.return_value = True
    runner.get_status.return_value.to_dict.return_value = {
        "state": "running",
        "action": "participate_triple",
        "message": "进行中",
    }
    scheduler = AutoScheduler(job_runner=runner)
    with pytest.raises(CollisionError, match="仍有任务在运行"):
        scheduler._click_and_wait("refresh_status")
    runner.try_start.assert_not_called()
    runner.cancel.assert_not_called()


def test_collision_when_try_start_returns_none() -> None:
    runner = MagicMock()
    runner.is_running.return_value = False
    runner.try_start.return_value = None
    scheduler = AutoScheduler(job_runner=runner)
    with pytest.raises(CollisionError, match="已有任务正在运行"):
        scheduler._click_and_wait("refresh_all")
    runner.cancel.assert_not_called()


def test_wait_until_terminal_never_calls_cancel() -> None:
    runner = MagicMock()
    runner.is_running.return_value = False
    runner.try_start.return_value = 42
    poll_count = {"n": 0}

    def status_factory() -> JobStatus:
        poll_count["n"] += 1
        if poll_count["n"] < 2:
            return JobStatus(id=42, state="running", action="refresh_status", message="working")
        return JobStatus(id=42, state="success", action="refresh_status", message="done")

    _bind_resolve(runner, status_factory)
    scheduler = AutoScheduler(job_runner=runner)
    scheduler._click_and_wait("refresh_status")
    runner.cancel.assert_not_called()
    runner.try_start.assert_called_once_with("refresh_status", {}, source="auto")


def test_collision_in_refresh_batch_skips_without_fatal_or_cancel() -> None:
    runner = MagicMock()
    runner.is_running.return_value = True
    runner.get_status.return_value.to_dict.return_value = {
        "state": "running",
        "action": "refresh_all",
        "message": "busy",
    }
    scheduler = AutoScheduler(job_runner=runner)
    scheduler._run_refresh_batch("2026-07-17-3")
    assert "2026-07-17-3" in scheduler._done_refresh
    runner.try_start.assert_not_called()
    runner.cancel.assert_not_called()
    assert scheduler._status.state != "fatal"


def test_probe_only_reads_runner_status() -> None:
    runner = MagicMock()
    status = MagicMock()
    status.to_dict.return_value = {
        "id": None,
        "state": "idle",
        "action": "",
        "message": "",
        "label": "",
    }
    runner.get_status.return_value = status
    result = _probe_job(runner)
    assert result["reachable"] is True
    assert result["job_state"] == "idle"
    assert result["job_id"] is None
    runner.get_status.assert_called()
    runner.try_start.assert_not_called()
    runner.cancel.assert_not_called()


def test_soft_failure_for_empty_triple() -> None:
    from web.auto_scheduler import _is_triple_empty_skip

    assert _is_triple_empty_skip("当前列表没有可参与的未参加活动") is True
    assert _is_triple_empty_skip("当前没有可参与的未参加活动，已跳过") is True
    assert _is_triple_empty_skip("网络请求失败: ConnectError") is False


def test_click_and_wait_treats_skipped_triple_as_success() -> None:
    runner = MagicMock()
    runner.is_running.return_value = False
    runner.try_start.return_value = 7
    _bind_resolve(
        runner,
        lambda: JobStatus(
            id=7,
            state="success",
            action="participate_triple",
            message="当前没有可参与的未参加活动，已跳过",
            result={"skipped": True, "items": []},
        ),
    )
    scheduler = AutoScheduler(job_runner=runner)
    outcome = scheduler._click_and_wait("participate_triple")
    assert outcome["skipped"] is True
    runner.try_start.assert_called_once_with("participate_triple", {"from_auto": True}, source="auto")
    runner.cancel.assert_not_called()


def test_click_and_wait_treats_legacy_empty_triple_error_as_skip() -> None:
    runner = MagicMock()
    runner.is_running.return_value = False
    runner.try_start.return_value = 8
    _bind_resolve(
        runner,
        lambda: JobStatus(
            id=8,
            state="error",
            action="participate_triple",
            message="当前列表没有可参与的未参加活动",
            result=None,
        ),
    )
    scheduler = AutoScheduler(job_runner=runner)
    outcome = scheduler._click_and_wait("participate_triple")
    assert outcome["skipped"] is True
    runner.cancel.assert_not_called()


def test_click_and_wait_cancelled_raises(isolated_home: Path) -> None:
    _ = isolated_home
    runner = MagicMock()
    runner.is_running.return_value = False
    runner.try_start.return_value = 9
    _bind_resolve(
        runner,
        lambda: JobStatus(
            id=9,
            state="cancelled",
            action="refresh_status",
            message="任务已取消",
        ),
    )
    scheduler = AutoScheduler(job_runner=runner)
    with pytest.raises(RuntimeError, match="已取消"):
        scheduler._click_and_wait("refresh_status")
    runner.cancel.assert_not_called()


def test_click_and_wait_interrupted_raises(isolated_home: Path) -> None:
    _ = isolated_home
    runner = MagicMock()
    runner.is_running.return_value = False
    runner.try_start.return_value = 10
    _bind_resolve(
        runner,
        lambda: JobStatus(
            id=10,
            state="interrupted",
            action="refresh_status",
            message="进程退出，任务中断",
        ),
    )
    scheduler = AutoScheduler(job_runner=runner)
    with pytest.raises(RuntimeError, match="中断"):
        scheduler._click_and_wait("refresh_status")
    runner.cancel.assert_not_called()


def test_next_slot_before_refresh() -> None:
    now = datetime(2026, 7, 17, 2, 56, tzinfo=timezone(timedelta(hours=8)))
    slot = _next_slot(now)
    assert slot["kind"] == "refresh"
    assert slot["hour"] == 3
    assert slot["action"] == "refresh_all"
    assert slot["action_label"] == "一键更新活动链接"
    assert slot["at_unix"]


def test_next_slot_participate_minute() -> None:
    now = datetime(2026, 7, 17, 2, 50, tzinfo=timezone(timedelta(hours=8)))
    slot = _next_slot(now)
    assert slot["kind"] == "participate"
    assert slot["minute"] == 55
    assert slot["action"] == "participate_triple"
    assert slot["action_label"] == "三连参与"
def _cn(dt):
    from datetime import datetime
    return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, tzinfo=timezone(timedelta(hours=8)))


def test_next_auto_task_refresh_priority_on_tie() -> None:
    task = next_auto_task(_cn(datetime(2026, 7, 17, 5, 56)))
    assert task["label"] == "刷新批次"
    assert task["minutes"] == 4


def test_next_auto_task_following_at_1258() -> None:
    task = next_auto_task(_cn(datetime(2026, 7, 17, 12, 58)))
    assert task["label"] == "关注补漏"
    assert task["minutes"] == 5


def test_next_auto_task_cleanup_uses_real_0205_slot() -> None:
    task = next_auto_task(_cn(datetime(2026, 7, 17, 2, 2)))
    assert task["label"] == "清理维护"
    assert task["minutes"] == 3


def test_next_auto_task_participate_after_refresh_hour() -> None:
    task = next_auto_task(_cn(datetime(2026, 7, 17, 3, 10)))
    assert task["label"] == "自动参与"
    assert task["minutes"] == 55


def test_next_auto_task_winner_uses_real_0504_slot() -> None:
    task = next_auto_task(_cn(datetime(2026, 7, 17, 5, 2)))
    assert task["label"] == "中奖深探"
    assert task["minutes"] == 2
