"""V80 unified repost history: every owned repost shares one assessment pipeline."""

from __future__ import annotations

from src.pipeline.classify_step import ClassifyOutcome
from src.repost_cleanup import scan_expired_reposts, sync_repost_history
from src.repost_history import list_repost_assessments, list_repost_history


UID = "12345"


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _feed_item(index: int) -> dict:
    return {
        "type": "DYNAMIC_TYPE_FORWARD",
        "id_str": f"2{index:018d}",
        "modules": {
            "module_author": {"mid": UID, "name": "me", "pub_ts": 1_700_000_000 + index},
        },
        "orig": {
            "id_str": f"1{index:018d}",
            "modules": {"module_author": {"mid": "999", "name": "author"}},
        },
    }


def _login(monkeypatch) -> None:
    monkeypatch.setattr("src.repost_cleanup.require_login", lambda: ("csrf", int(UID)))
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf", int(UID)),
    )


def test_sync_imports_owned_reposts_without_activity_association(isolated_home, monkeypatch) -> None:
    """Space history is the source of truth; local activities are not an import filter."""
    _login(monkeypatch)
    monkeypatch.setattr("src.repost_cleanup.PAGE_REQUEST_DELAY", 0)
    monkeypatch.setattr("src.repost_cleanup.fetch_space_feed_page", lambda *args, **kwargs: {
        "items": [_feed_item(1), _feed_item(2)],
        "offset": "",
    })
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])

    result = sync_repost_history(client_factory=_FakeClient, enforce_request_budget=False)

    assert result["sync_complete"] is True
    records, total = list_repost_history(UID, page_size=200)
    assert total == 2
    assert {record.original_dynamic_id for record in records} == {"1000000000000000001", "1000000000000000002"}


def test_assessment_uses_same_pipeline_for_activity_and_unassociated_history(
    isolated_home, monkeypatch
) -> None:
    """An unassociated original is classified, never silently dropped or marked safe."""
    from src.repost_history import RepostImportRecord, upsert_repost_records

    upsert_repost_records(
        UID,
        [
            RepostImportRecord("2000000000000000001", "1000000000000000001", 100),
            RepostImportRecord("2000000000000000002", "1000000000000000002", 101),
        ],
        seen_at=100,
    )
    _login(monkeypatch)
    monkeypatch.setattr(
        "src.repost_cleanup.load_activities",
        lambda: [{
            "dynamic_id": "1000000000000000001",
            "lottery_type": "转发抽奖",
            "status_classified": True,
        }],
    )
    classified: list[str] = []

    def classify(client, original_id):
        classified.append(original_id)
        return ClassifyOutcome(original_id, "非抽奖活动", True, "普通动态"), None

    monkeypatch.setattr("src.repost_cleanup.classify_for_cleanup", classify)

    result = scan_expired_reposts(
        now_ts=1_800_000_000,
        client_factory=_FakeClient,
        enforce_request_budget=False,
    )

    assessments = list_repost_assessments(UID)
    assert result["evaluated_originals"] == 2
    assert set(assessments) == {"1000000000000000001", "1000000000000000002"}
    assert classified == ["1000000000000000002"]
    assert assessments["1000000000000000001"].classification_source == "activities"
    assert assessments["1000000000000000002"].classification_source == "public_classifier"
    assert assessments["1000000000000000002"].assessment_level == "excluded"
