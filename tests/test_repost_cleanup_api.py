from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from src.repost_history import RepostHistoryRecord, RepostSyncCheckpoint
from web.app import app


client = TestClient(app)
REPOST_ID = "1234567890123456789"
ORIGINAL_ID = "2234567890123456789"


def _running_job(action: str, job_id: int = 71) -> dict:
    return {"id": job_id, "state": "running", "action": action, "source": "ui"}


def test_history_is_scoped_to_runtime_cookie_uid() -> None:
    row = RepostHistoryRecord(
        uid="123",
        repost_dynamic_id=REPOST_ID,
        original_dynamic_id=ORIGINAL_ID,
        reposted_at=100,
        original_author_uid="456",
        original_author_name="作者",
        source="history_import",
        delete_status="active",
        delete_requested_at=None,
        deleted_at=None,
        last_seen_at=200,
        last_error=None,
        updated_at=200,
    )
    checkpoint = RepostSyncCheckpoint(
        uid="123",
        head_dynamic_id=REPOST_ID,
        head_published_at=100,
        full_scan_completed=True,
        last_synced_at=200,
        updated_at=200,
    )
    with patch("web.app.get_account_profile", return_value={"logged_in": True}), patch(
        "web.app._require_runtime_bilibili_uid", return_value="123"
    ), patch(
        "src.repost_history.list_repost_history", return_value=([row], 1)
    ) as list_mock, patch(
        "src.repost_history.get_checkpoint", return_value=checkpoint
    ):
        response = client.get("/api/repost-cleanup/history?page=1&page_size=20")

    assert response.status_code == 200
    payload = response.json()
    assert payload["uid"] == "123"
    assert payload["items"][0]["repost_dynamic_id"] == REPOST_ID
    assert payload["items"][0]["original_dynamic_id"] == ORIGINAL_ID
    list_mock.assert_called_once_with("123", page=1, page_size=20, status=None)


def test_cleanup_jobs_require_login_but_not_llm() -> None:
    with patch("web.app.get_account_profile", return_value={"logged_in": True}), patch(
        "web.api_errors.is_llm_ready", return_value=False
    ), patch("web.app.runner.try_start", return_value=72) as start_mock, patch(
        "web.app.runner.get_status"
    ) as status_mock:
        status_mock.return_value.to_dict.return_value = _running_job("scan_expired_reposts", 72)
        response = client.post(
            "/api/jobs", json={"action": "scan_expired_reposts", "params": {}}
        )

    assert response.status_code == 200
    start_mock.assert_called_once_with("scan_expired_reposts", {}, source="ui")


