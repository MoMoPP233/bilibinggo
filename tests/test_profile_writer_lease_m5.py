"""Profile WriterLease（M5）离线测试：同进程 + 跨进程（subprocess），零真实网络。

A 获取/释放；B 同 Profile 跨进程互斥；C 不同 Profile 不互斥；D 正常退出释放；
E 异常释放；F kill 后 OS 释放；G 残留文件不误判；H 同进程双 writer busy；
I（read-only 语义见 R）；J/K/L CLI fail-closed；M 不同 Profile 写型不互相阻塞；
N 业务异常释放；O risk/cancel 等价异常释放；P 嵌套不自身死锁（直接 busy）；
R run_action 写型/只读分类。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CHILD_SNIPPET = r"""
import sys, time
sys.path.insert(0, r"{root}")
from src.profile_writer_lease import profile_writer_acquire, profile_writer_release
lease = profile_writer_acquire(profile_id="{profile}", owner="child")
print("acquired", flush=True)
sys.stdout.flush()
time.sleep({hold})
lease.release()
"""

HOLD_CHILD = r"""
import sys, time
sys.path.insert(0, r"{root}")
from src.profile_writer_lease import profile_writer_acquire, profile_writer_release
lease = profile_writer_acquire(profile_id="{profile}", owner="child")
print("acquired", flush=True)
time.sleep(60)
"""


def _env(data_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["BINGGO_DATA_ROOT"] = str(data_root)
    env.pop("BINGGO_HOME", None)
    env.pop("BINGGO_LEGACY_DATA_ROOT", None)
    return env


def _make_root(tmp_path: Path, *profiles: str) -> Path:
    root = tmp_path / "data-root"
    for profile in profiles:
        directory = root / "profiles" / profile
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "profile.json").write_text(
            '{"profile_id": "%s"}' % profile, encoding="utf-8"
        )
    (root / "active_profile.json").write_text(
        '{"profile_id": "%s"}' % profiles[0], encoding="utf-8"
    )
    return root


def _spawn_child(root: Path, code: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env(root),
        cwd=str(ROOT),
    )


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = _make_root(tmp_path, "account-1", "account-2")
    from src import data_paths

    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(root))
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    monkeypatch.delenv(data_paths.LEGACY_DATA_ROOT_ENV, raising=False)
    monkeypatch.delenv("BINGGO_PORTABLE", raising=False)
    data_paths.reset_runtime_profile_for_tests()
    return root


def _lock_path(root: Path, profile: str) -> Path:
    return root / "profiles" / profile / "binggo.writer.lock"


# ---------- A：单进程获取/释放 ----------


def test_single_process_acquire_release(data_root: Path) -> None:
    import src.profile_writer_lease as pwl

    lease = pwl.profile_writer_acquire(profile_id="account-1", owner="test")
    assert lease.held
    pwl.profile_writer_release(lease, profile_id="account-1")
    # 释放后再次获取成功
    again = pwl.profile_writer_acquire(profile_id="account-1", owner="test")
    assert again.held
    pwl.profile_writer_release(again, profile_id="account-1")


# ---------- B：同 Profile 跨进程互斥 ----------


def test_same_profile_cross_process_conflict(data_root: Path) -> None:
    import src.profile_writer_lease as pwl

    child = _spawn_child(data_root, CHILD_SNIPPET.format(root=ROOT, profile="account-1", hold=5))
    try:
        line = child.stdout.readline() if child.stdout else ""
        assert "acquired" in line
        with pytest.raises(pwl.ProfileWriterLeaseBusy):
            pwl.profile_writer_acquire(profile_id="account-1", owner="parent")
    finally:
        child.kill()
        child.wait(timeout=10)


# ---------- C：不同 Profile 跨进程不互斥 ----------


def test_different_profiles_cross_process_independent(data_root: Path) -> None:
    import src.profile_writer_lease as pwl

    child = _spawn_child(data_root, CHILD_SNIPPET.format(root=ROOT, profile="account-1", hold=5))
    try:
        line = child.stdout.readline() if child.stdout else ""
        assert "acquired" in line
        # Profile-2 可正常获取
        lease = pwl.profile_writer_acquire(profile_id="account-2", owner="parent")
        assert lease.held
        pwl.profile_writer_release(lease, profile_id="account-2")
    finally:
        child.kill()
        child.wait(timeout=10)


# ---------- D：正常退出释放 ----------


def test_normal_exit_releases(data_root: Path) -> None:
    import src.profile_writer_lease as pwl

    child = _spawn_child(data_root, CHILD_SNIPPET.format(root=ROOT, profile="account-1", hold=1))
    assert child.wait(timeout=10) == 0
    lease = pwl.profile_writer_acquire(profile_id="account-1", owner="parent")
    assert lease.held
    pwl.profile_writer_release(lease, profile_id="account-1")


# ---------- E/N/O：业务/风险异常释放 ----------


@pytest.mark.parametrize("error", [
    RuntimeError("business failure"),
    RuntimeError("API error -352: 风控校验失败"),
    KeyboardInterrupt("ctrl-c"),
])
def test_exception_paths_always_release(data_root: Path, error: BaseException) -> None:
    import src.profile_writer_lease as pwl

    try:
        with pwl.profile_writer_lease(profile_id="account-1", owner="test"):
            raise error
    except BaseException as exc:  # noqa: BLE001
        assert isinstance(exc, type(error))
    lease = pwl.profile_writer_acquire(profile_id="account-1", owner="after")
    assert lease.held
    pwl.profile_writer_release(lease, profile_id="account-1")


# ---------- F：kill 后 OS 自动释放 ----------


def test_killed_child_releases_os_lock(data_root: Path) -> None:
    import src.profile_writer_lease as pwl

    child = _spawn_child(data_root, HOLD_CHILD.format(root=ROOT, profile="account-1"))
    try:
        line = child.stdout.readline() if child.stdout else ""
        assert "acquired" in line
        # 锁文件存在（不表示占用）
        assert _lock_path(data_root, "account-1").exists()
    finally:
        child.kill()
        child.wait(timeout=10)
    # kill 后 OS 锁已释放，下一 writer 可获取
    lease = pwl.profile_writer_acquire(profile_id="account-1", owner="after-kill")
    assert lease.held
    pwl.profile_writer_release(lease, profile_id="account-1")


# ---------- G：残留文件不误判 ----------


def test_stale_lock_file_without_os_lock_is_acquirable(data_root: Path) -> None:
    import src.profile_writer_lease as pwl

    target = _lock_path(data_root, "account-1")
    target.write_text("残留诊断内容 pid=999\n", encoding="utf-8")
    lease = pwl.profile_writer_acquire(profile_id="account-1", owner="test")
    assert lease.held
    pwl.profile_writer_release(lease, profile_id="account-1")


# ---------- H：同进程并发双 writer busy ----------


def test_same_process_second_writer_busy(data_root: Path) -> None:
    import src.profile_writer_lease as pwl

    lease = pwl.profile_writer_acquire(profile_id="account-1", owner="writer-a")
    try:
        result: dict[str, object] = {}

        def second() -> None:
            try:
                pwl.profile_writer_acquire(profile_id="account-1", owner="writer-b")
                result["ok"] = True
            except pwl.ProfileWriterLeaseBusy:
                result["ok"] = False

        thread = threading.Thread(target=second)
        thread.start()
        thread.join(10)
        assert result.get("ok") is False
    finally:
        pwl.profile_writer_release(lease, profile_id="account-1")


# ---------- P：嵌套获取直接 busy，不自身死锁 ----------


def test_nested_acquire_is_busy_not_deadlock(data_root: Path) -> None:
    import src.profile_writer_lease as pwl

    lease = pwl.profile_writer_acquire(profile_id="account-1", owner="outer")
    try:
        with pytest.raises(pwl.ProfileWriterLeaseBusy):
            pwl.profile_writer_acquire(profile_id="account-1", owner="inner")
    finally:
        pwl.profile_writer_release(lease, profile_id="account-1")


# ---------- J/K/L：写型 CLI fail-closed（锁被占用 → 退出码 2、0 mutation） ----------


@pytest.mark.parametrize("script", ["participate.py", "bili_login.py", "maintain_local_activities.py"])
def test_writer_cli_fails_closed_when_profile_busy(data_root: Path, script: str) -> None:
    import src.profile_writer_lease as pwl

    lease = pwl.profile_writer_acquire(profile_id="account-1", owner="web-job")
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), "--help"],
            capture_output=True,
            text=True,
            env=_env(data_root),
            cwd=str(ROOT),
            timeout=30,
        )
        # 锁被占用时任何参数路径都会在进入业务前 fail-closed（help 也走 main 包装）
        assert proc.returncode == 2
        assert "另一个写入任务使用" in proc.stderr
    finally:
        pwl.profile_writer_release(lease, profile_id="account-1")


# ---------- R：run_action 写型/只读分类 ----------


def test_writer_action_classification() -> None:
    import web.actions as actions

    assert "participate" in actions.WRITER_LEASE_ACTIONS
    assert "participate_triple" in actions.WRITER_LEASE_ACTIONS
    assert "update_all_datasources" in actions.WRITER_LEASE_ACTIONS
    assert "scan_expired_reposts" in actions.WRITER_LEASE_ACTIONS
    assert "sync_assess_repost_history" in actions.WRITER_LEASE_ACTIONS
    assert "cleanup_maintenance_cycle" in actions.WRITER_LEASE_ACTIONS
    # 不存在只读 action 误加锁的清单成员
    for read_only in ("not_an_action", "health_check", "list_activities"):
        assert read_only not in actions.WRITER_LEASE_ACTIONS

# ---------- M5 补查：其余 Profile writer CLI busy fail-closed（0 Profile 写入） ----------


NEW_WRITER_SCRIPTS = [
    "import_json_to_db.py",
    "rebuild_all_activity_details.py",
    "backfill_missing_details.py",
    "retry_failed_rebuild.py",
    "purge_all_dead_links.py",
    "fetch_activity_info.py",
    "replenish_watch_users.py",
    "audit_charging_activities.py",
    "migrate_lottery_times.py",
]


@pytest.mark.parametrize("script", NEW_WRITER_SCRIPTS)
def test_additional_writer_clis_fail_closed_before_any_profile_write(data_root: Path, script: str) -> None:
    import src.profile_writer_lease as pwl

    lease = pwl.profile_writer_acquire(profile_id="account-1", owner="web-job")
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), "--help"],
            capture_output=True,
            text=True,
            env=_env(data_root),
            cwd=str(ROOT),
            timeout=60,
        )
        assert proc.returncode == 2
        assert "另一个写入任务使用" in proc.stderr
        # busy 时在第一次 Profile 写入之前失败：Profile DB 未被创建/修改
        assert not (data_root / "profiles" / "account-1" / "binggo.db").exists()
        assert not (data_root / "profiles" / "account-1" / "cookies.txt").exists()
    finally:
        pwl.profile_writer_release(lease, profile_id="account-1")


def test_additional_writer_runs_fine_when_profile_free(data_root: Path) -> None:
    # lease 空闲时：--help 正常进入业务并打印帮助（退出码 0/2 之外的非 busy 行为）
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "import_json_to_db.py"), "--help"],
        capture_output=True,
        text=True,
        env=_env(data_root),
        cwd=str(ROOT),
        timeout=60,
    )
    assert proc.returncode == 0
    assert "usage" in proc.stdout.lower()


# ---------- H：read-only 脚本在锁占用时仍正常只读运行 ----------


def test_read_only_script_unaffected_by_writer_busy(data_root: Path) -> None:
    import src.profile_writer_lease as pwl

    lease = pwl.profile_writer_acquire(profile_id="account-1", owner="web-job")
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_ds1.py"), "--help"],
            capture_output=True,
            text=True,
            env=_env(data_root),
            cwd=str(ROOT),
            timeout=60,
        )
        assert proc.returncode == 0  # read-only 入口不应因 WriterLease 占用而失败
    finally:
        pwl.profile_writer_release(lease, profile_id="account-1")
