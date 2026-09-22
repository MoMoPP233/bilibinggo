from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.canonical_identity import resolve_dynamic_identity


CURRENT = "1000000000000000001"
FRIEND = "1000000000000000002"
ORIGINAL_AUTHOR = "1000000000000000003"
ORIGINAL = "2000000000000000001"
WRAPPER_A = "2000000000000000002"
WRAPPER_B = "2000000000000000003"


def _item(
    dynamic_id: str,
    author: str,
    *,
    following: bool | None = True,
    item_type: str = "DYNAMIC_TYPE_DRAW",
    orig: dict | None = None,
) -> dict:
    author_payload = {"mid": int(author)}
    if following is not None:
        author_payload["following"] = following
    item = {
        "id_str": dynamic_id,
        "type": item_type,
        "modules": {"module_author": author_payload},
    }
    if orig is not None:
        item["orig"] = orig
    return item


class _FeedClient:
    def __init__(self, items: list[dict]) -> None:
        self.payload = {"code": 0, "data": {"items": items}}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_json(self, *args, **kwargs):
        return self.payload


@pytest.fixture
def feed_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from src import following_feed as ff

    state = tmp_path / "profile" / "following_feed_state.json"
    monkeypatch.setattr(ff, "_state_path", lambda: state)
    return state


def _run_feed(items, monkeypatch, *, uid: int | None):
    from src import following_feed as ff

    captured: dict = {}

    def fake_pipeline(urls, **kwargs):
        captured["urls"] = list(urls)
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            persisted_count=len(urls),
            new_link_count=len(urls),
            skip_reasons={},
            partial=False,
        )

    monkeypatch.setattr(ff, "run_new_links_pipeline", fake_pipeline)
    monkeypatch.setattr(ff, "get_login_uid", lambda: uid)
    result = ff.scan_following_feed(
        client_factory=lambda: _FeedClient(items),
    )
    return result, captured


def test_original_identity_uses_wrapper_id_and_author() -> None:
    identity = resolve_dynamic_identity(_item(ORIGINAL, ORIGINAL_AUTHOR))
    assert identity.raw_dynamic_id == ORIGINAL
    assert identity.canonical_dynamic_id == ORIGINAL
    assert identity.wrapper_author_uid == ORIGINAL_AUTHOR
    assert identity.canonical_author_uid == ORIGINAL_AUTHOR
    assert not identity.is_forward
    assert identity.following is True


def test_forward_identity_uses_original_id_and_author() -> None:
    original = _item(ORIGINAL, ORIGINAL_AUTHOR)
    identity = resolve_dynamic_identity(
        _item(WRAPPER_A, FRIEND, orig=original, item_type="DYNAMIC_TYPE_FORWARD")
    )
    assert identity.raw_dynamic_id == WRAPPER_A
    assert identity.canonical_dynamic_id == ORIGINAL
    assert identity.wrapper_author_uid == FRIEND
    assert identity.canonical_author_uid == ORIGINAL_AUTHOR
    assert identity.canonical_item == original


def test_current_uid_original_is_skipped(feed_state, monkeypatch) -> None:
    from src import following_feed as ff

    result, captured = _run_feed(
        [_item(ORIGINAL, CURRENT)], monkeypatch, uid=int(CURRENT)
    )
    assert captured["urls"] == []
    assert result["self_skipped"] == 1
    assert result["added"] == 0
    assert ff.following_feed_info()["self_skipped"] == 1


def test_current_uid_forward_is_skipped(feed_state, monkeypatch) -> None:
    original = _item(ORIGINAL, ORIGINAL_AUTHOR)
    result, captured = _run_feed(
        [_item(WRAPPER_A, CURRENT, orig=original, item_type="DYNAMIC_TYPE_FORWARD")],
        monkeypatch,
        uid=int(CURRENT),
    )
    assert captured["urls"] == []
    assert result["self_skipped"] == 1


def test_followed_original_is_accepted(feed_state, monkeypatch) -> None:
    result, captured = _run_feed(
        [_item(ORIGINAL, ORIGINAL_AUTHOR)], monkeypatch, uid=int(CURRENT)
    )
    assert captured["urls"] == [f"https://www.bilibili.com/opus/{ORIGINAL}"]
    assert result["accepted"] == 1


def test_any_friend_repost_is_skipped_without_following_lookup(feed_state, monkeypatch) -> None:
    original = _item(ORIGINAL, ORIGINAL_AUTHOR, following=False)
    result, captured = _run_feed(
        [_item(WRAPPER_A, FRIEND, orig=original, item_type="DYNAMIC_TYPE_FORWARD")],
        monkeypatch,
        uid=int(CURRENT),
    )
    assert captured["urls"] == []
    assert result["forward_skipped"] == 1


