"""V109 offline runtime ownership, port discovery, and launch safety."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src import data_paths
from src import runtime_instances as instances
from src import runtime_launch as launch


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "data"
    for name in ("account-1", "account-2", "default"):
        directory = base / "profiles" / name
        directory.mkdir(parents=True)
        (directory / "profile.json").write_text(
            json.dumps({"profile_id": name, "mid": name}), encoding="utf-8"
        )
    (base / "active_profile.json").write_text('{"profile_id":"account-1"}', encoding="utf-8")
    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(base))
    monkeypatch.delenv(instances.RUNTIME_PROFILE_ENV, raising=False)
    data_paths.reset_runtime_profile_for_tests()
    yield base
    data_paths.reset_runtime_profile_for_tests()


def test_explicit_runtime_freezes_independently_of_active_pointer(root: Path, monkeypatch) -> None:
    monkeypatch.setenv(instances.RUNTIME_PROFILE_ENV, "account-2")
    assert data_paths.get_runtime_profile_id() == "account-2"
    (root / "active_profile.json").write_text('{"profile_id":"account-1"}', encoding="utf-8")
    assert data_paths.get_runtime_profile_id() == "account-2"
    assert data_paths.get_selected_profile_id() == "account-1"


def test_profile_paths_are_isolated(root: Path) -> None:
    assert data_paths.get_database_path("account-1") != data_paths.get_database_path("account-2")
    assert data_paths.get_cookie_path("account-1") != data_paths.get_cookie_path("account-2")
    assert data_paths.get_profile_dir("account-1") / "logs" != data_paths.get_profile_dir("account-2") / "logs"
    assert data_paths.get_profile_dir("account-1") / "scheduler_slots.json" != data_paths.get_profile_dir("account-2") / "scheduler_slots.json"


def test_runtime_locks_allow_different_profiles_but_not_duplicate(root: Path) -> None:
    first = instances.acquire_runtime_lock("account-1")
    try:
        with pytest.raises(instances.RuntimeBusyError):
            instances.acquire_runtime_lock("account-1")
        second = instances.acquire_runtime_lock("account-2")
        second.release()
    finally:
        first.release()
    third = instances.acquire_runtime_lock("account-1")
    third.release()


def test_runtime_lock_releases_after_other_process_exits(root: Path) -> None:
    code = (
        "from src.runtime_instances import acquire_runtime_lock; "
        "import sys; lock=acquire_runtime_lock('account-1'); "
        "print('READY',flush=True); sys.stdin.read(1); lock.release()"
    )
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", code], cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, data_paths.DATA_ROOT_ENV: str(root)},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        with pytest.raises(instances.RuntimeBusyError):
            instances.acquire_runtime_lock("account-1")
    finally:
        child.kill()
        child.wait(timeout=10)
    lease = instances.acquire_runtime_lock("account-1")
    lease.release()


@pytest.mark.parametrize("stopped", ["account-1", "account-2"])
def test_stopping_one_profile_process_does_not_release_the_other_lock(root: Path, stopped: str) -> None:
    code = (
        "import sys; from src.runtime_instances import acquire_runtime_lock, acquire_service_lock; "
        "p=sys.argv[1]; a=acquire_runtime_lock(p); b=acquire_service_lock(p); "
        "print('READY', flush=True); sys.stdin.read(1); b.release(); a.release()"
    )
    children: dict[str, subprocess.Popen[str]] = {}
    try:
        for profile_id in ("account-1", "account-2"):
            child = subprocess.Popen(
                [sys.executable, "-u", "-c", code, profile_id],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, data_paths.DATA_ROOT_ENV: str(root)},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
            children[profile_id] = child
            assert child.stdout is not None and child.stdout.readline().strip() == "READY"
        survivor = "account-2" if stopped == "account-1" else "account-1"
        children[stopped].kill()
        children[stopped].wait(timeout=10)
        assert children[survivor].poll() is None
        assert instances.runtime_lock_held(survivor)
        assert instances.service_lock_held(survivor)
        assert not instances.runtime_lock_held(stopped)
    finally:
        for child in children.values():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


def test_registry_reserves_distinct_ports_and_blocks_duplicate_click(root: Path) -> None:
    a = uuid.uuid4().hex
    b = uuid.uuid4().hex
    port_a = instances.reserve_runtime("account-1", instance_id=a, preferred_port=8181, owner_pid=os.getpid())
    with pytest.raises(instances.RuntimeBusyError):
        instances.reserve_runtime("account-1", instance_id=uuid.uuid4().hex, preferred_port=8181, owner_pid=os.getpid())
    port_b = instances.reserve_runtime("account-2", instance_id=b, preferred_port=8181, owner_pid=os.getpid())
    assert (port_a, port_b) == (8181, 8182)
    instances.clear_runtime("account-1", instance_id=a)
    instances.clear_runtime("account-2", instance_id=b)


def test_two_concurrent_reservations_for_one_profile_admit_only_one(root: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    def attempt() -> str:
        try:
            instances.reserve_runtime(
                "account-2", instance_id=uuid.uuid4().hex,
                preferred_port=8181, owner_pid=os.getpid(),
            )
            return "reserved"
        except instances.RuntimeBusyError:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: attempt(), range(2)))
    assert sorted(results) == ["busy", "reserved"]


def test_dead_starting_pid_is_reaped_before_new_reservation(root: Path) -> None:
    path = root / "shared" / "runtime-registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"instances": {"account-1": {
        "profile_id": "account-1", "port": 8181, "pid": 999999999,
        "instance_id": uuid.uuid4().hex, "started_at": 1, "status": "starting",
    }}}), encoding="utf-8")
    new_id = uuid.uuid4().hex
    assert instances.reserve_runtime("account-1", instance_id=new_id, preferred_port=8181, owner_pid=os.getpid()) == 8181
    instances.clear_runtime("account-1", instance_id=new_id)


def test_ready_probe_requires_exact_profile_instance_and_port(root: Path) -> None:
    profile_id = "account-2"
    instance_id = uuid.uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"ready": True, "profile_id": profile_id,
                               "instance_id": instance_id, "port": self.server.server_port}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        assert instances.runtime_ready(profile_id, instance_id=instance_id, port=port)
        assert not instances.runtime_ready("account-1", instance_id=instance_id, port=port)
        assert not instances.runtime_ready(profile_id, instance_id=uuid.uuid4().hex, port=port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_source_and_packaged_launch_commands_are_fixed_and_non_shell(root: Path, monkeypatch) -> None:
    instance_id = uuid.uuid4().hex
    monkeypatch.delattr(launch.sys, "frozen", raising=False)
    args = launch._launch_args("account-2", 8182, instance_id)
    assert args[:2] == [sys.executable, str(launch.ROOT / "scripts" / "run_dashboard.py")]
    assert args[-1] == "--no-browser"
    with pytest.raises(ValueError):
        launch._launch_args("account-2 & calc", 8182, instance_id)
    monkeypatch.setattr(launch.sys, "frozen", True, raising=False)
    assert launch._launch_args("account-2", 8182, instance_id)[:2] == [sys.executable, "--profile"]


def test_launch_opens_browser_only_after_ready(root: Path, monkeypatch) -> None:
    class FakeProcess:
        def poll(self):
            return None

    events: list[str] = []
    monkeypatch.setattr(launch, "_port_available", lambda _port: True)
    monkeypatch.setattr(launch, "_spawn_runtime", lambda *_args: events.append("spawn") or FakeProcess())
    monkeypatch.setattr(launch, "_wait_ready", lambda *_args: events.append("ready") or True)
    monkeypatch.setattr(launch.webbrowser, "open", lambda *_args, **_kwargs: events.append("browser") or True)
    result = launch.launch_existing_profile("account-2")
    assert events == ["spawn", "ready", "browser"]
    assert result["profile_id"] == "account-2"
    assert json.loads((root / "active_profile.json").read_text(encoding="utf-8"))["profile_id"] == "account-1"


def test_launch_existing_ready_profile_does_not_spawn_a_third_runtime(root: Path, monkeypatch) -> None:
    monkeypatch.setattr(launch, "get_runtime_instance", lambda _profile: {
        "profile_id": "account-2", "port": 8182,
        "instance_id": uuid.uuid4().hex, "ready": True, "running": True,
    })
    monkeypatch.setattr(launch, "_spawn_runtime", lambda *_args: pytest.fail("unexpected third runtime"))
    result = launch.launch_existing_profile("account-2")
    assert result["already_running"] is True
    assert result["url"] == "http://127.0.0.1:8182/"


def test_launch_identity_failure_never_opens_browser(root: Path, monkeypatch) -> None:
    class FailedProcess:
        def poll(self):
            return 1

    opened: list[str] = []
    monkeypatch.setattr(launch, "_port_available", lambda _port: True)
    monkeypatch.setattr(launch, "_spawn_runtime", lambda *_args: FailedProcess())
    monkeypatch.setattr(launch, "_wait_ready", lambda *_args: False)
    monkeypatch.setattr(launch.webbrowser, "open", lambda url, **_kwargs: opened.append(url))
    with pytest.raises(RuntimeError):
        launch.launch_existing_profile("account-2")
    assert opened == []


def test_default_uid_is_reused_without_second_database(root: Path) -> None:
    from src.login_onboarding import commit_staged_login

    default = root / "profiles" / "default"
    (default / "profile.json").write_text(
        '{"profile_id":"default","mid":"90001","nickname":"old"}', encoding="utf-8"
    )
    (default / "cookies.txt").write_text("SESSDATA=old;DedeUserID=90001", encoding="utf-8")
    (default / "binggo.db").write_bytes(b"old database sentinel")
    result = commit_staged_login(cookie_str="SESSDATA=new;DedeUserID=90001", nickname="new")
    assert result["profile_id"] == "default"
    assert result["created_new"] is False
    assert (default / "binggo.db").read_bytes() == b"old database sentinel"
    assert not (root / "profiles" / "account-3").exists()


def test_running_uid_scan_does_not_overwrite_cookie(root: Path) -> None:
    from src.login_onboarding import commit_staged_login

    target = root / "profiles" / "account-2"
    (target / "profile.json").write_text(
        '{"profile_id":"account-2","mid":"90001","nickname":"old"}', encoding="utf-8"
    )
    (target / "cookies.txt").write_text("SESSDATA=old;DedeUserID=90001", encoding="utf-8")
    runtime_id = uuid.uuid4().hex
    lease = instances.acquire_runtime_lock("account-2")
    try:
        instances.claim_runtime("account-2", instance_id=runtime_id, port=8182, pid=os.getpid())
        result = commit_staged_login(cookie_str="SESSDATA=new;DedeUserID=90001", nickname="new")
        assert result["profile_id"] == "account-2"
        assert result["already_running"] is True
        assert (target / "cookies.txt").read_text(encoding="utf-8") == "SESSDATA=old;DedeUserID=90001"
        assert not (root / "profiles" / "account-3").exists()
    finally:
        instances.clear_runtime("account-2", instance_id=runtime_id)
        lease.release()


def test_running_profile_cannot_be_deleted(root: Path) -> None:
    from src.profile_manager import delete_profile

    runtime_id = uuid.uuid4().hex
    lease = instances.acquire_runtime_lock("account-2")
    try:
        instances.claim_runtime("account-2", instance_id=runtime_id, port=8182, pid=os.getpid())
        with pytest.raises(ValueError, match="正在运行"):
            delete_profile("account-2")
        assert (root / "profiles" / "account-2").is_dir()
    finally:
        instances.clear_runtime("account-2", instance_id=runtime_id)
        lease.release()


def test_stale_ready_record_does_not_block_new_supervisor(root: Path) -> None:
    stale_id = uuid.uuid4().hex
    path = root / "shared" / "runtime-registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"instances": {"account-2": {
        "profile_id": "account-2", "port": 8182, "pid": os.getpid(),
        "instance_id": stale_id, "started_at": 1, "status": "ready",
    }}}), encoding="utf-8")
    lease = instances.acquire_runtime_lock("account-2")
    try:
        new_id = uuid.uuid4().hex
        assert instances.reserve_runtime(
            "account-2", instance_id=new_id, preferred_port=8181, owner_pid=os.getpid()
        ) == 8181
        instances.clear_runtime("account-2", instance_id=new_id)
    finally:
        lease.release()


def test_ready_api_exposes_frozen_identity_only(root: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from web.app import app

    identity = uuid.uuid4().hex
    monkeypatch.setenv(instances.RUNTIME_INSTANCE_ENV, identity)
    monkeypatch.setenv(instances.RUNTIME_PORT_ENV, "8182")
    response = TestClient(app, base_url="http://127.0.0.1:8182").get("/api/runtime/ready")
    assert response.status_code == 200
    assert response.json() == {
        "ready": True, "profile_id": "account-1", "instance_id": identity, "port": 8182,
    }


def test_launch_route_rejects_foreign_origin_and_only_accepts_profile_id(root: Path, monkeypatch) -> None:
    import importlib
    from fastapi.testclient import TestClient

    web_app = importlib.import_module("web.app")
    calls: list[str] = []
    monkeypatch.setattr(web_app, "launch_existing_profile", lambda pid: calls.append(pid) or {
        "profile_id": pid, "port": 8182, "ready": True, "running": True,
        "instance_id": uuid.uuid4().hex, "url": "http://127.0.0.1:8182/",
    })
    client = TestClient(web_app.app, base_url="http://127.0.0.1:8787")
    path = "/api/profiles/account-2/launch"
    assert client.post(path, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(path, headers={"Origin": "http://127.0.0.1:9999"}).status_code == 403
    assert calls == []
    injected = client.post(path, headers={"Origin": "http://127.0.0.1:8787"}, json={
        "exe": "calc.exe", "port": 9999,
    })
    assert injected.status_code == 400
    assert calls == []
    response = client.post(path, headers={"Origin": "http://127.0.0.1:8787"})
    assert response.status_code == 200, response.text
    assert calls == ["account-2"]
    assert response.json()["port"] == 8182


def test_profile_list_marks_other_running_instance_without_credentials(root: Path, monkeypatch) -> None:
    import importlib

    web_app = importlib.import_module("web.app")
    monkeypatch.setattr(web_app, "list_profiles", lambda: [
        {"profile_id": "account-1", "mid": "1", "nickname": "A"},
        {"profile_id": "account-2", "mid": "2", "nickname": "B"},
    ])
    monkeypatch.setattr(web_app, "list_runtime_instances", lambda: {
        "account-2": {"port": 8182, "ready": True, "running": True},
    })
    result = web_app.api_profiles()
    assert result["profiles"][0]["running"] is False
    assert result["profiles"][1]["runtime_port"] == 8182
    assert "cookie" not in json.dumps(result).lower()


def test_running_target_switch_is_rejected_before_pointer_write(root: Path, monkeypatch) -> None:
    import importlib
    from fastapi.testclient import TestClient

    web_app = importlib.import_module("web.app")
    monkeypatch.setattr(web_app, "get_runtime_instance", lambda pid: {
        "profile_id": pid, "port": 8182, "ready": True,
    })
    monkeypatch.setattr(web_app, "_require_profile_operations_idle", lambda **_kwargs: None)
    monkeypatch.setattr(web_app.restart_control, "begin_restart", lambda transition: transition())
    client = TestClient(web_app.app, base_url="http://127.0.0.1:8787")
    response = client.post("/api/profiles/account-2/switch")
    assert response.status_code in (400, 409)
    assert json.loads((root / "active_profile.json").read_text(encoding="utf-8"))["profile_id"] == "account-1"


def test_onboarding_rejects_conflicting_metadata_and_cookie_uid(root: Path) -> None:
    from src.login_onboarding import OnboardingError, commit_staged_login

    target = root / "profiles" / "default"
    (target / "profile.json").write_text(
        '{"profile_id":"default","mid":"90001"}', encoding="utf-8"
    )
    (target / "cookies.txt").write_text("SESSDATA=old;DedeUserID=90002", encoding="utf-8")
    with pytest.raises(OnboardingError, match="不一致"):
        commit_staged_login(cookie_str="SESSDATA=new;DedeUserID=90001")
    assert not (root / "profiles" / "account-3").exists()


@pytest.mark.parametrize("value", ["0", "80", "65536", "not-a-port"])
def test_service_rejects_invalid_explicit_port(root: Path, monkeypatch, value: str) -> None:
    from src.dashboard_server import get_dashboard_port

    monkeypatch.setenv(instances.RUNTIME_PORT_ENV, value)
    with pytest.raises(RuntimeError):
        get_dashboard_port()


def test_spawn_uses_current_executable_without_shell_or_console(root: Path, monkeypatch) -> None:
    from src import app_paths

    captured: dict[str, object] = {}
    monkeypatch.setattr(app_paths, "bundle_root", lambda: root)
    monkeypatch.setattr(launch.sys, "platform", "win32")

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(launch.subprocess, "Popen", fake_popen)
    launch._spawn_runtime("account-2", 8182, uuid.uuid4().hex)
    assert (captured["args"])[0] == sys.executable
    assert (captured["kwargs"])["creationflags"] == launch.CREATE_NO_WINDOW
    assert (captured["kwargs"])["cwd"] == str(root)
    assert (captured["kwargs"])["env"][data_paths.DATA_ROOT_ENV] == str(root)
    assert "shell" not in captured["kwargs"]


def test_launch_skips_occupied_port_and_uses_next_candidate(root: Path, monkeypatch) -> None:
    class FakeProcess:
        def poll(self):
            return None

    started: list[int] = []
    monkeypatch.setattr(launch, "_port_available", lambda port: port != 8787)
    monkeypatch.setattr(launch, "_spawn_runtime", lambda _profile, port, _id: started.append(port) or FakeProcess())
    monkeypatch.setattr(launch, "_wait_ready", lambda *_args: True)
    monkeypatch.setattr(launch.webbrowser, "open", lambda *_args, **_kwargs: True)
    monkeypatch.delattr(launch.sys, "frozen", raising=False)
    result = launch.launch_existing_profile("account-2")
    assert started == [8788]
    assert result["port"] == 8788


def test_browser_failure_retains_manual_url(root: Path, monkeypatch) -> None:
    class FakeProcess:
        def poll(self):
            return None

    monkeypatch.setattr(launch, "_port_available", lambda _port: True)
    monkeypatch.setattr(launch, "_spawn_runtime", lambda *_args: FakeProcess())
    monkeypatch.setattr(launch, "_wait_ready", lambda *_args: True)
    monkeypatch.setattr(launch.webbrowser, "open", lambda *_args, **_kwargs: False)
    result = launch.launch_existing_profile("account-2")
    assert result["browser_opened"] is False
    assert result["url"].startswith("http://127.0.0.1:")


def test_stale_ready_record_is_pruned_after_runtime_exit(root: Path) -> None:
    runtime_id = uuid.uuid4().hex
    lease = instances.acquire_runtime_lock("account-2")
    instances.claim_runtime("account-2", instance_id=runtime_id, port=8182, pid=os.getpid())
    instances.mark_runtime_ready("account-2", instance_id=runtime_id)
    lease.release()
    assert instances.get_runtime_instance("account-2") is None
    assert "account-2" not in json.loads(
        (root / "shared" / "runtime-registry.json").read_text(encoding="utf-8")
    )["instances"]


def test_orphan_service_lock_blocks_new_supervisor_and_delete(root: Path, monkeypatch) -> None:
    from src.profile_manager import delete_profile

    instance_id = uuid.uuid4().hex
    service_lease = instances.acquire_service_lock("account-2")
    monkeypatch.setattr(instances, "_ready_identity", lambda _entry, **_kwargs: True)
    try:
        instances.claim_runtime("account-2", instance_id=instance_id, port=8182, pid=999999999)
        instances.mark_runtime_ready("account-2", instance_id=instance_id)
        found = instances.get_runtime_instance("account-2")
        assert found is not None and found["ready"] is True
        with pytest.raises(instances.RuntimeBusyError):
            instances.acquire_runtime_lock("account-2")
        with pytest.raises(ValueError, match="正在运行"):
            delete_profile("account-2")
    finally:
        service_lease.release()
    assert instances.get_runtime_instance("account-2") is None
