"""V118 safety regressions: temporary roots, mock transport, no real mutations."""
from __future__ import annotations

from contextlib import closing
import json
import re
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from src import comment_breaker as breaker, comment_probe as probe
from src.comment_confirmation import get_comment_status
from src.lottery_actions import comment_dynamic
from src.platform_risk import risk_code_of
from tests.test_comment_probe_v51 import _candidate, _due, _patch_readiness
from tests.test_login_onboarding_m7 import root  # fixture
from tests.test_winner_deep_scan_m3 import (
    WIN_TEXT, _dm_entry, _install, _session, profile,
)
from src.winner_scan import service as winner


def test_probe_unknown_survives_other_probe_success_and_restart(isolated_home, monkeypatch):
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    a, b = _candidate(), _candidate("1251573729479098373")
    client = Mock()
    client.post_form.side_effect = [httpx.ReadTimeout("lost response"), {"code": 0}]
    assert probe.run_comment_recovery_probe(client=client, now=13600, candidate=a)["result"] == "unknown"
    assert get_comment_status(a["dynamic_id"], uid=42) in {"pending", "unknown"}
    assert probe.run_comment_recovery_probe(client=client, now=17200, candidate=b)["result"] == "success"
    assert not breaker.is_comment_breaker_active()
    import src.comment_confirmation as guards
    # This ledger has no in-memory result cache: reopening its durable file is
    # the restart boundary, without reloading exception classes used by peers.
    state = json.loads(guards._state_path().read_text(encoding="utf-8"))
    assert state["guards"]["42"][a["dynamic_id"]]["status"] == "unknown"
    other = Mock()
    result = comment_dynamic(other, rid="rid", comment_type=17, message="normal",
                             csrf="csrf", referer="local", dynamic_id=a["dynamic_id"], uid=42)
    assert not result.ok
    other.post_form.assert_not_called()
    assert client.post_form.call_count == 2
    assert all(call.kwargs["retries"] == 0 for call in client.post_form.call_args_list)


@pytest.mark.parametrize("risk", [-352, -509, 429])
def test_probe_formal_risk_is_not_swallowed(isolated_home, monkeypatch, risk):
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    client = Mock()
    if risk == 429:
        response = httpx.Response(429, request=httpx.Request("POST", "https://invalid.test"))
        client.post_form.side_effect = httpx.HTTPStatusError("rate limited", request=response.request, response=response)
    else:
        client.post_form.return_value = {"code": risk, "message": "risk"}
    with pytest.raises(Exception) as error:
        probe.run_comment_recovery_probe(client=client, now=13600, candidate=_candidate())
    assert risk_code_of(error.value) == risk
    assert client.post_form.call_count == 1
    assert breaker.is_comment_breaker_active()


@pytest.mark.parametrize("risk", [-352, -509, 429])
def test_probe_formal_risk_survives_terminal_guard_write_failure(isolated_home, monkeypatch, risk):
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    import src.comment_confirmation as guards
    target = _candidate()["dynamic_id"]
    write = guards._write_state_unlocked
    def fail_terminal(state):
        if state["guards"]["42"][target]["status"] != "pending":
            raise OSError("cannot write terminal guard")
        write(state)
    monkeypatch.setattr(guards, "_write_state_unlocked", fail_terminal)
    client = Mock()
    if risk == 429:
        response = httpx.Response(429, request=httpx.Request("POST", "https://invalid.test"))
        client.post_form.side_effect = httpx.HTTPStatusError("limited", request=response.request, response=response)
    else:
        client.post_form.return_value = {"code": risk}
    with pytest.raises(Exception) as error:
        probe.run_comment_recovery_probe(client=client, now=13600, candidate=_candidate())
    assert risk_code_of(error.value) == risk
    assert get_comment_status(target, uid=42) == "pending"
    client.post_form.assert_called_once()


