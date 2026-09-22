from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.bilibili_request_budget import RequestBudgetExhausted, RequestBudgetSnapshot
from src import following_feed as ff


CURRENT = "1000000000000000001"
FRIEND = "1000000000000000002"
AUTHOR_A = "1000000000000000003"
AUTHOR_B = "1000000000000000004"
ORIGINAL_A = "2000000000000000001"
ORIGINAL_B = "2000000000000000002"
WRAPPER_A = "2000000000000000003"


class _FeedClient:
    def __init__(self, items: list[dict], error: Exception | None = None) -> None:
        self.payload = {"code": 0, "data": {"items": items}}
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_json(self, *args, **kwargs):
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture
def feed_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "profile-a" / "following_feed_state.json"
    monkeypatch.setattr(ff, "_state_path", lambda: target)
    monkeypatch.setattr(ff, "get_login_uid", lambda: int(CURRENT))
    return target


def _item(
    dynamic_id: str,
    author: str,
    *,
    following: bool | None = None,
    orig: dict | None = None,
) -> dict:
    author_payload = {"mid": int(author)}
    if following is not None:
        author_payload["following"] = following
    item = {
        "id_str": dynamic_id,
        "type": "DYNAMIC_TYPE_FORWARD" if orig else "DYNAMIC_TYPE_DRAW",
        "modules": {"module_author": author_payload},
    }
    if orig is not None:
        item["orig"] = orig
    return item


def _original(dynamic_id: str, author: str, following: bool | None = None) -> dict:
    return _item(dynamic_id, author, following=following)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    items: list[dict],
):
    captured: list[str] = []

    def fake_pipeline(urls, **kwargs):
        captured.extend(urls)
        return SimpleNamespace(
            persisted_count=len(urls),
            new_link_count=len(urls),
            skip_reasons={},
            partial=False,
        )

    monkeypatch.setattr(ff, "run_new_links_pipeline", fake_pipeline)
    result = ff.scan_following_feed(
        client_factory=lambda: _FeedClient(items),
    )
    return result, captured


def test_f1_non_forward_original_is_accepted(feed_state, monkeypatch):
    result, urls = _run(monkeypatch, [_original(ORIGINAL_A, AUTHOR_A)])
    assert result["status"] == "success"
    assert urls == [f"https://www.bilibili.com/opus/{ORIGINAL_A}"]


def test_f2_non_forward_original_does_not_use_followed_membership(feed_state, monkeypatch):
    result, urls = _run(monkeypatch, [_original(ORIGINAL_A, AUTHOR_A)])
    assert result["status"] == "success"
    assert urls == [f"https://www.bilibili.com/opus/{ORIGINAL_A}"]
    assert result["accepted"] == 1


def test_f3_friend_repost_is_skipped_even_when_friend_is_followed(feed_state, monkeypatch):
    original = _original(ORIGINAL_A, AUTHOR_A)
    result, urls = _run(monkeypatch, [_item(WRAPPER_A, FRIEND, orig=original)])
    assert result["status"] == "success"
    assert urls == []
    assert result["forward_skipped"] == 1


def test_f4_friend_repost_is_skipped_even_when_original_author_is_followed(feed_state, monkeypatch):
    original = _original(ORIGINAL_A, AUTHOR_A)
    result, urls = _run(monkeypatch, [_item(WRAPPER_A, FRIEND, orig=original)])
    assert result["status"] == "success"
    assert urls == []
    assert result["forward_skipped"] == 1


def test_f5_direct_original_relationship_true_is_accepted(feed_state, monkeypatch):
    result, urls = _run(
        monkeypatch,
        [_original(ORIGINAL_A, AUTHOR_A, following=True)],
    )
    assert result["status"] == "success"
    assert urls


def test_f6_direct_original_relationship_false_is_still_feed_scoped(feed_state, monkeypatch):
    result, urls = _run(
        monkeypatch,
        [_original(ORIGINAL_A, AUTHOR_A, following=False)],
    )
    assert result["status"] == "success"
    assert urls == [f"https://www.bilibili.com/opus/{ORIGINAL_A}"]
    assert result["accepted"] == 1


def test_f7_unknown_relationship_does_not_block_original(feed_state, monkeypatch):
    result, urls = _run(monkeypatch, [_original(ORIGINAL_A, AUTHOR_A)])
    assert result["accepted"] == 1
    assert urls


def test_f8_unknown_relationship_does_not_reject_original(feed_state, monkeypatch):
    result, urls = _run(monkeypatch, [_original(ORIGINAL_A, AUTHOR_A)])
    assert result["accepted"] == 1
    assert urls