def test_following_feed_does_not_accept_friend_reposts(feed_state, monkeypatch) -> None:
    original = _item(ORIGINAL, ORIGINAL_AUTHOR, following=True)
    result, captured = _run_feed(
        [_item(WRAPPER_A, FRIEND, orig=original, item_type="DYNAMIC_TYPE_FORWARD")],
        monkeypatch,
        uid=int(CURRENT),
    )
    assert result["added"] == 0
    assert result["forward_skipped"] == 1
    assert captured["urls"] == []


def test_same_original_and_two_wrappers_are_one_feed_candidate(feed_state, monkeypatch) -> None:
    original = _item(ORIGINAL, ORIGINAL_AUTHOR, following=True)
    items = [
        original,
        _item(WRAPPER_A, FRIEND, orig=original, item_type="DYNAMIC_TYPE_FORWARD"),
        _item(WRAPPER_B, FRIEND, orig=original, item_type="DYNAMIC_TYPE_FORWARD"),
    ]
    result, captured = _run_feed(items, monkeypatch, uid=int(CURRENT))
    assert captured["urls"] == [f"https://www.bilibili.com/opus/{ORIGINAL}"]
    assert result["canonical_duplicate_skipped"] == 0
    assert result["forward_skipped"] == 2


def test_following_feed_uses_one_feed_request_for_many_authors(feed_state, monkeypatch) -> None:
    requests = 0
    items = [
        _item(ORIGINAL, ORIGINAL_AUTHOR, following=None),
        _item(
            WRAPPER_A,
            FRIEND,
            orig=_item(ORIGINAL, ORIGINAL_AUTHOR, following=None),
            item_type="DYNAMIC_TYPE_FORWARD",
        ),
    ]

    from src import following_feed as ff

    captured: dict = {}

    def fake_pipeline(urls, **kwargs):
        captured["urls"] = list(urls)
        return SimpleNamespace(
            persisted_count=len(urls),
            new_link_count=len(urls),
            skip_reasons={},
            partial=False,
        )

    monkeypatch.setattr(ff, "run_new_links_pipeline", fake_pipeline)
    monkeypatch.setattr(ff, "get_login_uid", lambda: int(CURRENT))
    client = _FeedClient(items)

    def factory():
        nonlocal requests
        requests += 1
        return client

    result = ff.scan_following_feed(
        client_factory=factory
    )
    assert requests == 1
    assert result["forward_skipped"] == 1
    assert len(captured["urls"]) == 1


def test_malformed_forward_is_fail_closed_but_later_item_continues(feed_state, monkeypatch) -> None:
    malformed = _item(WRAPPER_A, FRIEND, item_type="DYNAMIC_TYPE_FORWARD", orig=None)
    result, captured = _run_feed(
        [malformed, _item(ORIGINAL, ORIGINAL_AUTHOR)],
        monkeypatch,
        uid=int(CURRENT),
    )
    assert result["forward_unresolved_skipped"] == 1
    assert result["forward_skipped"] == 1
    assert captured["urls"] == [f"https://www.bilibili.com/opus/{ORIGINAL}"]


