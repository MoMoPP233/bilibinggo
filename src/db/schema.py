from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import text
from sqlmodel import Session, SQLModel

from src.db.engine import get_engine
from src.db.models import SchemaMeta

# 确保全部表注册到 metadata
from src.db import models as _models  # noqa: F401

SCHEMA_VERSION = 5

_JOB_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ("label", "TEXT NOT NULL DEFAULT ''"),
    ("source", "TEXT NOT NULL DEFAULT 'ui'"),
    ("params_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("result_json", "TEXT"),
    ("error_kind", "TEXT"),
)


def migrate_v1_to_v2(session: Session) -> None:
    """为已有 jobs 表补齐 v2 列与索引；列已存在则跳过（幂等）。"""
    conn = session.connection()
    table_exists = conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'")
    ).fetchone()
    if table_exists is None:
        # create_all 应已建表；若仍无表则交由下轮 create_all/启动失败暴露
        return
    existing = {
        str(row[1])
        for row in conn.execute(text("PRAGMA table_info(jobs)")).fetchall()
    }
    for name, decl in _JOB_V2_COLUMNS:
        if name not in existing:
            conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {name} {decl}"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_finished_at ON jobs(finished_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_created_at ON jobs(created_at)"))


def _validate_participation_guard_table(session: Session) -> None:
    conn = session.connection()
    columns = conn.execute(text("PRAGMA table_info(participation_guard)")).all()
    expected = {"uid", "dynamic_id", "repost_status", "updated_at"}
    if {str(row[1]) for row in columns} != expected:
        raise RuntimeError("participation_guard 表结构不完整，无法安全启用参与保护")
    primary_key = [str(row[1]) for row in sorted(columns, key=lambda row: row[5]) if row[5]]
    if primary_key != ["uid", "dynamic_id"] or any(not row[3] for row in columns):
        raise RuntimeError("participation_guard 主键或非空约束不匹配，无法安全启用参与保护")
    types = {str(row[1]): str(row[2]).upper() for row in columns}
    if types != {
        "uid": "VARCHAR(64)", "dynamic_id": "VARCHAR(32)",
        "repost_status": "VARCHAR(16)", "updated_at": "INTEGER",
    }:
        raise RuntimeError("participation_guard 字段类型不匹配，无法安全启用参与保护")
    table_sql = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='participation_guard'")
    ).scalar_one()
    normalized = "".join(str(table_sql).lower().split())
    if "check(repost_statusin('pending','confirmed','unknown','suspected'))" not in normalized:
        raise RuntimeError("participation_guard 状态约束不匹配，无法安全启用参与保护")


def migrate_v2_to_v3(session: Session) -> None:
    """只增加独立的参与保护表；不改写任何旧业务表或推断历史状态。"""
    session.connection().execute(
        text(
            "CREATE TABLE IF NOT EXISTS participation_guard ("
            "uid VARCHAR(64) NOT NULL, "
            "dynamic_id VARCHAR(32) NOT NULL, "
            "repost_status VARCHAR(16) NOT NULL, "
            "updated_at INTEGER NOT NULL, "
            "PRIMARY KEY (uid, dynamic_id), "
            "CONSTRAINT ck_participation_guard_repost_status "
            "CHECK (repost_status IN ('pending','confirmed','unknown','suspected')))"
        )
    )
    _validate_participation_guard_table(session)


def _table_columns(session: Session, table: str) -> list:
    return list(session.connection().execute(text(f"PRAGMA table_info({table})")).all())


def _validate_columns(
    session: Session,
    *,
    table: str,
    expected_types: dict[str, str],
    primary_key: list[str],
    required: set[str],
    label: str,
) -> None:
    columns = _table_columns(session, table)
    if {str(row[1]) for row in columns} != set(expected_types):
        raise RuntimeError(f"{label} 表结构不完整，无法安全启用转发清理")
    actual_primary_key = [
        str(row[1]) for row in sorted(columns, key=lambda row: row[5]) if row[5]
    ]
    if actual_primary_key != primary_key:
        raise RuntimeError(f"{label} 主键不匹配，无法安全启用转发清理")
    nullable_required = {str(row[1]) for row in columns if str(row[1]) in required and not row[3]}
    if nullable_required:
        raise RuntimeError(f"{label} 非空约束不匹配，无法安全启用转发清理")
    actual_types = {str(row[1]): str(row[2]).upper() for row in columns}
    if actual_types != expected_types:
        raise RuntimeError(f"{label} 字段类型不匹配，无法安全启用转发清理")


def _validate_index(session: Session, table: str, name: str, columns: list[str]) -> None:
    conn = session.connection()
    indexes = {str(row[1]): row for row in conn.execute(text(f"PRAGMA index_list({table})"))}
    entry = indexes.get(name)
    if entry is None or bool(entry[2]):
        raise RuntimeError(f"{table} 索引 {name} 不匹配，无法安全启用转发清理")
    actual = [str(row[2]) for row in conn.execute(text(f"PRAGMA index_info({name})"))]
    if actual != columns:
        raise RuntimeError(f"{table} 索引 {name} 不匹配，无法安全启用转发清理")


