"""V26 Scheduler Cadence 专项（fake clock / mock，零真实网络/通知/mutation）。

V38 更新参与/中奖预期；保留 V26 其余 cadence 与全部安全回归。
CASE 1-4  24h 计数：refresh 4 / Following 6 / Winner 2。
CASE 5    Triple 不再枚举固定 cron slot。
CASE 6-9  精确时间（refresh/Following/Winner）。
CASE 10-11 Triple 仅展示已保存计划；固定任务分钟仍优先。
CASE 12   collision 只 skip（无 fatal / 无补跑 / 无并发）。
CASE 13-14 旧 persistent slot 不补跑；restart 同 slot 不重复。
CASE 15   stale generation 无 authority。
CASE 16-17 V24 cooldown 期间 remote slot 0 请求；到期不补跑。
CASE 18   普通 network error 不 fatal。
CASE 19   V22 budget 不因新 Cleanup slot 重置。
CASE 20   V25 Winner 过滤不受时间调整影响。
CASE 21-22 participate_triple 仍走同一 action；unknown outcome 不 replay。
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

CN_TZ = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def _isolate_profile_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src import data_paths

    monkeypatch.setattr(data_paths, "get_profile_dir", lambda profile_id=None: tmp_path)


def _scheduler() -> tuple[object, MagicMock]:
    from web.auto_scheduler import AutoScheduler

    runner = MagicMock()
    scheduler = AutoScheduler(job_runner=runner)
    scheduler._status.state = "running"
    return scheduler, runner


def _day_candidates():
    from web.auto_scheduler import _next_occurrence_candidates

    # 从 7/16 23:59 起枚举 24h：覆盖 7/17 00:00 至 23:59 的完整一天。
    return _next_occurrence_candidates(datetime(2026, 7, 16, 23, 59, tzinfo=CN_TZ))


def _times(candidates, kind: str) -> list[str]:
    return sorted({due.strftime("%H:%M") for item_kind, _label, due in candidates if item_kind == kind})


# ---------- CASE 1-4: 24h 计数 ----------


def test_case1_4_24h_counts() -> None:
    counts = Counter(kind for kind, _label, _due in _day_candidates())
    assert counts["refresh"] == 4
    assert counts["following"] == 6
    assert counts["winner"] == 2
    assert counts["cleanup"] == 0


def test_case5_triple_has_no_fixed_cron_slots() -> None:
    counts = Counter(kind for kind, _label, _due in _day_candidates())
    assert counts["participate"] == 0
    assert counts["refresh"] != 8
    assert counts["cleanup"] == 0
    assert counts["participate"] != 220


# ---------- CASE 6-9: 精确时间 ----------


def test_case6_refresh_exact_times() -> None:
    assert _times(_day_candidates(), "refresh") == ["07:10", "12:40", "18:10", "22:40"]


def test_case7_following_exact_times() -> None:
    assert _times(_day_candidates(), "following") == ["08:00", "11:00", "14:00", "17:00", "20:00", "23:00"]


def test_case8_winner_uses_twice_daily_times() -> None:
    assert _times(_day_candidates(), "winner") == ["12:10", "22:10"]


def test_case9_cleanup_is_manual_only() -> None:
    assert _times(_day_candidates(), "cleanup") == []


# ---------- CASE 10-11: Triple 与 dedicated 错峰 ----------


def test_case10_triple_shows_only_persisted_next_run() -> None:
    from web.auto_scheduler import AutoScheduler

    now = datetime(2026, 7, 17, 0, 0, tzinfo=CN_TZ)
    AutoScheduler(delay_sampler=lambda low, high: 301)._plan_triple(now, None)
    participate = [due for kind, _label, due in _day_candidates() if kind == "participate"]
    assert participate == [now + timedelta(seconds=301)]


def test_case11_no_same_minute_between_dedicated_and_triple() -> None:
    from web.auto_scheduler import _dedicated_minute

    by_minute: dict[str, set[str]] = {}
    for kind, _label, due in _day_candidates():
        by_minute.setdefault(due.strftime("%H:%M"), set()).add(kind)
    assert all(len(kinds) == 1 for kinds in by_minute.values())
    for kind, _label, due in _day_candidates():
        assert _dedicated_minute(due)


# ---------- CASE 12-15: slot 安全 ----------


def test_case12_collision_skip_only(monkeypatch) -> None:
    from web.auto_scheduler import CollisionError

    scheduler, runner = _scheduler()
    fatal_spy = MagicMock()
    scheduler._fatal = fatal_spy
    monkeypatch.setattr(
        scheduler, "_click_and_wait", MagicMock(side_effect=CollisionError("busy"))
    )

    scheduler._run_triple_slot("2026-07-17-04-15")

    fatal_spy.assert_not_called()
    runner.try_start.assert_not_called()
    assert scheduler._status.state == "running"
    assert scheduler._stop_event.is_set() is False
    assert "2026-07-17-04-15" in scheduler._done_triple
    assert getattr(scheduler, "_thread", None) is None


def test_case13_old_persistent_slot_no_replay(tmp_path: Path, monkeypatch) -> None:
    import json

    import web.auto_scheduler as scheduler_mod
    from web.auto_scheduler import _next_slot

    # 旧版本（8 次/天）已消费的 03:00 refresh slot 仍留在 slot 文件里。
    assert scheduler_mod._persist_slot_claimed("refresh", "2026-07-16-03") is True

    # 03:00 不再产生 refresh slot；下一项是新的 06:00 刻度。
    slot = _next_slot(datetime(2026, 7, 17, 3, 0, tzinfo=CN_TZ))
    assert slot["kind"] == "refresh"
    assert slot["at"].endswith("07:10:00")

    # 新调度只在新 slot（00:00）执行一次；旧 key 不会自行触发任何执行。
    scheduler, runner = _scheduler()
    clicks: list[str] = []
    monkeypatch.setattr(
        scheduler,
        "_click_and_wait",
        lambda action, pipeline_index=None: clicks.append(action) or {"skipped": False},
    )
    assert getattr(scheduler, "_thread", None) is None
    runner.try_start.assert_not_called()
    scheduler._run_refresh_batch("2026-07-17-00")
    assert clicks == ["refresh_all", "refresh_watch", "refresh_status"]
    # slot 文件被新 key 覆盖；旧 key 不会造成重复执行或崩溃。
    data = json.loads((tmp_path / "scheduler_slots.json").read_text(encoding="utf-8"))
    assert data["refresh"] == "2026-07-17-00"


def test_case14_restart_same_slot_not_duplicated(monkeypatch) -> None:
    scheduler_a, _runner = _scheduler()
    clicks: list[str] = []
    monkeypatch.setattr(
        scheduler_a,
        "_click_and_wait",
        lambda action, pipeline_index=None: clicks.append(action) or {"skipped": False},
    )
    scheduler_a._run_refresh_batch("2026-07-17-06")
    assert len(clicks) == 3

    scheduler_b, _runner_b = _scheduler()
    clicks_b: list[str] = []
    monkeypatch.setattr(
        scheduler_b,
        "_click_and_wait",
        lambda action, pipeline_index=None: clicks_b.append(action) or {"skipped": False},
    )
    scheduler_b._run_refresh_batch("2026-07-17-06")
    assert clicks_b == []


def test_case15_stale_generation_has_no_authority(tmp_path: Path, monkeypatch) -> None:
    import threading

    scheduler, _runner = _scheduler()
    scheduler._generation = 2
    clicks: list[str] = []
    monkeypatch.setattr(
        scheduler,
        "_click_and_wait",
        lambda action, pipeline_index=None: clicks.append(action) or {"skipped": False},
    )

    def stale_worker() -> None:
        scheduler._ctx.generation = 1
        scheduler._ctx.stop_event = threading.Event()
        scheduler._run_triple_slot("2026-07-17-03-15")

    thread = threading.Thread(target=stale_worker)
    thread.start()
    thread.join(timeout=10)
    assert clicks == []
    assert not (tmp_path / "scheduler_slots.json").exists()


# ---------- CASE 16-18: V24 语义在新 cadence 下保持 ----------


def test_case16_cooldown_blocks_triple_zero_remote(tmp_path: Path) -> None:
    from web.auto_remote_state import record_auto_remote_risk

    record_auto_remote_risk(trigger_stage="t", reason="API error -352", code="-352")
    scheduler, runner = _scheduler()

    scheduler._run_triple_slot("2026-07-17-03-15")

    runner.try_start.assert_not_called()
    assert scheduler._status.state == "running"
    assert scheduler._stop_event.is_set() is False
    assert "2026-07-17-03-15" in scheduler._done_triple


def test_case17_cooldown_expiry_no_auto_replay() -> None:
    from web import auto_remote_state as state_mod

    state_mod.record_auto_remote_risk(
        trigger_stage="t",
        reason="API error -352",
        code="-352",
        now_ts=int(time.time()) - 21 * 60,
    )
    scheduler, runner = _scheduler()

    assert scheduler._remote_gate().allowed is True
    runner.try_start.assert_not_called()
    assert getattr(scheduler, "_thread", None) is None
    assert state_mod.is_risk_paused() is False


def test_case18_ordinary_network_error_not_fatal(monkeypatch) -> None:
    scheduler, _runner = _scheduler()
    fatal_spy = MagicMock()
    scheduler._fatal = fatal_spy
    monkeypatch.setattr(
        scheduler,
        "_click_and_wait",
        MagicMock(
            side_effect=httpx.ConnectError(
                "connection refused", request=httpx.Request("GET", "https://api.bilibili.com")
            )
        ),
    )

    scheduler._run_winner_scan("winner-2026-07-17-05")

    fatal_spy.assert_not_called()
    assert scheduler._status.state == "running"
    assert scheduler._stop_event.is_set() is False
    assert scheduler._status.tasks["winner"]["status"] == "error"


# ---------- CASE 19-22: 其他语义不受 cadence 调整影响 ----------


def test_case19_cleanup_slot_does_not_exist_or_reset_budget(tmp_path: Path, monkeypatch) -> None:
    from src.bilibili_request_budget import (
        get_bilibili_request_budget,
        reserve_bilibili_request,
    )

    now = 1_800_000_000
    for _ in range(3):
        reserve_bilibili_request(now=now, limit=120, window_seconds=6 * 3600)
    from web.auto_scheduler import _next_occurrence_candidates

    assert all(kind != "cleanup" for kind, _label, _due in _next_occurrence_candidates(
        datetime(2026, 7, 17, 4, 5, tzinfo=CN_TZ)
    ))

    snap = get_bilibili_request_budget(now=now, limit=120, window_seconds=6 * 3600)
    assert snap.used_requests == 3
    assert snap.budget_limit == 120


def test_case20_winner_filter_unaffected() -> None:
    from src.winner_scan import service as winmod

    assert winmod.is_welcome_template("感谢关注") is True
    assert winmod.is_welcome_template("恭喜你中奖了") is False
    assert winmod.is_high_confidence_follow_auto_reply(
        winmod.MessageEntry(
            msg_type=winmod.MessageType.DM,
            id="1",
            talker_id=3001,
            text="感谢关注",
            raw={"msg_seqno": 1, "sender_uid": 3001, "system_msg_type": 0},
        ),
        type("S", (), {"is_follow": True})(),
    ) is True


def test_case21_triple_still_uses_same_action(monkeypatch) -> None:
    scheduler, _runner = _scheduler()
    clicks: list[str] = []
    monkeypatch.setattr(
        scheduler,
        "_click_and_wait",
        lambda action, pipeline_index=None: clicks.append(action) or {"skipped": False},
    )

    scheduler._run_triple_slot("2026-07-17-03-15")

    assert clicks == ["participate_triple"]


def test_case22_unknown_outcome_no_replay(monkeypatch) -> None:
    scheduler, _runner = _scheduler()
    monkeypatch.setattr(
        scheduler, "_click_and_wait", MagicMock(side_effect=RuntimeError("unknown outcome"))
    )

    scheduler._run_triple_slot("2026-07-17-03-30")

    assert "2026-07-17-03-30" in scheduler._done_triple
    assert not hasattr(scheduler, "_retry_queue")
    assert not hasattr(scheduler, "_pending_replay")
    assert getattr(scheduler, "_thread", None) is None

    clicks: list[str] = []
    monkeypatch.setattr(
        scheduler,
        "_click_and_wait",
        lambda action, pipeline_index=None: clicks.append(action) or {"skipped": False},
    )
    scheduler._run_triple_slot("2026-07-17-03-30")
    assert clicks == []
