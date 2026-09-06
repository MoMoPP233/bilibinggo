"""V12：关注用户普通 CRUD 增量写（targeted INSERT/UPDATE/DELETE），不整表 rewrite。"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import event

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import watch_users as wu  # noqa: E402
from src.db import engine as db_engine  # noqa: E402


class _NoopClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _seed_users(count: int, *, prefix: int = 1000) -> None:
    users = [{"mid": prefix + i, "name": f"u{i}"} for i in range(count)]
    wu._write_payload({"users": users, "updated_at": 1, "seeded_from": None})


def _ids(users: list[wu.WatchUser]) -> list[int]:
    return [user.mid for user in users]


def _count_statements() -> list[int]:
    counter: list[int] = []

    def on_cursor(*args, **kwargs):
        counter.append(1)

    event.listen(db_engine.get_engine(), "before_cursor_execute", on_cursor)
    return counter


def test_empty_and_single_add(isolated_home: Path, monkeypatch) -> None:
    calls: list[int] = []

    def fake_resolve(client, mid: int) -> str:
        calls.append(mid)
        return f"user-{mid}"

    with patch.object(wu, "BilibiliClient", lambda: _NoopClient()), patch.object(
        wu, "resolve_watch_user_name", fake_resolve
    ):
        wu.add_watch_user(mid=9001)
    users = wu.list_watch_users()
    assert len(users) == 1
    assert users[0].mid == 9001
    assert users[0].name == "user-9001"
    assert calls == [9001]  # 远程解析仍每次 add 恰好一次


def test_duplicate_add_replaces_single_row(isolated_home: Path, monkeypatch) -> None:
    def fake_resolve(_client, mid: int) -> str:
        return f"name-{mid}"

    with patch.object(wu, "BilibiliClient", lambda: _NoopClient()), patch.object(
        wu, "resolve_watch_user_name", fake_resolve
    ):
        wu.add_watch_user(mid=9100)
        wu.add_watch_user(mid=9100)
    users = wu.list_watch_users()
    assert len(users) == 1
    assert users[0].name == "name-9100"


def test_delete_existing_and_missing(isolated_home: Path) -> None:
    _seed_users(5)
    assert wu.remove_watch_user(mid=1001) is True
    assert [user.mid for user in wu.list_watch_users()] == [1000, 1002, 1003, 1004]
    assert wu.remove_watch_user(mid=1001) is False
    assert wu.remove_watch_user(mid=999999) is False


def _add_with_fake(monkeypatch, mid: int) -> None:
    with patch.object(wu, "BilibiliClient", lambda: _NoopClient()), patch.object(
        wu, "resolve_watch_user_name", lambda _c, m: f"name-{m}"
    ):
        wu.add_watch_user(mid=mid)


@pytest.mark.parametrize("size", [100, 1000, 5000])
def test_add_one_does_not_rewrite_existing(
    isolated_home: Path, monkeypatch, size: int,
) -> None:
    _seed_users(size)
    guard: list[str] = []

    def forbid_full_write(*args, **kwargs):
        guard.append("full-rewrite")

    monkeypatch.setattr(wu, "_write_payload", forbid_full_write)
    statements = _count_statements()
    _add_with_fake(monkeypatch, 900000)
    # 语句数恒定小常数（target get + insert + meta + commit），与 N 无关。
    assert len(statements) < 12, f"语句数不应随存量增长: {len(statements)}"
    assert guard == []  # 未发生任何整表 rewrite
    assert len(wu.list_watch_users()) == size + 1


@pytest.mark.parametrize("size", [100, 1000, 5000])
def test_delete_one_does_not_rewrite_existing(
    isolated_home: Path, monkeypatch, size: int,
) -> None:
    _seed_users(size)
    guard: list[str] = []
    monkeypatch.setattr(wu, "_write_payload", lambda *a, **k: guard.append("full"))
    statements = _count_statements()
    assert wu.remove_watch_user(mid=1000 + size // 2) is True
    assert len(statements) < 12
    assert guard == []
    remaining = wu.list_watch_users()
    assert len(remaining) == size - 1
    # 剩余列表顺序仍按 mid 升序，字段精确相等。
    assert _ids(remaining) == sorted(_ids(remaining))


def test_residual_rows_exact_equality(isolated_home: Path) -> None:
    _seed_users(20)
    wu.remove_watch_user(mid=1003)
    wu.remove_watch_user(mid=1010)
    expected = [u for u in range(1000, 1020) if u not in (1003, 1010)]
    remaining = wu.list_watch_users()
    assert _ids(remaining) == expected
    for index, user in enumerate(remaining):
        assert user.name == f"u{user.mid - 1000}"


def test_remote_resolution_failure_writes_nothing(isolated_home: Path) -> None:
    _seed_users(3)

    def boom(_client, mid: int) -> str:
        raise RuntimeError("网络失败")

    with patch.object(wu, "BilibiliClient", lambda: _NoopClient()), patch.object(
        wu, "resolve_watch_user_name", boom
    ):
        with pytest.raises(RuntimeError):
            wu.add_watch_user(mid=5555)
    assert [u.mid for u in wu.list_watch_users()] == [1000, 1001, 1002]  # 无半条


def test_profile_a_b_isolation(tmp_path: Path, monkeypatch) -> None:
    from src import data_paths as _dp
    from src.db.schema import init_db

    def switch(root: Path) -> None:
        monkeypatch.setenv("BINGGO_DATA_ROOT", str(root))
        _dp.reset_runtime_profile_for_tests()
        db_engine.reset_engine_for_tests()
        init_db()

    root_a = tmp_path / "pa"
    root_b = tmp_path / "pb"
    switch(root_a)
    _add_with_fake(monkeypatch, 100)
    switch(root_b)
    _add_with_fake(monkeypatch, 200)
    assert [u.mid for u in wu.list_watch_users()] == [200]
    switch(root_a)
    assert [u.mid for u in wu.list_watch_users()] == [100]


def test_concurrent_duplicate_add_single_row(isolated_home: Path) -> None:
    # 主线程先完成一次布局初始化，避免 worker 内再次并发执行第一次迁移创建 Profile 目录。
    from src.data_paths import initialize_data_layout

    initialize_data_layout()
    from src.db.session import session_scope as _scope

    for _ in range(6):
        with _scope():
            pass
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(tag: str) -> None:
        try:
            barrier.wait(timeout=10)
            with patch.object(wu, "BilibiliClient", lambda: _NoopClient()), patch.object(
                wu, "resolve_watch_user_name", lambda _c, m: f"{tag}-{m}"
            ):
                wu.add_watch_user(mid=424242)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=("t1",)), threading.Thread(target=worker, args=("t2",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors
    users = wu.list_watch_users()
    assert len(users) == 1
    assert users[0].name in {"t1-424242", "t2-424242"}


def test_meta_updated_at_touched_on_crud(isolated_home: Path, monkeypatch) -> None:
    from src.db.models import WatchUsersMetaRow
    from src.db.session import session_scope

    _add_with_fake(monkeypatch, 777)
    with session_scope() as session:
        meta = session.get(WatchUsersMetaRow, 1)
        assert meta is not None and meta.updated_at > 0
