from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from src import participation_guard as guard
from src.db.engine import db_path, reset_engine_for_tests
from src.db.models import ParticipationGuardRow
from src.db.session import session_scope
from src.participation_log import ParticipationActionRecord, append_action_record, load_action_entries_for_uid
from src.participation_store import load_participations

UID = "12345"
DID = "1240566780561195013"
ROOT = Path(__file__).resolve().parents[1]


def test_pending_is_persistent_and_not_a_joined_participation(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr("src.participation_store.participation_uid", lambda: UID)
    assert guard.get_guard(UID, DID) is None
    guard.record_pending(UID, DID)
    assert load_participations() == {}
    reset_engine_for_tests()
    record = guard.get_guard(UID, DID)
    assert record is not None and record.repost_status == "pending"
    with pytest.raises(guard.ParticipationGuardBlocked) as caught:
        guard.record_pending(UID, DID)
    assert caught.value.repost_status == "pending"


@pytest.mark.parametrize("state", ["pending", "confirmed", "unknown", "suspected"])
def test_any_existing_guard_blocks_new_pending(isolated_home, state) -> None:
    with session_scope() as session:
        session.add(ParticipationGuardRow(uid=UID, dynamic_id=DID, repost_status=state))
    with pytest.raises(guard.ParticipationGuardBlocked):
        guard.record_pending(UID, DID)
    assert guard.get_guard(UID, DID).repost_status == state


def test_transitions_never_downgrade_confirmed(isolated_home) -> None:
    guard.record_pending(UID, DID)
    guard.mark_repost_suspected(UID, DID)
    assert guard.get_guard(UID, DID).repost_status == "pending"
    guard.mark_repost_unknown(UID, DID)
    assert guard.get_guard(UID, DID).repost_status == "unknown"
    guard.confirm_repost(UID, DID)
    confirmed = guard.get_guard(UID, DID)
    guard.mark_repost_unknown(UID, DID)
    guard.mark_repost_suspected(UID, DID)
    guard.confirm_repost(UID, DID)
    assert guard.get_guard(UID, DID) == confirmed


def test_unknown_only_updates_pending_and_suspected_only_inserts(isolated_home) -> None:
    guard.mark_repost_unknown(UID, DID)
    assert guard.get_guard(UID, DID) is None
    guard.mark_repost_suspected(UID, DID)
    guard.mark_repost_unknown(UID, DID)
    assert guard.get_guard(UID, DID).repost_status == "suspected"


def test_compound_key_and_blocked_listing_are_uid_scoped(isolated_home) -> None:
    for number, state in enumerate(("pending", "confirmed", "unknown", "suspected")):
        with session_scope() as session:
            session.add(ParticipationGuardRow(uid=UID, dynamic_id=str(number), repost_status=state))
    guard.record_pending("other-uid", "1")
    assert guard.load_blocked_guard_ids(UID) == {"0", "2", "3"}
    assert guard.load_blocked_guard_ids("other-uid") == {"1"}
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(ParticipationGuardRow(uid=UID, dynamic_id="0", repost_status="pending"))


def test_invalid_state_is_rejected_by_database(isolated_home) -> None:
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            session.add(ParticipationGuardRow(uid=UID, dynamic_id=DID, repost_status="joined"))
    assert guard.get_guard(UID, DID) is None


def test_concurrent_pending_claim_has_exactly_one_winner(isolated_home) -> None:
    def claim(_):
        try:
            guard.record_pending(UID, DID)
            return True
        except guard.ParticipationGuardBlocked:
            return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(claim, range(8))).count(True) == 1


def test_log_pruning_and_activity_removal_do_not_delete_guard(isolated_home, monkeypatch) -> None:
    from src.activity_store import remove_activity_ids, replace_all_activities

    monkeypatch.setattr("src.participation_log.participation_uid", lambda: UID)
    monkeypatch.setattr("src.participation_log._MAX_ENTRIES_PER_UID", 2)
    guard.confirm_repost(UID, DID)
    replace_all_activities([{"dynamic_id": DID}])
    for number in range(3):
        append_action_record(ParticipationActionRecord(number, DID, "转发抽奖", "failed", "x", "", [], {}))
    assert len(load_action_entries_for_uid(UID)) == 2
    remove_activity_ids({DID})
    replace_all_activities([])
    assert guard.get_guard(UID, DID).repost_status == "confirmed"


