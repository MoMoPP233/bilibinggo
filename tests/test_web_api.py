from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from web.app import app

client = TestClient(app)


def test_reject_unknown_job_action() -> None:
    resp = client.post("/api/jobs", json={"action": "delete_all", "params": {}})
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["code"] == "UNSUPPORTED_ACTION"
    assert data["detail"] == data["error"]["message"]


def test_comment_breaker_keeps_read_only_status_and_removes_manual_recover_route() -> None:
    paths = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }
    assert ("GET", "/api/participation/comment-breaker") in paths
    assert ("POST", "/api/participation/comment-breaker/recover") not in paths
    with patch("src.comment_breaker.comment_breaker_snapshot", return_value={"active": False}):
        assert client.get("/api/participation/comment-breaker").json() == {
            "ok": True,
            "active": False,
        }
    assert client.post("/api/participation/comment-breaker/recover").status_code in (404, 405)


def test_delete_notification_record_route_is_single_record_scope() -> None:
    with patch("web.app.delete_record") as delete_mock:
        resp = client.delete(
            "/api/notifications/records/winning_candidate%3Adm%3A39945369%3A1/serverchan"
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    delete_mock.assert_called_once_with("winning_candidate:dm:39945369:1", "serverchan")


def test_ds9_rejection_audit_api_is_local_read_and_clear_only() -> None:
    sample = {"dynamic_id": "100000000000000001", "source_topic_id": "47801"}
    with patch("web.app.list_rejections", return_value=[sample]) as list_mock:
        resp = client.get("/api/ds9/rejection-audit", params={"topic_id": "47801"})
    assert resp.status_code == 200
    assert resp.json() == {"samples": [sample], "count": 1, "max_count": 100}
    list_mock.assert_called_once_with(source_topic_id="47801")

    with patch("web.app.clear_rejections", return_value=2) as clear_mock:
        resp = client.delete("/api/ds9/rejection-audit")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "deleted_count": 2}
    clear_mock.assert_called_once_with()


def test_watch_users_api_seeds_and_lists() -> None:
    with patch("web.app.seed_from_candidates_if_empty", return_value=True):
        with patch(
            "web.app.get_watch_users_payload",
            return_value={"count": 1, "users": [{"mid": 1, "name": "u"}], "updated_at": 0},
        ):
            with patch("web.app.get_watch_last_synced_at", return_value=None):
                resp = client.get("/api/watch-users")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert "next_window" in data


def test_participate_triple_job_accepts_filter_params() -> None:
    with patch("web.app.runner.try_start", return_value=1) as start_mock:
        resp = client.post(
            "/api/jobs",
            json={
                "action": "participate_triple",
                "params": {"status": "未参加", "sort": "heat", "order": "desc"},
            },
        )
    assert resp.status_code in (200, 401)
    if resp.status_code == 200:
        start_mock.assert_called_once()
        assert start_mock.call_args.args[0] == "participate_triple"


def test_reject_invalid_dynamic_id() -> None:
    resp = client.post(
        "/api/jobs",
        json={"action": "participate", "params": {"dynamic_id": "not-valid"}},
    )
    assert resp.status_code in (400, 401)


def test_activities_pagination_bounds() -> None:
    resp = client.get("/api/activities", params={"page": 1, "page_size": 20})
    assert resp.status_code == 200
    data = resp.json()
    assert data["page_size"] == 20
    assert "triple_targets" in data
    assert "count" in data["triple_targets"]
    assert "items" in data["triple_targets"]


def test_activities_triple_targets_empty_for_joined_filter() -> None:
    rows = [
        {
            "dynamic_id": "100000000000000001",
            "activity_title": "已参加活动",
            "lottery_type": "转发抽奖",
            "activity_status": "已参加",
            "draw_status": "active",
            "can_participate": False,
            "prize": "奖品",
            "repost_count": 0,
        }
    ]
    with patch("web.activity_service._filtered_activity_rows", return_value=rows):
        resp = client.get("/api/activities", params={"status": "已参加"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["triple_targets"]["count"] == 0
    assert data["triple_targets"]["items"] == []


def test_settings_includes_participate_text_mode() -> None:
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("participate_text_mode") in {"custom", "random_comment"}
    assert data.get("default_participate_fallback_text") == "好运连连！"


def test_save_participate_fallback_text_when_logged_in() -> None:
    with patch("web.app.get_account_profile", return_value={"logged_in": True}):
        resp = client.put(
            "/api/settings/participate-text",
            json={"participate_fallback_text": "好运连连！"},
        )
    assert resp.status_code == 200
    assert resp.json().get("participate_fallback_text") == "好运连连！"


def test_login_qrcode_does_not_crash() -> None:
    resp = client.get("/api/login/qrcode")
    assert resp.status_code in (200, 404)


def test_api_account_does_not_crash() -> None:
    resp = client.get("/api/account")
    assert resp.status_code == 200
    assert "logged_in" in resp.json()


def test_api_account_extras_requires_login_or_returns() -> None:
    resp = client.get("/api/account/extras")
    assert resp.status_code in (200, 401)
