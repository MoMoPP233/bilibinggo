"""V6 清理维护健康检查：纯本地、0 远程、只诊断不修复。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.db.engine import reset_engine_for_tests
from src.db.models import ParticipationGuardRow
from src.db.session import session_scope
from src.repost_cleanup import cleanup_health_check, reconciliation_needs_sync
from src.repost_history import (
    RepostImportRecord,
    claim_delete_pending,
    get_checkpoint,
    get_repost,
    mark_delete_result,
    mark_sync_needed,
    pause_maintenance_risk,
    save_checkpoint,
    upsert_repost_records,
)

UID = "12345"
OTHER_UID = "54321"
ORIGINAL = "1000000000000000001"
REPOSTS = {
    "unknown": "2000000000000000001",
    "failed": "2000000000000000002",
    "pending": "2000000000000000003",
    "deleted": "2000000000000000004",
    "active": "2000000000000000005",
}


def _seed_row(uid: str, repost_id: str, original_id: str, *, at: int) -> None:
    upsert_repost_records(
        uid,
        [RepostImportRecord(repost_dynamic_id=repost_id, original_dynamic_id=original_id, reposted_at=at)],
        seen_at=at,
    )


def _health(uid: str = UID, **kwargs) -> dict:
    return cleanup_health_check(uid, **kwargs)


def test_health_is_ok_and_never_touches_network(monkeypatch, isolated_home: Path) -> None:
    """空账本健康状态为 normal，全程不创建 Bilibili 客户端、不删除、不改库。"""
    _ = isolated_home

    def forbidden_client(*args, **kwargs):
        raise AssertionError("健康检查不得创建 Bilibili 客户端")

    def forbidden_delete(*args, **kwargs):
        raise AssertionError("健康检查不得删除")

    monkeypatch.setattr("src.repost_cleanup.BilibiliClient", forbidden_client)
    monkeypatch.setattr("src.repost_cleanup.delete_reposts", forbidden_delete)
    monkeypatch.setattr("src.bilibili_auth.require_login", forbidden_client)

    result = _health()

    assert result["status"] == "normal"
    assert result["status_text"] == "维护状态正常"
    assert result["runtime"]["consistent"] is True
    assert result["counts"]["all_total"] == 0
    assert result["issues"] == []
    assert result["issue_total"] == 0
    keys = {check["key"] for check in result["checks"]}
    assert "sync_not_started" in keys


def test_health_reports_sync_needed(isolated_home: Path) -> None:
    mark_sync_needed(UID, at=1_700_000_000)
    result = _health()
    assert result["status"] == "sync_pending"
    assert result["checkpoint"]["sync_needed"] is True
    texts = " ".join(check["text"] for check in result["checks"])
    assert "等待增量同步" in texts


def test_health_reports_reconciliation_missing(isolated_home: Path) -> None:
    save_checkpoint(
        UID,
        head_dynamic_id=REPOSTS["active"],
        head_published_at=100,
        full_scan_completed=True,
        last_synced_at=200,
    )
    with session_scope() as session:
        session.add(
            ParticipationGuardRow(
                uid=UID,
                dynamic_id=ORIGINAL,
                repost_status="confirmed",
                updated_at=300,
            )
        )
    assert reconciliation_needs_sync(UID) is True

    result = _health()
    assert result["status"] == "sync_pending"
    texts = " ".join(check["text"] for check in result["checks"])
    assert "补同步一次" in texts
    keys = {check["key"] for check in result["checks"]}
    assert "sync_reconciliation" in keys


def test_health_reports_risk_pause_with_reason_and_time(isolated_home: Path) -> None:
    pause_maintenance_risk(UID, "-352 自动维护风控暂停", at=1_700_000_000)
    result = _health()
    assert result["status"] == "risk_paused"
    assert result["status_text"] == "维护已暂停（风控）"
    assert result["maintenance"]["risk_paused"] is True
    assert result["maintenance"]["risk_paused_at"] == 1_700_000_000
    assert result["maintenance"]["risk_reason"] == "-352 自动维护风控暂停"
    texts = " ".join(check["text"] for check in result["checks"])
    assert "风控暂停" in texts
    assert "-352" in texts


def _seed_issue_statuses() -> None:
    _seed_row(UID, REPOSTS["unknown"], ORIGINAL, at=100)
    _seed_row(UID, REPOSTS["failed"], ORIGINAL, at=101)
    _seed_row(UID, REPOSTS["pending"], ORIGINAL, at=102)
    _seed_row(UID, REPOSTS["deleted"], ORIGINAL, at=103)
    _seed_row(UID, REPOSTS["active"], ORIGINAL, at=104)
    claim_delete_pending(UID, REPOSTS["unknown"], requested_at=200)
    mark_delete_result(UID, REPOSTS["unknown"], status="unknown", error="网络超时", updated_at=200)
    claim_delete_pending(UID, REPOSTS["failed"], requested_at=201)
    mark_delete_result(
        UID,
        REPOSTS["failed"],
        status="delete_failed",
        error="Bilibili API error -101: 无权限",
        updated_at=201,
    )
    claim_delete_pending(UID, REPOSTS["pending"], requested_at=202)
    claim_delete_pending(UID, REPOSTS["deleted"], requested_at=203)
    mark_delete_result(
        UID,
        REPOSTS["deleted"],
        status="deleted",
        deleted_at=203,
        updated_at=203,
    )


def test_health_counts_and_lists_issue_rows(isolated_home: Path) -> None:
    _seed_issue_statuses()
    result = _health()
    assert result["status"] == "review_needed"
    assert result["counts"]["all_total"] == 5
    assert result["counts"]["active"] == 1
    assert result["counts"]["delete_pending"] == 1
    assert result["counts"]["delete_failed"] == 1
    assert result["counts"]["unknown"] == 1
    assert result["counts"]["deleted"] == 1
    assert result["issue_total"] == 3

    rows = {row["repost_dynamic_id"]: row for row in result["issues"]}
    assert set(rows) == {
        REPOSTS["unknown"],
        REPOSTS["failed"],
        REPOSTS["pending"],
    }
    assert rows[REPOSTS["unknown"]]["delete_status"] == "unknown"
    assert rows[REPOSTS["unknown"]]["last_error"] == "网络超时"
    assert rows[REPOSTS["failed"]]["last_error"] == "Bilibili API error -101: 无权限"
    assert rows[REPOSTS["pending"]]["delete_status"] == "delete_pending"
    # deleted tombstone 绝不能被视为 active 或进入 issue 列表。
    assert REPOSTS["deleted"] not in rows
    assert result["counts"]["deleted"] == 1
    assert get_repost(UID, REPOSTS["deleted"]).delete_status == "deleted"

    texts = " ".join(check["text"] for check in result["checks"])
    assert "不会自动再次删除" in texts
    assert "不会在后台自动重试" in texts
    assert "未完成删除操作" in texts


def test_health_issue_list_is_bounded(isolated_home: Path) -> None:
    for index in range(25):
        repost_id = f"20000000000000000{index + 10:02d}"
        original_id = f"10000000000000000{index + 10:02d}"
        _seed_row(UID, repost_id, original_id, at=index + 1)
        claim_delete_pending(UID, repost_id, requested_at=index + 2)
    result = _health()
    assert result["issue_total"] == 25
    assert len(result["issues"]) == 20


def test_stale_pending_is_warn_but_pending_during_job_is_info(
    isolated_home: Path,
) -> None:
    _seed_row(UID, REPOSTS["pending"], ORIGINAL, at=100)
    claim_delete_pending(UID, REPOSTS["pending"], requested_at=200)

    result = _health(delete_job_in_progress=False)
    texts = " ".join(check["text"] for check in result["checks"])
    assert "需要人工检查" in texts
    assert "不会自动恢复" in texts
    assert result["status"] == "review_needed"

    running = _health(delete_job_in_progress=True)
    running_texts = " ".join(check["text"] for check in running["checks"])
    assert "正在运行" in running_texts
    check = next(c for c in running["checks"] if c["key"] == "delete_pending")
    assert check["tone"] == "info"


def test_health_does_not_repair_or_redelete(isolated_home: Path) -> None:
    _seed_issue_statuses()
    before = {
        "unknown": get_repost(UID, REPOSTS["unknown"]),
        "failed": get_repost(UID, REPOSTS["failed"]),
        "pending": get_repost(UID, REPOSTS["pending"]),
    }
    _health()
    for key, record in before.items():
        after = get_repost(UID, REPOSTS[key])
        assert after.delete_status == record.delete_status
        assert after.updated_at == record.updated_at


def test_health_is_uid_scoped_and_profile_safe(isolated_home: Path) -> None:
    _seed_issue_statuses()
    other = _health(OTHER_UID)
    assert other["counts"]["all_total"] == 0
    assert other["issue_total"] == 0
    assert other["status"] == "normal"
    assert other["runtime"]["uid"] == OTHER_UID


def test_health_runtime_profile_switch_is_info(isolated_home: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "account-1")
    monkeypatch.setattr("src.repost_cleanup.get_selected_profile_id", lambda: "account-2")
    result = _health()
    assert result["runtime"]["selected_profile_id"] == "account-2"
    assert result["status"] == "normal"
    keys = {check["key"] for check in result["checks"]}
    assert "profile_switch_pending" in keys


def test_health_runtime_identity_mismatch_stops_with_error(
    isolated_home: Path, monkeypatch
) -> None:
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "account-1")
    monkeypatch.setattr("src.repost_cleanup.get_selected_profile_id", lambda: "account-1")
    monkeypatch.setattr(
        "src.profile_manager.get_profile_metadata",
        lambda profile_id: {"profile_id": profile_id, "mid": "999999", "nickname": ""},
    )
    result = _health()
    assert result["status"] == "runtime_error"
    assert result["runtime"]["consistent"] is False
    texts = " ".join(check["text"] for check in result["checks"])
    assert "不一致" in texts


def test_health_runtime_identity_match_is_ok(isolated_home: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "account-1")
    monkeypatch.setattr("src.repost_cleanup.get_selected_profile_id", lambda: "account-1")
    monkeypatch.setattr(
        "src.profile_manager.get_profile_metadata",
        lambda profile_id: {"profile_id": profile_id, "mid": UID, "nickname": ""},
    )
    result = _health()
    assert result["runtime"]["consistent"] is True
    assert result["status"] == "normal"


def test_health_after_pending_only_is_review_needed_but_not_recovered(isolated_home: Path) -> None:
    _seed_row(UID, REPOSTS["pending"], ORIGINAL, at=100)
    claim_delete_pending(UID, REPOSTS["pending"], requested_at=200)
    # 模拟进程重启：pending 状态必须保留且被诊断，不能恢复成 active。
    reset_engine_for_tests()
    result = _health()
    assert result["status"] == "review_needed"
    assert get_repost(UID, REPOSTS["pending"]).delete_status == "delete_pending"
    assert get_checkpoint(UID) is None or get_checkpoint(UID).sync_needed is False


def test_unknown_survives_restart_and_health_keeps_it(isolated_home: Path) -> None:
    _seed_row(UID, REPOSTS["unknown"], ORIGINAL, at=100)
    claim_delete_pending(UID, REPOSTS["unknown"], requested_at=200)
    mark_delete_result(UID, REPOSTS["unknown"], status="unknown", error="结果未知", updated_at=200)
    reset_engine_for_tests()
    record = get_repost(UID, REPOSTS["unknown"])
    assert record.delete_status == "unknown"
    result = _health()
    texts = " ".join(check["text"] for check in result["checks"])
    assert "不会自动再次删除" in texts


@pytest.mark.parametrize("code", [-352, -509, 429])
def test_health_reason_keeps_code_text(isolated_home: Path, code: int) -> None:
    pause_maintenance_risk(UID, f"自动维护命中平台风控（{code}）已暂停，需人工恢复", at=100)
    result = _health()
    assert result["status"] == "risk_paused"
    assert str(code) in str(result["maintenance"]["risk_reason"])
