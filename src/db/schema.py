from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import text
from sqlmodel import Session, SQLModel

from src.db.engine import get_engine
from src.db.models import SchemaMeta

# 确保全部表注册到 metadata
from src.db import models as _models  # noqa: F401

SCHEMA_VERSION = 3

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


_MIGRATIONS: dict[int, Callable[[Session], None]] = {
    1: migrate_v1_to_v2,
    2: migrate_v2_to_v3,
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
        "请勿降低 schema_meta 或删除数据库；参与保护记录必须保留。"
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
                session.flush()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