def _validate_repost_history_table_structure(session: Session) -> None:
    _validate_columns(
        session,
        table="repost_history",
        expected_types={
            "uid": "VARCHAR(64)",
            "repost_dynamic_id": "VARCHAR(32)",
            "original_dynamic_id": "VARCHAR(32)",
            "reposted_at": "INTEGER",
            "original_author_uid": "VARCHAR(64)",
            "original_author_name": "TEXT",
            "source": "VARCHAR(16)",
            "delete_status": "VARCHAR(16)",
            "delete_requested_at": "INTEGER",
            "deleted_at": "INTEGER",
            "last_seen_at": "INTEGER",
            "last_error": "TEXT",
            "updated_at": "INTEGER",
        },
        primary_key=["uid", "repost_dynamic_id"],
        required={
            "uid",
            "repost_dynamic_id",
            "original_dynamic_id",
            "reposted_at",
            "source",
            "delete_status",
            "updated_at",
        },
        label="repost_history",
    )
    table_sql = session.connection().execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='repost_history'")
    ).scalar_one()
    normalized = "".join(str(table_sql).lower().split())
    constraints = (
        "check(sourcein('binggo','history_import'))",
        "check(delete_statusin('active','delete_pending','deleted','delete_failed','unknown'))",
        "check(repost_dynamic_id<>original_dynamic_id)",
    )
    if any(constraint not in normalized for constraint in constraints):
        raise RuntimeError("repost_history 状态或身份约束不匹配，无法安全启用转发清理")


def _validate_repost_sync_checkpoint_table_structure(session: Session) -> None:
    _validate_columns(
        session,
        table="repost_sync_checkpoint",
        expected_types={
            "uid": "VARCHAR(64)",
            "head_dynamic_id": "VARCHAR(32)",
            "head_published_at": "INTEGER",
            "full_scan_completed": "BOOLEAN",
            "last_synced_at": "INTEGER",
            "updated_at": "INTEGER",
        },
        primary_key=["uid"],
        required={"uid", "full_scan_completed", "updated_at"},
        label="repost_sync_checkpoint",
    )
    checkpoint_sql = session.connection().execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='repost_sync_checkpoint'")
    ).scalar_one()
    normalized_checkpoint = "".join(str(checkpoint_sql).lower().split())
    if "check(full_scan_completedin(0,1))" not in normalized_checkpoint:
        raise RuntimeError("repost_sync_checkpoint 完成状态约束不匹配，无法安全启用转发清理")


def _validate_repost_history_tables(session: Session) -> None:
    _validate_repost_history_table_structure(session)
    _validate_repost_sync_checkpoint_table_structure(session)
    _validate_index(
        session, "repost_history", "ix_repost_history_uid_original", ["uid", "original_dynamic_id"]
    )
    _validate_index(
        session, "repost_history", "ix_repost_history_uid_delete_status", ["uid", "delete_status"]
    )
    _validate_index(
        session, "repost_history", "ix_repost_history_uid_reposted_at", ["uid", "reposted_at"]
    )


def _validate_repost_assessment_table(session: Session) -> None:
    _validate_columns(
        session,
        table="repost_assessment",
        expected_types={
            "uid": "VARCHAR(64)",
            "original_dynamic_id": "VARCHAR(32)",
            "lottery_type": "VARCHAR(16)",
            "lottery_time": "INTEGER",
            "eligible_after": "INTEGER",
            "reason": "TEXT",
            "assessed_at": "INTEGER",
            "updated_at": "INTEGER",
        },
        primary_key=["uid", "original_dynamic_id"],
        required={"uid", "original_dynamic_id", "assessed_at", "updated_at"},
        label="repost_assessment",
    )


