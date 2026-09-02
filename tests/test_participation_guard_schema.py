from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import text

from src import data_paths, participation_guard, profile_manager
from src.db import schema
from src.db.engine import db_path, get_engine, reset_engine_for_tests
from src.db.models import ParticipationGuardRow
from src.db.session import session_scope


def _v2_database() -> None:
    with get_engine().begin() as conn:
        conn.exec_driver_sql("DROP TABLE participation_guard")
        conn.exec_driver_sql("DROP TABLE repost_history")
        conn.exec_driver_sql("DROP TABLE repost_sync_checkpoint")
        conn.exec_driver_sql("DROP TABLE repost_assessment")
        conn.exec_driver_sql("UPDATE schema_meta SET version=2 WHERE id=1")
        conn.exec_driver_sql("INSERT INTO participations(uid,dynamic_id,user_status,updated_at,source) VALUES ('u','d','已参加',123,'participate')")
        conn.exec_driver_sql("INSERT INTO activities(dynamic_id,payload_json,updated_at,status_classified,skipped) VALUES ('d','{}',234,0,0)")
        conn.exec_driver_sql("INSERT INTO source_checkpoints(source_id,container_url) VALUES ('DS-test','unchanged')")


def _snapshot() -> dict:
    with get_engine().connect() as conn:
        return {
            table: [tuple(row) for row in conn.exec_driver_sql(f"SELECT * FROM {table}")]
            for table in ("participations", "activities", "source_checkpoints")
        }


def test_v2_to_v10_preserves_existing_business_data(isolated_home) -> None:
    _v2_database()
    before = _snapshot()
    schema.init_db()
    schema.init_db()
    assert _snapshot() == before
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10
        assert conn.exec_driver_sql("SELECT count(*) FROM participation_guard").scalar_one() == 0
        assert conn.exec_driver_sql("SELECT count(*) FROM repost_history").scalar_one() == 0
        assert conn.exec_driver_sql("SELECT count(*) FROM repost_assessment").scalar_one() == 0


def test_existing_guard_with_v2_meta_keeps_all_rows(isolated_home) -> None:
    with session_scope() as session:
        session.add(ParticipationGuardRow(uid="u", dynamic_id="d", repost_status="pending", updated_at=123))
        session.execute(text("UPDATE schema_meta SET version=2"))
    schema.init_db()
    schema.init_db()
    with session_scope() as session:
        row = session.get(ParticipationGuardRow, ("u", "d"))
        assert row.repost_status == "pending" and row.updated_at == 123


def test_v1_jobs_migration_continues_through_v10(isolated_home) -> None:
    _v2_database()
    with get_engine().begin() as conn:
        conn.exec_driver_sql("DROP TABLE jobs")
        conn.exec_driver_sql("CREATE TABLE jobs (id INTEGER PRIMARY KEY, action TEXT, state TEXT, created_at INTEGER, started_at INTEGER, finished_at INTEGER)")
        conn.exec_driver_sql("INSERT INTO jobs(id,action,state,created_at) VALUES (1,'refresh_all','success',123)")
        conn.exec_driver_sql("UPDATE schema_meta SET version=1")
    schema.init_db()
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT action,state,created_at FROM jobs WHERE id=1").one() == ("refresh_all", "success", 123)
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(jobs)")}
        assert {name for name, _ in schema._JOB_V2_COLUMNS} <= columns
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10