def test_gate_same_key_busy_different_uid_and_id_allowed(isolated_home) -> None:
    with guard.participation_gate(UID, DID):
        with pytest.raises(guard.ParticipationBusyError):
            with guard.participation_gate(UID, DID):
                pytest.fail("same key entered twice")
        with guard.participation_gate("other", DID):
            pass
        with guard.participation_gate(UID, "other"):
            pass
    with guard.participation_gate(UID, DID):
        pass
    assert list((db_path().parent / ".participation_locks").glob("*.lock"))


def test_gate_blocks_same_key_from_another_thread_only(isolated_home) -> None:
    def enter(dynamic_id):
        try:
            with guard.participation_gate(UID, dynamic_id):
                return "entered"
        except guard.ParticipationBusyError:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as executor:
        with guard.participation_gate(UID, DID):
            assert executor.submit(enter, DID).result(timeout=5) == "busy"
            assert executor.submit(enter, "another-dynamic").result(timeout=5) == "entered"
        assert executor.submit(enter, DID).result(timeout=5) == "entered"


def test_gate_releases_after_exception_and_scopes_actual_database(isolated_home, monkeypatch, tmp_path) -> None:
    with pytest.raises(ValueError):
        with guard.participation_gate(UID, DID):
            raise ValueError("abort")
    with guard.participation_gate(UID, DID):
        other_database = db_path().with_name("another-profile.db")
        monkeypatch.setattr(guard, "db_path", lambda: other_database)
        with guard.participation_gate(UID, DID):
            pass


_GATE_CHILD = """
import sys
from pathlib import Path
from src import participation_guard as guard
guard.db_path = lambda: Path(sys.argv[1])
try:
    with guard.participation_gate(sys.argv[2], sys.argv[3]):
        print('entered')
except guard.ParticipationBusyError:
    print('busy')
    raise SystemExit(17)
"""


def test_gate_excludes_another_process_without_blocking(isolated_home) -> None:
    def child(uid, did):
        return subprocess.run(
            [sys.executable, "-c", _GATE_CHILD, str(db_path()), uid, did],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )

    with guard.participation_gate(UID, DID):
        same = child(UID, DID)
        other = child(UID, "different")
    released = child(UID, DID)
    assert same.returncode == 17, same.stderr
    assert same.stdout.strip() == "busy"
    assert other.returncode == released.returncode == 0, (other.stderr, released.stderr)


def test_gate_is_released_by_os_after_child_exits_without_cleanup(isolated_home) -> None:
    script = _GATE_CHILD.replace("import sys", "import sys, os").replace("print('entered')", "os._exit(0)")
    result = subprocess.run(
        [sys.executable, "-c", script, str(db_path()), UID, DID],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    with guard.participation_gate(UID, DID):
        pass


def test_core_participation_in_another_process_is_blocked_before_client_or_db_checks(isolated_home) -> None:
    script = """
import json
import sys
from pathlib import Path
from src import participation as flow, participation_guard as guard
guard.db_path = lambda: Path(sys.argv[1])
flow.require_login = lambda: ('test-csrf', int(sys.argv[2]))
clients = []
def forbidden_client(*args, **kwargs):
    clients.append(True)
    raise AssertionError('Network client constructed before cross-process gate')
def forbidden_local(*args, **kwargs):
    raise AssertionError('Database checks ran before cross-process gate')
flow.BilibiliClient = forbidden_client
flow.get_participation = forbidden_local
result = flow.participate_activity(
    dynamic_id=sys.argv[3], lottery_type='\u8f6c\u53d1\u62bd\u5956', preflight=True,
).to_dict()
assert result['status'] == 'skipped' and not result['actions']
print(json.dumps({'reason': result['skip_reason'], 'client_count': len(clients)}))
"""
    child_root = isolated_home / "child-runtime"
    env = dict(os.environ)
    for name in ("BINGGO_DATA_ROOT", "BINGGO_LEGACY_DATA_ROOT", "BINGGO_HOME"):
        env[name] = str(child_root)
    env["BINGGO_DATA_ROOT_LOCATOR"] = str(child_root / "locator.json")
    with guard.participation_gate(UID, DID):
        result = subprocess.run(
            [sys.executable, "-c", script, str(db_path()), UID, DID],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
        )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {"reason": "participation_busy", "client_count": 0}


@pytest.mark.parametrize("uid,did", [(None, DID), (UID, None), ("", DID), (UID, ""), ("x" * 65, DID)])
def test_invalid_keys_are_rejected_before_database_access(uid, did) -> None:
    with pytest.raises(ValueError):
        guard.get_guard(uid, did)