def test_cleanup_job_rejects_extra_params_before_start() -> None:
    with patch("web.app.get_account_profile", return_value={"logged_in": True}), patch(
        "web.app.runner.try_start"
    ) as start_mock:
        response = client.post(
            "/api/jobs",
            json={"action": "sync_repost_history", "params": {"uid": "another"}},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    start_mock.assert_not_called()


def test_delete_requires_explicit_confirmation() -> None:
    with patch("web.app.get_account_profile", return_value={"logged_in": True}), patch(
        "web.app._require_runtime_bilibili_uid", return_value="123"
    ), patch("web.app.runner.try_start") as start_mock:
        response = client.post(
            "/api/repost-cleanup/delete",
            json={"repost_dynamic_ids": [REPOST_ID], "confirmed": False},
        )

    assert response.status_code == 400
    start_mock.assert_not_called()


def test_delete_endpoint_only_passes_deduplicated_repost_ids() -> None:
    with patch("web.app.get_account_profile", return_value={"logged_in": True}), patch(
        "web.app._require_runtime_bilibili_uid", return_value="123"
    ), patch("web.app.runner.try_start", return_value=73) as start_mock, patch(
        "web.app.runner.get_status"
    ) as status_mock:
        status_mock.return_value.to_dict.return_value = _running_job(
            "delete_expired_reposts", 73
        )
        response = client.post(
            "/api/repost-cleanup/delete",
            json={
                "repost_dynamic_ids": [REPOST_ID, REPOST_ID],
                "confirmed": True,
            },
        )

    assert response.status_code == 200
    start_mock.assert_called_once_with(
        "delete_expired_reposts",
        {"repost_dynamic_ids": [REPOST_ID], "manual_review_confirmed": False},
        source="ui",
    )


def test_delete_endpoint_forbids_original_id_field_and_generic_delete_action() -> None:
    with patch("web.app.get_account_profile", return_value={"logged_in": True}), patch(
        "web.app._require_runtime_bilibili_uid", return_value="123"
    ), patch("web.app.runner.try_start") as start_mock:
        response = client.post(
            "/api/repost-cleanup/delete",
            json={
                "repost_dynamic_ids": [REPOST_ID],
                "original_dynamic_id": ORIGINAL_ID,
                "confirmed": True,
            },
        )
        generic = client.post(
            "/api/jobs", json={"action": "delete_expired_reposts", "params": {}}
        )

    assert response.status_code == 400
    assert generic.status_code == 400
    assert generic.json()["error"]["code"] == "UNSUPPORTED_ACTION"
    start_mock.assert_not_called()


def test_candidates_endpoint_reads_persisted_local_assessment() -> None:
    candidate = {
        "repost_dynamic_id": REPOST_ID,
        "original_dynamic_id": ORIGINAL_ID,
        "delete_status": "active",
    }
    with patch("web.app.get_account_profile", return_value={"logged_in": True}), patch(
        "web.app._require_runtime_bilibili_uid", return_value="123"
    ), patch(
        "src.repost_cleanup.repost_cleanup_summary",
        return_value={
            "uid": "123",
            "history_total": 1,
            "safe": 1,
            "manual_review": 0,
            "blocked": 0,
            "excluded": 0,
            "pending_evaluation": 0,
            "candidates": [candidate],
        },
    ) as summary_mock:
        response = client.get("/api/repost-cleanup/candidates")

    assert response.status_code == 200
    payload = response.json()
    assert payload["uid"] == "123"
    assert payload["candidates"] == [candidate]
    assert payload["safe"] == 1
    summary_mock.assert_called_once_with("123")


def test_scan_job_accepts_force_original_ids_only() -> None:
    with patch("web.app.get_account_profile", return_value={"logged_in": True}), patch(
        "web.app.runner.try_start", return_value=74
    ) as start_mock, patch(
        "web.app.runner.get_status"
    ) as status_mock:
        status_mock.return_value.to_dict.return_value = _running_job("scan_expired_reposts", 74)
        response = client.post(
            "/api/jobs",
            json={"action": "scan_expired_reposts", "params": {"force_original_ids": [ORIGINAL_ID]}},
        )
        rejected = client.post(
            "/api/jobs",
            json={"action": "scan_expired_reposts", "params": {"uid": "other"}},
        )

    assert response.status_code == 200
    start_mock.assert_called_once_with(
        "scan_expired_reposts",
        {"force_original_ids": [ORIGINAL_ID]},
        source="ui",
    )
    assert rejected.status_code == 400


def test_defer_endpoint_is_local_only_and_returns_summary() -> None:
    with patch("web.app.get_account_profile", return_value={"logged_in": True}), patch(
        "web.app._require_runtime_bilibili_uid", return_value="123"
    ), patch(
        "src.repost_cleanup.defer_candidate"
    ) as defer_mock, patch(
        "src.repost_cleanup.repost_cleanup_summary",
        return_value={"uid": "123", "deferred": 1, "candidates": []},
    ):
        response = client.post(
            "/api/repost-cleanup/defer",
            json={"repost_dynamic_ids": [REPOST_ID]},
        )

    assert response.status_code == 200
    assert response.json()["deferred"] == 1
    defer_mock.assert_called_once_with("123", REPOST_ID)


def test_restore_endpoint_is_local_only_and_returns_summary() -> None:
    with patch("web.app.get_account_profile", return_value={"logged_in": True}), patch(
        "web.app._require_runtime_bilibili_uid", return_value="123"
    ), patch(
        "src.repost_cleanup.restore_candidate"
    ) as restore_mock, patch(
        "src.repost_cleanup.repost_cleanup_summary",
        return_value={"uid": "123", "deferred": 0, "candidates": []},
    ):
        response = client.post(
            "/api/repost-cleanup/restore",
            json={"repost_dynamic_ids": [REPOST_ID]},
        )

    assert response.status_code == 200
    restore_mock.assert_called_once_with("123", REPOST_ID)
