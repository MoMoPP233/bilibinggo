from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.exc import IntegrityError

from src.db import schema
from src.db.engine import get_engine


def _downgrade_fixture_to_v3() -> None:
    with get_engine().begin() as conn:
        conn.exec_driver_sql("DROP TABLE repost_history")
        conn.exec_driver_sql("DROP TABLE repost_sync_checkpoint")
        conn.exec_driver_sql("DROP TABLE repost_assessment")
        conn.exec_driver_sql("UPDATE schema_meta SET version=3 WHERE id=1")


def test_v3_to_v5_is_pure_add_and_idempotent(isolated_home) -> None:
    with get_engine().begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO participation_guard(uid,dynamic_id,repost_status,updated_at) "
            "VALUES ('123','100','confirmed',10)"
        )
        conn.exec_driver_sql(
            "INSERT INTO activities(dynamic_id,payload_json,updated_at,status_classified,skipped) "
            "VALUES ('100','{}',20,0,0)"
        )
    _downgrade_fixture_to_v3()

    schema.init_db()
    schema.init_db()

    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 5
        assert conn.exec_driver_sql(
            "SELECT uid,dynamic_id,repost_status,updated_at FROM participation_guard"
        ).one() == ("123", "100", "confirmed", 10)
        assert conn.exec_driver_sql("SELECT dynamic_id,updated_at FROM activities").one() == (
            "100",
            20,
        )
        assert conn.exec_driver_sql("SELECT count(*) FROM repost_history").scalar_one() == 0
        assert conn.exec_driver_sql(
            "SELECT count(*) FROM repost_sync_checkpoint"
        ).scalar_one() == 0
        assert conn.exec_driver_sql(
            "SELECT count(*) FROM repost_assessment"
        ).scalar_one() == 0


def test_v3_to_v5_interruption_rolls_back_and_can_retry(isolated_home, monkeypatch) -> None:
    _downgrade_fixture_to_v3()
    migrate = schema.migrate_v3_to_v4

    def interrupted(session):
        migrate(session)
        raise RuntimeError("v4 interrupted")

    monkeypatch.setitem(schema._MIGRATIONS, 3, interrupted)
    with pytest.raises(RuntimeError, match="v4 interrupted"):
        schema.init_db()
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 3
        assert conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='repost_history'"
        ).first() is None
        assert conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='repost_sync_checkpoint'"
        ).first() is None

    monkeypatch.setitem(schema._MIGRATIONS, 3, migrate)
    schema.init_db()
    schema.init_db()
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 5


def test_concurrent_v3_to_v5_initialization_is_serialized(isolated_home) -> None:
    _downgrade_fixture_to_v3()
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: schema.init_db(), range(2)))
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 5
        assert conn.exec_driver_sql("SELECT count(*) FROM repost_history").scalar_one() == 0


@pytest.mark.parametrize(
    "table",
    ["repost_history", "repost_sync_checkpoint", "repost_assessment"],
)
def test_missing_repost_table_fails_closed(isolated_home, table) -> None:
    with get_engine().begin() as conn:
        conn.exec_driver_sql(f"DROP TABLE {table}")
    with pytest.raises(RuntimeError, match=table):
        schema.init_db()
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 5
        assert conn.exec_driver_sql(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
        ).first() is None


def test_malformed_history_table_is_not_silently_accepted(isolated_home) -> None:
    _downgrade_fixture_to_v3()
    with get_engine().begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE repost_history ("
            "uid VARCHAR(64) NOT NULL, repost_dynamic_id VARCHAR(32) NOT NULL, "
            "original_dynamic_id BLOB NOT NULL, evidence TEXT, "
            "PRIMARY KEY(uid,repost_dynamic_id))"
        )
        conn.exec_driver_sql(
            "INSERT INTO repost_history(uid,repost_dynamic_id,original_dynamic_id,evidence) "
            "VALUES ('123','200',x'31','keep')"
        )
    with pytest.raises(RuntimeError, match="repost_history"):
        schema.init_db()
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 3
        assert conn.exec_driver_sql("SELECT evidence FROM repost_history").scalar_one() == "keep"


@pytest.mark.parametrize(
    "statement",
    [
        (
            "INSERT INTO repost_history(uid,repost_dynamic_id,original_dynamic_id,reposted_at,"
            "source,delete_status,updated_at) VALUES ('123','200','100',1,'other','active',1)"
        ),
        (
            "INSERT INTO repost_history(uid,repost_dynamic_id,original_dynamic_id,reposted_at,"
            "source,delete_status,updated_at) VALUES ('123','200','100',1,'history_import','gone',1)"
        ),
        (
            "INSERT INTO repost_history(uid,repost_dynamic_id,original_dynamic_id,reposted_at,"
            "source,delete_status,updated_at) VALUES ('123','100','100',1,'history_import','active',1)"
        ),
        (
            "INSERT INTO repost_sync_checkpoint(uid,full_scan_completed,updated_at) "
            "VALUES ('123',2,1)"
        ),
    ],
)
def test_v4_database_constraints_reject_invalid_rows(isolated_home, statement) -> None:
    with pytest.raises(IntegrityError):
        with get_engine().begin() as conn:
            conn.exec_driver_sql(statement)
