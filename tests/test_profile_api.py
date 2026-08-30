from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.restart_control import RESTART_SUPERVISED_ENV, restart_control
from web.api_contract import API_CONTRACT_HEADER, API_CONTRACT_VERSION
from web.app import app


client = TestClient(app)


class _FakeServer:
    def __init__(self) -> None:
        self.should_exit = False


@pytest.fixture(autouse=True)
def _reset_restart_state(monkeypatch: pytest.MonkeyPatch):
    restart_control.reset_for_tests()
    monkeypatch.delenv(RESTART_SUPERVISED_ENV, raising=False)
    yield
    restart_control.reset_for_tests()


def _enable_supervised_restart(monkeypatch: pytest.MonkeyPatch) -> _FakeServer:
    server = _FakeServer()
    monkeypatch.setenv(RESTART_SUPERVISED_ENV, "1")
    restart_control.register_server(server)
    return server


def _assert_error(response, *, status: int, code: str) -> dict:
    assert response.status_code == status
    assert response.headers.get(API_CONTRACT_HEADER) == str(API_CONTRACT_VERSION)
    payload = response.json()
    assert payload["error"]["code"] == code
    assert payload["detail"] == payload["error"]["message"]
    return payload


def _idle_patches():
    return (
        patch("web.app.runner.is_running", return_value=False),
        patch("web.app.auto_scheduler.get_status", return_value={"state": "idle"}),
    )


def test_profiles_get_reports_runtime_and_pending_restart() -> None:
    profiles = [
        {"profile_id": "account-1", "mid": "100", "nickname": "Alice"},
        {"profile_id": "account-2", "mid": "", "nickname": ""},
    ]
    with patch("web.app.get_runtime_profile_id", return_value="account-1"), patch(
        "web.app.get_selected_profile_id", return_value="account-2"
    ), patch("web.app.get_data_root", return_value="D:/BinggoData"), patch(
        "web.app.list_profiles", return_value=profiles
    ):
        response = client.get("/api/profiles")

    assert response.status_code == 200
    assert response.headers.get(API_CONTRACT_HEADER) == str(API_CONTRACT_VERSION)
    assert response.json() == {
        "runtime_profile_id": "account-1",
        "active_profile_id": "account-2",
        "restart_required": True,
        "data_root": "D:/BinggoData",
        "profiles": profiles,
    }


def test_profile_create_returns_created_profile() -> None:
    created = {"profile_id": "account-2", "mid": "", "nickname": ""}
    with patch("web.app.create_profile", return_value=created) as create_mock:
        response = client.post("/api/profiles")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "profile": created}
    create_mock.assert_called_once_with()


def test_profile_activate_requires_restart_without_hot_switch() -> None:
    selected_ids = iter(("account-2",))
    runner_idle, scheduler_idle = _idle_patches()
    with runner_idle, scheduler_idle, patch(
        "web.app.set_active_profile",
        return_value={"profile_id": "account-2", "mid": "", "nickname": ""},
    ) as activate_mock, patch(
        "web.app.get_selected_profile_id", side_effect=selected_ids
    ), patch("web.app.get_runtime_profile_id", return_value="account-1"):
        response = client.post("/api/profiles/account-2/activate")

    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    activate_mock.assert_called_once_with("account-2")


def test_profile_activate_current_runtime_clears_restart_requirement() -> None:
    runner_idle, scheduler_idle = _idle_patches()
    with runner_idle, scheduler_idle, patch(
        "web.app.set_active_profile",
        return_value={"profile_id": "account-1", "mid": "100", "nickname": "Alice"},
    ), patch("web.app.get_selected_profile_id", return_value="account-1"), patch(
        "web.app.get_runtime_profile_id", return_value="account-1"
    ):
        response = client.post("/api/profiles/account-1/activate")

    assert response.status_code == 200
    assert response.json()["restart_required"] is False


def test_profile_switch_updates_active_without_hot_switch_and_requests_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _enable_supervised_restart(monkeypatch)
    target = {"profile_id": "account-2", "mid": "200", "nickname": "Bob"}
    runner_idle, scheduler_idle = _idle_patches()
    with runner_idle, scheduler_idle, patch(
        "web.app.set_active_profile", return_value=target
    ) as activate_mock, patch(
        "web.app.get_runtime_profile_id", return_value="account-1"
    ), patch(
        "web.app.get_selected_profile_id", return_value="account-2"
    ):
        response = client.post("/api/profiles/account-2/switch")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "profile": target,
        "runtime_profile_id": "account-1",
        "active_profile_id": "account-2",
        "restart_requested": True,
    }
    activate_mock.assert_called_once_with("account-2")
    assert restart_control.is_restart_pending() is True
    assert server.should_exit is True


def test_profile_switch_rejected_while_job_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _enable_supervised_restart(monkeypatch)
    with patch("web.app.runner.is_running", return_value=True), patch(
        "web.app.auto_scheduler.get_status", return_value={"state": "idle"}
    ), patch("web.app.get_runtime_profile_id", return_value="account-1"), patch(
        "web.app.set_active_profile"
    ) as activate_mock:
        response = client.post("/api/profiles/account-2/switch")

    payload = _assert_error(response, status=409, code="JOB_BUSY")
    assert payload["error"]["message"] == "当前有任务正在运行，请等待任务结束后再切换账号。"
    activate_mock.assert_not_called()
    assert restart_control.is_restart_pending() is False
    assert server.should_exit is False


