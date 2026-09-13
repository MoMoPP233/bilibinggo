"""V30：complete 契约修复的真实 route 级 E2E（临时 DATA_ROOT）。

真实实现：web app routes、filesystem、SQLite、ProfileManager、active pointer、
onboarding_complete/commit_staged_login、switch route。
只 mock 外部 Bilibili generate/poll（httpx.MockTransport）；0 真实网络。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src import bilibili_login as blogin
from src import data_paths
from src.restart_control import RESTART_SUPERVISED_ENV, restart_control
from web.app import app

client = TestClient(app)

COMPLETE_PATH = "/api/accounts/onboarding/complete"


class _FakeServer:
    def __init__(self) -> None:
        self.should_exit = False


@pytest.fixture(autouse=True)
def _reset_restart_state(monkeypatch: pytest.MonkeyPatch):
    restart_control.reset_for_tests()
    monkeypatch.delenv(RESTART_SUPERVISED_ENV, raising=False)
    yield
    restart_control.reset_for_tests()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "data-root"
    profiles = base / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    account = profiles / "account-1"
    account.mkdir()
    (account / "profile.json").write_text(
        json.dumps({"profile_id": "account-1", "mid": "88001", "nickname": "old"}),
        encoding="utf-8",
    )
    (account / "cookies.txt").write_text("SESSDATA=old;DedeUserID=88001", encoding="utf-8")
    (base / "active_profile.json").write_text(
        json.dumps({"profile_id": "account-1"}), encoding="utf-8"
    )
    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(base))
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    monkeypatch.delenv(data_paths.LEGACY_DATA_ROOT_ENV, raising=False)
    data_paths.reset_runtime_profile_for_tests()
    data_paths.get_runtime_profile_id()
    yield base
    data_paths.reset_runtime_profile_for_tests()


def _install_bilibili(monkeypatch: pytest.MonkeyPatch, *, dede_user_id: str = "90001") -> None:
    headers = [
        ("set-cookie", "SESSDATA=sess-1; Path=/; Domain=.bilibili.com"),
        ("set-cookie", "bili_jct=jct-1; Path=/; Domain=.bilibili.com"),
        ("set-cookie", f"DedeUserID={dede_user_id}; Path=/; Domain=.bilibili.com"),
        ("set-cookie", "DedeUserID__ckMd5=ck-1; Path=/; Domain=.bilibili.com"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"url": "https://qr.test/1", "qrcode_key": "k-1"},
                },
            )
        return httpx.Response(
            200,
            json={"data": {"code": 0, "url": ""}},
            headers=headers,
        )

    def factory(*, timeout: float = blogin.PASSPORT_TIMEOUT_SECONDS) -> httpx.Client:
        return httpx.Client(
            headers=blogin.PASSPORT_HEADERS,
            follow_redirects=True,
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(blogin, "create_passport_client", factory)
    monkeypatch.setattr(blogin, "QR_POLL_RETRY_BACKOFF_SECONDS", 0.0)


def _enable_supervised_switch(monkeypatch: pytest.MonkeyPatch) -> _FakeServer:
    server = _FakeServer()
    monkeypatch.setenv(RESTART_SUPERVISED_ENV, "1")
    restart_control.register_server(server)
    return server


def _patch_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("web.app.runner.is_running", lambda: False)
    monkeypatch.setattr("web.app.auto_scheduler.get_status", lambda: {"state": "idle"})


def _scan_to_staged(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dede_user_id: str = "90001",
) -> str:
    _install_bilibili(monkeypatch, dede_user_id=dede_user_id)
    start = client.post("/api/accounts/onboarding/start")
    assert start.status_code == 200, start.text
    onboarding_id = start.json()["result"]["onboarding_id"]
    assert onboarding_id
    poll = client.post(f"/api/accounts/onboarding/{onboarding_id}/poll")
    assert poll.status_code == 200, poll.text
    assert poll.json()["result"]["status"] == "success"
    return onboarding_id


def _db_integrity(db: Path) -> str:
    con = sqlite3.connect(db)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


def test_new_uid_complete_creates_profile_and_switch_succeeds(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_bilibili(monkeypatch)
    onboarding_dir = root / "shared" / "onboarding"
    secrets_dir = onboarding_dir / ".secrets"

    start = client.post("/api/accounts/onboarding/start")
    assert start.status_code == 200, start.text
    onboarding_id = start.json()["result"]["onboarding_id"]
    assert onboarding_id
    # V31：start 后普通 JSON 无 qrcode_key，secret 在 sidecar
    session_text = (onboarding_dir / f"{onboarding_id}.json").read_text(encoding="utf-8")
    assert "k-1" not in session_text
    assert '"qr_key"' not in session_text
    assert (secrets_dir / f"{onboarding_id}.qr_key").read_text(encoding="utf-8") == "k-1"

    poll = client.post(f"/api/accounts/onboarding/{onboarding_id}/poll")
    assert poll.status_code == 200, poll.text
    assert poll.json()["result"]["status"] == "success"
    # V31：poll success 后普通 JSON 无 Cookie；cookie sidecar 存在，qr_key 已删除
    session_text = (onboarding_dir / f"{onboarding_id}.json").read_text(encoding="utf-8")
    assert "sess-1" not in session_text
    assert "90001" not in session_text
    assert '"cookie"' not in session_text
    assert (secrets_dir / f"{onboarding_id}.cookie").is_file()
    assert not (secrets_dir / f"{onboarding_id}.qr_key").exists()

    complete = client.post(COMPLETE_PATH, json={"onboarding_id": onboarding_id})
    assert complete.status_code == 200, complete.text
    result = complete.json()["result"]
    assert result["profile_id"] == "account-2"
    assert result["created_new"] is True
    assert result["reused_existing"] is False
    assert result["restart_required"] is True
    assert "uid" not in result
    # V31：complete 后全部 onboarding secret 清理，receipt sanitized
    assert not (secrets_dir / f"{onboarding_id}.cookie").exists()
    assert not (secrets_dir / f"{onboarding_id}.qr_key").exists()
    receipt = (onboarding_dir / f"{onboarding_id}.json").read_text(encoding="utf-8")
    assert "sess-1" not in receipt
    assert "90001" not in receipt

    profile_dir = root / "profiles" / "account-2"
    assert (profile_dir / "profile.json").is_file()
    assert (profile_dir / "cookies.txt").is_file()
    db = profile_dir / "binggo.db"
    assert db.is_file()
    assert _db_integrity(db) == "ok"

    pointer = json.loads((root / "active_profile.json").read_text(encoding="utf-8"))
    assert pointer["profile_id"] == "account-2"

    profiles = client.get("/api/profiles")
    assert profiles.status_code == 200
    ids = [p["profile_id"] for p in profiles.json()["profiles"]]
    assert "account-2" in ids

    server = _enable_supervised_switch(monkeypatch)
    _patch_idle(monkeypatch)
    switch = client.post("/api/profiles/account-2/switch")
    assert switch.status_code == 200, switch.text
    assert switch.json()["restart_requested"] is True
    assert server.should_exit is True


def test_existing_uid_reuses_profile_without_duplicate(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "profiles" / "account-1" / "profile.json").write_text(
        json.dumps({"profile_id": "account-1", "mid": "90001", "nickname": "same"}),
        encoding="utf-8",
    )
    onboarding_id = _scan_to_staged(root, monkeypatch, dede_user_id="90001")

    complete = client.post(COMPLETE_PATH, json={"onboarding_id": onboarding_id})
    assert complete.status_code == 200, complete.text
    result = complete.json()["result"]
    assert result["profile_id"] == "account-1"
    assert result["created_new"] is False
    assert result["reused_existing"] is True
    assert not (root / "profiles" / "account-2").exists()
    assert len([p for p in (root / "profiles").iterdir() if p.is_dir()]) == 1
    # V31：existing UID 路径同样清理全部 onboarding secret
    secrets_dir = root / "shared" / "onboarding" / ".secrets"
    assert not (secrets_dir / f"{onboarding_id}.cookie").exists()
    assert not (secrets_dir / f"{onboarding_id}.qr_key").exists()

    _patch_idle(monkeypatch)
    switch = client.post("/api/profiles/account-1/switch")
    assert switch.status_code == 200, switch.text
    assert switch.json()["restart_requested"] is False


def test_complete_retry_is_idempotent_after_success(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    onboarding_id = _scan_to_staged(root, monkeypatch)

    first = client.post(COMPLETE_PATH, json={"onboarding_id": onboarding_id})
    assert first.status_code == 200
    # 模拟 HTTP 响应丢失后的重试：同一 onboarding_id 再次 complete
    second = client.post(COMPLETE_PATH, json={"onboarding_id": onboarding_id})
    assert second.status_code == 200
    assert second.json()["result"] == first.json()["result"]
    assert not (root / "profiles" / "account-3").exists()


def test_complete_switch_failure_keeps_pointer_for_manual_restart(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    onboarding_id = _scan_to_staged(root, monkeypatch)
    complete = client.post(COMPLETE_PATH, json={"onboarding_id": onboarding_id})
    assert complete.status_code == 200

    # switch 不可用（未启用 supervised restart）→ 503，但 active pointer 已由 complete 写好
    _patch_idle(monkeypatch)
    switch = client.post("/api/profiles/account-2/switch")
    assert switch.status_code == 503

    pointer = json.loads((root / "active_profile.json").read_text(encoding="utf-8"))
    assert pointer["profile_id"] == "account-2"
    # 下一次启动/读取 pointer 即绑定 account-2
    from src.profile_manager import get_active_profile

    assert get_active_profile()["profile_id"] == "account-2"