def test_pipeline_canonicalizes_cross_source_wrappers(monkeypatch) -> None:
    from src import participation_guard
    from src.pipeline import refresh_all_pipeline as pipeline
    from src.pipeline.classify_step import ClassifyOutcome

    original = _item(ORIGINAL, ORIGINAL_AUTHOR, following=True)
    wrapper_a = _item(WRAPPER_A, FRIEND, orig=original, item_type="DYNAMIC_TYPE_FORWARD")
    wrapper_b = _item(WRAPPER_B, FRIEND, orig=original, item_type="DYNAMIC_TYPE_FORWARD")
    details = {ORIGINAL: original, WRAPPER_A: wrapper_a, WRAPPER_B: wrapper_b}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Activity:
        skipped = False

        def __init__(self, dynamic_id: str):
            self.dynamic_id = dynamic_id

        def to_dict(self):
            return {"dynamic_id": self.dynamic_id}

    rows: list[dict] = []
    monkeypatch.setattr(pipeline, "known_activity_ids", lambda: set())
    monkeypatch.setattr(pipeline, "load_participations", lambda: {})
    monkeypatch.setattr("src.activity_store.known_activity_aliases", lambda: set())
    monkeypatch.setattr(participation_guard, "load_blocked_guard_ids", lambda uid: set())
    monkeypatch.setattr(participation_guard, "list_confirmed_guards", lambda uid: [])
    monkeypatch.setattr(pipeline, "BilibiliClient", FakeClient)
    monkeypatch.setattr(
        pipeline,
        "classify_new_link",
        lambda _client, dynamic_id: ClassifyOutcome(
            dynamic_id,
            "转发抽奖",
            False,
            detail_item=details[dynamic_id],
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "enrich_activity",
        lambda _client, *, dynamic_id, **kwargs: Activity(dynamic_id),
    )
    monkeypatch.setattr(pipeline, "apply_initial_status", lambda item: item)
    monkeypatch.setattr(
        pipeline,
        "append_activities",
        lambda values: rows.extend(values) or len(values),
    )

    result = pipeline.run_new_links_pipeline(
        [
            f"https://www.bilibili.com/opus/{ORIGINAL}",
            f"https://www.bilibili.com/opus/{WRAPPER_A}",
            f"https://www.bilibili.com/opus/{WRAPPER_B}",
        ],
        workers=1,
    )
    assert result.persisted_count == 1
    assert len(rows) == 1
    assert rows[0]["dynamic_id"] == ORIGINAL


def test_legacy_participation_protects_canonical_reobservation(monkeypatch) -> None:
    from src import participation_guard
    from src.pipeline import refresh_all_pipeline as pipeline
    from src.pipeline.classify_step import ClassifyOutcome

    original = _item(ORIGINAL, ORIGINAL_AUTHOR, following=True)
    wrapper = _item(WRAPPER_A, FRIEND, orig=original, item_type="DYNAMIC_TYPE_FORWARD")

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(pipeline, "known_activity_ids", lambda: set())
    monkeypatch.setattr(
        pipeline,
        "load_participations",
        lambda: {ORIGINAL: SimpleNamespace(user_status="已参加")},
    )
    monkeypatch.setattr("src.activity_store.known_activity_aliases", lambda: set())
    monkeypatch.setattr(participation_guard, "load_blocked_guard_ids", lambda uid: set())
    monkeypatch.setattr(participation_guard, "list_confirmed_guards", lambda uid: [])
    monkeypatch.setattr(pipeline, "BilibiliClient", FakeClient)
    monkeypatch.setattr(
        pipeline,
        "classify_new_link",
        lambda _client, dynamic_id: ClassifyOutcome(
            dynamic_id,
            "转发抽奖",
            False,
            detail_item=wrapper,
        ),
    )
    persisted: list[dict] = []
    monkeypatch.setattr(pipeline, "append_activities", lambda values: persisted.extend(values) or len(values))

    result = pipeline.run_new_links_pipeline(
        [f"https://www.bilibili.com/opus/{WRAPPER_A}"], workers=1
    )
    assert result.persisted_count == 0
    assert result.duplicate_link_count == 1
    assert persisted == []


def test_public_pipeline_does_not_apply_following_policy(monkeypatch) -> None:
    from src import participation_guard
    from src.pipeline import refresh_all_pipeline as pipeline
    from src.pipeline.classify_step import ClassifyOutcome

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Activity:
        skipped = False

        def __init__(self, dynamic_id: str):
            self.dynamic_id = dynamic_id

        def to_dict(self):
            return {"dynamic_id": self.dynamic_id}

    rows: list[dict] = []
    item = _item(ORIGINAL, ORIGINAL_AUTHOR, following=False)
    monkeypatch.setattr(pipeline, "known_activity_ids", lambda: set())
    monkeypatch.setattr(pipeline, "load_participations", lambda: {})
    monkeypatch.setattr("src.activity_store.known_activity_aliases", lambda: set())
    monkeypatch.setattr(participation_guard, "load_blocked_guard_ids", lambda uid: set())
    monkeypatch.setattr(participation_guard, "list_confirmed_guards", lambda uid: [])
    monkeypatch.setattr(pipeline, "BilibiliClient", FakeClient)
    monkeypatch.setattr(
        pipeline,
        "classify_new_link",
        lambda _client, dynamic_id: ClassifyOutcome(
            dynamic_id, "转发抽奖", False, detail_item=item
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "enrich_activity",
        lambda _client, *, dynamic_id, **kwargs: Activity(dynamic_id),
    )
    monkeypatch.setattr(pipeline, "apply_initial_status", lambda value: value)
    monkeypatch.setattr(
        pipeline,
        "append_activities",
        lambda values: rows.extend(values) or len(values),
    )

    result = pipeline.run_new_links_pipeline(
        [f"https://www.bilibili.com/opus/{ORIGINAL}"], workers=1
    )
    assert result.persisted_count == 1
    assert rows[0]["dynamic_id"] == ORIGINAL
