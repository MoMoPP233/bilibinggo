from __future__ import annotations

import pytest

from src.lottery_actions import repost_dynamic
from src.participation_guard import (
    confirm_repost,
    get_guard,
    load_blocked_guard_ids,
)
from src.participation_store import get_participation
from src.repost_cleanup import auto_maintain
from src.repost_history import (
    RepostImportRecord,
    get_checkpoint,
    upsert_repost_assessment,
    upsert_repost_records,
)

UID = "12345"
DYNAMIC_ID = "1000000000000000001"


def test_confirm_repost_survives_mark_sync_needed_failure(isolated_home, monkeypatch) -> None:
    def broken_mark(uid, **kwargs):
        raise RuntimeError("no such column: repost_sync_checkpoint.sync_needed")

    monkeypatch.setattr("src.repost_history.mark_sync_needed", broken_mark)

    confirm_repost(UID, DYNAMIC_ID)  # 不应抛错

    assert get_guard(UID, DYNAMIC_ID).repost_status == "confirmed"


@pytest.mark.parametrize(
    "level",
    ["safe", "manual_review", "blocked", "excluded"],
)
def test_cleanup_assessment_does_not_block_participation(isolated_home, level) -> None:
    confirm_repost(UID, DYNAMIC_ID)
    upsert_repost_assessment(
        UID,
        DYNAMIC_ID,
        assessment_level=level,
        assessment_status="final",
        reason_code=f"level_{level}",
        assessed_at=100,
    )

    assert DYNAMIC_ID not in load_blocked_guard_ids(UID)
    assert get_guard(UID, DYNAMIC_ID).repost_status == "confirmed"


def test_confirmed_guard_skips_repost_without_new_request(isolated_home, monkeypatch) -> None:
    confirm_repost(UID, DYNAMIC_ID)
    calls = {"post": 0}

    class _Client:
        def post_form(self, *args, **kwargs):
            calls["post"] += 1
            raise AssertionError("confirmed guard 不应再次发送 repost POST")

    result = repost_dynamic(
        _Client(),
        dynamic_id=DYNAMIC_ID,
        my_uid=int(UID),
        csrf="token",
        referer="https://space.bilibili.com/12345/dynamic",
        content="转发抽奖",
    )

    assert result.ok is True
    assert "已确认转发，跳过" in result.detail
    assert calls["post"] == 0


def test_sync_needed_does_not_change_participation_status(isolated_home) -> None:
    assert get_participation(DYNAMIC_ID, uid=UID) is None

    confirm_repost(UID, DYNAMIC_ID)

    assert get_guard(UID, DYNAMIC_ID).repost_status == "confirmed"
    assert get_checkpoint(UID).sync_needed is True
    assert get_participation(DYNAMIC_ID, uid=UID) is None  # 参与状态未被改动


def test_auto_maintain_manual_review_does_not_affect_activity_api(isolated_home, monkeypatch) -> None:
    upsert_repost_records(
        UID,
        [RepostImportRecord(repost_dynamic_id="2000000000000000001", original_dynamic_id=DYNAMIC_ID, reposted_at=100)],
        seen_at=100,
    )
    monkeypatch.setattr(
        "src.repost_cleanup.require_login",
        lambda: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "profile-1")
    monkeypatch.setattr(
        "src.repost_cleanup.db_path",
        lambda: type("_P", (), {"resolve": lambda self: "db-1"})(),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assess_original",
        lambda *args, **kwargs: _ManualReview(),
    )

    result = auto_maintain(client_factory=lambda: _FakeClient())
    assert result["evaluated_originals"] == 1

    from web.activity_service import _can_participate

    item = {
        "dynamic_id": DYNAMIC_ID,
        "lottery_type": "转发抽奖",
        "skipped": False,
        "draw_status": "active",
    }
    assert _can_participate(item, "未参加") is True


class _ManualReview:
    level = "manual_review"
    reason = "已确认属于历史抽奖，但无法确认中奖/领奖状态"
    reason_code = "forward_lottery_manual"
    lottery_type = "转发抽奖"
    lottery_time = None
    lottery_time_reliable = False
    eligible_after = None
    classification_source = "public_classifier"
    summary = ""
    remote_checked_at = None


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None
