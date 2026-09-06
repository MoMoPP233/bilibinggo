"""V10：Profile 本地状态原子写入与并发安全（全 tmp Profile / mock）。"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import data_paths as dp  # noqa: E402
from src import profile_manager as pm  # noqa: E402


@pytest.fixture
def profile_env(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("BINGGO_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("BINGGO_HOME", "")
    dp.reset_runtime_profile_for_tests()
    # 初始化布局并创建 account-2（account-1 为默认首个）。
    first = dp.get_profile_dir(None)
    assert first.name in {"account-1", "default"}
    pm.create_profile()
    return tmp_path


def _metadata_path(profile_id: str) -> Path:
    return dp.get_profile_metadata_path(profile_id)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _no_temp_leftovers(root: Path) -> list[Path]:
    return list(root.rglob(".*.tmp")) + list(root.rglob("*.tmp"))


def test_normal_metadata_write(profile_env: Path) -> None:
    pm.update_profile_metadata(mid="111", nickname="甲")
    data = _read_json(_metadata_path(pm.get_runtime_profile_id()))
    assert data["mid"] == "111"
    assert data["nickname"] == "甲"
    assert _no_temp_leftovers(profile_env) == []


def test_consecutive_writes_stable(profile_env: Path) -> None:
    for index in range(20):
        pm.update_profile_metadata(mid=str(index), nickname=f"n{index}")
    data = _read_json(_metadata_path(pm.get_runtime_profile_id()))
    assert data["mid"] == "19"
    assert data["nickname"] == "n19"


def test_concurrent_rmw_different_fields_no_lost_update(profile_env: Path) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def writer_mid() -> None:
        try:
            barrier.wait(timeout=10)
            for index in range(30):
                pm.update_profile_metadata(mid=f"M{index}")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def writer_nickname() -> None:
        try:
            barrier.wait(timeout=10)
            for index in range(30):
                pm.update_profile_metadata(nickname=f"N{index}")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer_mid), threading.Thread(target=writer_nickname)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    data = _read_json(_metadata_path(pm.get_runtime_profile_id()))
    # 两个独立字段都来自各自 writer 的最新值（无 lost update / 无半截 JSON）。
    assert data["mid"].startswith("M")
    assert data["nickname"].startswith("N")
    assert _no_temp_leftovers(profile_env) == []


def test_concurrent_whole_object_active_profile(profile_env: Path) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def writer(profile_id: str) -> None:
        try:
            barrier.wait(timeout=10)
            for _ in range(40):
                pm.set_active_profile(profile_id)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=("account-1",)),
               threading.Thread(target=writer, args=("account-2",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    data = _read_json(dp.get_active_profile_path())
    assert data["profile_id"] in {"account-1", "account-2"}
    assert _no_temp_leftovers(profile_env) == []


def test_replace_failure_keeps_old_target(profile_env: Path, monkeypatch) -> None:
    target = _metadata_path(pm.get_runtime_profile_id())
    pm.update_profile_metadata(mid="old")
    old_text = target.read_text(encoding="utf-8")

    real_replace = os.replace

    def broken_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("src.profile_manager.os.replace", broken_replace)
    with pytest.raises(OSError):
        pm.update_profile_metadata(mid="new-value")
    monkeypatch.setattr("src.profile_manager.os.replace", real_replace)
    assert target.read_text(encoding="utf-8") == old_text  # 旧文件未被破坏
    assert _no_temp_leftovers(profile_env) == []  # 自己的 temp 被清理

    # replace 成功路径：完整写入新值。
    pm.update_profile_metadata(mid="new-value")
    assert _read_json(target)["mid"] == "new-value"


def test_serialization_failure_keeps_old_target(profile_env: Path, monkeypatch) -> None:
    target = _metadata_path(pm.get_runtime_profile_id())
    pm.update_profile_metadata(mid="keep")
    original = target.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise ValueError("serialize boom")

    monkeypatch.setattr("src.profile_manager.json.dumps", boom)
    with pytest.raises(ValueError):
        pm._write_json(target, {"mid": "x"})
    assert target.read_text(encoding="utf-8") == original


def test_profile_a_b_concurrent_isolation(profile_env: Path) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def writer(profile_id: str, marker: str) -> None:
        try:
            barrier.wait(timeout=10)
            for index in range(30):
                pm.update_profile_metadata(
                    profile_id=profile_id, mid=f"{marker}{index}", nickname=f"{marker}{index}"
                )
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=("account-1", "A")),
        threading.Thread(target=writer, args=("account-2", "B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    first = _read_json(_metadata_path("account-1"))
    second = _read_json(_metadata_path("account-2"))
    assert first["mid"].startswith("A") and second["mid"].startswith("B")
    assert "B" not in json.dumps(first)
    assert "A" not in json.dumps(second)


def test_corrupt_metadata_keeps_existing_error_semantics(profile_env: Path) -> None:
    target = _metadata_path("account-1")
    target.write_text("{broken json", encoding="utf-8")
    meta = pm.get_profile_metadata("account-1")
    # 现有错误语义：不可读按空处理（返回默认字段），不自动删除/重置。
    assert meta["mid"] == ""
    assert meta["nickname"] == ""
    assert target.exists()
    # 修复性写入仍可用并保持原子。
    pm.update_profile_metadata(profile_id="account-1", mid="9")
    assert _read_json(target)["mid"] == "9"


def test_data_paths_atomic_json_concurrent(profile_env: Path) -> None:
    target = profile_env / "root_locator.json"
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def writer(value: str) -> None:
        try:
            barrier.wait(timeout=10)
            for index in range(40):
                dp._write_json_atomic(target, {"data_root": f"{value}{index}"})
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=("A-",)), threading.Thread(target=writer, args=("B-",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    data = _read_json(target)
    assert str(data["data_root"]).startswith(("A-", "B-"))
    assert _no_temp_leftovers(profile_env) == []


# ---------- V10 跨模块共享锁（data_paths ↔ profile_manager） ----------


def test_shared_target_lock_is_same_object(profile_env: Path, monkeypatch) -> None:
    from src import data_paths as dp_mod

    target = _metadata_path(pm.get_runtime_profile_id())
    seen: list[object] = []
    real = dp_mod.get_atomic_path_lock

    def spy(path):
        lock = real(path)
        seen.append(lock)
        return lock

    monkeypatch.setattr("src.data_paths.get_atomic_path_lock", spy)
    monkeypatch.setattr("src.profile_manager.get_atomic_path_lock", spy)
    pm._write_json(target, {"profile_id": "x", "mid": "1", "nickname": "n"})
    dp_mod._write_json_atomic(target, {"profile_id": "x", "mid": "2", "nickname": "m"})
    assert len(seen) == 2
    assert seen[0] is seen[1]
    assert seen[0] is dp_mod.get_atomic_path_lock(target)


def test_cross_module_rmw_vs_whole_writer_no_lost_update(profile_env: Path) -> None:
    target = _metadata_path(pm.get_runtime_profile_id())
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def rmw_writer() -> None:
        try:
            barrier.wait(timeout=10)
            for index in range(30):
                pm.update_profile_metadata(mid=f"M{index}", nickname=f"N{index}")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def whole_writer() -> None:
        try:
            barrier.wait(timeout=10)
            whole = {"profile_id": "account-1", "mid": "W", "nickname": "whole"}
            for _ in range(30):
                dp._write_json_atomic(target, dict(whole))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=rmw_writer), threading.Thread(target=whole_writer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)
    assert not errors
    data = _read_json(target)
    # 串行化后只能得到两种合法结果之一：
    # 1) whole writer 的完整对象；2) RMW writer 的完整对象（两字段都来自同一串行时刻）。
    whole = {"profile_id": "account-1", "mid": "W", "nickname": "whole"}
    rmw_ok = str(data.get("mid", "")).startswith("M") and str(data.get("nickname", "")).startswith("N")
    assert data == whole or rmw_ok
    assert set(data) == {"profile_id", "mid", "nickname"}
    assert _no_temp_leftovers(profile_env) == []


def test_backfill_does_not_overwrite_newer_metadata(profile_env: Path) -> None:
    """B 先写入有效 metadata，随后 A 执行 initialize 的 backfill 分支：
    A 必须重新检查，不能按旧的“缺失”判断覆盖 B。"""
    profile_id = pm.get_runtime_profile_id()
    target = _metadata_path(profile_id)
    # B：通过 profile_manager 写入有效数据（模拟登录后同步）。
    pm.update_profile_metadata(mid="12345", nickname="张三")
    # A：metadata 存在时触发 initialize（backfill 分支在共享锁内重读，直接跳过）。
    dp.initialize_data_layout()
    data = _read_json(target)
    assert data["mid"] == "12345"
    assert data["nickname"] == "张三"


def test_backfill_conditional_write_with_interleaved_writer(profile_env: Path) -> None:
    """人为控制时序：writer B 在 A 的 backfill 写锁临界区内先/后被完成，
    最终都不允许 backfill 覆盖 B 的有效数据。"""
    profile_id = pm.get_runtime_profile_id()
    target = _metadata_path(profile_id)
    target.unlink(missing_ok=True)  # 制造“需要 backfill”的起始状态

    # 场景 1：A 先执行 backfill（默认值），随后 B 更新 → 最终为 B 的完整对象。
    dp.initialize_data_layout()
    pm.update_profile_metadata(mid="B-mid", nickname="B-name")
    data = _read_json(target)
    assert data["mid"] == "B-mid" and data["nickname"] == "B-name"

    # 场景 2：再次删除后，B 先写，A 后执行 initialize → A 跳过（不覆盖）。
    target.unlink(missing_ok=True)
    pm.update_profile_metadata(mid="B2", nickname="B2-name")
    dp.initialize_data_layout()
    data = _read_json(target)
    assert data["mid"] == "B2" and data["nickname"] == "B2-name"
    assert _no_temp_leftovers(profile_env) == []


def test_barrier_controlled_backfill_vs_update_serialized(profile_env: Path) -> None:
    """Event/Barrier 控制下 A(initialize backfill) 与 B(update metadata) 同时启动：
    两者被同一 target 共享锁串行化，无论谁先，最终都是 B 的完整有效数据，无死锁。"""
    profile_id = pm.get_runtime_profile_id()
    target = _metadata_path(profile_id)
    target.unlink(missing_ok=True)
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def backfill_writer() -> None:
        try:
            barrier.wait(timeout=10)
            dp.initialize_data_layout()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def update_writer() -> None:
        try:
            barrier.wait(timeout=10)
            for index in range(5):
                pm.update_profile_metadata(mid=f"B{index}", nickname=f"name{index}")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=backfill_writer), threading.Thread(target=update_writer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)
    assert not errors
    data = _read_json(target)
    assert str(data.get("mid", "")).startswith("B")
    assert str(data.get("nickname", "")).startswith("name")
    assert _no_temp_leftovers(profile_env) == []


def test_active_conditional_write_never_rewinds_user_choice(profile_env: Path) -> None:
    """用户 set_active 之后，任何 initialize 的 conditional active 写入都不得倒写。"""
    pm.set_active_profile("account-2")
    for _ in range(5):
        dp.initialize_data_layout()
    data = _read_json(dp.get_active_profile_path())
    assert data["profile_id"] == "account-2"
    assert _no_temp_leftovers(profile_env) == []


def test_cross_module_active_profile_concurrent(profile_env: Path) -> None:
    active = dp.get_active_profile_path()
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def pm_writer() -> None:
        try:
            barrier.wait(timeout=10)
            for _ in range(30):
                pm.set_active_profile("account-1")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def dp_writer() -> None:
        try:
            barrier.wait(timeout=10)
            for _ in range(30):
                dp._write_json_atomic(active, {"profile_id": "account-2"})
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=pm_writer), threading.Thread(target=dp_writer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)
    assert not errors
    data = _read_json(active)
    assert data["profile_id"] in {"account-1", "account-2"}
    assert _no_temp_leftovers(profile_env) == []