def test_profile_switch_rejected_while_auto_scheduler_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _enable_supervised_restart(monkeypatch)
    with patch("web.app.runner.is_running", return_value=False), patch(
        "web.app.auto_scheduler.get_status", return_value={"state": "running"}
    ), patch("web.app.get_runtime_profile_id", return_value="account-1"), patch(
        "web.app.set_active_profile"
    ) as activate_mock:
        response = client.post("/api/profiles/account-2/switch")

    _assert_error(response, status=409, code="JOB_BUSY")
    activate_mock.assert_not_called()
    assert restart_control.is_restart_pending() is False
    assert server.should_exit is False


def test_profile_switch_refuses_unsupervised_entry_before_changing_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with patch("web.app.get_runtime_profile_id", return_value="account-1"), patch(
        "web.app.set_active_profile"
    ) as activate_mock:
        response = client.post("/api/profiles/account-2/switch")

    payload = _assert_error(response, status=503, code="INTERNAL")
    assert "不支持自动重新启动" in payload["error"]["message"]
    activate_mock.assert_not_called()


def test_restart_gate_rejects_new_job_and_auto_scheduler_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_supervised_restart(monkeypatch)
    restart_control.begin_restart(lambda: None)

    with patch("web.app.get_account_profile", return_value={"logged_in": False}):
        job_response = client.post("/api/jobs", json={"action": "login", "params": {}})
    auto_response = client.post("/api/auto/start")

    job_payload = _assert_error(job_response, status=409, code="JOB_BUSY")
    auto_payload = _assert_error(auto_response, status=409, code="JOB_BUSY")
    assert "重新启动" in job_payload["error"]["message"]
    assert "重新启动" in auto_payload["error"]["message"]


def test_profile_activate_rejected_while_job_is_running() -> None:
    with patch("web.app.runner.is_running", return_value=True), patch(
        "web.app.auto_scheduler.get_status", return_value={"state": "idle"}
    ), patch("web.app.set_active_profile") as activate_mock:
        response = client.post("/api/profiles/account-2/activate")

    payload = _assert_error(response, status=409, code="JOB_BUSY")
    assert "运行" in payload["error"]["message"]
    activate_mock.assert_not_called()


def test_profile_delete_rejected_while_auto_scheduler_is_running() -> None:
    with patch("web.app.runner.is_running", return_value=False), patch(
        "web.app.auto_scheduler.get_status", return_value={"state": "running"}
    ), patch("web.app.delete_profile") as delete_mock:
        response = client.delete("/api/profiles/account-2")

    _assert_error(response, status=409, code="JOB_BUSY")
    delete_mock.assert_not_called()


def test_profile_delete_success() -> None:
    runner_idle, scheduler_idle = _idle_patches()
    with runner_idle, scheduler_idle, patch("web.app.delete_profile") as delete_mock:
        response = client.delete("/api/profiles/account-2")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    delete_mock.assert_called_once_with("account-2")


def test_profile_delete_protects_current_runtime_profile() -> None:
    runner_idle, scheduler_idle = _idle_patches()
    with runner_idle, scheduler_idle, patch(
        "web.app.delete_profile",
        side_effect=ValueError("不能删除当前正在使用的 Profile"),
    ):
        response = client.delete("/api/profiles/account-1")

    payload = _assert_error(response, status=400, code="VALIDATION_ERROR")
    assert "不能删除当前" in payload["error"]["message"]


def test_profile_delete_protects_next_start_profile() -> None:
    runner_idle, scheduler_idle = _idle_patches()
    with runner_idle, scheduler_idle, patch(
        "web.app.delete_profile",
        side_effect=ValueError("不能删除下次启动将使用的 Profile"),
    ):
        response = client.delete("/api/profiles/account-2")

    payload = _assert_error(response, status=400, code="VALIDATION_ERROR")
    assert "下次启动" in payload["error"]["message"]


def test_profile_not_found_is_reported_as_not_found() -> None:
    runner_idle, scheduler_idle = _idle_patches()
    with runner_idle, scheduler_idle, patch(
        "web.app.set_active_profile",
        side_effect=ValueError("Profile 不存在：account-99"),
    ):
        response = client.post("/api/profiles/account-99/activate")

    _assert_error(response, status=404, code="NOT_FOUND")


def test_profile_api_real_lifecycle_keeps_runtime_frozen(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接口级验证磁盘指针会变化，但当前进程 Profile 不会热切换。"""
    from src.data_paths import reset_runtime_profile_for_tests

    data_root = tmp_path / "BinggoData"
    monkeypatch.setenv("BINGGO_DATA_ROOT", str(data_root))
    monkeypatch.setenv("BINGGO_DATA_ROOT_LOCATOR", str(tmp_path / "locator.json"))
    reset_runtime_profile_for_tests()
    runner_idle, scheduler_idle = _idle_patches()
    try:
        with runner_idle, scheduler_idle:
            initial = client.get("/api/profiles")
            created = client.post("/api/profiles")
            activated = client.post("/api/profiles/account-2/activate")
            protected = client.delete("/api/profiles/account-2")
            restored = client.post("/api/profiles/account-1/activate")
            deleted = client.delete("/api/profiles/account-2")
            final = client.get("/api/profiles")
    finally:
        # 必须在 monkeypatch 恢复环境变量前清理运行时路径快照。
        reset_runtime_profile_for_tests()

    assert initial.json()["runtime_profile_id"] == "account-1"
    assert initial.json()["active_profile_id"] == "account-1"
    assert initial.json()["restart_required"] is False
    assert created.json()["profile"]["profile_id"] == "account-2"
    assert activated.json()["restart_required"] is True
    _assert_error(protected, status=400, code="VALIDATION_ERROR")
    assert restored.json()["restart_required"] is False
    assert deleted.json() == {"ok": True}
    assert [item["profile_id"] for item in final.json()["profiles"]] == ["account-1"]
