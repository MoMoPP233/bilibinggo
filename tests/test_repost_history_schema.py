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


def test_v3_to_v10_is_pure_add_and_idempotent(isolated_home) -> None:
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
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10
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


def test_v3_to_v10_interruption_rolls_back_and_can_retry(isolated_home, monkeypatch) -> None:
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
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10


def test_concurrent_v3_to_v10_initialization_is_serialized(isolated_home) -> None:
    _downgrade_fixture_to_v3()
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: schema.init_db(), range(2)))
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10
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
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10
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


def test_v5_to_v10_backfills_trusted_identity_and_safe_levels(isolated_home) -> None:
    with get_engine().begin() as conn:
        conn.exec_driver_sql("DROP TABLE repost_assessment")
        conn.exec_driver_sql("DROP TABLE repost_history")
        conn.exec_driver_sql("DROP TABLE repost_sync_checkpoint")
        conn.exec_driver_sql(
            "CREATE TABLE repost_history ("
            "uid VARCHAR(64) NOT NULL, repost_dynamic_id VARCHAR(32) NOT NULL, "
            "original_dynamic_id VARCHAR(32) NOT NULL, reposted_at INTEGER NOT NULL, "
            "original_author_uid VARCHAR(64), original_author_name TEXT, "
            "source VARCHAR(16) NOT NULL, delete_status VARCHAR(16) NOT NULL, "
            "delete_requested_at INTEGER, deleted_at INTEGER, last_seen_at INTEGER, "
            "last_error TEXT, updated_at INTEGER NOT NULL, "
            "PRIMARY KEY (uid, repost_dynamic_id), "
            "CONSTRAINT ck_repost_history_source CHECK (source IN ('binggo','history_import')), "
            "CONSTRAINT ck_repost_history_delete_status "
            "CHECK (delete_status IN ('active','delete_pending','deleted','delete_failed','unknown')), "
            "CONSTRAINT ck_repost_history_distinct_dynamic_ids "
            "CHECK (repost_dynamic_id <> original_dynamic_id))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE repost_assessment ("
            "uid VARCHAR(64) NOT NULL, original_dynamic_id VARCHAR(32) NOT NULL, "
            "lottery_type VARCHAR(16), lottery_time INTEGER, eligible_after INTEGER, "
            "reason TEXT, assessed_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
            "PRIMARY KEY (uid, original_dynamic_id))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE repost_sync_checkpoint ("
            "uid VARCHAR(64) NOT NULL PRIMARY KEY, "
            "head_dynamic_id VARCHAR(32), head_published_at INTEGER, "
            "full_scan_completed BOOLEAN NOT NULL, last_synced_at INTEGER, "
            "updated_at INTEGER NOT NULL, "
            "CONSTRAINT ck_repost_sync_checkpoint_completed "
            "CHECK (full_scan_completed IN (0,1)))"
        )
        conn.exec_driver_sql(
            "CREATE INDEX ix_repost_history_uid_original "
            "ON repost_history(uid, original_dynamic_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX ix_repost_history_uid_delete_status "
            "ON repost_history(uid, delete_status)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX ix_repost_history_uid_reposted_at "
            "ON repost_history(uid, reposted_at)"
        )
        conn.exec_driver_sql("UPDATE schema_meta SET version=5 WHERE id=1")
        conn.exec_driver_sql(
            "INSERT INTO repost_history(uid,repost_dynamic_id,original_dynamic_id,reposted_at,"
            "source,delete_status,updated_at,last_seen_at) "
            "VALUES ('123','2000000000000000001','1000000000000000001',100,"
            "'history_import','active',300,300)"
        )
        conn.exec_driver_sql(
            "INSERT INTO repost_assessment(uid,original_dynamic_id,lottery_type,lottery_time,"
            "eligible_after,reason,assessed_at,updated_at) "
            "VALUES ('123','1000000000000000001','互动抽奖',1000,1000,'safe',500,500)"
        )

    schema.init_db()
    schema.init_db()

    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10
        identity = conn.exec_driver_sql(
            "SELECT identity_source, identity_ok, identity_checked_at FROM repost_history"
        ).one()
        assert identity == ("legacy_space_feed", 1, 300)
        assessment = conn.exec_driver_sql(
            "SELECT assessment_level, classification_source, reason_code FROM repost_assessment"
        ).one()
        assert assessment == ("safe", "legacy", "v1_safe")


def test_v6_to_v10_backfills_assessment_lifecycle_final(isolated_home) -> None:
    with get_engine().begin() as conn:
        conn.exec_driver_sql("DROP TABLE repost_assessment")
        conn.exec_driver_sql(
            "CREATE TABLE repost_assessment ("
            "uid VARCHAR(64) NOT NULL, original_dynamic_id VARCHAR(32) NOT NULL, "
            "assessment_level VARCHAR(16) NOT NULL DEFAULT 'safe', "
            "reason_code VARCHAR(32), lottery_type VARCHAR(16), lottery_time INTEGER, "
            "eligible_after INTEGER, reason TEXT, "
            "classification_source VARCHAR(16) NOT NULL DEFAULT 'activities', "
            "summary TEXT, evaluated_at INTEGER, remote_checked_at INTEGER, "
            "assessed_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
            "PRIMARY KEY (uid, original_dynamic_id))"
        )
        conn.exec_driver_sql("UPDATE schema_meta SET version=6 WHERE id=1")
        conn.exec_driver_sql(
            "INSERT INTO repost_assessment(uid,original_dynamic_id,assessment_level,"
            "reason_code,assessed_at,updated_at) "
            "VALUES ('123','1000000000000000001','safe','safe_official_notice',100,100)"
        )

    schema.init_db()
    schema.init_db()

    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10
        status = conn.exec_driver_sql(
            "SELECT assessment_status FROM repost_assessment"
        ).scalar_one()
        assert status == "final"


