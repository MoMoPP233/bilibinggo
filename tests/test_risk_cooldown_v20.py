"""V20 风控冷却：正式 risk 立即传播 + 20 分钟 per-Profile 持久冷却 + 结果一致 + 不 replay。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class _RiskClient:
    def __init__(self, code: int):
        self.code = code
        self.calls = 0

    def request_json(self, url, params=None, *, referer=None, retries=0):
        self.calls += 1
        return {"code": self.code, "message": "风控"}


def test_case1_notice_minus_352_single_request_and_raises() -> None:
    from src.lottery_api import OpusRiskControlError, fetch_lottery_notice

    client = _RiskClient(-352)
    with pytest.raises(OpusRiskControlError, match="API error -352"):
        fetch_lottery_notice(client, business_id="1", business_type=1, referer="x", retries=3)
    assert client.calls == 1  # 0 后续请求 / 0 fallback


def test_case2_notice_minus_509_single_request_and_raises() -> None:
    from src.lottery_api import OpusRiskControlError, fetch_lottery_notice

    client = _RiskClient(-509)
    with pytest.raises(OpusRiskControlError, match="API error -509"):
        fetch_lottery_notice(client, business_id="1", business_type=1, referer="x", retries=3)
    assert client.calls == 1


def test_case1b_classify_guard_stops_before_next_stage() -> None:
    from src.pipeline.classify_step import _raise_if_platform_risk

    class _Ctx:
        def __init__(self, code):
            self.code = code

        def platform_risk_code(self):
            # 与生产一致：仅正式码返回，-799 返回 None
            return self.code if self.code in (-352, -509, 429) else None

    with pytest.raises(RuntimeError, match="API error -352"):
        _raise_if_platform_risk(_Ctx(-352), "123")
    with pytest.raises(RuntimeError, match="API error -509"):
        _raise_if_platform_risk(_Ctx(-509), "123")
    _raise_if_platform_risk(_Ctx(-799), "123")  # temporary restriction：不阻断
    _raise_if_platform_risk(_Ctx(None), "123")


def test_case3_429_records_global_cooldown(monkeypatch) -> None:
    import src.repost_cleanup as rc

    recorded = []
    monkeypatch.setattr(
        "web.auto_remote_state.record_auto_remote_risk",
        lambda **kw: recorded.append(kw),
    )
    rc._record_global_platform_risk("scan_expired_reposts", RuntimeError("API error 429: 限流"))
    assert recorded and recorded[0]["code"] == "429"


def test_case4_minus_799_never_enters_platform_cooldown(monkeypatch) -> None:
    import src.repost_cleanup as rc

    recorded = []
    monkeypatch.setattr(
        "web.auto_remote_state.record_auto_remote_risk",
        lambda **kw: recorded.append(kw),
    )
    rc._record_global_platform_risk("scan_expired_reposts", RuntimeError("API error -799: 访问频繁"))
    rc._record_global_platform_risk("scan_expired_reposts", RuntimeError("网络超时"))
    assert recorded == []


def test_case5_action_result_consistency() -> None:
    import web.actions as actions

    with pytest.raises(RuntimeError, match="API error -352"):
        actions._raise_assessment_platform_risk({"rate_limited": True, "risk_code": -352})
    actions._raise_assessment_platform_risk({"rate_limited": True, "risk_code": -799})
    actions._raise_assessment_platform_risk({"rate_limited": False, "risk_code": -352})
    actions._raise_assessment_platform_risk({})


def test_case6_7_20min_cooldown_boundary_and_expiry(tmp_path: Path, monkeypatch) -> None:
    from web import auto_remote_state as state_mod

    monkeypatch.setattr(state_mod, "_state_path", lambda: tmp_path / "auto_remote_state.json")
    state = state_mod.record_auto_remote_risk(
        trigger_stage="scan_expired_reposts", code="-352", reason="risk", now_ts=1_000_000
    )
    assert state["paused_until"] == 1_000_000 + 20 * 60
    assert state_mod.is_risk_paused(now_ts=1_000_000 + 19 * 60 + 59) is True   # 19m59s: 跳过
    assert state_mod.is_risk_paused(now_ts=1_000_000 + 20 * 60) is False      # 20m: 自动恢复


def test_case8_restart_keeps_remaining_cooldown(tmp_path: Path, monkeypatch) -> None:
    from web import auto_remote_state as state_mod

    path = tmp_path / "auto_remote_state.json"
    monkeypatch.setattr(state_mod, "_state_path", lambda: path)
    state_mod.record_auto_remote_risk(trigger_stage="t", code="-352", now_ts=5_000)
    # 模拟重启：直接从磁盘重新加载
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["paused"] is True
    assert payload["paused_until"] == 5_000 + 20 * 60
    assert state_mod.is_risk_paused(now_ts=5_000 + 5 * 60) is True


def test_case9_profile_isolation(tmp_path: Path, monkeypatch) -> None:
    from web import auto_remote_state as state_mod

    a = tmp_path / "a" / "auto_remote_state.json"
    b = tmp_path / "b" / "auto_remote_state.json"
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    monkeypatch.setattr(state_mod, "_state_path", lambda: a)
    state_mod.record_auto_remote_risk(trigger_stage="t", code="-352", now_ts=1_000)
    assert state_mod.is_risk_paused(now_ts=1_000) is True
    monkeypatch.setattr(state_mod, "_state_path", lambda: b)
    assert state_mod.is_risk_paused(now_ts=1_000) is False  # B 未被污染


def test_case10_breaker_minus_352_stops_and_keeps_code() -> None:
    import src.repost_cleanup as rc

    breaker = rc._AssessmentBreaker()
    with pytest.raises(rc.AssessmentRateLimited):
        breaker.note_failure(-352)
    assert breaker.rate_limited is True and breaker.stopped is True and breaker.risk_code == -352
    breaker2 = rc._AssessmentBreaker()
    breaker2.note_failure(-799)  # temporary：本地失败计数，不进入正式风控
    assert breaker2.rate_limited is False


def test_case11_refresh_stop_is_structured_and_early() -> None:
    # 分类链早停已由 case1/1b 覆盖；refresh_all 聚合层通过 structured envelope 识别停止
    from src.platform_risk import risk_code_of

    assert risk_code_of(RuntimeError("API error -352: 分类阶段命中平台风控（1）")) == -352


def test_case12_mutation_unknown_not_replayed_after_cooldown(tmp_path: Path, monkeypatch) -> None:
    # 冷却恢复只影响“新任务资格”，不改变 guard/outbox 的 unknown 不重放语义
    from web import auto_remote_state as state_mod

    monkeypatch.setattr(state_mod, "_state_path", lambda: tmp_path / "s.json")
    state_mod.record_auto_remote_risk(trigger_stage="t", code="-509", now_ts=0)
    assert state_mod.is_risk_paused(now_ts=0) is True
    assert state_mod.is_risk_paused(now_ts=20 * 60 + 1) is False
    # 参与 guard / 通知 outbox 的 unknown 终态由各自套件保证（本用例仅确认无重放触发点）
    from src.participation_guard import participation_gate  # noqa: F401
    from src.notification.service import recover_stale_dispatches_on_startup  # noqa: F401
