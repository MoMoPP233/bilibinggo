from __future__ import annotations

from unittest.mock import Mock

import pytest

from src import repost_cleanup as cleanup
from src.repost_history import RepostAssessmentRecord
from tests.test_repost_cleanup_service import UID, REPOST_ID, ORIGINAL_ID, _record, _FakeClient, _FakeDbPath


@pytest.fixture
def manual_delete(monkeypatch):
    monkeypatch.setattr(cleanup, "_require_verified_login", lambda client: ("csrf-token", int(UID)))
    monkeypatch.setattr(cleanup, "_assert_frozen_runtime", lambda **kwargs: "csrf-token")
    monkeypatch.setattr(cleanup, "get_runtime_profile_id", lambda: "profile-1")
    monkeypatch.setattr(cleanup, "db_path", lambda: _FakeDbPath())
    monkeypatch.setattr(cleanup, "load_activities", lambda: [])
    monkeypatch.setattr(cleanup, "get_repost", lambda *args: _record())
    monkeypatch.setattr(cleanup, "_verify_owned_repost_detail", Mock())
    monkeypatch.setattr(cleanup, "list_repost_assessments", lambda uid: {
        ORIGINAL_ID: RepostAssessmentRecord(uid=uid, original_dynamic_id=ORIGINAL_ID,
                                           assessment_level="manual_review", reason_code="manual", assessed_at=100),
    })
    monkeypatch.setattr(cleanup, "claim_delete_pending", Mock(return_value=True))
    monkeypatch.setattr(cleanup, "mark_delete_result", Mock(return_value=True))
    return _FakeClient()


@pytest.mark.parametrize("current", ["blocked", "excluded", "deferred", "manual_review", "safe"])
def test_manual_exception_uses_fresh_assessment_not_stale_history(manual_delete, monkeypatch, current):
    recheck = Mock(return_value=cleanup.CandidateAssessment(current, "fresh reason"))
    monkeypatch.setattr(cleanup, "_assess_original", recheck)
    result = cleanup.delete_reposts([REPOST_ID], client_factory=lambda: manual_delete, manual_review_confirmed=True)
    recheck.assert_called_once()
    assert recheck.call_args.kwargs["original_id"] == ORIGINAL_ID
    if current in {"safe", "manual_review"}:
        assert result["deleted_count"] == 1
        assert manual_delete.post_json_calls[0]["payload"] == {"dyn_id_str": REPOST_ID}
        assert manual_delete.post_json_calls[0]["retries"] == 0
    else:
        assert result["deleted_count"] == 0
        assert not manual_delete.post_json_calls
        cleanup.claim_delete_pending.assert_not_called()


def test_manual_identity_change_during_recheck_blocks_delete(manual_delete, monkeypatch):
    monkeypatch.setattr(cleanup, "_assess_original", lambda *args, **kwargs: cleanup.CandidateAssessment("manual_review", "fresh"))
    identity = Mock(side_effect=["csrf-token", ValueError("UID changed")])
    monkeypatch.setattr(cleanup, "_assert_frozen_runtime", identity)
    result = cleanup.delete_reposts([REPOST_ID], client_factory=lambda: manual_delete, manual_review_confirmed=True)
    assert result["deleted_count"] == 0
    assert not manual_delete.post_json_calls
