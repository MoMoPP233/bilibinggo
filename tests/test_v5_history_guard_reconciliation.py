from __future__ import annotations

import pytest
from sqlalchemy.dialects.sqlite import insert

from src.db.models import ParticipationGuardRow
from src.db.session import session_scope
from src.participation_guard import (
    confirm_repost,
    get_guard,
    mark_repost_suspected,
    mark_repost_unknown,
    reconcile_uncertain_guards_from_history,
    record_pending,
)
from src.repost_cleanup import auto_maintain, reconciliation_needs_sync, sync_repost_history
from src.repost_history import (
    RepostImportRecord,
    claim_delete_pending,
    defer_repost,
    get_checkpoint,
    mark_delete_result,
    mark_sync_needed,
    upsert_repost_assessment,
    upsert_repost_records,
)

UID = "12345"
OTHER_UID = "54321"
DYNAMIC_ID = "1000000000000000001"
OTHER_DYNAMIC_ID = "1000000000000000002"
REPOST_ID = "2000000000000000001"


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class _SafeAssessment:
    level = "safe"
    reason = "ok"
    reason_code = "safe_official_notice"
    lottery_type = "互动抽奖"
    lottery_time = 1_700_000_000
    lottery_time_reliable = True
    eligible_after = None
    classification_source = "public_classifier"
    summary = ""
    remote_checked_at = None


def _history(
    *,
    uid: str = UID,
    original_id: str = DYNAMIC_ID,
    repost_id: str = REPOST_ID,
    source: str = "history_import",
) -> None:
    upsert_repost_records(
        uid,
        [
            RepostImportRecord(
                repost_dynamic_id=repost_id,
                original_dynamic_id=original_id,
                reposted_at=100,
                source=source,
            )
        ],
        seen_at=100,
    )


def _uncertain(status: str, dynamic_id: str = DYNAMIC_ID) -> None:
    if status == "pending":
        record_pending(UID, dynamic_id)
    elif status == "unknown":
        record_pending(UID, dynamic_id)
        mark_repost_unknown(UID, dynamic_id)
    elif status == "suspected":
        mark_repost_suspected(UID, dynamic_id)
    else:  # pragma: no cover - test helper contract
        raise AssertionError(status)


def _login(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup.require_login",
        lambda: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "profile-1")
    monkeypatch.setattr(
        "src.repost_cleanup.db_path",
        lambda: type("_P", (), {"resolve": lambda self: "db-1"})(),
    )


def _feed_item() -> dict:
    return {
        "id_str": REPOST_ID,
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {
            "module_author": {"mid": int(UID), "name": "当前账号", "pub_ts": 100}
        },
        "orig": {
            "id_str": DYNAMIC_ID,
            "modules": {"module_author": {"mid": 67890, "name": "原作者"}},
        },
    }


@pytest.mark.parametrize("status", ["pending", "unknown", "suspected"])
def test_exact_trusted_history_resolves_uncertain_guard(isolated_home, status) -> None:
    _uncertain(status)
    _history()

    result = reconcile_uncertain_guards_from_history(UID, reconciled_at=200)

    assert result == {"checked": 1, "resolved": 1, "remaining": 0}
    assert get_guard(UID, DYNAMIC_ID).repost_status == "confirmed"
    assert get_checkpoint(UID) is None  # 账本已存在，不重新制造 sync_needed。


def test_other_uid_history_cannot_resolve_guard(isolated_home) -> None:
    _uncertain("suspected")
    _history(uid=OTHER_UID)

    result = reconcile_uncertain_guards_from_history(UID, reconciled_at=200)

    assert result == {"checked": 1, "resolved": 0, "remaining": 1}
    assert get_guard(UID, DYNAMIC_ID).repost_status == "suspected"


def test_other_original_id_cannot_resolve_guard(isolated_home) -> None:
    _uncertain("unknown")
    _history(original_id=OTHER_DYNAMIC_ID)

    result = reconcile_uncertain_guards_from_history(UID, reconciled_at=200)

    assert result["resolved"] == 0
    assert get_guard(UID, DYNAMIC_ID).repost_status == "unknown"


def test_absent_history_is_not_proof_of_no_repost(isolated_home) -> None:
    _uncertain("pending")

    result = reconcile_uncertain_guards_from_history(UID, reconciled_at=200)

    assert result == {"checked": 1, "resolved": 0, "remaining": 1}
    assert get_guard(UID, DYNAMIC_ID).repost_status == "pending"


def test_unverified_history_row_is_not_accepted_as_proof(isolated_home) -> None:
    _uncertain("suspected")
    _history(source="binggo")  # identity_ok/source 尚未被严格空间 feed 验证。

    result = reconcile_uncertain_guards_from_history(UID, reconciled_at=200)

    assert result["resolved"] == 0
    assert get_guard(UID, DYNAMIC_ID).repost_status == "suspected"


def test_deleted_tombstone_still_proves_historical_repost(isolated_home) -> None:
    _uncertain("suspected")
    _history()
    assert claim_delete_pending(UID, REPOST_ID, requested_at=150) is True
    assert mark_delete_result(
        UID,
        REPOST_ID,
        status="deleted",
        deleted_at=160,
        updated_at=160,
    ) is True

    result = reconcile_uncertain_guards_from_history(UID, reconciled_at=200)

    assert result["resolved"] == 1
    assert get_guard(UID, DYNAMIC_ID).repost_status == "confirmed"