def test_probe_pending_is_durable_before_post_and_write_failure_blocks(isolated_home, monkeypatch):
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    import src.lottery_actions as actions
    candidate = _candidate()
    client = Mock()
    def crash(*args, **kwargs):
        # Do not reacquire the OS mutation gate from inside the POST.
        assert actions._get_comment_status_unlocked(candidate["dynamic_id"], uid=42) == "pending"
        raise KeyboardInterrupt()
    client.post_form.side_effect = crash
    with pytest.raises(KeyboardInterrupt):
        probe.run_comment_recovery_probe(client=client, now=13600, candidate=candidate)
    assert get_comment_status(candidate["dynamic_id"], uid=42) == "pending"
    breaker.recover_stale_comment_probe(now=14000)
    client.post_form.assert_called_once()


def test_probe_guard_write_failure_never_posts(isolated_home, monkeypatch):
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    import src.comment_confirmation as guards
    monkeypatch.setattr(guards, "_write_state_unlocked", Mock(side_effect=OSError("disk full")))
    client = Mock()
    result = probe.run_comment_recovery_probe(client=client, now=13600, candidate=_candidate())
    assert result["result"] == "unknown"
    client.post_form.assert_not_called()


def test_modern_probe_unknown_is_uid_scoped_even_after_clear(isolated_home, monkeypatch):
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    client = Mock()
    client.post_form.side_effect = httpx.ReadTimeout("lost")
    probe.run_comment_recovery_probe(client=client, now=13600, candidate=_candidate())
    breaker.clear_comment_breaker()
    assert get_comment_status(_candidate()["dynamic_id"], uid=42) == "unknown"
    assert get_comment_status(_candidate()["dynamic_id"], uid=99) is None


@pytest.mark.parametrize("response", [{}, {"code": None}, {"code": False}, {"code": "0"}, {"code": -412}])
def test_probe_unreliable_success_does_not_confirm(isolated_home, monkeypatch, response):
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    client = Mock()
    client.post_form.return_value = response
    result = probe.run_comment_recovery_probe(client=client, now=13600, candidate=_candidate())
    assert result["result"] == "inconclusive"
    assert get_comment_status(_candidate()["dynamic_id"], uid=42) == "unknown"


def test_probe_rejected_12014_can_recover_without_false_unknown(isolated_home, monkeypatch):
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    client = Mock()
    client.post_form.side_effect = [{"code": 12014}, {"code": 0}]
    assert probe.run_comment_recovery_probe(client=client, now=13600, candidate=_candidate())["result"] == "12014"
    assert get_comment_status(_candidate()["dynamic_id"], uid=42) == "failed"
    assert probe.run_comment_recovery_probe(client=client, now=17200, candidate=_candidate())["result"] == "success"
    assert get_comment_status(_candidate()["dynamic_id"], uid=42) == "confirmed"


def test_legacy_probe_unknown_survives_clear_without_guessing_uid(isolated_home, monkeypatch):
    _due(monkeypatch)
    _patch_readiness(monkeypatch)
    candidate = _candidate()
    breaker.claim_comment_probe(candidate["dynamic_id"], now=13600)
    breaker.record_comment_probe_result("unknown", now=13600)
    breaker.clear_comment_breaker()
    assert get_comment_status(candidate["dynamic_id"], uid=42) == "unknown"
    assert get_comment_status(candidate["dynamic_id"], uid=99) == "unknown"


@pytest.mark.parametrize("risk", [-352, -509, 429])
def test_winner_nickname_risk_keeps_candidate_but_stops_remote(profile, monkeypatch, risk):
    _install(monkeypatch, sessions=[_session(202, 2, name=""), _session(203, 1, name="")],
             dm_messages={202: [_dm_entry(202, 1, WIN_TEXT, talker_name="")],
                          203: [_dm_entry(203, 1, WIN_TEXT, talker_name="")]})
    client = Mock()
    if risk == 429:
        response = httpx.Response(429, request=httpx.Request("GET", "https://invalid.test"))
        client.get_json.side_effect = httpx.HTTPStatusError("limited", request=response.request, response=response)
    else:
        client.get_json.return_value = {"code": risk, "message": "risk"}
    with pytest.raises(Exception) as error:
        winner.service.run(client, notify=False)
    assert risk_code_of(error.value) == risk
    client.get_json.assert_called_once()
    candidates = winner.list_candidates()
    assert candidates and any(item["sender"]["id"] == 202 for item in candidates)
    assert not winner._load_state()["dm"].get("last_success_at")