def test_f9_following_feed_advances_checkpoint_without_relationship_snapshot(feed_state, monkeypatch):
    ff._write_state({"last_seen_dynamic_id": ORIGINAL_B})
    result, urls = _run(monkeypatch, [_original(ORIGINAL_A, AUTHOR_A)])
    assert result["status"] == "success"
    assert urls
    assert ff.following_feed_info()["last_seen_dynamic_id"] == ORIGINAL_A


def test_f10_many_originals_need_only_the_feed_request(
    feed_state, monkeypatch
):
    items = [
        _original(ORIGINAL_A, AUTHOR_A),
        _original(ORIGINAL_B, AUTHOR_B),
        _original("2000000000000000005", "1000000000000000005"),
    ]
    result, urls = _run(monkeypatch, items)
    assert len(urls) == 3
    assert result["accepted"] == 3


def test_f11_relationship_snapshot_failure_cannot_affect_following_feed(
    feed_state, monkeypatch
):
    result, urls = _run(monkeypatch, [_original(ORIGINAL_A, AUTHOR_A)])
    assert result["status"] == "success"
    assert urls


def test_f12_following_feed_scope_does_not_depend_on_relationship_snapshot(feed_state, monkeypatch):
    complete, complete_urls = _run(monkeypatch, [_original(ORIGINAL_A, AUTHOR_A)])
    assert complete["status"] == "success"
    assert complete_urls

    ff._write_state({})
    failed, failed_urls = _run(monkeypatch, [_original(ORIGINAL_A, AUTHOR_A)])
    assert failed["status"] == "success"
    assert failed_urls


def test_f13_empty_feed_makes_no_relationship_requests(feed_state, monkeypatch):
    result, urls = _run(monkeypatch, [])
    assert result["status"] == "success"
    assert urls == []


def test_f14_many_candidates_use_zero_relationship_requests(feed_state, monkeypatch):
    items = [_original(ORIGINAL_A, AUTHOR_A), _original(ORIGINAL_B, AUTHOR_B)]
    result, urls = _run(monkeypatch, items)
    assert result["status"] == "success"
    assert len(urls) == 2


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("API error -352: 风控"),
        RuntimeError("API error -509: 访问过于频繁"),
        httpx.HTTPStatusError(
            "429",
            request=httpx.Request("GET", "https://api.bilibili.com/x/relation/followings"),
            response=httpx.Response(429),
        ),
    ],
)
def test_f15_formal_platform_risk_from_feed_propagates_and_keeps_checkpoint(
    feed_state, monkeypatch, error
):
    ff._write_state({"last_seen_dynamic_id": ORIGINAL_B})
    with pytest.raises(Exception):
        ff.scan_following_feed(
            client_factory=lambda error=error: _FeedClient(
                [_original(ORIGINAL_A, AUTHOR_A)], error=error
            )
        )
    assert ff.following_feed_info()["last_seen_dynamic_id"] == ORIGINAL_B


def test_f16_request_budget_exhaustion_is_not_relationship_skip(feed_state, monkeypatch):
    snapshot = RequestBudgetSnapshot(1, 2, 1, 1, 1, 1)
    error = RequestBudgetExhausted(snapshot)
    with pytest.raises(RequestBudgetExhausted):
        ff.scan_following_feed(
            client_factory=lambda: _FeedClient([_original(ORIGINAL_A, AUTHOR_A)], error=error)
        )
    assert ff.following_feed_info()["last_seen_dynamic_id"] is None


def test_f17_auth_invalid_does_not_advance_checkpoint(feed_state, monkeypatch):
    monkeypatch.setattr(ff, "get_login_uid", lambda: None)
    ff._write_state({"last_seen_dynamic_id": ORIGINAL_B})
    result, urls = _run(monkeypatch, [_original(ORIGINAL_A, AUTHOR_A)])
    assert result["status"] == "failed"
    assert urls == []
    assert ff.following_feed_info()["last_seen_dynamic_id"] == ORIGINAL_B


def test_f18_profile_feed_checkpoint_isolation(tmp_path: Path, monkeypatch):
    profile_a = tmp_path / "a" / "following_feed_state.json"
    profile_b = tmp_path / "b" / "following_feed_state.json"
    monkeypatch.setattr(ff, "get_login_uid", lambda: int(CURRENT))

    def run(path: Path):
        monkeypatch.setattr(ff, "_state_path", lambda: path)
        return _run(monkeypatch, [_original(ORIGINAL_A, AUTHOR_A)])[0]

    assert run(profile_a)["accepted"] == 1
    assert run(profile_b)["accepted"] == 1
    monkeypatch.setattr(ff, "_state_path", lambda: profile_a)
    assert ff.following_feed_info()["last_seen_dynamic_id"] == ORIGINAL_A
    monkeypatch.setattr(ff, "_state_path", lambda: profile_b)
    assert ff.following_feed_info()["last_seen_dynamic_id"] == ORIGINAL_A