def test_already_confirmed_guard_is_not_modified(isolated_home) -> None:
    _history()
    confirm_repost(UID, DYNAMIC_ID)
    before = get_guard(UID, DYNAMIC_ID)

    result = reconcile_uncertain_guards_from_history(UID, reconciled_at=999)

    assert result == {"checked": 0, "resolved": 0, "remaining": 0}
    assert get_guard(UID, DYNAMIC_ID) == before
    assert reconciliation_needs_sync(UID) is False  # 可信 history 已证明它被同步覆盖。


@pytest.mark.parametrize("level", ["safe", "manual_review", "blocked", "excluded"])
def test_cleanup_assessment_does_not_affect_history_proof(
    isolated_home, level,
) -> None:
    _uncertain("suspected")
    _history()
    upsert_repost_assessment(
        UID,
        DYNAMIC_ID,
        assessment_level=level,
        assessment_status="final",
        reason_code=f"test_{level}",
        assessed_at=150,
    )

    result = reconcile_uncertain_guards_from_history(UID, reconciled_at=200)

    assert result["resolved"] == 1
    assert get_guard(UID, DYNAMIC_ID).repost_status == "confirmed"


def test_cleanup_defer_state_does_not_affect_history_proof(isolated_home) -> None:
    _uncertain("suspected")
    _history()
    defer_repost(UID, REPOST_ID, deferred_at=150)

    result = reconcile_uncertain_guards_from_history(UID, reconciled_at=200)

    assert result["resolved"] == 1


def test_reconciliation_is_pure_local_and_does_not_call_remote(
    isolated_home, monkeypatch,
) -> None:
    _uncertain("unknown")
    _history()
    monkeypatch.setattr(
        "src.bilibili_client.BilibiliClient",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不得联网")),
    )
    monkeypatch.setattr(
        "src.pipeline.classify_step.classify_for_cleanup",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不得调用 LLM/分类")),
    )

    result = reconcile_uncertain_guards_from_history(UID, reconciled_at=200)

    assert result["resolved"] == 1


def test_hundreds_of_uncertain_guards_are_reconciled_in_one_local_batch(
    isolated_home,
) -> None:
    originals = [str(1_000_000_000_000_000_000 + index) for index in range(240)]
    with session_scope() as session:
        session.execute(
            insert(ParticipationGuardRow),
            [
                {
                    "uid": UID,
                    "dynamic_id": dynamic_id,
                    "repost_status": "suspected",
                    "updated_at": 50,
                }
                for dynamic_id in originals
            ],
        )
    upsert_repost_records(
        UID,
        [
            RepostImportRecord(
                repost_dynamic_id=str(2_000_000_000_000_000_000 + index),
                original_dynamic_id=dynamic_id,
                reposted_at=100 + index,
            )
            for index, dynamic_id in enumerate(originals[:200])
        ],
        seen_at=500,
    )

    result = reconcile_uncertain_guards_from_history(UID, reconciled_at=600)

    assert result == {"checked": 240, "resolved": 200, "remaining": 40}


def test_manual_history_sync_runs_local_reconciliation(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    _uncertain("suspected")
    monkeypatch.setattr(
        "src.repost_cleanup.fetch_space_feed_page",
        lambda *args, **kwargs: {"items": [_feed_item()], "offset": ""},
    )

    result = sync_repost_history(client_factory=lambda: _FakeClient())

    assert result["guard_reconciliation"] == {
        "checked": 1,
        "resolved": 1,
        "remaining": 0,
    }
    assert get_guard(UID, DYNAMIC_ID).repost_status == "confirmed"


def test_auto_history_sync_runs_reconciliation_then_assessment(
    isolated_home, monkeypatch,
) -> None:
    _login(monkeypatch)
    _uncertain("unknown")
    mark_sync_needed(UID, occurred_at=90)
    monkeypatch.setattr(
        "src.repost_cleanup.fetch_space_feed_page",
        lambda *args, **kwargs: {"items": [_feed_item()], "offset": ""},
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assess_original",
        lambda *args, **kwargs: _SafeAssessment(),
    )
    monkeypatch.setattr(
        "src.repost_cleanup.delete_reposts",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("自动维护不得删除")),
    )

    result = auto_maintain(client_factory=lambda: _FakeClient())

    assert result["guard_reconciliation"]["resolved"] == 1
    assert result["evaluated_originals"] == 1
    assert "自动确认 1 条历史参与记录" in result["message"]
    assert get_guard(UID, DYNAMIC_ID).repost_status == "confirmed"
    assert get_checkpoint(UID).sync_needed is False


def test_failed_sync_does_not_run_reconciliation(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    _uncertain("suspected")
    monkeypatch.setattr("src.repost_cleanup.PAGE_REQUEST_DELAY", 0)
    calls = {"count": 0}

    def partial_then_fail(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return {"items": [_feed_item()], "offset": "next-page"}
        raise RuntimeError("同步失败")

    monkeypatch.setattr("src.repost_cleanup.fetch_space_feed_page", partial_then_fail)

    with pytest.raises(RuntimeError, match="同步失败"):
        sync_repost_history(client_factory=lambda: _FakeClient())

    assert calls["count"] == 2
    assert get_guard(UID, DYNAMIC_ID).repost_status == "suspected"


def test_manual_sync_action_reports_locally_resolved_guards(
    isolated_home, monkeypatch,
) -> None:
    from web.actions import run_action

    monkeypatch.setattr(
        "src.repost_cleanup.sync_repost_history",
        lambda **kwargs: {
            "uid": UID,
            "found_reposts": 300,
            "imported_count": 12,
            "guard_reconciliation": {
                "checked": 300,
                "resolved": 287,
                "remaining": 13,
            },
        },
    )

    result = run_action("sync_repost_history")

    assert result["ok"] is True
    assert "自动确认 287 条历史参与记录" in result["message"]
