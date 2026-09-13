"""Operational polish V10（离线）：

1. 写型 CLI 最外层 WriterLease（check_ds / backfill / rollback；discover 只读不加锁）
2. .creating-* 孤儿安全清理（TTL + RegistryLease + fail-safe）
3. WriterLease 锁文件有界（连续 acquire/release 不增长）
4. UI 周期口径 6 小时（auto_config 展示常量）
5. 测试顺序依赖（conftest autouse reset，见 conftest.py）
6. onboarding startup sweep 一次且失败不阻塞启动
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src import data_paths
from src.profile_manager import create_profile, list_profiles, sweep_stale_creating_profiles
from src.profile_writer_lease import _ProfileWriterLease, profile_writer_lease


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


# ---------- 1. CLI WriterLease ----------


def test_writer_cli_run_check_refuses_busy_before_any_action(proot: Path) -> None:
    import src.check_cli as check_cli

    called: list[str] = []

    def check_update(**kwargs):
        called.append("check_update")
        raise AssertionError("busy 时不应执行任何 check")

    def save_result(result):
        called.append("save_result")
        raise AssertionError("busy 时不应写入")

    with profile_writer_lease(owner="test:busy"):
        with pytest.raises(SystemExit) as excinfo:
            check_cli.run_check(
                description="x",
                force_help="f",
                no_update_label="n",
                check_update=check_update,
                save_result=save_result,
                output_path=Path("unused.json"),
            )
    assert excinfo.value.code == 2
    assert called == []


def test_writer_cli_backfill_refuses_when_busy(proot: Path) -> None:
    import scripts.backfill_missing_lottery_times as backfill_cli

    with open(backfill_cli.__file__, encoding="utf-8") as handle:
        source_text = handle.read()
    assert "cli_profile_writer_lease" in source_text

    with profile_writer_lease(owner="test:busy"):
        with pytest.raises(SystemExit) as excinfo:
            backfill_cli.main()
    assert excinfo.value.code == 2


def test_writer_cli_rollback_refuses_when_busy(proot: Path) -> None:
    import scripts.rollback_ds_containers as rollback_cli

    with open(rollback_cli.__file__, encoding="utf-8") as handle:
        source_text = handle.read()
    assert "cli_profile_writer_lease" in source_text

    with profile_writer_lease(owner="test:busy"):
        with pytest.raises(SystemExit) as excinfo:
            rollback_cli.main()
    assert excinfo.value.code == 2


def test_readonly_cli_discover_watch_users_has_no_lease() -> None:
    import scripts.discover_watch_users as discover_cli

    with open(discover_cli.__file__, encoding="utf-8") as handle:
        source_text = handle.read()
    # 只读（相对 Profile）：写仓库级 config/watch_users.json，不获取 WriterLease
    assert "cli_profile_writer_lease" not in source_text
    assert "profile_writer_lease" not in source_text


# ---------- 3. WriterLease 锁文件有界 ----------


def test_lock_file_stays_bounded_after_many_cycles(tmp_path: Path) -> None:
    path = tmp_path / "binggo.writer.lock"
    for _ in range(300):
        lease = _ProfileWriterLease(path, owner="cycle")
        assert lease.acquire()
        lease.release()
    assert path.stat().st_size <= 16
    assert path.read_text(encoding="utf-8") == "binggo\n"


def test_lock_second_holder_busy_same_profile(tmp_path: Path) -> None:
    path = tmp_path / "binggo.writer.lock"
    first = _ProfileWriterLease(path, owner="a")
    assert first.acquire()
    second = _ProfileWriterLease(path, owner="b")
    assert second.acquire() is False  # 同 Profile 第二持有者 busy
    first.release()
    third = _ProfileWriterLease(path, owner="c")
    assert third.acquire()
    third.release()


def test_lock_different_profiles_independent(proot: Path) -> None:
    create_profile()  # account-2
    with profile_writer_lease(owner="test:a", profile_id="account-1"):
        with profile_writer_lease(owner="test:b", profile_id="account-2"):
            pass  # 不同 Profile 互不阻塞


# ---------- 2. .creating-* 孤儿清理 ----------


def _make_creating(proot: Path, name: str, *, age: int) -> Path:
    directory = proot / "profiles" / name
    directory.mkdir()
    (directory / "profile.json").write_text("{}", encoding="utf-8")
    old = time.time() - age
    os.utime(directory, (old, old))
    return directory


def test_creating_fresh_kept_stale_removed(proot: Path) -> None:
    fresh = _make_creating(proot, ".creating-account-9-fresh", age=10)
    stale = _make_creating(proot, ".creating-account-9-stale", age=2 * 3600)

    removed = sweep_stale_creating_profiles()

    assert fresh.exists()
    assert not stale.exists()
    assert stale.name in removed


def test_creating_sweep_keeps_normal_profiles_intact(proot: Path) -> None:
    _make_creating(proot, ".creating-account-9-old", age=2 * 3600)
    before = list_profiles()

    sweep_stale_creating_profiles()

    assert list_profiles() == before
    assert (proot / "profiles" / "account-1" / "profile.json").exists()


def test_creating_sweep_runs_inside_create_profile(proot: Path) -> None:
    stale = _make_creating(proot, ".creating-account-2-old", age=2 * 3600)
    created = create_profile()
    assert created["profile_id"] == "account-2"
    assert not stale.exists()


def test_creating_sweep_not_run_when_registry_busy(proot: Path) -> None:
    from src.profile_writer_lease import ProfileWriterLeaseBusy, profile_registry_lease

    stale = _make_creating(proot, ".creating-account-2-old", age=2 * 3600)
    with profile_registry_lease(owner="test:holder"):
        with pytest.raises(ProfileWriterLeaseBusy):
            create_profile()
    assert stale.exists()  # busy → sweep 未执行，孤儿保留


# ---------- 6. startup sweep ----------


def test_startup_onboarding_sweep_is_invoked_and_non_blocking(monkeypatch) -> None:
    import web.app as app_module

    def boom():
        raise RuntimeError("simulated sweep failure")

    monkeypatch.setattr("web.app.logger", type("L", (), {"exception": lambda *a, **k: None})())
    monkeypatch.setattr("src.login_onboarding.onboarding_sweep_expired", boom)
    app_module._startup_onboarding_sweep()  # 失败不抛错 = 不阻塞启动

    source = Path(app_module.__file__).read_text(encoding="utf-8")
    assert "_startup_onboarding_sweep()" in source


# ---------- 4. 展示口径 12 小时（V26: cleanup 2 次/天） ----------


def test_cleanup_display_interval_is_twelve_hours() -> None:
    from web.auto_config import CLEANUP_MAINTAIN_INTERVAL_HOURS

    assert CLEANUP_MAINTAIN_INTERVAL_HOURS == 12
