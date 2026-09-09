"""M6：strict-safe 自动清理 + 每批20连续批处理（全离线，零真实 DELETE/网络）。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.pipeline.classify_step import ClassifyOutcome
from src.repost_cleanup import (
    auto_cleanup_delete,
    delete_reposts,
    get_auto_delete_enabled,
    set_auto_delete_enabled,
)
from src.repost_history import (
    RepostImportRecord,
    get_repost,
    list_repost_assessments,
    upsert_repost_assessment,
    upsert_repost_records,
)

UID = "12345"


class _FakeClient:
    def __init__(self, delete_payload=None, delete_error=None):
        self.delete_payload = delete_payload
        self.delete_error = delete_error
        self.deleted: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post_json(self, url, payload, *, params=None, referer=None, retries=3, raise_on_code=True):
        assert url.endswith("/operate/remove")
        assert retries == 0  # DELETE 禁止 retry
        self.deleted.append(str(payload.get("dyn_id_str") or ""))
        if self.delete_error is not None:
            raise self.delete_error
        return dict(self.delete_payload or {"code": 0})


def _login(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])
    monkeypatch.setattr("src.repost_cleanup.get_guard", lambda uid, original: None)
    monkeypatch.setattr("src.repost_cleanup._defer_state", lambda record, stored: None)


def _seed(uid: str, n: int, monkeypatch) -> list[str]:
    _login(monkeypatch)
    ids = [f"200000000000000000{i:02d}" for i in range(1, n + 1)]
    originals = [f"100000000000000000{i:02d}" for i in range(1, n + 1)]
    upsert_repost_records(
        uid,
        [
            RepostImportRecord(
                repost_dynamic_id=rid,
                original_dynamic_id=original,
                reposted_at=100 + i,
                source="history_import",
            )
            for i, (rid, original) in enumerate(zip(ids, originals), 1)
        ],
        seen_at=100,
    )
    for original in originals:
        upsert_repost_assessment(
            uid,
            original,
            assessment_level="safe",
            assessment_status="final",
            reason_code="safe_official_notice",
            lottery_type="互动抽奖",
            lottery_time=1_600_000_000,
            lottery_time_reliable=True,
            eligible_after=1_500_000_000,
            assessed_at=1_700_000_000,
        )
    return ids


def _identity_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup._verify_owned_repost_detail", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assert_frozen_runtime", lambda **k: "csrf-token"
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assess_original",
        lambda client, *, original_id, uid, now_ts, activities_map: SimpleNamespace(
            level="safe", reason_code="safe_official_notice"
        ),
    )


def _run(monkeypatch, client, now_ts=1_800_000_000):
    return auto_cleanup_delete(
        now_ts=now_ts,
        client_factory=lambda: client,
    )


@pytest.fixture(autouse=True)
def _db(isolated_home):
    return isolated_home


def test_setting_default_off_and_profile_scoped(monkeypatch) -> None:
    assert get_auto_delete_enabled() is False
    set_auto_delete_enabled(True)
    assert get_auto_delete_enabled() is True


def test_disabled_engine_performs_zero_delete(monkeypatch) -> None:
    set_auto_delete_enabled(False)
    ids = _seed(UID, 3, monkeypatch)
    client = _FakeClient()
    result = _run(monkeypatch, client)
    assert result["deleted_count"] == 0
    assert client.deleted == []


def test_zero_safe_zero_delete(monkeypatch) -> None:
    set_auto_delete_enabled(True)
    _seed(UID, 5, monkeypatch)
    # 全部改为 non-safe（level excluded）
    for original in [f"100000000000000000{i:02d}" for i in range(1, 6)]:
        upsert_repost_assessment(
            UID, original, assessment_level="excluded", assessment_status="final",
            reason_code="non_lottery", assessed_at=1,
        )
    client = _FakeClient()
    result = _run(monkeypatch, client)
    assert result["deleted_count"] == 0
    assert client.deleted == []


@pytest.mark.parametrize("n,expected_batches", [
    (1, 1), (20, 1), (21, 2), (40, 2), (41, 3), (87, 5),
])
def test_continuous_batches_single_job(monkeypatch, n, expected_batches) -> None:
    set_auto_delete_enabled(True)
    ids = _seed(UID, n, monkeypatch)
    _identity_ok(monkeypatch)
    client = _FakeClient()
    result = _run(monkeypatch, client)
    assert result["deleted_count"] == n
    assert result["batches_done"] == expected_batches
    assert len(client.deleted) == n
    assert client.deleted == ids


def test_barrier_pending_blocks_all_deletes(monkeypatch) -> None:
    set_auto_delete_enabled(True)
    ids = _seed(UID, 3, monkeypatch)
    _identity_ok(monkeypatch)
    from src.repost_history import claim_delete_pending

    claim_delete_pending(UID, ids[0], requested_at=100)  # 留下 delete_pending
    client = _FakeClient()
    result = _run(monkeypatch, client)
    assert result["deleted_count"] == 0
    assert client.deleted == []
    assert result["stop_reason"] == "BARRIER"


def test_unknown_deletes_stop_job_zero_retry(monkeypatch) -> None:
    set_auto_delete_enabled(True)
    ids = _seed(UID, 3, monkeypatch)
    _identity_ok(monkeypatch)
    client = _FakeClient(delete_error=httpx.ReadTimeout("lost", request=None))
    result = _run(monkeypatch, client)
    assert result["deleted_count"] == 0
    assert len(client.deleted) == 1
    assert client.deleted == [ids[0]]
    # DELETE unknown：整 Job 立即停止，后续候选 0 删除
    assert result["stop_reason"] == "DELETE_UNKNOWN"
    record = get_repost(UID, ids[0])
    assert getattr(record, "delete_status") == "unknown"


def test_business_rejection_stops_job(monkeypatch) -> None:
    set_auto_delete_enabled(True)
    ids = _seed(UID, 3, monkeypatch)
    _identity_ok(monkeypatch)
    client = _FakeClient(delete_payload={"code": 22001, "message": "不允许删除"})
    result = _run(monkeypatch, client)
    assert result["deleted_count"] == 0
    assert result["stop_reason"] == "DELETE_FAILED"
    assert len(client.deleted) == 1
    record = get_repost(UID, ids[0])
    assert getattr(record, "delete_status") == "delete_failed"


@pytest.mark.parametrize("message", [
    "API error -352: 风控校验失败",
    "API error -509: 访问过于频繁",
])
def test_structured_risk_blocks_all_following(monkeypatch, message) -> None:
    set_auto_delete_enabled(True)
    ids = _seed(UID, 3, monkeypatch)
    _identity_ok(monkeypatch)
    client = _FakeClient(delete_payload={"code": int(message.split()[2].split(":")[0]) if message.startswith("API error ") else 0})
    client.delete_payload = {"code": -352 if "-352" in message else -509, "message": "风控"}
    result = _run(monkeypatch, client)
    assert result["rate_limited"] is True
    assert len(client.deleted) == 1
    assert result["deleted_count"] == 0

def test_scheduler_cleanup_cycle_never_starts_auto_delete(isolated_home, monkeypatch) -> None:
    from unittest.mock import MagicMock

    from web.auto_scheduler import AutoScheduler
    import web.actions as actions

    assert "cleanup_auto_delete" in actions.WRITER_LEASE_ACTIONS
    calls: list[str] = []

    def fake(action, *, pipeline_index=None):
        calls.append(action)
        return {"skipped": False}

    scheduler = AutoScheduler(job_runner=MagicMock())
    scheduler._click_and_wait = fake
    # 触发现有维护周期：不得因此启动 DELETE（M6 不隐式接入 Scheduler）
    scheduler._run_cleanup_cycle("cleanup-2026-07-17-06")
    assert "cleanup_auto_delete" not in calls
    assert calls == ["cleanup_maintenance_cycle"]
