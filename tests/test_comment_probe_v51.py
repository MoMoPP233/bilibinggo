from __future__ import annotations

import importlib
import json
import threading
from pathlib import Path
from unittest.mock import Mock

import httpx
from unittest.mock import MagicMock

from src import comment_breaker as breaker
from src import comment_probe as probe
from src.comment_confirmation import is_comment_confirmed
from src.lottery_actions import ActionResult, comment_dynamic, comment_dynamic_probe


def _candidate(dynamic_id: str = "1251573729479098372") -> dict[str, str]:
    return {"dynamic_id": dynamic_id, "lottery_type": "转发抽奖"}


def _due(monkeypatch, *, stamp: int = 10_000) -> None:
    monkeypatch.setattr(breaker.time, "time", lambda: stamp)
    breaker.trigger_comment_limit()


def _patch_readiness(monkeypatch) -> None:
    monkeypatch.setattr(probe, "require_login", lambda: ("csrf", 42))
    monkeypatch.setattr("src.lottery_actions.require_login", lambda: ("csrf", 42))
    monkeypatch.setattr("src.comment_confirmation.get_login_uid", lambda: 42)
    monkeypatch.setattr(
        probe,
        "resolve_comment_rid_and_type",
        lambda _client, _dynamic_id: ("rid", 17, "https://www.bilibili.com/opus/1251573729479098372"),
    )
    monkeypatch.setattr(
        probe,
        "resolve_participate_text_for_activity",
        lambda _client, *, dynamic_id: Mock(text="probe text"),
    )