def test_frozen_onboarding_uses_supported_entry_and_keeps_runtime(root, monkeypatch):
    from src import login_onboarding as onboarding, data_paths
    from src.db import schema
    before = data_paths.get_runtime_profile_id()
    calls = []
    real_run = onboarding.subprocess.run
    python = sys.executable
    def fake_frozen_child(command, **kwargs):
        calls.append(command)
        assert "-c" not in command
        assert command[1] == "--init-profile-db"
        # Execute the same internal initializer under real source Python.
        kwargs["cwd"] = str(Path(__file__).resolve().parents[1])
        return real_run([python, "-m", "src.profile_db_init", command[2]], **kwargs)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(onboarding.subprocess, "run", fake_frozen_child)
    result = onboarding.commit_staged_login(cookie_str="DedeUserID=90001;SESSDATA=test", nickname="new")
    db = root / "profiles" / result["profile_id"] / "binggo.db"
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as connection:
        assert connection.execute("SELECT version FROM schema_meta WHERE id=1").fetchone()[0] == schema.SCHEMA_VERSION
    assert calls and data_paths.get_runtime_profile_id() == before


def test_onboarding_child_false_success_does_not_publish(root, monkeypatch):
    from src import login_onboarding as onboarding
    monkeypatch.setattr(onboarding.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr=""))
    with pytest.raises(onboarding.OnboardingError):
        onboarding.commit_staged_login(cookie_str="DedeUserID=90001;SESSDATA=test")
    assert sorted(p.name for p in (root / "profiles").iterdir()) == ["account-1"]


def test_installer_never_force_stops_or_restarts_instances():
    script = (Path(__file__).resolve().parents[1] / "packaging/windows/installer.iss").read_text(encoding="utf-8")
    assert "taskkill" not in script.lower()
    assert "CloseApplications=no" in script
    assert "RestartApplications=no" in script
    assert "Win32_Process" in script and "Binggo.exe" in script
    assert "PrepareToInstall" in script and "InitializeUninstall" in script


@pytest.mark.parametrize("processes, allowed", [
    ([], True),
    (["supervisor", "service"], False),
    (["A:supervisor", "A:service", "B:supervisor", "B:service"], False),
    (["orphan:service"], False),
])
def test_installer_process_count_policy_blocks_all_live_instances(processes, allowed):
    # Static policy simulation, not execution of native Inno Setup/WMI. Take
    # the accepted count from the actual Pascal predicate, not a second policy.
    script = (Path(__file__).resolve().parents[1] / "packaging/windows/installer.iss").read_text(encoding="utf-8")
    body = script.split("function BinggoStopped(): Boolean;", 1)[1].split("function InitializeSetup", 1)[0]
    condition = re.search(r"Result := Processes\.Count = (\d+);", body)
    assert condition is not None
    assert (len(processes) == int(condition[1])) is allowed
    failure_handler = body.split("except", 1)[1]
    assert "Result := False;" in body.split("try", 1)[0]
    assert "Result := True" not in failure_handler


def test_frozen_initializer_dispatch_does_not_enter_live_runtime(monkeypatch, tmp_path):
    import binggo_launcher as launcher
    from src import profile_db_init, data_paths
    initializer = Mock(return_value=0)
    monkeypatch.setattr(profile_db_init, "main", initializer)
    monkeypatch.setattr(data_paths, "ensure_data_root_selected", Mock(side_effect=AssertionError("must not select runtime")))
    monkeypatch.setattr(launcher, "_acquire_single_instance", Mock(side_effect=AssertionError("must not lock runtime")))
    monkeypatch.setattr(sys, "argv", ["Binggo.exe", "--init-profile-db", str(tmp_path)])
    assert launcher.main() == 0
    initializer.assert_called_once_with(str(tmp_path))


@pytest.mark.parametrize("name", ["account-1", ".creating-account-2-not-an-attempt", ".creating-account-2-123456789abc"])
def test_initializer_never_overwrites_published_or_existing_db(tmp_path, name):
    from src.profile_db_init import initialize_staging_database
    path = tmp_path / "profiles" / name
    path.mkdir(parents=True)
    db = path / "binggo.db"
    db.write_bytes(b"existing data")
    with pytest.raises(ValueError):
        initialize_staging_database(str(path))
    assert db.read_bytes() == b"existing data"