def migrate_v3_to_v4(session: Session) -> None:
    """纯增表建立个人转发历史与增量扫描锚点，不触碰参与保护或旧业务表。"""
    conn = session.connection()
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS repost_history ("
            "uid VARCHAR(64) NOT NULL, "
            "repost_dynamic_id VARCHAR(32) NOT NULL, "
            "original_dynamic_id VARCHAR(32) NOT NULL, "
            "reposted_at INTEGER NOT NULL, "
            "original_author_uid VARCHAR(64), "
            "original_author_name TEXT, "
            "source VARCHAR(16) NOT NULL, "
            "delete_status VARCHAR(16) NOT NULL, "
            "delete_requested_at INTEGER, "
            "deleted_at INTEGER, "
            "last_seen_at INTEGER, "
            "last_error TEXT, "
            "updated_at INTEGER NOT NULL, "
            "PRIMARY KEY (uid, repost_dynamic_id), "
            "CONSTRAINT ck_repost_history_source "
            "CHECK (source IN ('binggo','history_import')), "
            "CONSTRAINT ck_repost_history_delete_status "
            "CHECK (delete_status IN ('active','delete_pending','deleted','delete_failed','unknown')), "
            "CONSTRAINT ck_repost_history_distinct_dynamic_ids "
            "CHECK (repost_dynamic_id <> original_dynamic_id))"
        )
    )
    _validate_repost_history_table_structure(session)
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS repost_sync_checkpoint ("
            "uid VARCHAR(64) NOT NULL PRIMARY KEY, "
            "head_dynamic_id VARCHAR(32), "
            "head_published_at INTEGER, "
            "full_scan_completed BOOLEAN NOT NULL, "
            "last_synced_at INTEGER, "
            "updated_at INTEGER NOT NULL, "
            "CONSTRAINT ck_repost_sync_checkpoint_completed "
            "CHECK (full_scan_completed IN (0,1)))"
        )
    )
    _validate_repost_sync_checkpoint_table_structure(session)
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_repost_history_uid_original "
            "ON repost_history(uid, original_dynamic_id)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_repost_history_uid_delete_status "
            "ON repost_history(uid, delete_status)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_repost_history_uid_reposted_at "
            "ON repost_history(uid, reposted_at)"
        )
    )
    _validate_repost_history_tables(session)


def migrate_v4_to_v5(session: Session) -> None:
    """纯增表保存官方抽奖可安全清理评估结果，供前端无远程调用恢复候选。"""
    conn = session.connection()
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS repost_assessment ("
            "uid VARCHAR(64) NOT NULL, "
            "original_dynamic_id VARCHAR(32) NOT NULL, "
            "lottery_type VARCHAR(16), "
            "lottery_time INTEGER, "
            "eligible_after INTEGER, "
            "reason TEXT, "
            "assessed_at INTEGER NOT NULL, "
            "updated_at INTEGER NOT NULL, "
            "PRIMARY KEY (uid, original_dynamic_id))"
        )
    )
    _validate_repost_assessment_table(session)


_MIGRATIONS: dict[int, Callable[[Session], None]] = {
    1: migrate_v1_to_v2,
    2: migrate_v2_to_v3,
    3: migrate_v3_to_v4,
    4: migrate_v4_to_v5,
}


def _jobs_table_has_v2_columns(session: Session) -> bool:
    conn = session.connection()
    if conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'")
    ).fetchone() is None:
        return False
    existing = {
        str(row[1])
        for row in conn.execute(text("PRAGMA table_info(jobs)")).fetchall()
    }
    return all(name in existing for name, _ in _JOB_V2_COLUMNS)


def _schema_newer_than_code_error(recorded: int) -> RuntimeError:
    from src.db.engine import db_path

    db = db_path()
    return RuntimeError(
        f"数据库 schema_version={recorded} 高于本程序支持的 {SCHEMA_VERSION}，无法安全启动。\n\n"
        f"数据库文件：{db}\n\n"
        "请使用支持该数据库版本的 Binggo，并完全退出其他版本后重试。\n"
        "请勿降低 schema_meta 或删除数据库；参与保护和转发历史记录必须保留。"
    )


def init_db() -> None:
    """用短写事务串行化跨进程初始化；版本检查先于任何建表操作。"""
    engine = get_engine()
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            has_meta = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'")
            ).first() is not None
            recorded = (
                conn.execute(text("SELECT version FROM schema_meta WHERE id=1")).scalar_one_or_none()
                if has_meta else None
            )
            if recorded is not None and int(recorded) > SCHEMA_VERSION:
                raise _schema_newer_than_code_error(int(recorded))

            with Session(bind=conn, join_transaction_mode="rollback_only") as session:
                # 已升级的保护表若丢失/变形，不能自动重建空账本后继续参与。
                if recorded is not None and int(recorded) >= 3:
                    _validate_participation_guard_table(session)
                if recorded is not None and int(recorded) >= 4:
                    _validate_repost_history_tables(session)
                if recorded is not None and int(recorded) >= 5:
                    _validate_repost_assessment_table(session)
                SQLModel.metadata.create_all(conn)
                meta = session.get(SchemaMeta, 1)
                current = int(meta.version) if meta is not None else 1
                if meta is None:
                    meta = SchemaMeta(id=1, version=current)
                    session.add(meta)
                while current < SCHEMA_VERSION:
                    migrate = _MIGRATIONS.get(current)
                    if migrate is None:
                        raise RuntimeError(f"缺少 schema 迁移：{current} -> {current + 1}")
                    migrate(session)
                    current += 1
                    meta.version = current
                if not _jobs_table_has_v2_columns(session):
                    raise RuntimeError("jobs 表结构不完整，无法安全启动")
                _validate_participation_guard_table(session)
                _validate_repost_history_tables(session)
                _validate_repost_assessment_table(session)
                session.flush()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
