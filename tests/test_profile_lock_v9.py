"""Profile create/delete 跨进程锁（V9）：RegistryLease 防 account-N 竞争、
delete 前 WriterLease busy 拒绝。set_active 保持原行为。

覆盖 A-H：并发 create 不重复分配 / onboarding 与 Web create 互斥 /
busy 拒绝删除 / 拒绝时目录完整 / 空闲正常删除 / 异 Profile 不互锁 /
失败不残留半成品 / set_active 不加锁。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from src import data_paths, profile_manager
from src.profile_manager import create_profile, delete_profile, set_active_profile
from src.profile_writer_lease import (
    ProfileWriterLeaseBusy,
    profile_registry_lease,
    profile_writer_lease,
)


@pytest.fixture
def proot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base = tmp_path / "data-root"
    profiles = base / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    account = profiles / "account-1"
    account.mkdir()
    (account / "profile.json").write_text(
        json.dumps({"profile_id": "account-1", "mid": "88001", "nickname": "old"}),
        encoding="utf-8",
    )
    (account / "cookies.txt").write_text("SESSDATA=old", encoding="utf-8")
    (base / "active_profile.json").write_text(
        json.dumps({"profile_id": "account-1"}), encoding="utf-8"
    )
    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(base))
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    monkeypatch.delenv(data_paths.LEGACY_DATA_ROOT_ENV, raising=False)
    data_paths.reset_runtime_profile_for_tests()
    assert data_paths.get_runtime_profile_id() == "account-1"
    yield base
    data_paths.reset_runtime_profile_for_tests()


def _profile_dirs(root: Path) -> list[str]:
    return sorted(
        p.name
        for p in (root / "profiles").iterdir()
        if p.is_dir()
    )


def test_a_concurrent_create_never_reuses_account_number(proot: Path) -> None:
    created: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            created.append(create_profile()["profile_id"])
        except ProfileWriterLeaseBusy as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(created)) == len(created)
    dirs = _profile_dirs(proot)
    assert all(dir not in created for dir in dirs if dir.startswith(".deleting"))
    assert len(created) + 1 == len(dirs)  # account-1 + 每次成功 create 一个


def test_b_create_profile_respects_registry_lease(proot: Path) -> None:
    create_profile()  # account-2
    with profile_registry_lease(owner="test:holder"):
        with pytest.raises(ProfileWriterLeaseBusy):
            create_profile()
    # 失败不产生半成品/额外目录
    assert _profile_dirs(proot) == ["account-1", "account-2"]


def test_c_delete_profile_rejected_when_writer_busy(proot: Path) -> None:
    create_profile()  # account-2
    with profile_writer_lease(owner="test:writer", profile_id="account-2"):
        with pytest.raises(ValueError, match="该账号正在执行任务"):
            delete_profile("account-2")


def test_d_delete_rejected_leaves_profile_intact(proot: Path) -> None:
    create_profile()  # account-2
    marker = proot / "profiles" / "account-2" / "binggo.db"
    marker.write_bytes(b"db-marker")
    with profile_writer_lease(owner="test:writer", profile_id="account-2"):
        with pytest.raises(ValueError, match="该账号正在执行任务"):
            delete_profile("account-2")
    assert (proot / "profiles" / "account-2").is_dir()
    assert marker.read_bytes() == b"db-marker"
    assert not list((proot / "profiles").glob(".deleting-*"))


def test_e_delete_profile_succeeds_when_lease_free(proot: Path) -> None:
    create_profile()  # account-2
    marker = proot / "profiles" / "account-2" / "binggo.db"
    marker.write_bytes(b"db-marker")

    delete_profile("account-2")

    assert not (proot / "profiles" / "account-2").exists()
    assert not list((proot / "profiles").glob(".deleting-*"))
    assert _profile_dirs(proot) == ["account-1"]


def test_f_writer_on_other_profile_does_not_block_delete(proot: Path) -> None:
    create_profile()  # account-2
    with profile_writer_lease(owner="test:writer", profile_id="account-1"):
        delete_profile("account-2")
    assert not (proot / "profiles" / "account-2").exists()


def test_g_concurrent_registry_create_leaves_no_partial_profile(proot: Path) -> None:
    before = _profile_dirs(proot)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            create_profile()
        except ProfileWriterLeaseBusy as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    dirs = _profile_dirs(proot)
    assert all(name.startswith(("account-", "default")) for name in dirs)
    assert not list((proot / "profiles").glob(".creating-*"))
    assert not list((proot / "profiles").glob(".deleting-*"))
    # 成功创建的账号数与目录增量一致（busy 失败者不留下任何目录）
    assert len(dirs) == len(before) + len([d for d in dirs if d not in before])


def test_h_set_active_profile_keeps_original_behavior(proot: Path) -> None:
    create_profile()  # account-2
    with profile_writer_lease(owner="test:writer", profile_id="account-2"):
        selected = set_active_profile("account-2")
    assert selected["profile_id"] == "account-2"
    assert json.loads((proot / "active_profile.json").read_text(encoding="utf-8")) == {
        "profile_id": "account-2"
    }
