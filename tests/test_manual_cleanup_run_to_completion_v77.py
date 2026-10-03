"""V77 manual cleanup: no routine request-window stop, durable safe resume."""

from __future__ import annotations

import pytest

from src.bilibili_request_budget import (
    RequestBudgetExhausted,
    bilibili_request_budget_scope,
    get_bilibili_request_budget,
    reserve_bilibili_http_attempt,
)
from src.repost_cleanup import CandidateAssessment, scan_expired_reposts, sync_repost_history
from src.repost_history import (
    RepostImportRecord,
    get_checkpoint,
    get_repost,
    list_repost_assessments,
    mark_sync_needed,
    save_checkpoint,
    upsert_repost_records,
)


UID = "12345"
DETAIL_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id=1"
FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"


def _original(index: int) -> str:
    return f"1{index:018d}"


def _repost(index: int) -> str:
    return f"2{index:018d}"


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _login(monkeypatch) -> None:
    monkeypatch.setattr("src.repost_cleanup.require_login", lambda: ("csrf", int(UID)))
    monkeypatch.setattr("src.repost_cleanup._require_verified_login", lambda client: ("csrf", int(UID)))
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])
    monkeypatch.setattr("src.repost_cleanup._record_global_platform_risk", lambda *args: None)
    monkeypatch.setattr("src.repost_cleanup.PAGE_REQUEST_DELAY", 0)


def _seed(count: int) -> None:
    upsert_repost_records(
        UID,
        [
            RepostImportRecord(
                repost_dynamic_id=_repost(index),
                original_dynamic_id=_original(index),
                reposted_at=100 + index,
                source="history_import",
            )
            for index in range(1, count + 1)
        ],
        seen_at=100,
    )


def _feed_item(index: int) -> dict:
    return {
        "type": "DYNAMIC_TYPE_FORWARD",
        "id_str": _repost(index),
        "modules": {"module_author": {"mid": UID, "name": "me", "pub_ts": 10000 - index}},
        "orig": {"id_str": _original(index), "modules": {"module_author": {"mid": "999"}}},
    }


def test_unmetered_manual_scope_keeps_other_protected_budget(tmp_path) -> None:
    with bilibili_request_budget_scope(limit=1, profile_dir=tmp_path, enforce_limit=False) as scope:
        for _ in range(250):
            reserve_bilibili_http_attempt(DETAIL_URL)
        assert scope.total_attempts == 250
        assert scope.result_fields(exhausted=False, partial=False)["budget_exhausted"] is False
    assert get_bilibili_request_budget(limit=1, profile_dir=tmp_path).used_requests == 0

    with bilibili_request_budget_scope(limit=1, profile_dir=tmp_path):
        reserve_bilibili_http_attempt(DETAIL_URL)
        with pytest.raises(RequestBudgetExhausted):
            reserve_bilibili_http_attempt(DETAIL_URL)


