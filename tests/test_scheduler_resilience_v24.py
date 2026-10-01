"""V24 Scheduler Resilience 专项测试（全离线 mock/fixture，零真实网络）。

CASE 1-9  普通 network/DNS/TLS/timeout/HTTP5xx/LLM/login/401/等待超时：不 fatal。
CASE 10-12 -352/-509/Bilibili HTTP429：当前 Job 停止 + 20 分钟 cooldown + Scheduler running。
CASE 13-15 cooldown 内 remote slot 0 请求、slot 消费、本地 status_refresh 仍运行。
CASE 16    cooldown 到期只恢复资格，不自动补跑、无后台线程。
CASE 17-19 cleanup 身份阶段风控必须结构化上抛（cooldown + DELETE=0 + 非 success）。
CASE 20-22 RequestBudgetExhausted / LLM429 / -799：不 fatal、不写 B 站 cooldown。
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from tests.scheduler_test_helpers import daytime_cn_clock


@pytest.fixture(autouse=True)
def _isolate_profile_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src import data_paths

    monkeypatch.setattr(data_paths, "get_profile_dir", lambda profile_id=None: tmp_path)


def _request() -> httpx.Request:
    return httpx.Request("GET", "https://api.bilibili.com/x/test")


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = _request()
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


def _scheduler() -> tuple[object, MagicMock, MagicMock]:
    from web.auto_scheduler import AutoScheduler

    runner = MagicMock()
    scheduler = AutoScheduler(job_runner=runner, clock=daytime_cn_clock)
    scheduler._status.state = "running"
    fatal_spy = MagicMock()
    scheduler._fatal = fatal_spy
    return scheduler, runner, fatal_spy


def _assert_alive(scheduler, fatal_spy: MagicMock) -> None:
    fatal_spy.assert_not_called()
    assert scheduler._status.state == "running"
    assert scheduler._stop_event.is_set() is False


def _logs(scheduler) -> str:
    return "\n".join(entry.message for entry in scheduler._logs)


def _risk_paused() -> bool:
    from web import auto_remote_state as state_mod

    return state_mod.is_risk_paused()


# ---------- CASE 1-9: 普通业务错误绝不 fatal ----------

ORDINARY_ERRORS = [
    ("connect", httpx.ConnectError("connection refused", request=_request())),
    ("dns", httpx.ConnectError("Name or service not known", request=_request())),
    ("tls_eof", httpx.RemoteProtocolError("Server disconnected without response", request=_request())),
    ("timeout", httpx.ReadTimeout("read timeout", request=_request())),
    ("connect_timeout", httpx.ConnectTimeout("connect timeout", request=_request())),
    ("http503", _status_error(503)),
    ("http500", _status_error(500)),
    ("friendly_network", RuntimeError("无法连接 B 站服务器，请检查网络、代理或 DNS 后点击「刷新账号」")),
    ("raw_network", RuntimeError("网络请求失败: ConnectError")),
    ("job_wait_timeout", RuntimeError("等待「一键更新活动链接」超时（超过 21600 秒）")),
    ("llm503", RuntimeError("LLM 服务暂时不可用（HTTP 503）")),
    ("llm_temporary", RuntimeError("LLM 服务连接超时或网络不可达")),
    ("cookie", RuntimeError("Cookie 已过期，请重新扫码登录")),
    ("login_required", RuntimeError("请先扫码登录后再试")),
    ("auth_401", RuntimeError("LLM 请求失败（HTTP 401）")),
]


@pytest.mark.parametrize("label,error", ORDINARY_ERRORS, ids=[item[0] for item in ORDINARY_ERRORS])
def test_case1_9_ordinary_errors_never_fatal(label, error, monkeypatch) -> None:
    scheduler, runner, fatal_spy = _scheduler()
    monkeypatch.setattr(scheduler, "_click_and_wait", MagicMock(side_effect=error))

    scheduler._run_refresh_batch("v24-ordinary")

    _assert_alive(scheduler, fatal_spy)
    assert "v24-ordinary" in scheduler._done_refresh
    assert scheduler._status.tasks["refresh"]["status"] == "error"
    assert _risk_paused() is False
    logs = _logs(scheduler)
    assert "Scheduler 继续运行" in logs
    assert "致命停机" not in logs
    assert "平台风控" not in logs


def test_case1_connect_error_keeps_local_status_refresh(monkeypatch) -> None:
    """CASE 1/14/15 基础：remote 动作失败不影响本地 refresh_status 继续执行。"""
    scheduler, runner, fatal_spy = _scheduler()
    calls: list[str] = []

    def click(action, *, pipeline_index=None):
        calls.append(action)
        if action == "refresh_all":
            raise httpx.ConnectError("connection refused", request=_request())
        return {"skipped": False, "message": "ok"}

    monkeypatch.setattr(scheduler, "_click_and_wait", click)

    scheduler._run_refresh_batch("v24-split")

    _assert_alive(scheduler, fatal_spy)
    assert calls == ["refresh_all", "refresh_watch", "refresh_status"]
    assert scheduler._status.tasks["refresh"]["status"] == "error"


# ---------- CASE 10-12: 正式风控保持 V20 ----------

RISK_ERRORS = [
    (-352, RuntimeError("API error -352: 风控校验失败")),
    (-509, RuntimeError("API error -509: 访问过于频繁")),
    (429, _status_error(429)),
]


@pytest.mark.parametrize("code,error", RISK_ERRORS, ids=["-352", "-509", "429"])
def test_case10_11_12_platform_risk_pauses_without_fatal(code, error, monkeypatch) -> None:
    from web import auto_remote_state as state_mod

    scheduler, runner, fatal_spy = _scheduler()
    monkeypatch.setattr(scheduler, "_click_and_wait", MagicMock(side_effect=error))

    scheduler._run_following_scan("v24-risk")

    _assert_alive(scheduler, fatal_spy)
    assert "v24-risk" in scheduler._done_following
    assert scheduler._status.tasks["following"]["status"] == "risk_paused"
    state = state_mod.risk_pause_state()
    assert state["paused"] is True
    assert int(state["code"]) == code
    remaining = int(state["paused_until"]) - int(time.time())
    assert 0 < remaining <= 20 * 60
    logs = _logs(scheduler)
    assert "平台风控" in logs
    assert "20 分钟冷却" in logs
    assert "Scheduler 继续运行" in logs
    assert "致命停机" not in logs


# ---------- CASE 13-15: cooldown 行为 ----------

def _seed_cooldown() -> None:
    from web.auto_remote_state import record_auto_remote_risk

    record_auto_remote_risk(trigger_stage="v24-test", reason="API error -352", code="-352")


def test_case13_cooldown_remote_slot_zero_requests() -> None:
    _seed_cooldown()
    scheduler, runner, fatal_spy = _scheduler()

    scheduler._run_following_scan("v24-cd-following")

    runner.try_start.assert_not_called()
    _assert_alive(scheduler, fatal_spy)
    assert "v24-cd-following" in scheduler._done_following
    # 内部结构化状态为 skipped_by_platform_cooldown；API 视图按既有最接近状态
    # risk_paused + resume_at 呈现（get_status 会规范化 tasks 视图）。
    assert scheduler._status.tasks["following"]["status"] == "risk_paused"
    assert scheduler._status.tasks["following"].get("resume_at")
    logs = _logs(scheduler)
    assert "平台风控冷却中" in logs
    assert "预计" in logs


def test_case14_15_cooldown_local_status_runs_remote_zero(monkeypatch) -> None:
    _seed_cooldown()
    scheduler, runner, fatal_spy = _scheduler()
    calls: list[str] = []

    def click(action, *, pipeline_index=None):
        calls.append(action)
        return {"skipped": False, "message": "ok"}

    monkeypatch.setattr(scheduler, "_click_and_wait", click)

    scheduler._run_refresh_batch("v24-cd-refresh")

    assert calls == ["refresh_status"]  # 0 次 refresh_all / refresh_watch
    runner.try_start.assert_not_called()
    _assert_alive(scheduler, fatal_spy)
    assert "v24-cd-refresh" in scheduler._done_refresh
    assert scheduler._status.tasks["refresh"]["status"] == "risk_paused"
    assert "本地状态已刷新" in _logs(scheduler)
    cooldown_logs = [line for line in _logs(scheduler).splitlines() if "平台风控冷却中" in line]
    assert len(cooldown_logs) == 1  # 同一 slot 只记录一次


def test_case16_cooldown_expiry_restores_slot_only_no_auto_replay() -> None:
    from web import auto_remote_state as state_mod

    # 已经到期的 cooldown：只恢复“下一次正常调度资格”。
    state_mod.record_auto_remote_risk(
        trigger_stage="v24-expired",
        reason="API error -352",
        code="-352",
        now_ts=int(time.time()) - 21 * 60,
    )
    scheduler, runner, fatal_spy = _scheduler()

    assert scheduler._remote_gate().allowed is True
    runner.try_start.assert_not_called()
    assert getattr(scheduler, "_thread", None) is None
    assert _risk_paused() is False  # 到期本地清除


# ---------- CASE 17-19: cleanup 风控传播缺口 ----------

@pytest.mark.parametrize(
    "code,stage2",
    [
        (-352, {"rate_limited": True, "risk_code": -352, "stopped_early": True, "message": "risk"}),
        (-509, {"rate_limited": True, "risk_code": -509, "stopped_early": True, "message": "risk"}),
        (429, {"rate_limited": True, "risk_code": 429, "stopped_early": True, "message": "risk"}),
    ],
    ids=["-352", "-509", "429"],
)
def test_case17_18_19_cleanup_cycle_surfaces_risk(code, stage2, isolated_home, monkeypatch) -> None:
    import src.repost_cleanup as rc
    from src.platform_risk import risk_code_of
    from web.actions import run_action

    cycle_result = {
        "ok": True,
        "phases": [
            {"stage": "同步与本地维护", "result": {"synced": True}},
            {"stage": "连续智能评估", "result": stage2},
            {"stage": "strict-safe 自动删除", "result": {"skipped": True}},
        ],
        "message": "自动维护周期完成：3 阶段",
    }
    monkeypatch.setattr(rc, "cleanup_maintenance_cycle", lambda **kwargs: cycle_result)

    with pytest.raises(RuntimeError, match=f"API error {code}") as excinfo:
        run_action("cleanup_maintenance_cycle")

    assert risk_code_of(excinfo.value) == code


@pytest.mark.parametrize("code", [-352, -509, 429])
def test_case17_19_cleanup_risk_skips_delete(code, isolated_home, monkeypatch) -> None:
    import src.repost_cleanup as rc

    monkeypatch.setattr(
        rc,
        "auto_maintain",
        lambda **kwargs: {
            "synced": True,
            "rate_limited": False,
            "stopped_early": False,
            "stop_reason": "",
            "message": "ok",
            "evaluated_originals": 0,
        },
    )
    monkeypatch.setattr(
        rc,
        "scan_expired_reposts",
        lambda **kwargs: {
            "rate_limited": True,
            "risk_code": code,
            "stopped_early": True,
            "message": "risk",
            "pending_originals": 5,
        },
    )
    delete_calls: list[object] = []
    monkeypatch.setattr(
        rc,
        "auto_cleanup_delete",
        lambda **kwargs: delete_calls.append(kwargs) or {"message": "deleted"},
    )

    result = rc.cleanup_maintenance_cycle()

    assert delete_calls == []  # DELETE=0
    stage2 = next(
        phase["result"] for phase in result["phases"] if phase["stage"] == "连续智能评估"
    )
    assert stage2["risk_code"] == code
    assert result.get("ok") is True  # cycle 结构本身仍返回 phases；由 action 层上抛风险


# ---------- CASE 20-22: budget / LLM429 / -799 ----------

def test_case20_request_budget_exhausted_not_fatal_no_cooldown(monkeypatch) -> None:
    from src.bilibili_request_budget import (
        RequestBudgetExhausted,
        get_bilibili_request_budget,
    )

    snapshot = get_bilibili_request_budget(limit=1, window_seconds=60)
    budget_error = RequestBudgetExhausted(snapshot)
    scheduler, runner, fatal_spy = _scheduler()
    monkeypatch.setattr(scheduler, "_click_and_wait", MagicMock(side_effect=budget_error))

    scheduler._run_winner_scan("v24-budget")

    _assert_alive(scheduler, fatal_spy)
    assert scheduler._status.tasks["winner"]["status"] == "error"
    assert _risk_paused() is False
    from src.platform_risk import risk_code_of

    assert risk_code_of(budget_error) is None


def test_case21_llm429_not_fatal_no_bilibili_cooldown(monkeypatch) -> None:
    scheduler, runner, fatal_spy = _scheduler()
    monkeypatch.setattr(
        scheduler,
        "_click_and_wait",
        MagicMock(side_effect=RuntimeError("LLM 服务当前限流，请稍后重试")),
    )

    scheduler._run_winner_scan("v24-llm429")

    _assert_alive(scheduler, fatal_spy)
    assert _risk_paused() is False
    logs = _logs(scheduler)
    assert "Scheduler 继续运行" in logs
    assert "平台风控" not in logs


def test_case22_minus_799_unchanged(monkeypatch) -> None:
    from src.platform_risk import is_platform_risk_code, matches_platform_risk

    assert is_platform_risk_code(-799) is False
    minus_799 = RuntimeError("API error -799: 请求过于频繁")
    assert matches_platform_risk(minus_799) is False

    scheduler, runner, fatal_spy = _scheduler()
    monkeypatch.setattr(scheduler, "_click_and_wait", MagicMock(side_effect=minus_799))

    scheduler._run_triple_slot("v24-799")

    _assert_alive(scheduler, fatal_spy)
    assert _risk_paused() is False


# ---------- Scheduler invariant fatal 仍保留 ----------

def test_scheduler_internal_fatal_still_works() -> None:
    from web.auto_scheduler import AutoScheduler

    scheduler = AutoScheduler(job_runner=MagicMock(), clock=daytime_cn_clock)
    scheduler._fatal("调度器内部不可恢复错误")

    assert scheduler._status.state == "fatal"
    assert scheduler._stop_event.is_set() is True
    assert "内部" in str(scheduler._status.fatal_error)
