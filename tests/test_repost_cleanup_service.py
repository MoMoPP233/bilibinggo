from __future__ import annotations

import json

from src.repost_cleanup import CandidateAssessment, DELETE_REPOST_URL, delete_reposts
from src.repost_history import RepostAssessmentRecord, RepostHistoryRecord

UID = "123"
REPOST_ID = "1234567890123456789"
ORIGINAL_ID = "2234567890123456789"


def _record() -> RepostHistoryRecord:
    return RepostHistoryRecord(
        uid=UID,
        repost_dynamic_id=REPOST_ID,
        original_dynamic_id=ORIGINAL_ID,
        reposted_at=100,
        original_author_uid="456",
        original_author_name="作者",
        source="history_import",
        delete_status="active",
        delete_requested_at=None,
        deleted_at=None,
        last_seen_at=200,
        last_error=None,
        identity_source="space_feed",
        identity_checked_at=200,
        identity_ok=True,
        updated_at=200,
    )


class _FakeClient:
    def __init__(self) -> None:
        self.post_json_calls: list[dict] = []

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post_json(
        self,
        url: str,
        payload: dict,
        *,
        params: dict | None = None,
        referer: str | None = None,
        retries: int = 3,
        raise_on_code: bool = True,
    ) -> dict:
        self.post_json_calls.append(
            {
                "url": url,
                "payload": payload,
                "params": params,
                "referer": referer,
                "retries": retries,
                "raise_on_code": raise_on_code,
            }
        )
        return {"code": 0}


class _FakeDbPath:
    def resolve(self) -> str:
        return "db-1"


def test_delete_reposts_sends_json_body_with_query_csrf_and_zero_retry(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assert_frozen_runtime",
        lambda *, profile_id, database_path, uid: "csrf-token",
    )
    monkeypatch.setattr(
        "src.repost_cleanup.get_runtime_profile_id",
        lambda: "profile-1",
    )
    monkeypatch.setattr("src.repost_cleanup.db_path", lambda: _FakeDbPath())
    monkeypatch.setattr(
        "src.repost_cleanup.load_activities",
        lambda: [{"dynamic_id": ORIGINAL_ID, "lottery_type": "互动抽奖", "status_classified": True}],
    )
    monkeypatch.setattr(
        "src.repost_cleanup.get_repost",
        lambda uid, repost_id: _record(),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._verify_owned_repost_detail",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "src.repost_cleanup.list_repost_assessments",
        lambda uid: {
            ORIGINAL_ID: RepostAssessmentRecord(
                uid=uid,
                original_dynamic_id=ORIGINAL_ID,
                assessment_level="safe",
                reason_code="safe_official_notice",
                assessed_at=100,
            )
        },
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assess_original",
        lambda *args, **kwargs: CandidateAssessment(
            "safe", "ok", "safe_official_notice", "互动抽奖", 100, 100
        ),
    )
    monkeypatch.setattr(
        "src.repost_cleanup.claim_delete_pending",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "src.repost_cleanup.mark_delete_result",
        lambda *args, **kwargs: True,
    )

    fake = _FakeClient()
    result = delete_reposts([REPOST_ID], client_factory=lambda: fake)

    assert result["deleted_count"] == 1
    assert len(fake.post_json_calls) == 1
    call = fake.post_json_calls[0]
    assert call["url"] == DELETE_REPOST_URL
    assert call["payload"] == {"dyn_id_str": REPOST_ID}
    assert call["params"] == {"platform": "web", "csrf": "csrf-token"}
    assert call["retries"] == 0
    assert call["raise_on_code"] is False
    assert "original_dynamic_id" not in call["payload"]
    assert ORIGINAL_ID not in json.dumps(call["payload"])


def test_manual_review_delete_requires_extra_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assert_frozen_runtime",
        lambda *, profile_id, database_path, uid: "csrf-token",
    )
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "profile-1")
    monkeypatch.setattr("src.repost_cleanup.db_path", lambda: _FakeDbPath())
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])
    monkeypatch.setattr("src.repost_cleanup.get_repost", lambda uid, repost_id: _record())
    monkeypatch.setattr(
        "src.repost_cleanup._verify_owned_repost_detail",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "src.repost_cleanup.list_repost_assessments",
        lambda uid: {
            ORIGINAL_ID: RepostAssessmentRecord(
                uid=uid,
                original_dynamic_id=ORIGINAL_ID,
                assessment_level="manual_review",
                reason_code="forward_lottery_manual",
                assessed_at=100,
            )
        },
    )
    monkeypatch.setattr("src.repost_cleanup.claim_delete_pending", lambda *args, **kwargs: True)
    monkeypatch.setattr("src.repost_cleanup.mark_delete_result", lambda *args, **kwargs: True)

    fake = _FakeClient()
    unconfirmed = delete_reposts([REPOST_ID], client_factory=lambda: fake)
    assert unconfirmed["skipped_count"] == 1
    assert fake.post_json_calls == []

    confirmed = delete_reposts(
        [REPOST_ID],
        client_factory=lambda: fake,
        manual_review_confirmed=True,
    )
    assert confirmed["deleted_count"] == 1
    assert len(fake.post_json_calls) == 1


