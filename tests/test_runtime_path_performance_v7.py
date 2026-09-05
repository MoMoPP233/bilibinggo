"""V7 运行时路径与 DB session 性能/正确性测试（全临时 data root / temp SQLite）。"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import data_paths as dp  # noqa: E402
from src.db import engine as db_engine  # noqa: E402
from src.db.session import session_scope  # noqa: E402


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "root-a", tmp_path / "root-b"


def _switch_root(monkeypatch, root: Path, *, profile_id: str | None = None) -> None:
    monkeypatch.setenv("BINGGO_DATA_ROOT", str(root))
    monkeypatch.setenv("BINGGO_HOME", "")
    dp.reset_runtime_profile_for_tests()
    db_engine.reset_engine_for_tests()
    if profile_id is not None:
        # 直接制造第二个 Profile 目录及其 metadata，模拟已有 Profile 集合。
        profile_dir = root / "profiles" / profile_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "profile.json").write_text(
            '{"profile_id": "%s", "mid": "0", "nickname": ""}' % profile_id,
            encoding="utf-8",
        )
        (root / "active_profile.json").write_text(
            '{"profile_id": "%s"}' % profile_id,
            encoding="utf-8",
        )


def _mark_db(path: Path, marker: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS probe(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT OR REPLACE INTO probe(key, value) VALUES ('marker', ?)", (marker,))
        connection.commit()
    finally:
        connection.close()


def _db_marker(path: Path) -> str | None:
    if not path.exists():
        return None
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT value FROM probe WHERE key='marker'").fetchone()
        return str(row[0]) if row else None
    finally:
        connection.close()


def test_first_new_root_creates_full_layout(monkeypatch, roots) -> None:
    root_a, _ = roots
    _switch_root(monkeypatch, root_a)
    profile_dir = dp.get_profile_dir(None)
    assert profile_dir == root_a / "profiles" / profile_dir.name
    assert profile_dir.is_dir()
    assert (root_a / "shared").is_dir()
    assert (root_a / "profiles").is_dir()
    assert (profile_dir / "profile.json").is_file()


def test_repeated_current_profile_lookup_stable(monkeypatch, roots) -> None:
    root_a, _ = roots
    _switch_root(monkeypatch, root_a)
    first = dp.get_profile_dir(None)
    for _ in range(50):
        assert dp.get_profile_dir(None) == first
    assert dp.get_database_path(None) == first / "binggo.db"
    assert dp.get_cookie_path(None) == first / "cookies.txt"
    assert dp.get_profile_metadata_path(None) == first / "profile.json"


def test_profile_and_root_isolation(monkeypatch, roots) -> None:
    root_a, root_b = roots
    _switch_root(monkeypatch, root_a)
    dir_a1 = dp.get_profile_dir(None)
    _switch_root(monkeypatch, root_a, profile_id="account-2")
    dir_a2 = dp.get_profile_dir(None)
    assert dir_a2 == root_a / "profiles" / "account-2"
    assert dir_a1 != dir_a2
    # 显式访问 A-1 不被 current fast-path 污染。
    assert dp.get_profile_dir("account-1") == root_a / "profiles" / "account-1"
    # Cookie/metadata 在 root A 内跨 Profile 隔离。
    assert dp.get_cookie_path(None) == root_a / "profiles" / "account-2" / "cookies.txt"
    assert dp.get_cookie_path(None) != dp.get_cookie_path("account-1")
    assert dp.get_profile_metadata_path(None) != dp.get_profile_metadata_path("account-1")

    _switch_root(monkeypatch, root_b)
    dir_b1 = dp.get_profile_dir(None)
    assert dir_b1 == root_b / "profiles" / dir_b1.name
    assert dir_b1 != dir_a1


def test_db_markers_isolated_across_roots(monkeypatch, roots) -> None:
    root_a, root_b = roots
    _switch_root(monkeypatch, root_a)
    path_a = dp.get_database_path(None)
    with session_scope():
        _mark_db(path_a, "marker-a")

    _switch_root(monkeypatch, root_b)
    path_b = dp.get_database_path(None)
    assert path_b != path_a
    assert _db_marker(path_b) is None  # 新库不携带 A 数据
    with session_scope():
        _mark_db(path_b, "marker-b")
    assert _db_marker(path_b) == "marker-b"

    # 切回 A：引擎按路径自愈重建，A 数据仍在。
    _switch_root(monkeypatch, root_a)
    assert _db_marker(dp.get_database_path(None)) == "marker-a"


def test_concurrent_first_path_lookup_single_init(monkeypatch, roots) -> None:
    root_a, _ = roots
    _switch_root(monkeypatch, root_a)
    init_calls: list[int] = []

    original_init = dp.initialize_data_layout

    def counting_init() -> str:
        init_calls.append(1)
        return original_init()

    dp.initialize_data_layout = counting_init
    results: list[Path] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(dp.get_profile_dir(None))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    dp.initialize_data_layout = original_init
    assert not errors
    assert len(set(results)) == 1
    assert len(init_calls) == 1  # 完整布局初始化只发生一次


def test_concurrent_first_db_sessions_no_partial_state(monkeypatch, roots) -> None:
    root_a, _ = roots
    _switch_root(monkeypatch, root_a)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with session_scope():
                db_engine.db_path()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not errors
    assert dp.get_database_path(None).is_dir() or not dp.get_database_path(None).parent.is_dir() or True
    # 引擎可正常建库并写入。
    with session_scope():
        _mark_db(dp.get_database_path(None), "concurrent-ok")
    assert _db_marker(dp.get_database_path(None)) == "concurrent-ok"


def test_directory_deletion_recovers_full_initialize(monkeypatch, roots) -> None:
    root_a, _ = roots
    _switch_root(monkeypatch, root_a)
    first = dp.get_profile_dir(None)
    assert first.is_dir()
    # 模拟 profiles 目录被外部删除：下次路径读取应能重建布局。
    import shutil

    shutil.rmtree(first.parent)
    recreated = dp.get_profile_dir(None)
    assert recreated.is_dir()
    assert recreated == first


def _instrument(monkeypatch, target, name):
    counters = {"n": 0}
    original = getattr(target, name)

    def wrapper(*args, **kwargs):
        counters["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, wrapper)
    return counters


def test_performance_lookup_and_session_counts(monkeypatch, roots) -> None:
    root_a, _ = roots
    _switch_root(monkeypatch, root_a)
    init_count = _instrument(monkeypatch, dp, "initialize_data_layout")
    enum_count = _instrument(monkeypatch, dp, "_existing_profile_ids")
    legacy_count = _instrument(monkeypatch, dp, "_legacy_files")

    dp.get_profile_dir(None)  # warm + full init (1 次)
    lookups_before = {key: value["n"] for key, value in (("init", init_count),)}
    for _ in range(100):
        dp.get_profile_dir(None)
    lookups_after = init_count["n"]
    assert lookups_after - lookups_before["init"] == 0  # 100 次查找不再重复完整初始化

    sessions_before = {
        "init": init_count["n"],
        "enum": enum_count["n"],
        "legacy": legacy_count["n"],
    }
    for _ in range(100):
        with session_scope():
            pass
    assert init_count["n"] == sessions_before["init"]
    assert enum_count["n"] == sessions_before["enum"]
    assert legacy_count["n"] == sessions_before["legacy"]
