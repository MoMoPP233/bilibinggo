from __future__ import annotations

from src.participation_guard import (
    get_guard,
    load_blocked_guard_ids,
    mark_repost_suspected,
    record_pending,
)
from src.db.engine import reset_engine_for_tests
from web import activity_service


UID_A = "v36-profile-a"
UID_B = "v36-profile-b"


def _activity(dynamic_id: str, *, title: str | None = None, status: str = "未参加") -> dict:
    return {
        "dynamic_id": dynamic_id,
        "activity_title": title or f"活动 {dynamic_id}",
        "lottery_type": "转发抽奖",
        "draw_status": "active",
        "activity_status": status,
        "status_classified": True,
    }


def _patch_service(monkeypatch, activities: list[dict], blocked: set[str]) -> None:
    monkeypatch.setattr(activity_service, "_load_activities_payload", lambda: {"activities": activities})
    monkeypatch.setattr(activity_service, "_load_participation_actions", lambda: {})
    monkeypatch.setattr(activity_service, "load_participations", lambda: {})
    monkeypatch.setattr(activity_service, "_load_blocked_activity_ids", lambda: set(blocked))


def test_guarded_rows_are_removed_from_list_search_filters_and_page_total(monkeypatch) -> None:
    ordinary = [_activity(f"ordinary-{index}") for index in range(10)]
    guarded = [
        _activity("pending-id", title="guarded pending title"),
        _activity("unknown-id", title="guarded unknown title"),
        _activity("suspected-id", title="guarded suspected title"),
    ]
    _patch_service(monkeypatch, ordinary + guarded, {item["dynamic_id"] for item in guarded})

    rows = activity_service._filtered_activity_rows()
    assert [row["dynamic_id"] for row in rows] == [item["dynamic_id"] for item in ordinary]
    assert activity_service._filtered_activity_rows(status="未参加") == rows
    assert activity_service._filtered_activity_rows(q="guarded") == []

    payload = activity_service.list_activities(page=1, page_size=10)
    assert payload["total"] == 10
    assert payload["page_size"] == 10
    assert payload["pages"] == 1
    assert len(payload["items"]) == 10


def test_summary_excludes_guarded_rows_without_reclassifying_confirmed(monkeypatch) -> None:
    activities = [
        _activity("ordinary-id"),
        _activity("confirmed-id", status="已参加"),
        _activity("unknown-id"),
    ]
    _patch_service(monkeypatch, activities, {"unknown-id"})
    monkeypatch.setattr(activity_service, "_load_source_snapshot", lambda _source: {})
    monkeypatch.setattr(
        activity_service,
        "get_last_pipeline_persisted",
        lambda: {"action": "", "persisted_count": 0, "synced_at": None},
    )

    summary = activity_service.get_summary()
    assert summary["total_count"] == 2
    assert summary["user_status_counts"] == {"已结束": 0, "已参加": 1, "未参加": 1}
    assert summary["counts"] == {"active": 2, "ended": 0}


def test_guard_survives_restart_and_is_profile_scoped(isolated_home, monkeypatch) -> None:
    pending_id = "1000000000000000001"
    suspected_id = "1000000000000000002"
    record_pending(UID_A, pending_id)
    mark_repost_suspected(UID_A, suspected_id)
    reset_engine_for_tests()

    activities = [_activity(pending_id), _activity(suspected_id), _activity("shared-id")]
    _patch_service(monkeypatch, activities, set())
    monkeypatch.setattr(activity_service, "participation_uid", lambda: UID_A)
    # Use the real persisted guard lookup after the simulated restart.
    monkeypatch.setattr(
        activity_service,
        "_load_blocked_activity_ids",
        lambda: load_blocked_guard_ids(UID_A),
    )
    assert [row["dynamic_id"] for row in activity_service._filtered_activity_rows()] == ["shared-id"]
    assert get_guard(UID_A, pending_id).repost_status == "pending"
    assert get_guard(UID_A, suspected_id).repost_status == "suspected"

    # The same dynamic is visible for another Profile with no corresponding guard.
    monkeypatch.setattr(activity_service, "participation_uid", lambda: UID_B)
    monkeypatch.setattr(
        activity_service,
        "_load_blocked_activity_ids",
        lambda: load_blocked_guard_ids(UID_B),
    )
    assert [row["dynamic_id"] for row in activity_service._filtered_activity_rows()] == [
        pending_id,
        suspected_id,
        "shared-id",
    ]


def test_triple_preview_and_selection_use_the_same_hidden_rows(monkeypatch) -> None:
    guarded = _activity("1000000000000000001")
    ordinary = _activity("1000000000000000002")
    _patch_service(monkeypatch, [guarded, ordinary], {guarded["dynamic_id"]})

    targets = activity_service.pick_triple_participate_targets()
    preview = activity_service.summarize_triple_participate_targets()
    assert [row["dynamic_id"] for row in targets] == [ordinary["dynamic_id"]]
    assert [item["dynamic_id"] for item in preview["items"]] == [ordinary["dynamic_id"]]