def test_v7_to_v8_backfills_defer_columns_and_reliable_time(isolated_home) -> None:
    with get_engine().begin() as conn:
        conn.exec_driver_sql("DROP TABLE repost_assessment")
        conn.exec_driver_sql(
            "CREATE TABLE repost_assessment ("
            "uid VARCHAR(64) NOT NULL, original_dynamic_id VARCHAR(32) NOT NULL, "
            "assessment_level VARCHAR(16) NOT NULL DEFAULT 'safe', "
            "assessment_status VARCHAR(16) NOT NULL DEFAULT 'final', "
            "reason_code VARCHAR(32), lottery_type VARCHAR(16), lottery_time INTEGER, "
            "eligible_after INTEGER, reason TEXT, "
            "classification_source VARCHAR(16) NOT NULL DEFAULT 'activities', "
            "summary TEXT, evaluated_at INTEGER, remote_checked_at INTEGER, "
            "assessed_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
            "PRIMARY KEY (uid, original_dynamic_id))"
        )
        conn.exec_driver_sql(
            "INSERT INTO repost_assessment(uid,original_dynamic_id,assessment_level,"
            "lottery_time,assessed_at,updated_at) "
            "VALUES ('123','1000000000000000001','manual_review',1000,100,100)"
        )
        conn.exec_driver_sql("UPDATE schema_meta SET version=7 WHERE id=1")

    schema.init_db()
    schema.init_db()

    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10
        reliable = conn.exec_driver_sql(
            "SELECT lottery_time_reliable FROM repost_assessment"
        ).scalar_one()
        assert reliable == 1
        columns = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(repost_history)").all()
        }
        assert {
            "cleanup_defer_reason",
            "cleanup_deferred_at",
            "cleanup_deferred_until",
        } <= columns


def test_v8_to_v10_backfills_sync_needed_flag(isolated_home) -> None:
    with get_engine().begin() as conn:
        conn.exec_driver_sql("DROP TABLE repost_sync_checkpoint")
        conn.exec_driver_sql(
            "CREATE TABLE repost_sync_checkpoint ("
            "uid VARCHAR(64) NOT NULL PRIMARY KEY, "
            "head_dynamic_id VARCHAR(32), head_published_at INTEGER, "
            "full_scan_completed BOOLEAN NOT NULL, last_synced_at INTEGER, "
            "updated_at INTEGER NOT NULL, "
            "CONSTRAINT ck_repost_sync_checkpoint_completed "
            "CHECK (full_scan_completed IN (0,1)))"
        )
        conn.exec_driver_sql(
            "INSERT INTO repost_sync_checkpoint(uid,head_dynamic_id,head_published_at,"
            "full_scan_completed,last_synced_at,updated_at) "
            "VALUES ('123','3000000000000000001',1000,1,1100,1100)"
        )
        conn.exec_driver_sql("UPDATE schema_meta SET version=8 WHERE id=1")

    schema.init_db()
    schema.init_db()

    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10
        row = conn.exec_driver_sql(
            "SELECT head_dynamic_id, full_scan_completed, sync_needed FROM repost_sync_checkpoint"
        ).one()
        assert row == ("3000000000000000001", 1, 0)


def test_v9_to_v10_backfills_risk_pause_columns(isolated_home) -> None:
    with get_engine().begin() as conn:
        conn.exec_driver_sql("DROP TABLE repost_sync_checkpoint")
        conn.exec_driver_sql(
            "CREATE TABLE repost_sync_checkpoint ("
            "uid VARCHAR(64) NOT NULL PRIMARY KEY, "
            "head_dynamic_id VARCHAR(32), head_published_at INTEGER, "
            "full_scan_completed BOOLEAN NOT NULL, last_synced_at INTEGER, "
            "sync_needed BOOLEAN NOT NULL DEFAULT 0, sync_needed_at INTEGER, "
            "updated_at INTEGER NOT NULL, "
            "CONSTRAINT ck_repost_sync_checkpoint_completed "
            "CHECK (full_scan_completed IN (0,1)))"
        )
        conn.exec_driver_sql(
            "INSERT INTO repost_sync_checkpoint(uid,full_scan_completed,sync_needed,updated_at) "
            "VALUES ('123',1,0,100)"
        )
        conn.exec_driver_sql("UPDATE schema_meta SET version=9 WHERE id=1")

    schema.init_db()
    schema.init_db()

    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM schema_meta").scalar_one() == 10
        row = conn.exec_driver_sql(
            "SELECT sync_needed, maintenance_risk_paused FROM repost_sync_checkpoint"
        ).one()
        assert row == (0, 0)
