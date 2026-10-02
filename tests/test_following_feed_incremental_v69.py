from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import following_feed as ff


IDS = [f"20000000000000{n:05d}" for n in range(1, 121)]


class FakeClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_json(self, url, params=None, *, referer=None, retries=0):
        self.calls.append(dict(params or {}))
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "profile" / "following_feed_state.json"
    monkeypatch.setattr(ff, "_state_path", lambda: path)
    monkeypatch.setattr(ff, "get_login_uid", lambda: None)
    return path


def payload(ids, *, offset="", has_more=False):
    return {
        "code": 0,
        "data": {
            "items": [{"id_str": item_id} for item_id in ids],
            "offset": offset,
            "has_more": has_more,
        },
    }


def pipeline_spy(monkeypatch):
    calls = []

    def run(urls, **kwargs):
        calls.append((list(urls), kwargs))
        return SimpleNamespace(
            persisted_count=len(urls),
            new_link_count=len(urls),
            skip_reasons={},
            partial=False,
        )

    monkeypatch.setattr(ff, "run_new_links_pipeline", run)
    return calls


def test_page_offset_chain_collects_all_pages_and_observations(state_path, monkeypatch):
    old_anchor = IDS[100]
    ff._write_state({"last_seen_dynamic_id": old_anchor})
    calls = pipeline_spy(monkeypatch)
    client = FakeClient(
        [
            payload(IDS[:20], offset="o1", has_more=True),
            payload(IDS[20:40], offset="o2", has_more=True),
            payload(IDS[40:60], offset="o3", has_more=True),
            payload(IDS[60:80] + [old_anchor], offset="", has_more=False),
        ]
    )

    result = ff.scan_following_feed(client_factory=lambda: client)

    assert result["status"] == "success"
    assert result["found"] == 80
    assert result["accepted"] == 80
    assert client.calls == [
        {"type": "all", "page": 1},
        {"type": "all", "page": 2, "offset": "o1"},
        {"type": "all", "page": 3, "offset": "o2"},
        {"type": "all", "page": 4, "offset": "o3"},
    ]
    assert len(calls) == 1
    assert len(calls[0][0]) == 80
    observations = calls[0][1]["source_observations"]
    assert observations[0]["page_index"] == 1
    assert observations[20]["page_index"] == 2
    assert observations[-1]["page_index"] == 4
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "offset" not in state
    assert len(state["anchors"]) <= ff.ANCHOR_LIMIT


def test_cross_page_duplicate_enters_pipeline_once(state_path, monkeypatch):
    ff._write_state({"last_seen_dynamic_id": IDS[-1]})
    calls = pipeline_spy(monkeypatch)
    client = FakeClient(
        [
            payload(IDS[:3], offset="o1", has_more=True),
            payload([IDS[2], IDS[3]], has_more=False),
        ]
    )
    result = ff.scan_following_feed(client_factory=lambda: client)
    assert result["found"] == 4
    assert len(calls[0][0]) == 4


def test_missing_offset_is_incomplete_and_does_not_call_pipeline(state_path, monkeypatch):
    ff._write_state({"last_seen_dynamic_id": IDS[-1]})
    calls = pipeline_spy(monkeypatch)
    client = FakeClient([payload(IDS[:2], offset="", has_more=True)])
    result = ff.scan_following_feed(client_factory=lambda: client)
    assert result["status"] == "incomplete"
    assert result["stop_reason"] == "missing_next_offset"
    assert calls == []
    assert ff.following_feed_info()["last_seen_dynamic_id"] == IDS[-1]


def test_hard_cap_never_requests_page_21_and_processes_safe_window(state_path, monkeypatch):
    ff._write_state({"last_seen_dynamic_id": IDS[-1]})
    calls = pipeline_spy(monkeypatch)
    pages = [payload([IDS[index]], offset=f"o{index + 1}", has_more=True) for index in range(ff.FOLLOW_FEED_MAX_PAGES)]
    client = FakeClient(pages)
    result = ff.scan_following_feed(client_factory=lambda: client)
    assert result["status"] == "incomplete"
    assert result["stop_reason"] == "hard_cap_reached"
    assert len(client.calls) == ff.FOLLOW_FEED_MAX_PAGES
    assert client.calls[-1]["page"] == ff.FOLLOW_FEED_MAX_PAGES
    assert len(calls) == 1
    assert len(calls[0][0]) == ff.FOLLOW_FEED_MAX_PAGES
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["history_status"] == "pending"
    assert state["history_next_page"] == ff.FOLLOW_FEED_MAX_PAGES + 1


def test_partial_pipeline_preserves_checkpoint_and_anchors(state_path, monkeypatch):
    ff._write_state({"last_seen_dynamic_id": IDS[5], "anchors": [IDS[5], IDS[6]]})

    def partial(urls, **kwargs):
        return SimpleNamespace(
            persisted_count=0,
            new_link_count=len(urls),
            skip_reasons={},
            partial=True,
        )

    monkeypatch.setattr(ff, "run_new_links_pipeline", partial)
    client = FakeClient([payload(IDS[:6], has_more=False)])
    ff.scan_following_feed(client_factory=lambda: client)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_seen_dynamic_id"] == IDS[5]
    assert state["anchors"] == [IDS[5], IDS[6]]
    assert state["last_stop_reason"] == "pipeline_partial"


def test_malformed_state_makes_zero_requests_and_preserves_bytes(state_path, monkeypatch):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(b"{broken")
    client = FakeClient([payload(IDS[:1])])
    result = ff.scan_following_feed(client_factory=lambda: client)
    assert result["status"] == "failed"
    assert result["message"] == "following_feed_state_corrupt"
    assert client.calls == []
    assert state_path.read_bytes() == b"{broken"

