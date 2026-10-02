from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from src.canonical_identity import CanonicalIdentity
from src.pipeline import refresh_all_pipeline as pipeline
from src.pipeline.refresh_all_pipeline import run_new_links_pipeline
from src.manual_ignored_activities import (
    ignore_activity,
    ignore_stored_activity,
    manual_ignore_ids,
)
from web.user_messages import format_participation_log


CANONICAL = "1220000000000000001"
WRAPPER = "1220000000000000002"


def test_manual_ignore_persists_canonical_and_wrapper_aliases(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "manual_ignored_activities.json"
    monkeypatch.setattr("src.manual_ignored_activities._path", lambda: path)
    monkeypatch.setattr(
        "src.manual_ignored_activities.load_activities",
        lambda: [{
            "dynamic_id": CANONICAL,
            "observed_wrapper_dynamic_id": WRAPPER,
        }],
    )
    removed: list[set[str]] = []
    monkeypatch.setattr(
        "src.manual_ignored_activities.remove_activity_ids",
        lambda ids: removed.append(set(ids)) or len(ids),
    )

    assert ignore_stored_activity(WRAPPER) == {CANONICAL, WRAPPER}
    assert manual_ignore_ids() == {CANONICAL, WRAPPER}
    assert removed == [{CANONICAL, WRAPPER}]

    # Repeating the action is idempotent and retains the same scope.
    assert ignore_activity(CANONICAL, aliases=[WRAPPER]) == {CANONICAL, WRAPPER}
    assert manual_ignore_ids() == {CANONICAL, WRAPPER}


def test_pipeline_skips_ignored_canonical_before_bilibili_or_llm(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "manual_ignored_activities.json"
    monkeypatch.setattr("src.manual_ignored_activities._path", lambda: path)
    monkeypatch.setattr("src.manual_ignored_activities.remove_activity_ids", lambda _ids: 0)
    ignore_activity(CANONICAL, aliases=[WRAPPER])

    identity = CanonicalIdentity(
        raw_dynamic_id=WRAPPER,
        canonical_dynamic_id=CANONICAL,
        wrapper_author_uid="9",
        canonical_author_uid="10",
        is_forward=True,
    )
    called = {"classify": 0, "client": 0}

    def should_not_classify(*_args, **_kwargs):
        called["classify"] += 1
        raise AssertionError("ignored activity must not reach classification")

    monkeypatch.setattr(pipeline, "classify_new_link", should_not_classify)
    monkeypatch.setattr(pipeline, "BilibiliClient", lambda: called.__setitem__("client", 1))
    monkeypatch.setattr(pipeline, "known_activity_ids", lambda: set())

    result = run_new_links_pipeline(
        [f"https://www.bilibili.com/opus/{WRAPPER}"],
        workers=1,
        identity_by_raw={WRAPPER: identity},
    )
    assert result.persisted_count == 0
    assert result.skip_reasons == {"manual_ignore": 1}
    assert called == {"classify": 0, "client": 0}


def test_comment_unknown_message_is_specific_but_other_mutations_keep_legacy() -> None:
    comment = format_participation_log(
        {"context_snapshot": {"result_class": "unknown", "unknown_action": "comment"}}
    )
    repost = format_participation_log(
        {"context_snapshot": {"result_class": "unknown", "unknown_action": "repost"}}
    )
    assert comment == "评论结果未知，建议手动确认并参与。"
    assert repost == "转发结果未知，已停止自动重试"


def test_activity_delete_api_is_local_and_single_activity_scoped() -> None:
    from fastapi.testclient import TestClient
    from web.app import app

    with patch(
        "src.manual_ignored_activities.ignore_stored_activity",
        return_value={CANONICAL, WRAPPER},
    ) as ignore_mock:
        response = TestClient(app).delete(f"/api/activities/{WRAPPER}")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "ignored_ids": sorted({CANONICAL, WRAPPER}),
    }
    ignore_mock.assert_called_once_with(WRAPPER)