def test_manual_assessment_continues_past_120_and_persists(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    _seed(250)
    calls: list[str] = []

    def assess(client, *, original_id, uid, now_ts, activities_map):
        reserve_bilibili_http_attempt(DETAIL_URL)
        calls.append(original_id)
        return CandidateAssessment("manual_review", "已确认历史抽奖", "forward_lottery_manual")

    monkeypatch.setattr("src.repost_cleanup._assess_original", assess)
    result = scan_expired_reposts(
        client_factory=_FakeClient, enforce_request_budget=False, now_ts=1_800_000_000
    )
    assert result["evaluated_originals"] == 250
    assert result["pending_originals"] == 0
    assert result["request_attempts"] == 250
    assert result["budget_exhausted"] is False
    assert len(calls) == 250
    assert len(list_repost_assessments(UID)) == 250


@pytest.mark.parametrize("risk_code", [-352, -509, 429])
def test_manual_assessment_risk_stops_and_next_run_skips_completed(
    isolated_home, monkeypatch, risk_code
) -> None:
    _login(monkeypatch)
    _seed(152)
    calls: list[str] = []
    failing = True

    def assess(client, *, original_id, uid, now_ts, activities_map):
        nonlocal failing
        reserve_bilibili_http_attempt(DETAIL_URL)
        calls.append(original_id)
        if failing and original_id == _original(151):
            raise RuntimeError(f"API error {risk_code}: 风控")
        return CandidateAssessment("manual_review", "已确认历史抽奖", "forward_lottery_manual")

    monkeypatch.setattr("src.repost_cleanup._assess_original", assess)
    first = scan_expired_reposts(
        client_factory=_FakeClient, enforce_request_budget=False, now_ts=1_800_000_000
    )
    assert first["rate_limited"] is True
    assert first["risk_code"] == risk_code
    assert first["evaluated_originals"] == 150
    assert len(calls) == 151
    assert len(list_repost_assessments(UID)) == 150

    # New service invocation reads the same durable Profile DB (restart-equivalent).
    failing = False
    second = scan_expired_reposts(
        client_factory=_FakeClient, enforce_request_budget=False, now_ts=1_800_000_001
    )
    assert second["evaluated_originals"] == 2
    assert calls[151:] == [_original(151), _original(152)]
    assert len(list_repost_assessments(UID)) == 152


def test_manual_sync_continues_past_120_pages(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    seen: list[int] = []

    def page(client, *, mid, offset, retries):
        index = int(offset[1:]) if offset else 1
        seen.append(index)
        reserve_bilibili_http_attempt(FEED_URL)
        return {
            "items": [_feed_item(index)],
            "offset": f"p{index + 1}" if index < 125 else "",
        }

    monkeypatch.setattr("src.repost_cleanup.fetch_space_feed_page", page)
    result = sync_repost_history(client_factory=_FakeClient, enforce_request_budget=False)
    assert result["sync_complete"] is True
    assert result["scanned_pages"] == 125
    assert result["found_reposts"] == 125
    assert result["request_attempts"] == 125
    assert result["budget_exhausted"] is False
    assert seen == list(range(1, 126))
    assert get_checkpoint(UID).resume_offset is None


def test_v11_checkpoint_cursor_migration_is_additive_and_repeatable(tmp_path) -> None:
    from sqlalchemy import create_engine
    from sqlmodel import Session

    from src.db.schema import migrate_v11_to_v12

    engine = create_engine(f"sqlite:///{(tmp_path / 'v11.db').as_posix()}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE repost_sync_checkpoint ("
            "uid VARCHAR(64) NOT NULL PRIMARY KEY, head_dynamic_id VARCHAR(32), "
            "head_published_at INTEGER, full_scan_completed BOOLEAN NOT NULL, "
            "last_synced_at INTEGER, updated_at INTEGER NOT NULL, "
            "sync_needed BOOLEAN NOT NULL DEFAULT 0, sync_needed_at INTEGER, "
            "maintenance_risk_paused BOOLEAN NOT NULL DEFAULT 0, "
            "maintenance_risk_paused_at INTEGER, maintenance_risk_reason TEXT, "
            "CONSTRAINT ck_repost_sync_checkpoint_completed CHECK (full_scan_completed IN (0,1)))"
        )
        conn.exec_driver_sql(
            "INSERT INTO repost_sync_checkpoint(uid,head_dynamic_id,head_published_at,"
            "full_scan_completed,last_synced_at,updated_at) VALUES ('12345','98765',1,1,2,3)"
        )
        with Session(bind=conn) as session:
            migrate_v11_to_v12(session)
            migrate_v11_to_v12(session)
        names = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(repost_sync_checkpoint)")}
        assert {"resume_offset", "resume_head_dynamic_id", "resume_head_published_at", "resume_started_at"} <= names
        assert conn.exec_driver_sql(
            "SELECT head_dynamic_id,full_scan_completed,last_synced_at "
            "FROM repost_sync_checkpoint WHERE uid='12345'"
        ).one() == ("98765", 1, 2)


@pytest.mark.parametrize("risk_code", [-352, -509, 429])
def test_manual_sync_risk_resume_starts_at_unfinished_page(
    isolated_home, monkeypatch, risk_code
) -> None:
    _login(monkeypatch)
    old_head = _repost(999)
    save_checkpoint(
        UID, head_dynamic_id=old_head, head_published_at=1,
        full_scan_completed=True, last_synced_at=60,
    )
    seen: list[int] = []
    failing = True

    def page(client, *, mid, offset, retries):
        index = int(offset[1:]) if offset else 1
        seen.append(index)
        reserve_bilibili_http_attempt(FEED_URL)
        if failing and index == 151:
            raise RuntimeError(f"API error {risk_code}: 风控")
        if index == 251:
            return {"items": [{"type": "DYNAMIC_TYPE_NOTE", "id_str": old_head,
                              "modules": {"module_author": {"mid": UID, "pub_ts": 1}}}], "offset": ""}
        return {"items": [_feed_item(index)], "offset": f"p{index + 1}"}

    monkeypatch.setattr("src.repost_cleanup.fetch_space_feed_page", page)
    with pytest.raises(RuntimeError, match=str(risk_code)):
        sync_repost_history(client_factory=_FakeClient, enforce_request_budget=False)
    assert seen == list(range(1, 152))
    assert get_checkpoint(UID).resume_offset == "p151"
    assert get_checkpoint(UID).resume_head_dynamic_id == _repost(1)
    assert get_checkpoint(UID).head_dynamic_id == old_head

    failing = False
    seen.clear()
    result = sync_repost_history(client_factory=_FakeClient, enforce_request_budget=False)
    assert result["sync_complete"] is True
    assert seen == list(range(151, 252))
    assert get_checkpoint(UID).resume_offset is None
    assert get_checkpoint(UID).head_dynamic_id == _repost(1)


def test_resume_does_not_clear_newer_sync_needed_marker(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    old_head = _repost(999)
    save_checkpoint(
        UID, head_dynamic_id=old_head, head_published_at=1,
        full_scan_completed=True, last_synced_at=60,
    )
    seen: list[str] = []

    def page(client, *, mid, offset, retries):
        seen.append(offset)
        if not offset:
            return {"items": [_feed_item(1)], "offset": "p2"}
        return {"items": [{"type": "DYNAMIC_TYPE_NOTE", "id_str": old_head,
                          "modules": {"module_author": {"mid": UID, "pub_ts": 1}}}], "offset": ""}

    monkeypatch.setattr("src.repost_cleanup.fetch_space_feed_page", page)
    from src.repost_history import save_sync_progress
    import time

    scan_started = int(time.time()) - 100
    save_sync_progress(
        UID, next_offset="p2", newest_id=_repost(1),
        newest_ts=9999, started_at=scan_started,
    )
    mark_sync_needed(UID, occurred_at=scan_started + 10)
    result = sync_repost_history(client_factory=_FakeClient, enforce_request_budget=False)
    assert result["sync_complete"] is True
    assert seen == ["p2"]
    assert get_checkpoint(UID).sync_needed is True


def test_manual_assessment_empty_backlog_never_creates_client(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)

    def forbidden_client():
        raise AssertionError("empty manual backlog must be 0 remote")

    result = scan_expired_reposts(client_factory=forbidden_client, enforce_request_budget=False)
    assert result["evaluated_originals"] == 0
    assert result["request_attempts"] == 0


@pytest.mark.parametrize("risk_code", [-352, -509, 429])
def test_identity_detail_risk_stops_without_false_identity_failure(
    isolated_home, monkeypatch, risk_code
) -> None:
    _login(monkeypatch)
    upsert_repost_records(
        UID,
        [
            RepostImportRecord(
                repost_dynamic_id=_repost(index), original_dynamic_id=_original(index),
                reposted_at=100 + index, source="binggo",
            )
            for index in range(1, 4)
        ],
        seen_at=100,
    )
    checked: list[str] = []
    risks: list[object] = []

    def verify(client, *, uid, repost_dynamic_id, original_dynamic_id):
        checked.append(repost_dynamic_id)
        raise RuntimeError(f"API error {risk_code}: 风控")

    monkeypatch.setattr("src.repost_cleanup._verify_owned_repost_detail", verify)
    monkeypatch.setattr("src.repost_cleanup._record_global_platform_risk", lambda *args: risks.append(args))
    monkeypatch.setattr(
        "src.repost_cleanup._assess_original",
        lambda *args, **kwargs: pytest.fail("risk must stop before assessment"),
    )
    result = scan_expired_reposts(client_factory=_FakeClient, enforce_request_budget=False)
    assert result["rate_limited"] is True
    assert result["risk_code"] == risk_code
    assert len(checked) == 1
    assert len(risks) == 1
    assert get_repost(UID, checked[0]).identity_ok is None


def test_manual_web_entries_disable_only_their_budget(monkeypatch) -> None:
    from web.actions import _run_action_impl

    calls: list[tuple[str, bool]] = []

    def sync(**kwargs):
        calls.append(("sync", kwargs["enforce_request_budget"]))
        return {"sync_complete": True, "found_reposts": 0, "imported_count": 0}

    def assess(**kwargs):
        calls.append(("assess", kwargs["enforce_request_budget"]))
        return {"message": "智能评估完成", "candidates": [], "rate_limited": False}

    monkeypatch.setattr("src.repost_cleanup.sync_repost_history", sync)
    monkeypatch.setattr("src.repost_cleanup.scan_expired_reposts", assess)
    for action in ("sync_repost_history", "scan_expired_reposts", "sync_assess_repost_history"):
        _run_action_impl(action, on_progress=lambda **kwargs: None)
    assert calls == [
        ("sync", False), ("assess", False), ("sync", False), ("assess", False)
    ]


def test_manual_progress_updates_every_item_but_throttles_log_lines() -> None:
    from web.actions import _cleanup_progress_adapter

    events: list[dict] = []
    report = _cleanup_progress_adapter(lambda **event: events.append(event), None, log_stride=10)
    report(0, 25, "开始")
    for index in range(1, 26):
        report(index, 25, f"已评估 {index}/25")
    assert len(events) == 26
    assert [event["step"] for event in events if event["log_append"]] == [0, 10, 20, 25]
