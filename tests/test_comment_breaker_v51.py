from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

from src import comment_breaker as breaker
from src.comment_breaker import (
    COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS,
    clear_comment_breaker,
    comment_breaker_snapshot,
    is_comment_breaker_active,
    load_comment_breaker,
    trigger_comment_limit,
)


def test_first_12014_records_recovery_state(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr(breaker.time, "time", lambda: 1_000)

    state = trigger_comment_limit(message="server limit")

    assert state["active"] is True
    assert state["triggered_at"] == 1_000
    assert state["last_seen_at"] == 1_000
    assert state["next_probe_at"] == 1_000 + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    assert state["probe_count"] == 0
    assert state["last_probe_at"] is None
    assert state["last_probe_result"] is None


def test_repeated_12014_keeps_cycle_start_and_updates_last_seen(isolated_home, monkeypatch) -> None:
    now = {"value": 1_000}
    monkeypatch.setattr(breaker.time, "time", lambda: now["value"])
    first = trigger_comment_limit()
    now["value"] = 1_200
    second = trigger_comment_limit(message="still limited")

    assert second["triggered_at"] == first["triggered_at"]
    assert second["next_probe_at"] == first["next_probe_at"]
    assert second["last_seen_at"] == 1_200
    assert second["probe_count"] == 0


def test_first_12014_logs_once_and_repeat_does_not_spam(isolated_home, monkeypatch) -> None:
    now = {"value": 1_000}
    monkeypatch.setattr(breaker.time, "time", lambda: now["value"])
    logger = Mock()
    monkeypatch.setattr(breaker, "logger", logger)

    trigger_comment_limit()
    now["value"] = 1_200
    trigger_comment_limit()

    assert logger.info.call_count == 1
    assert "评论暂停" in logger.info.call_args.args[0]


def test_legacy_active_state_loads_with_in_memory_defaults(isolated_home) -> None:
    path = Path(isolated_home) / "profiles" / "account-1" / breaker.COMMENT_BREAKER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "version": 1,
            "active": True,
            "code": 12014,
            "message": "legacy",
            "triggered_at": 2_000,
            "last_seen_at": 2_100,
        }),
        encoding="utf-8",
    )

    state = load_comment_breaker()

    assert state["active"] is True
    assert state["next_probe_at"] == 2_000 + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    assert state["probe_count"] == 0
    assert "next_probe_at" not in json.loads(path.read_text(encoding="utf-8"))


def test_step_three_12014_state_is_rearmed_and_persisted(isolated_home) -> None:
    path = Path(isolated_home) / "profiles" / "account-1" / breaker.COMMENT_BREAKER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "version": 1,
            "active": True,
            "code": 12014,
            "message": "still limited",
            "triggered_at": 1_000,
            "last_seen_at": 8_000,
            "probe_count": 2,
            "last_probe_at": 8_000,
            "last_probe_result": "12014",
            "next_probe_at": None,
        }),
        encoding="utf-8",
    )

    state = load_comment_breaker()

    assert state["next_probe_at"] == 8_000 + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["next_probe_at"] == 8_000 + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
    assert persisted["probe_count"] == 2


def test_terminal_null_states_are_not_rearmed(isolated_home) -> None:
    path = Path(isolated_home) / "profiles" / "account-1" / breaker.COMMENT_BREAKER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    for result in ("in_flight", "unknown", "inconclusive"):
        path.write_text(
            json.dumps({
                "version": 1,
                "active": True,
                "code": 12014,
                "triggered_at": 1_000,
                "last_probe_at": 8_000,
                "probe_count": 2,
                "last_probe_result": result,
                "next_probe_at": None,
            }),
            encoding="utf-8",
        )
        state = load_comment_breaker()
        assert state["next_probe_at"] is None

    path.write_text(json.dumps({"version": 1, "active": False, "next_probe_at": None}), encoding="utf-8")
    assert load_comment_breaker()["next_probe_at"] is None


def test_legacy_inactive_state_remains_inactive(isolated_home) -> None:
    path = Path(isolated_home) / "profiles" / "account-1" / breaker.COMMENT_BREAKER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "active": False, "cleared_at": 3_000}), encoding="utf-8")

    assert is_comment_breaker_active() is False
    assert load_comment_breaker()["active"] is False


def test_profile_isolation_and_restart_persistence(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr(breaker.time, "time", lambda: 4_000)
    trigger_comment_limit()
    expected = load_comment_breaker()

    import importlib

    importlib.reload(breaker)
    assert breaker.load_comment_breaker()["triggered_at"] == expected["triggered_at"]
    assert breaker.load_comment_breaker()["next_probe_at"] == expected["next_probe_at"]
    assert breaker.load_comment_breaker()["probe_count"] == expected["probe_count"]

    other = Path(isolated_home) / "profiles" / "account-2"
    other.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(breaker, "get_profile_dir", lambda: other)
    assert breaker.is_comment_breaker_active() is False


def test_probe_exclusion_is_profile_local(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr(breaker.time, "time", lambda: 4_000)
    trigger_comment_limit()
    breaker.claim_comment_probe("1251573729479098372", now=4_000 + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS)
    breaker.record_comment_probe_result("unknown", now=4_000 + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS)
    other = Path(isolated_home) / "profiles" / "account-2"
    other.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(breaker, "get_profile_dir", lambda: other)

    assert breaker.load_comment_breaker()["probe_excluded_dynamic_ids"] == []


def test_past_probe_time_does_not_probe_or_mutate(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr(breaker.time, "time", lambda: 5_000)
    trigger_comment_limit()
    monkeypatch.setattr(breaker.time, "time", lambda: 5_000 + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS + 1)

    assert breaker.is_comment_breaker_active() is True
    assert breaker.load_comment_breaker()["probe_count"] == 0


def test_corrupt_state_remains_fail_closed(isolated_home) -> None:
    path = Path(isolated_home) / "profiles" / "account-1" / breaker.COMMENT_BREAKER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")

    state = load_comment_breaker()

    assert state["active"] is True
    assert state["code"] == 12014
    assert comment_breaker_snapshot()["active"] is True