def test_success_probe_only_comments_then_clears_and_confirms(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    calls = []

    def fake_comment(*args, **kwargs):
        calls.append(kwargs)
        return ActionResult("comment", True, "ok")

    monkeypatch.setattr(probe, "comment_dynamic_probe", fake_comment)

    result = probe.run_comment_recovery_probe(
        client=Mock(), now=10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS,
        candidate=_candidate(),
    )

    state = breaker.load_comment_breaker()
    assert result["result"] == "success"
    assert len(calls) == 1
    assert state["active"] is False
    assert state["probe_count"] == 1
    assert state["last_probe_result"] == "success"
    from src.comment_confirmation import is_comment_confirmed

    assert is_comment_confirmed(_candidate()["dynamic_id"]) is True


def test_first_12014_probe_schedules_only_the_second_probe(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    monkeypatch.setattr(
        probe,
        "comment_dynamic_probe",
        lambda *args, **kwargs: ActionResult("comment", False, "code=12014 仍受限"),
    )
    before = breaker.load_comment_breaker()["triggered_at"]

    result = probe.run_comment_recovery_probe(
        client=Mock(), now=10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS,
        candidate=_candidate(),
    )

    state = breaker.load_comment_breaker()
    assert result["result"] == "12014"
    assert state["active"] is True
    assert state["triggered_at"] == before
    assert state["last_seen_at"] == 10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    assert state["probe_count"] == 1
    assert state["last_probe_result"] == "12014"
    assert state["next_probe_at"] == (
        10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS * 2
    )


def test_second_probe_before_due_does_not_post(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    post = Mock(return_value=ActionResult("comment", False, "code=12014 仍受限"))
    monkeypatch.setattr(probe, "comment_dynamic_probe", post)
    first_at = 10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    probe.run_comment_recovery_probe(client=Mock(), now=first_at, candidate=_candidate())

    result = probe.run_comment_recovery_probe(
        client=Mock(), now=first_at + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS - 1,
        candidate=_candidate(),
    )

    assert result["reason"] == "not_due"
    assert post.call_count == 1
    assert breaker.load_comment_breaker()["probe_count"] == 1


def test_second_probe_success_clears_after_exactly_two_comments(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    calls = []
    outcomes = [
        ActionResult("comment", False, "code=12014 仍受限"),
        ActionResult("comment", True, "ok"),
    ]
    monkeypatch.setattr(probe, "comment_dynamic_probe", lambda *args, **kwargs: (calls.append(kwargs) or outcomes.pop(0)))
    first_at = 10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    probe.run_comment_recovery_probe(client=Mock(), now=first_at, candidate=_candidate())
    second_at = first_at + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    result = probe.run_comment_recovery_probe(client=Mock(), now=second_at, candidate=_candidate())

    state = breaker.load_comment_breaker()
    assert result["result"] == "success"
    assert len(calls) == 2
    assert state["active"] is False
    assert state["probe_count"] == 2
    assert state["next_probe_at"] is None


def test_repeated_12014_schedules_next_probe_until_success(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    post = Mock(side_effect=[
        ActionResult("comment", False, "code=12014 仍受限"),
        ActionResult("comment", False, "code=12014 仍受限"),
        ActionResult("comment", False, "code=12014 仍受限"),
        ActionResult("comment", True, "ok"),
    ])
    monkeypatch.setattr(probe, "comment_dynamic_probe", post)
    triggered_at = breaker.load_comment_breaker()["triggered_at"]
    first_at = 10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    probe.run_comment_recovery_probe(client=Mock(), now=first_at, candidate=_candidate())
    second_at = first_at + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    probe.run_comment_recovery_probe(client=Mock(), now=second_at, candidate=_candidate())
    third_at = second_at + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    probe.run_comment_recovery_probe(client=Mock(), now=third_at, candidate=_candidate())
    fourth_at = third_at + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    fourth = probe.run_comment_recovery_probe(client=Mock(), now=fourth_at, candidate=_candidate())
    after_success = probe.run_comment_recovery_probe(client=Mock(), now=fourth_at + 10 * 3600, candidate=_candidate())

    state = breaker.load_comment_breaker()
    assert fourth["result"] == "success"
    assert after_success["reason"] == "inactive"
    assert post.call_count == 4
    assert state["active"] is False
    assert state["probe_count"] == 4
    assert state["next_probe_at"] is None
    assert triggered_at == 10_000


def test_ten_consecutive_12014_probes_have_no_count_limit(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    post = Mock(return_value=ActionResult("comment", False, "code=12014 仍受限"))
    monkeypatch.setattr(probe, "comment_dynamic_probe", post)
    probe_at = 10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS

    for _ in range(10):
        result = probe.run_comment_recovery_probe(client=Mock(), now=probe_at, candidate=_candidate())
        assert result["result"] == "12014"
        state = breaker.load_comment_breaker()
        assert state["probe_count"] == post.call_count
        assert state["next_probe_at"] == probe_at + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
        probe_at += breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS

    assert post.call_count == 10
    assert breaker.load_comment_breaker()["active"] is True


def test_step_three_terminal_state_runs_one_legacy_recovery_probe(isolated_home, monkeypatch) -> None:
    path = Path(isolated_home) / "profiles" / "account-1" / breaker.COMMENT_BREAKER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1,
        "active": True,
        "code": 12014,
        "triggered_at": 1_000,
        "last_seen_at": 8_000,
        "probe_count": 2,
        "last_probe_at": 8_000,
        "last_probe_result": "12014",
        "next_probe_at": None,
    }), encoding="utf-8")
    _patch_readiness(monkeypatch)
    post = Mock(return_value=ActionResult("comment", False, "code=12014 仍受限"))
    monkeypatch.setattr(probe, "comment_dynamic_probe", post)

    result = probe.run_comment_recovery_probe(
        client=Mock(), now=8_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS,
        candidate=_candidate(),
    )

    state = breaker.load_comment_breaker()
    assert result["result"] == "12014"
    assert post.call_count == 1
    assert state["probe_count"] == 3
    assert state["last_probe_at"] == 8_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    assert state["next_probe_at"] == 8_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS * 2


def test_second_probe_concurrency_has_one_post_and_count_two(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    post = Mock(side_effect=[
        ActionResult("comment", False, "code=12014 仍受限"),
        ActionResult("comment", True, "ok"),
    ])
    monkeypatch.setattr(probe, "comment_dynamic_probe", post)
    first_at = 10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    probe.run_comment_recovery_probe(client=Mock(), now=first_at, candidate=_candidate())
    second_at = first_at + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    results = []

    def run() -> None:
        results.append(probe.run_comment_recovery_probe(client=Mock(), now=second_at, candidate=_candidate()))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert post.call_count == 2
    assert sum(bool(item.get("claimed")) for item in results) == 1
    assert breaker.load_comment_breaker()["probe_count"] == 2


def test_no_candidate_keeps_first_probe_due_for_later_candidate(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    first_at = 10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    monkeypatch.setattr("web.activity_service.pick_comment_probe_candidate", lambda: None)

    result = probe.run_comment_recovery_probe(client=Mock(), now=first_at, candidate=None)

    state = breaker.load_comment_breaker()
    assert result["reason"] == "no_candidate"
    assert state["probe_count"] == 0
    assert state["last_probe_at"] is None
    assert state["next_probe_at"] == first_at


def test_restart_preserves_second_probe_schedule(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    monkeypatch.setattr(
        probe,
        "comment_dynamic_probe",
        lambda *args, **kwargs: ActionResult("comment", False, "code=12014 仍受限"),
    )
    first_at = 10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    probe.run_comment_recovery_probe(client=Mock(), now=first_at, candidate=_candidate())
    expected = breaker.load_comment_breaker()
    importlib.reload(breaker)

    restored = breaker.load_comment_breaker()
    assert restored["triggered_at"] == expected["triggered_at"]
    assert restored["probe_count"] == 1
    assert restored["next_probe_at"] == expected["next_probe_at"]


def test_unknown_probe_is_never_resent_after_restart(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    calls = []

    def fake_comment(*args, **kwargs):
        calls.append(1)
        raise httpx.ReadTimeout("response lost")

    monkeypatch.setattr(probe, "comment_dynamic_probe", fake_comment)
    first = probe.run_comment_recovery_probe(
        client=Mock(), now=10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS,
        candidate=_candidate(),
    )
    assert first["result"] == "unknown"
    assert len(calls) == 1
    assert breaker.load_comment_breaker()["next_probe_at"] == (
        10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS * 2
    )
    assert _candidate()["dynamic_id"] in breaker.load_comment_breaker()["probe_excluded_dynamic_ids"]

    importlib.reload(breaker)
    assert breaker.load_comment_breaker()["last_probe_result"] == "unknown"
    assert breaker.load_comment_breaker()["next_probe_at"] == (
        10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS * 2
    )
    assert len(calls) == 1


def test_inconclusive_probe_does_not_try_another_activity(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    calls = []
    monkeypatch.setattr(
        probe,
        "comment_dynamic_probe",
        lambda *args, **kwargs: (calls.append(1) or ActionResult("comment", False, "code=12078 受限")),
    )

    result = probe.run_comment_recovery_probe(
        client=Mock(), now=10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS,
        candidate=_candidate(),
    )

    assert result["result"] == "inconclusive"
    assert calls == [1]
    assert breaker.load_comment_breaker()["active"] is True
    assert breaker.load_comment_breaker()["next_probe_at"] == (
        10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS * 2
    )
    assert _candidate()["dynamic_id"] in breaker.load_comment_breaker()["probe_excluded_dynamic_ids"]


def test_unknown_probe_switches_to_another_candidate_after_one_hour(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    candidate_a = _candidate("1251573729479098372")
    candidate_b = _candidate("1251573729479098373")
    selected = iter([candidate_a, candidate_b])
    monkeypatch.setattr("web.activity_service.pick_comment_probe_candidate", lambda: next(selected))
    post = Mock(side_effect=[httpx.ReadTimeout("response lost"), ActionResult("comment", True, "ok")])
    monkeypatch.setattr(probe, "comment_dynamic_probe", post)
    first_at = 10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS

    first = probe.run_comment_recovery_probe(client=Mock(), now=first_at, candidate=None)
    second = probe.run_comment_recovery_probe(
        client=Mock(), now=first_at + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS, candidate=None,
    )

    assert first["result"] == "unknown"
    assert second["result"] == "success"
    assert [call.kwargs["dynamic_id"] for call in post.call_args_list] == [
        candidate_a["dynamic_id"], candidate_b["dynamic_id"],
    ]


def test_unknown_candidate_cannot_be_forced_back_in_after_restart(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    candidate_a = _candidate("1251573729479098372")
    post = Mock(side_effect=httpx.ReadTimeout("response lost"))
    monkeypatch.setattr(probe, "comment_dynamic_probe", post)
    first_at = 10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    probe.run_comment_recovery_probe(client=Mock(), now=first_at, candidate=candidate_a)
    importlib.reload(breaker)

    second = probe.run_comment_recovery_probe(
        client=Mock(), now=first_at + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS,
        candidate=candidate_a,
    )

    assert second["reason"] == "not_due_or_lost_claim"
    assert post.call_count == 1
    assert breaker.load_comment_breaker()["probe_count"] == 1


def test_inconclusive_probe_switches_candidate_after_one_hour(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    candidate_a = _candidate("1251573729479098372")
    candidate_b = _candidate("1251573729479098373")
    selected = iter([candidate_a, candidate_b])
    monkeypatch.setattr("web.activity_service.pick_comment_probe_candidate", lambda: next(selected))
    post = Mock(side_effect=[
        ActionResult("comment", False, "code=12078 活动禁止评论"),
        ActionResult("comment", True, "ok"),
    ])
    monkeypatch.setattr(probe, "comment_dynamic_probe", post)
    first_at = 10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS

    first = probe.run_comment_recovery_probe(client=Mock(), now=first_at, candidate=None)
    second = probe.run_comment_recovery_probe(
        client=Mock(), now=first_at + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS, candidate=None,
    )

    assert first["result"] == "inconclusive"
    assert second["result"] == "success"
    assert post.call_count == 2


def test_stale_in_flight_startup_recovery_excludes_dynamic_and_schedules_next(isolated_home, monkeypatch) -> None:
    path = Path(isolated_home) / "profiles" / "account-1" / breaker.COMMENT_BREAKER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate_a = _candidate("1251573729479098372")
    path.write_text(json.dumps({
        "version": 1,
        "active": True,
        "code": 12014,
        "triggered_at": 1_000,
        "last_seen_at": 8_000,
        "probe_count": 3,
        "last_probe_at": 8_000,
        "last_probe_result": "in_flight",
        "last_probe_dynamic_id": candidate_a["dynamic_id"],
        "next_probe_at": None,
    }), encoding="utf-8")

    state = breaker.recover_stale_comment_probe(now=8_000 + 20 * 60)

    assert state["last_probe_result"] == "unknown"
    assert state["next_probe_at"] == 8_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    assert candidate_a["dynamic_id"] in state["probe_excluded_dynamic_ids"]


def test_active_in_flight_is_not_recovered_by_normal_scheduler_checks(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    claim = breaker.claim_comment_probe(
        _candidate()["dynamic_id"], now=10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS,
    )
    assert claim is not None

    assert breaker.comment_probe_due(now=10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS + 3600) is False
    assert breaker.load_comment_breaker()["last_probe_result"] == "in_flight"


def test_no_safe_candidate_waits_without_consuming_due_probe(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    candidate_a = _candidate("1251573729479098372")
    candidate_c = _candidate("1251573729479098374")
    selected = iter([candidate_a, None, candidate_c])
    monkeypatch.setattr("web.activity_service.pick_comment_probe_candidate", lambda: next(selected))
    post = Mock(side_effect=[
        httpx.ReadTimeout("response lost"),
        ActionResult("comment", True, "ok"),
    ])
    monkeypatch.setattr(probe, "comment_dynamic_probe", post)
    first_at = 10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS

    probe.run_comment_recovery_probe(client=Mock(), now=first_at, candidate=None)
    no_candidate = probe.run_comment_recovery_probe(
        client=Mock(), now=first_at + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS, candidate=None,
    )
    state = breaker.load_comment_breaker()
    assert no_candidate["reason"] == "no_candidate"
    assert state["probe_count"] == 1
    assert state["last_probe_at"] == first_at
    assert state["next_probe_at"] == first_at + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS

    recovered = probe.run_comment_recovery_probe(
        client=Mock(), now=first_at + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS, candidate=None,
    )
    assert recovered["result"] == "success"
    assert post.call_count == 2


def test_full_automatic_recovery_returns_old_activities_without_manual_clear(isolated_home, monkeypatch) -> None:
    """Offline end-to-end recovery: 12014, 12014, success, then normal eligibility."""
    monkeypatch.setattr(breaker.time, "time", lambda: 10_000)
    _patch_readiness(monkeypatch)
    activity_a = _candidate("1251573729479098372")
    activity_b = _candidate("1251573729479098373")
    activity_c = _candidate("1251573729479098374")
    selected = iter([activity_b, activity_c])
    monkeypatch.setattr("web.activity_service.pick_comment_probe_candidate", lambda: next(selected))
    probe_posts = Mock(side_effect=[
        ActionResult("comment", False, "code=12014 仍受限"),
        ActionResult("comment", True, "ok"),
    ])
    monkeypatch.setattr(probe, "comment_dynamic_probe", probe_posts)
    first_due = 10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS

    # Activity A's normal comment triggers the breaker; no manual clear or
    # recover endpoint is used anywhere in this flow.
    initial_client = Mock()
    initial_client.post_form.return_value = {"code": 12014, "message": "评论频率达到上限"}
    initial = comment_dynamic(
        initial_client,
        rid="rid",
        comment_type=17,
        message="normal participation",
        csrf="csrf",
        referer="https://www.bilibili.com/opus/1251573729479098372",
        dynamic_id=activity_a["dynamic_id"],
    )
    assert initial.ok is False
    assert breaker.is_comment_breaker_active() is True
    first = probe.run_comment_recovery_probe(client=Mock(), now=first_due, candidate=None)
    before_success = probe.run_comment_recovery_probe(
        client=Mock(), now=first_due + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS, candidate=None,
    )

    assert first["result"] == "12014"
    assert before_success["result"] == "success"
    assert breaker.is_comment_breaker_active() is False
    assert is_comment_confirmed(activity_c["dynamic_id"]) is True
    assert probe_posts.call_count == 2

    normal_client = Mock()
    skipped = comment_dynamic(
        normal_client,
        rid="rid",
        comment_type=17,
        message="probe text",
        csrf="csrf",
        referer="https://www.bilibili.com/opus/1251573729479098374",
        dynamic_id=activity_c["dynamic_id"],
    )
    assert skipped.detail == "已评论，跳过"
    normal_client.post_form.assert_not_called()

    rows = [
        {
            "dynamic_id": item["dynamic_id"],
            "activity_status": "未参加",
            "can_participate": True,
            "lottery_type": "转发抽奖",
            "last_participation": None,
        }
        for item in (activity_a, activity_b, activity_c)
    ]
    from web import activity_service

    monkeypatch.setattr(activity_service, "_filtered_activity_rows", lambda **_kwargs: rows)
    targets = activity_service.pick_triple_participate_targets(limit=3)
    target_ids = {item["dynamic_id"] for item in targets}
    assert activity_a["dynamic_id"] in target_ids
    assert activity_c["dynamic_id"] in target_ids


def test_due_probe_claim_is_single_writer_under_concurrency(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    calls = []
    lock = threading.Lock()

    def fake_comment(*args, **kwargs):
        with lock:
            calls.append(1)
        return ActionResult("comment", True, "ok")

    monkeypatch.setattr(probe, "comment_dynamic_probe", fake_comment)
    results = []

    def run() -> None:
        results.append(
            probe.run_comment_recovery_probe(
                client=Mock(), now=10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS,
                candidate=_candidate(),
            )
        )

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert sum(bool(item.get("claimed")) for item in results) == 1
    assert breaker.load_comment_breaker()["probe_count"] == 1


def test_past_due_without_candidate_does_not_claim_or_log(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    monkeypatch.setattr("web.activity_service.pick_comment_probe_candidate", lambda: None)
    monkeypatch.setattr(probe, "comment_dynamic_probe", Mock())
    monkeypatch.setattr(probe, "require_login", Mock(side_effect=AssertionError("no probe")))

    result = probe.run_comment_recovery_probe(
        client=Mock(), now=10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS,
        candidate=None,
    )

    assert result["reason"] == "no_candidate"
    assert breaker.load_comment_breaker()["probe_count"] == 0
    probe.comment_dynamic_probe.assert_not_called()


def test_not_due_does_not_resolve_candidate_or_make_remote_reads(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    monkeypatch.setattr(probe, "require_login", Mock(side_effect=AssertionError("not due")))
    monkeypatch.setattr(probe, "resolve_comment_rid_and_type", Mock(side_effect=AssertionError("not due")))

    result = probe.run_comment_recovery_probe(
        client=Mock(), now=10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS - 1,
        candidate=_candidate(),
    )

    assert result["reason"] == "not_due"


def test_in_flight_state_blocks_restart_resend(isolated_home, monkeypatch) -> None:
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    claim = breaker.claim_comment_probe(
        _candidate()["dynamic_id"],
        now=10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS,
    )
    assert claim is not None
    monkeypatch.setattr(probe, "comment_dynamic_probe", Mock())

    result = probe.run_comment_recovery_probe(
        client=Mock(), now=10_000 + breaker.COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS + 1,
        candidate=_candidate(),
    )

    assert result["claimed"] is False
    assert breaker.load_comment_breaker()["last_probe_result"] == "in_flight"
    probe.comment_dynamic_probe.assert_not_called()


def test_scheduler_slot_uses_probe_without_starting_triple_job(isolated_home, monkeypatch) -> None:
    from web.auto_scheduler import AutoScheduler

    runner = MagicMock()
    runner.is_running.return_value = False
    scheduler = AutoScheduler(job_runner=runner)
    click = Mock(side_effect=AssertionError("probe must not invoke participate_triple"))
    scheduler._click_and_wait = click
    monkeypatch.setattr(
        "src.comment_probe.run_comment_recovery_probe",
        lambda: {"claimed": True, "result": "success", "dynamic_id": _candidate()["dynamic_id"]},
    )

    scheduler._run_triple_slot("probe-slot")

    assert "probe-slot" in scheduler._done_triple
    click.assert_not_called()
    runner.try_start.assert_not_called()


def test_probe_comment_rechecks_breaker_and_keeps_retries_zero(isolated_home, monkeypatch) -> None:
    _patch_readiness(monkeypatch)
    _due(monkeypatch)
    client = Mock()
    client.post_form.return_value = {"code": 0}

    result = comment_dynamic_probe(
        client,
        rid="rid",
        comment_type=17,
        message="probe",
        csrf="csrf",
        referer="https://www.bilibili.com/opus/1251573729479098372",
        dynamic_id=_candidate()["dynamic_id"],
    )

    assert result.ok is True
    assert client.post_form.call_args.kwargs["retries"] == 0
    breaker.clear_comment_breaker()
    client.post_form.reset_mock()
    cancelled = comment_dynamic_probe(
        client,
        rid="rid",
        comment_type=17,
        message="probe",
        csrf="csrf",
        referer="https://www.bilibili.com/opus/1251573729479098372",
        dynamic_id=_candidate()["dynamic_id"],
    )
    assert cancelled.detail == "恢复探测已取消"
    client.post_form.assert_not_called()