def test_blocked_and_unassessed_delete_are_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assert_frozen_runtime",
        lambda *, profile_id, database_path, uid: "csrf-token",
    )
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "profile-1")
    monkeypatch.setattr("src.repost_cleanup.db_path", lambda: _FakeDbPath())
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])
    monkeypatch.setattr("src.repost_cleanup.get_repost", lambda uid, repost_id: _record())
    monkeypatch.setattr(
        "src.repost_cleanup._verify_owned_repost_detail",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "src.repost_cleanup.list_repost_assessments",
        lambda uid: {
            ORIGINAL_ID: RepostAssessmentRecord(
                uid=uid,
                original_dynamic_id=ORIGINAL_ID,
                assessment_level="blocked",
                reason_code="identity_conflict",
                assessed_at=100,
            )
        },
    )
    monkeypatch.setattr("src.repost_cleanup.claim_delete_pending", lambda *args, **kwargs: True)
    monkeypatch.setattr("src.repost_cleanup.mark_delete_result", lambda *args, **kwargs: True)

    fake = _FakeClient()
    result = delete_reposts([REPOST_ID], client_factory=lambda: fake)
    assert result["skipped_count"] == 1
    assert fake.post_json_calls == []


def test_delete_batch_stops_on_rate_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assert_frozen_runtime",
        lambda *, profile_id, database_path, uid: "csrf-token",
    )
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "profile-1")
    monkeypatch.setattr("src.repost_cleanup.db_path", lambda: _FakeDbPath())
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])
    monkeypatch.setattr(
        "src.repost_cleanup.get_repost",
        lambda uid, repost_id: _record(),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._verify_owned_repost_detail",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "src.repost_cleanup.list_repost_assessments",
        lambda uid: {
            ORIGINAL_ID: RepostAssessmentRecord(
                uid=uid,
                original_dynamic_id=ORIGINAL_ID,
                assessment_level="safe",
                assessed_at=100,
            )
        },
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assess_original",
        lambda *args, **kwargs: CandidateAssessment(
            "safe", "ok", "safe_official_notice", "互动抽奖", 100, 100
        ),
    )
    monkeypatch.setattr("src.repost_cleanup.claim_delete_pending", lambda *args, **kwargs: True)
    monkeypatch.setattr("src.repost_cleanup.mark_delete_result", lambda *args, **kwargs: True)

    class _RateLimitedClient(_FakeClient):
        def post_json(self, *args, **kwargs):
            self.post_json_calls.append({"payload": kwargs.get("payload")})
            return {"code": -352, "message": "风控校验失败"}

    fake = _RateLimitedClient()
    second_id = "1234567890123456790"
    result = delete_reposts([REPOST_ID, second_id], client_factory=lambda: fake)

    assert result["rate_limited"] is True
    assert len(fake.post_json_calls) == 1
    assert result["requested_count"] == 2


def test_post_json_uses_http_post_with_json_body_and_content_type(monkeypatch) -> None:
    from src.bilibili_client import BilibiliClient

    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"code": 0}

    client = BilibiliClient(warmup=False)

    def fake_post(url, *, params=None, data=None, json=None, headers=None):
        captured.update(url=url, params=params, data=data, json=json, headers=headers)
        return _Resp()

    monkeypatch.setattr(client._client, "post", fake_post)
    monkeypatch.setattr("src.bilibili_client.acquire_bilibili_request_slot", lambda: None)

    client.post_json(
        DELETE_REPOST_URL,
        {"dyn_id_str": REPOST_ID},
        params={"platform": "web", "csrf": "csrf-token"},
        referer="https://space.bilibili.com/123/dynamic",
        retries=0,
        raise_on_code=False,
    )
    client.close()

    assert captured["url"] == DELETE_REPOST_URL
    assert captured["data"] is None
    assert captured["json"] == {"dyn_id_str": REPOST_ID}
    assert captured["params"] == {"platform": "web", "csrf": "csrf-token"}
    assert captured["headers"]["Content-Type"] == "application/json"