def test_migration_failure_rolls_back_table_and_version(isolated_home, monkeypatch) -> None:
    _v2_database()
    before = _snapshot()
    migrate = schema.migrate_v2_to_v3

    def interrupted(session):
        migrate(session)
        raise RuntimeError("interrupted migration")

    monkeypatch.setitem(schema._MIGRATIONS, 2, interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        schema.init_db()
    assert _snapshot() == before
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 2
        assert conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE name='participation_guard'").first() is None
    monkeypatch.setitem(schema._MIGRATIONS, 2, migrate)
    schema.init_db()
    schema.init_db()
    assert _snapshot() == before
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10
        assert conn.exec_driver_sql("SELECT count(*) FROM participation_guard").scalar_one() == 0


def test_future_version_rejected_before_any_create_all(isolated_home, monkeypatch) -> None:
    _v2_database()
    with get_engine().begin() as conn:
        conn.exec_driver_sql("UPDATE schema_meta SET version=11")
    before = _snapshot()

    def forbidden(*args, **kwargs):
        pytest.fail("future schema must be rejected before create_all")

    monkeypatch.setattr(schema.SQLModel.metadata, "create_all", forbidden)
    with pytest.raises(RuntimeError, match="schema_version=11"):
        schema.init_db()
    assert _snapshot() == before
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 11


def test_malformed_guard_is_not_rebuilt_or_upgraded(isolated_home) -> None:
    _v2_database()
    with get_engine().begin() as conn:
        conn.exec_driver_sql("CREATE TABLE participation_guard(uid TEXT, dynamic_id TEXT)")
        conn.exec_driver_sql("INSERT INTO participation_guard VALUES ('evidence','keep')")
    with pytest.raises(RuntimeError, match="participation_guard"):
        schema.init_db()
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT * FROM participation_guard").one() == ("evidence", "keep")
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 2


def test_missing_guard_in_v3_fails_closed(isolated_home) -> None:
    with get_engine().begin() as conn:
        conn.exec_driver_sql("DROP TABLE participation_guard")
    with pytest.raises(RuntimeError, match="participation_guard"):
        schema.init_db()


@pytest.mark.parametrize("removed", ["PRIMARY KEY (uid, dynamic_id),", "CHECK (repost_status IN ('pending','confirmed','unknown','suspected'))"])
def test_guard_constraints_are_validated_not_only_column_names(isolated_home, removed) -> None:
    _v2_database()
    ddl = (
        "CREATE TABLE participation_guard (uid VARCHAR(64) NOT NULL, "
        "dynamic_id VARCHAR(32) NOT NULL, repost_status VARCHAR(16) NOT NULL, "
        "updated_at INTEGER NOT NULL, PRIMARY KEY (uid, dynamic_id), "
        "CHECK (repost_status IN ('pending','confirmed','unknown','suspected')))"
    )
    ddl = ddl.replace(removed, "").replace(", )", ")")
    with get_engine().begin() as conn:
        conn.exec_driver_sql(ddl)
    with pytest.raises(RuntimeError, match="participation_guard"):
        schema.init_db()
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 2


@pytest.mark.parametrize(
    "original,replacement",
    [
        ("uid VARCHAR(64)", "uid INTEGER"),
        ("dynamic_id VARCHAR(32)", "dynamic_id BLOB"),
        ("repost_status VARCHAR(16)", "repost_status VARCHAR(32)"),
        ("updated_at INTEGER", "updated_at TEXT"),
    ],
)
def test_guard_column_types_are_validated(isolated_home, original, replacement) -> None:
    _v2_database()
    ddl = (
        "CREATE TABLE participation_guard (uid VARCHAR(64) NOT NULL, "
        "dynamic_id VARCHAR(32) NOT NULL, repost_status VARCHAR(16) NOT NULL, "
        "updated_at INTEGER NOT NULL, PRIMARY KEY (uid, dynamic_id), "
        "CHECK (repost_status IN ('pending','confirmed','unknown','suspected')))"
    )
    with get_engine().begin() as conn:
        conn.exec_driver_sql(ddl.replace(original, replacement))
        conn.exec_driver_sql("INSERT INTO participation_guard VALUES ('u','d','pending',123)")
    with pytest.raises(RuntimeError, match="字段类型"):
        schema.init_db()
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT repost_status FROM participation_guard").scalar_one() == "pending"
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 2


def test_concurrent_initialization_is_serialized(isolated_home) -> None:
    _v2_database()
    before = _snapshot()
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: schema.init_db(), range(2)))
    assert _snapshot() == before
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10


@pytest.fixture
def isolated_profile_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Use the real Profile database resolver, not isolated_home's legacy shim."""
    root = tmp_path / "profile-data"
    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(root))
    monkeypatch.setenv(data_paths.LEGACY_DATA_ROOT_ENV, str(root))
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    monkeypatch.delenv("BINGGO_PORTABLE", raising=False)
    monkeypatch.setenv("BINGGO_DATA_ROOT_LOCATOR", str(tmp_path / "locator.json"))
    reset_engine_for_tests()
    data_paths.reset_runtime_profile_for_tests()
    try:
        yield root
    finally:
        reset_engine_for_tests()
        data_paths.reset_runtime_profile_for_tests()


def test_real_profile_migrations_and_same_account_guard_are_isolated(isolated_profile_root) -> None:
    assert data_paths.initialize_data_layout() == "account-1"
    assert profile_manager.create_profile()["profile_id"] == "account-2"

    def restart_into(profile_id: str) -> None:
        profile_manager.set_active_profile(profile_id)
        reset_engine_for_tests()
        data_paths.reset_runtime_profile_for_tests()
        assert data_paths.get_runtime_profile_id() == profile_id
        assert db_path().resolve().is_relative_to(isolated_profile_root.resolve())

    snapshots = {}
    for profile_id in ("account-1", "account-2"):
        restart_into(profile_id)
        schema.init_db()
        _v2_database()
        snapshots[profile_id] = _snapshot()
    database_a = data_paths.get_database_path("account-1")
    database_b = data_paths.get_database_path("account-2")
    assert database_a != database_b

    restart_into("account-1")
    before_b = database_b.read_bytes()
    schema.init_db()
    participation_guard.record_pending("same-uid", "same-dynamic")
    assert _snapshot() == snapshots["account-1"]
    assert database_b.read_bytes() == before_b
    with closing(sqlite3.connect(database_b)) as conn:
        assert conn.execute("SELECT version FROM schema_meta").fetchone() == (2,)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='participation_guard'").fetchone() is None

    restart_into("account-2")
    schema.init_db()
    assert _snapshot() == snapshots["account-2"]
    assert participation_guard.get_guard("same-uid", "same-dynamic") is None
    participation_guard.confirm_repost("same-uid", "same-dynamic")
    assert participation_guard.get_guard("same-uid", "same-dynamic").repost_status == "confirmed"

    restart_into("account-1")
    schema.init_db()
    assert participation_guard.get_guard("same-uid", "same-dynamic").repost_status == "pending"
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10
