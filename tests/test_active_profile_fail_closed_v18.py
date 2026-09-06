"""active_profile.json 指针 Fail-Closed（V18）。

真实路径层测试（tmp_path + 假 Profile 目录，无真实用户数据、无网络）：
A  missing → 保持 V17 首次初始化语义（不含误报 corruption）
B  valid pointer → 正确选中/绑定
C  corrupt JSON → 抛 ActiveProfileStateError，绝不回退 account-1，文件不改
D  empty file → Fail-Closed
E  根类型/schema 非法（[] null 字符串 {} profile_id 非字符串）→ Fail-Closed
F  PermissionError/OSError → Fail-Closed
G  dangling pointer → Fail-Closed（不改写）
H  多 Profile 排序无关：损坏指针绝不产生 account-1
I  真实 set_active_profile 恢复路径：原子写回合法指针后正常绑定
J  （项目无 supervisor/runtime 显式 Profile 来源 → 无此测试，见报告）
K  V10 共享原子锁路径回归（并发 set_active_profile 不损坏、无残留 temp）
L  普通读取/initialize 后损坏文件内容逐字节不变（无自动 heal）
M  Fail-Closed 发生在打开任何 DB/Cookie/创建 profile 副作用之前
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from src import data_paths
from src import profile_manager
from src.data_paths import (
    ActiveProfileStateError,
    get_runtime_profile_id,
    get_selected_profile_id,
    initialize_data_layout,
    read_active_profile_pointer_state,
)


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "binggo-data"
    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(root))
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    monkeypatch.setenv(data_paths.LEGACY_DATA_ROOT_ENV, str(root))
    monkeypatch.delenv("BINGGO_PORTABLE", raising=False)
    monkeypatch.setenv("BINGGO_DATA_ROOT_LOCATOR", str(tmp_path / "locator.json"))
    data_paths.reset_runtime_profile_for_tests()
    yield root
    data_paths.reset_runtime_profile_for_tests()


def _make_profiles(root: Path, *ids: str) -> None:
    for profile_id in ids:
        profile_dir = root / "profiles" / profile_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "profile.json").write_text(
            json.dumps({"profile_id": profile_id, "mid": "", "nickname": ""}),
            encoding="utf-8",
        )


def _write_pointer(root: Path, content: str) -> None:
    path = root / "active_profile.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------- A：missing ----------


def test_missing_pointer_first_init_keeps_baseline(data_root: Path) -> None:
    assert initialize_data_layout() == "account-1"
    assert get_selected_profile_id() == "account-1"
    assert get_runtime_profile_id() == "account-1"
    assert json.loads((data_root / "active_profile.json").read_text(encoding="utf-8")) == {
        "profile_id": "account-1"
    }


def test_missing_pointer_with_existing_profiles_keeps_baseline_selection(
    data_root: Path,
) -> None:
    """指针缺失（从未产生选择）且已有 Profile：沿用 V17 基线（首个 Profile），
    不是 corruption 误报。"""
    _make_profiles(data_root, "account-1", "account-2")
    assert get_selected_profile_id() == "account-1"
    assert read_active_profile_pointer_state()["state_status"] == "missing"


# ---------- B：valid ----------


def test_valid_pointer_selects_pointed_profile(data_root: Path) -> None:
    _make_profiles(data_root, "account-1", "account-2")
    _write_pointer(data_root, json.dumps({"profile_id": "account-2"}))
    view = read_active_profile_pointer_state()
    assert view["state_status"] == "ok"
    assert view["profile_id"] == "account-2"
    assert get_selected_profile_id() == "account-2"
    assert get_runtime_profile_id() == "account-2"
    assert get_runtime_profile_id() == "account-2"  # 冻结


# ---------- C/D/E/F/G：损坏 → Fail-Closed ----------


@pytest.mark.parametrize("content", [
    "{",
    '{"profile_id": "account-2",',
    "",
    "   ",
    "[]",
    "null",
    '"account-2"',
    "123",
    "{}",
    '{"profile_id": 2}',
    '{"profile_id": ""}',
    '{"profile_id": "account-0"}',
    '{"profile_id": "other"}',
])
def test_invalid_pointer_never_falls_back_to_first_profile(data_root: Path, content: str) -> None:
    _make_profiles(data_root, "account-1", "account-2")
    _write_pointer(data_root, content)
    view = read_active_profile_pointer_state()
    assert view["state_status"] in ("invalid", "dangling")
    with pytest.raises(ActiveProfileStateError):
        get_selected_profile_id()
    with pytest.raises(ActiveProfileStateError):
        get_runtime_profile_id()
    with pytest.raises(ActiveProfileStateError):
        initialize_data_layout()
    # 原始内容未被改写
    assert (data_root / "active_profile.json").read_text(encoding="utf-8") == content


class _UnreadablePointer:
    def __init__(self, exc: type[OSError]) -> None:
        self._exc = exc

    def read_text(self, *args, **kwargs):
        raise self._exc("read denied")


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_unreadable_pointer_is_fail_closed(
    data_root: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[OSError],
) -> None:
    _make_profiles(data_root, "account-1", "account-2")
    monkeypatch.setattr(data_paths, "get_active_profile_path", lambda: _UnreadablePointer(error_type))
    assert read_active_profile_pointer_state()["state_status"] == "unreadable"
    with pytest.raises(ActiveProfileStateError):
        get_selected_profile_id()
    with pytest.raises(ActiveProfileStateError):
        get_runtime_profile_id()


def test_dangling_pointer_is_fail_closed_and_not_rewritten(data_root: Path) -> None:
    _make_profiles(data_root, "account-1", "account-2")
    content = json.dumps({"profile_id": "account-9"})
    _write_pointer(data_root, content)
    view = read_active_profile_pointer_state()
    assert view["state_status"] == "dangling"
    assert view["profile_id"] == "account-9"
    with pytest.raises(ActiveProfileStateError):
        get_selected_profile_id()
    assert (data_root / "active_profile.json").read_text(encoding="utf-8") == content


def test_sort_order_never_produces_first_profile_when_corrupt(data_root: Path) -> None:
    """无论排序如何（account-1..10），损坏指针都不会回退成 sorted()[0]。"""
    _make_profiles(data_root, "account-1", "account-2", "account-10", "account-3")
    _write_pointer(data_root, "{broken")
    with pytest.raises(ActiveProfileStateError):
        get_selected_profile_id()
    assert read_active_profile_pointer_state()["state_status"] == "invalid"


# ---------- I：真实恢复路径 ----------


def test_set_active_profile_recovers_after_corruption(data_root: Path) -> None:
    _make_profiles(data_root, "account-1", "account-2")
    _write_pointer(data_root, "{broken")
    with pytest.raises(ActiveProfileStateError):
        get_selected_profile_id()

    # 用户通过现有 Profile 选择动作明确选择 account-2（真实入口，非手写 JSON）。
    metadata = profile_manager.set_active_profile("account-2")
    assert metadata["profile_id"] == "account-2"
    assert read_active_profile_pointer_state()["state_status"] == "ok"
    assert get_selected_profile_id() == "account-2"

    # 重启语义：新进程（reset runtime 后）正常绑定 account-2。
    data_paths.reset_runtime_profile_for_tests()
    assert initialize_data_layout() == "account-2"
    assert get_runtime_profile_id() == "account-2"


# ---------- K：V10 共享原子锁回归 ----------


def test_concurrent_set_active_profile_stays_atomic(data_root: Path) -> None:
    _make_profiles(data_root, "account-1", "account-2", "account-3")
    errors: list[Exception] = []

    def worker(profile_id: str) -> None:
        try:
            for _ in range(30):
                profile_manager.set_active_profile(profile_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(pid,))
        for pid in ("account-1", "account-2", "account-3")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    final = json.loads((data_root / "active_profile.json").read_text(encoding="utf-8"))
    assert final["profile_id"] in {"account-1", "account-2", "account-3"}
    leftovers = [
        name for name in os.listdir(data_root)
        if name.startswith(".active_profile.json.")
    ]
    assert leftovers == []


# ---------- L：无自动 heal ----------


def test_pointer_bytes_never_auto_healed_after_reads_and_init(data_root: Path) -> None:
    _make_profiles(data_root, "account-1", "account-2")
    corrupt = "{corrupt-pointer"
    _write_pointer(data_root, corrupt)
    for _ in range(2):
        with pytest.raises(ActiveProfileStateError):
            get_selected_profile_id()
        with pytest.raises(ActiveProfileStateError):
            initialize_data_layout()
        read_active_profile_pointer_state()
    assert (data_root / "active_profile.json").read_text(encoding="utf-8") == corrupt


# ---------- M：Fail-Closed 先于任何 DB/Cookie/副作用 ----------


def test_fail_closed_happens_before_any_profile_side_effect(data_root: Path) -> None:
    _make_profiles(data_root, "account-1", "account-2")
    _write_pointer(data_root, "{broken")
    before = {
        rel: (path.read_bytes() if path.is_file() else None)
        for rel, path in _walk(data_root / "profiles")
    }
    for _ in range(2):
        with pytest.raises(ActiveProfileStateError):
            initialize_data_layout()
        with pytest.raises(ActiveProfileStateError):
            get_runtime_profile_id()
    after = {
        rel: (path.read_bytes() if path.is_file() else None)
        for rel, path in _walk(data_root / "profiles")
    }
    # 没有任何 Profile 目录发生写入（无 binggo.db 创建、无 cookie 读取落盘、无 metadata 改写）。
    assert after == before
    # 错误 Profile 的 DB 从未被创建
    assert not (data_root / "profiles" / "account-1" / "binggo.db").exists()
    assert not (data_root / "profiles" / "account-2" / "binggo.db").exists()


def _walk(directory: Path):
    for path in sorted(directory.rglob("*")):
        yield path.relative_to(directory), path
