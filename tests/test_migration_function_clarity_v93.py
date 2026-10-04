from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.db import schema
from src.db.engine import get_engine


def _make_legacy_database(version: int) -> None:
    """Turn a tmp-path current DB into the V10/V11 shape needed by this test."""
    with get_engine().begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO repost_sync_checkpoint "
            "(uid, head_dynamic_id, head_published_at, full_scan_completed, "
            "last_synced_at, updated_at, sync_needed, maintenance_risk_paused) "
            "VALUES ('uid-a', 'old-head', 10, 1, 20, 30, 1, 1)"
        )
        conn.exec_driver_sql(
            "INSERT INTO repost_history "
            "(uid, repost_dynamic_id, original_dynamic_id, reposted_at, "
            "source, delete_status, updated_at) "
            "VALUES ('uid-a', 'repost-1', 'original-1', 11, 'history_import', 'active', 12)"
        )
        for name in (
            "resume_offset",
            "resume_head_dynamic_id",
            "resume_head_published_at",
            "resume_started_at",
        ):
            conn.exec_driver_sql(f"ALTER TABLE repost_sync_checkpoint DROP COLUMN {name}")
        if version == 10:
            conn.exec_driver_sql("DROP TABLE ds9_rejection_audit")
        conn.exec_driver_sql(f"UPDATE schema_meta SET version={version} WHERE id=1")


def test_migration_names_are_unique_and_dispatcher_is_complete() -> None:
    source = Path(schema.__file__).read_text(encoding="utf-8")
    migration_names = [
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("migrate_v")
    ]
    assert len(migration_names) == len(set(migration_names)) == schema.SCHEMA_VERSION - 1
    assert set(schema._MIGRATIONS) == set(range(1, schema.SCHEMA_VERSION))
    for version, migration in schema._MIGRATIONS.items():
        assert migration.__name__ == f"migrate_v{version}_to_v{version + 1}"


@pytest.mark.parametrize("version,expected_steps", [(10, [10, 11]), (11, [11])])
def test_v10_and_v11_upgrade_preserves_data_and_does_not_replay(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, version: int, expected_steps: list[int]
) -> None:
    _make_legacy_database(version)
    steps: list[int] = []
    for step in expected_steps:
        migration = schema._MIGRATIONS[step]

        def record_and_migrate(session, *, _step=step, _migration=migration):
            steps.append(_step)
            assert session.get(schema.SchemaMeta, 1).version == _step
            _migration(session)

        monkeypatch.setitem(schema._MIGRATIONS, step, record_and_migrate)

    schema.init_db()
    schema.init_db()
    assert steps == expected_steps
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta WHERE id=1").scalar_one() == 12
        assert conn.exec_driver_sql(
            "SELECT head_dynamic_id, head_published_at, full_scan_completed, "
            "last_synced_at, sync_needed, maintenance_risk_paused "
            "FROM repost_sync_checkpoint WHERE uid='uid-a'"
        ).one() == ("old-head", 10, 1, 20, 1, 1)
        assert conn.exec_driver_sql(
            "SELECT original_dynamic_id, delete_status FROM repost_history "
            "WHERE uid='uid-a' AND repost_dynamic_id='repost-1'"
        ).one() == ("original-1", "active")
        assert {
            "resume_offset", "resume_head_dynamic_id", "resume_head_published_at", "resume_started_at"
        } <= {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(repost_sync_checkpoint)")
        }
        assert conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ds9_rejection_audit'"
        ).scalar_one() == "ds9_rejection_audit"


def test_fresh_and_current_database_skip_old_migrations(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta WHERE id=1").scalar_one() == 12

    def unexpected_migration(_session):
        pytest.fail("Current database must not replay a historical migration")

    for version in schema._MIGRATIONS:
        monkeypatch.setitem(schema._MIGRATIONS, version, unexpected_migration)
    schema.init_db()
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta WHERE id=1").scalar_one() == 12


def test_migration_changes_only_the_selected_profile_database(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.db import engine

    paths = {
        "account-1": isolated_home / "profiles" / "account-1" / "binggo.db",
        "account-2": isolated_home / "profiles" / "account-2" / "binggo.db",
    }
    current = "account-1"
    monkeypatch.setattr(engine, "db_path", lambda: paths[current])

    schema.init_db()
    _make_legacy_database(11)
    current = "account-2"
    schema.init_db()
    with get_engine().begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO repost_sync_checkpoint "
            "(uid, full_scan_completed, updated_at, sync_needed, maintenance_risk_paused) "
            "VALUES ('uid-b', 1, 99, 0, 0)"
        )

    current = "account-1"
    schema.init_db()
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta WHERE id=1").scalar_one() == 12
        assert conn.exec_driver_sql(
            "SELECT uid FROM repost_sync_checkpoint"
        ).scalar_one() == "uid-a"

    current = "account-2"
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta WHERE id=1").scalar_one() == 12
        assert conn.exec_driver_sql(
            "SELECT uid FROM repost_sync_checkpoint"
        ).scalar_one() == "uid-b"
