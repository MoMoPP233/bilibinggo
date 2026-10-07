from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx

from src import following_feed as ff


IDS = [f"40000000000000{number:05d}" for number in range(500)]
NOW = 1_791_000_000


def page(ids: list[str], offset: str, *, has_more: bool = True) -> dict:
    return {
        "code": 0,
        "data": {
            "items": [{"id_str": dynamic_id} for dynamic_id in ids],
            "offset": offset,
            "has_more": has_more,
        },
    }


class QueueClient:
    def __init__(self, payloads: list[object]):
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_json(self, _url, params=None, **_kwargs):
        self.calls.append(dict(params or {}))
        value = self.payloads.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def profile_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "profile" / "following_feed_state.json"
    monkeypatch.setattr(ff, "_state_path", lambda: path)
    monkeypatch.setattr(ff, "_cn_now_ts", lambda: NOW)
    monkeypatch.setattr(ff, "get_login_uid", lambda: "999")
    return path


@pytest.fixture
def dedup_pipeline(monkeypatch: pytest.MonkeyPatch):
    inserted: set[str] = set()
    calls: list[list[str]] = []

    def run(urls, **_kwargs):
        ids = [str(url).rsplit("/", 1)[-1] for url in urls]
        calls.append(ids)
        fresh = set(ids) - inserted
        inserted.update(fresh)
        return SimpleNamespace(
            persisted_count=len(fresh), new_link_count=len(fresh),
            skip_reasons={}, partial=False,
        )

    monkeypatch.setattr(ff, "run_new_links_pipeline", run)
    return inserted, calls


def state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def hard_cap(tag: str, start: int) -> list[dict]:
    return [page([IDS[start + i]], f"{tag}-{i + 1}") for i in range(ff.FOLLOW_FEED_MAX_PAGES)]


def old_history(path: Path) -> None:
    ff._write_state({
        "state_version": 2,
        "baseline_at": NOW,
        "history_cutoff_at": NOW - 15 * 24 * 60 * 60,
        "history_status": "pending",
        "history_mode": "initial_15d",
        "history_offset": "old-h1",
        "history_next_page": 51,
        "last_seen_dynamic_id": IDS[0],
        "anchors": [IDS[0]],
    })


def test_three_live_hard_caps_preserve_old_history_and_recover_every_recent_gap(
    profile_state: Path, dedup_pipeline,
) -> None:
    old_history(profile_state)
    inserted, calls = dedup_pipeline

    first = ff.scan_following_feed(client_factory=lambda: QueueClient(hard_cap("r1", 100)))
    assert first["stop_reason"] == "hard_cap_reached"
    assert state(profile_state)["recent_offset"] == "r1-20"
    assert state(profile_state)["history_offset"] == "old-h1"

    second_client = QueueClient([
        *hard_cap("r2", 200),
        *[page([IDS[n]], f"r2-{i}") for i, n in enumerate((220, 121, 222, 223, 224), 21)],
    ])
    ff.scan_following_feed(client_factory=lambda: second_client)
    assert second_client.calls[20]["offset"] == "r2-20"
    assert state(profile_state)["recent_offset"] == "r2-25"
    assert state(profile_state)["history_offset"] == "old-h1"

    third_client = QueueClient([
        *hard_cap("r3", 300),
        *[page([IDS[n]], f"r3-{i}") for i, n in enumerate((320, 220, 122, 323, 324), 21)],
    ])
    ff.scan_following_feed(client_factory=lambda: third_client)
    assert third_client.calls[20]["offset"] == "r3-20"
    assert state(profile_state)["recent_offset"] == "r3-25"
    assert state(profile_state)["history_offset"] == "old-h1"
    assert state(profile_state)["recent_target_checkpoint"] == IDS[0]

    # A process restart reads both cursors from JSON.  The recent walk closes
    # against the original live checkpoint, not against the newer checkpoint.
    resumed_client = QueueClient([
        page([IDS[400], IDS[300], IDS[326]], "live-resume"),
        page([IDS[325], IDS[326]], "r3-26"),
        page([IDS[0]], "r3-27"),
    ])
    ff.scan_following_feed(client_factory=lambda: resumed_client)
    assert resumed_client.calls[1] == {"type": "all", "page": 26, "offset": "r3-25"}
    assert state(profile_state)["recent_offset"] is None
    assert state(profile_state)["history_offset"] == "old-h1"
    assert {IDS[n] for n in (121, 122, 220, 320, 325, 326)} <= inserted

    history_client = QueueClient([
        page([IDS[400]], "live-next"),
        page([IDS[50]], "old-done", has_more=False),
    ])
    final = ff.scan_following_feed(client_factory=lambda: history_client)
    assert history_client.calls[1] == {"type": "all", "page": 51, "offset": "old-h1"}
    assert state(profile_state)["history_status"] == "complete"
    assert final["catch_up_complete"] is True
    assert IDS[50] in inserted
    assert sum(len(ids) for ids in calls) > len(inserted)  # overlap is deduped


def test_recent_pipeline_partial_keeps_both_cursors(
    profile_state: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_history(profile_state)
    persisted = state(profile_state)
    persisted.update(recent_offset="recent-h1", recent_next_page=21,
                     recent_target_checkpoint=IDS[0])
    ff._write_state(persisted)

    def partial(urls, **_kwargs):
        return SimpleNamespace(persisted_count=0, new_link_count=len(urls),
                               skip_reasons={}, partial=any(IDS[2] in url for url in urls))

    monkeypatch.setattr(ff, "run_new_links_pipeline", partial)
    client = QueueClient([
        page([IDS[1], IDS[0]], "live"),
        page([IDS[2]], "recent-next", has_more=False),
    ])
    ff.scan_following_feed(client_factory=lambda: client)
    current = state(profile_state)
    assert current["recent_offset"] == "recent-h1"
    assert current["recent_next_page"] == 21
    assert current["history_offset"] == "old-h1"
    assert current["recent_stop_reason"] == "recent_pipeline_partial"


@pytest.mark.parametrize("failure", [
    ConnectionError("offline"),
    RuntimeError("API error -352: risk"),
    RuntimeError("API error -509: risk"),
    httpx.HTTPStatusError(
        "rate limited",
        request=httpx.Request("GET", ff.FOLLOW_FEED_URL),
        response=httpx.Response(429),
    ),
])
def test_recent_failure_keeps_both_cursors_and_propagates(
    profile_state: Path, dedup_pipeline, failure: Exception,
) -> None:
    old_history(profile_state)
    persisted = state(profile_state)
    persisted.update(recent_offset="recent-h1", recent_next_page=21,
                     recent_target_checkpoint=IDS[0])
    ff._write_state(persisted)
    client = QueueClient([page([IDS[1], IDS[0]], "live"), failure])

    with pytest.raises(type(failure)):
        ff.scan_following_feed(client_factory=lambda: client)
    current = state(profile_state)
    assert current["recent_offset"] == "recent-h1"
    assert current["history_offset"] == "old-h1"


@pytest.mark.parametrize("code", [-315, -412])
def test_nonformal_feed_codes_do_not_advance_recent_or_history(
    profile_state: Path, dedup_pipeline, code: int,
) -> None:
    old_history(profile_state)
    persisted = state(profile_state)
    persisted.update(recent_offset="recent-h1", recent_next_page=21,
                     recent_target_checkpoint=IDS[0])
    ff._write_state(persisted)
    client = QueueClient([
        page([IDS[1], IDS[0]], "live"),
        {"code": code, "message": "ordinary rejection"},
    ])
    with pytest.raises(RuntimeError, match=str(code)):
        ff.scan_following_feed(client_factory=lambda: client)
    assert state(profile_state)["recent_offset"] == "recent-h1"
    assert state(profile_state)["history_offset"] == "old-h1"


def test_recent_second_page_network_failure_does_not_commit_first_page(
    profile_state: Path, dedup_pipeline,
) -> None:
    old_history(profile_state)
    persisted = state(profile_state)
    persisted.update(recent_offset="recent-h1", recent_next_page=21,
                     recent_target_checkpoint=IDS[0])
    ff._write_state(persisted)
    inserted, _calls = dedup_pipeline
    client = QueueClient([
        page([IDS[1], IDS[0]], "live"),
        page([IDS[2]], "recent-21"),
        ConnectionError("page 22 disconnected"),
    ])

    with pytest.raises(ConnectionError):
        ff.scan_following_feed(client_factory=lambda: client)
    assert client.calls[-1] == {"type": "all", "page": 22, "offset": "recent-21"}
    current = state(profile_state)
    assert current["recent_offset"] == "recent-h1"
    assert current["recent_next_page"] == 21
    assert current["history_offset"] == "old-h1"
    assert IDS[2] not in inserted  # failed batch is retried from its first page


def test_missing_login_uid_keeps_both_resume_positions(
    profile_state: Path, monkeypatch: pytest.MonkeyPatch, dedup_pipeline,
) -> None:
    old_history(profile_state)
    persisted = state(profile_state)
    persisted.update(recent_offset="recent-h1", recent_next_page=21,
                     recent_target_checkpoint=IDS[0])
    ff._write_state(persisted)
    monkeypatch.setattr(ff, "get_login_uid", lambda: None)
    live = page([IDS[1], IDS[0]], "live")
    for item in live["data"]["items"]:
        item.update(type="DYNAMIC_TYPE_DRAW", modules={"module_author": {"mid": "123"}})
    client = QueueClient([live])

    result = ff.scan_following_feed(client_factory=lambda: client)
    assert result["stop_reason"] == "missing_login_uid"
    assert len(client.calls) == 1
    assert state(profile_state)["recent_offset"] == "recent-h1"
    assert state(profile_state)["history_offset"] == "old-h1"


def test_old_state_defaults_and_profile_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dedup_pipeline,
) -> None:
    first = tmp_path / "account-a" / "following_feed_state.json"
    second = tmp_path / "account-b" / "following_feed_state.json"
    monkeypatch.setattr(ff, "_state_path", lambda: first)
    monkeypatch.setattr(ff, "_cn_now_ts", lambda: NOW)
    monkeypatch.setattr(ff, "get_login_uid", lambda: "999")
    old_history(first)
    ff.scan_following_feed(client_factory=lambda: QueueClient(hard_cap("first", 100)))
    assert state(first)["recent_offset"] == "first-20"
    monkeypatch.setattr(ff, "_state_path", lambda: second)
    assert ff.following_feed_info()["recent_pending"] is False
    assert not second.exists()
    assert ff._read_state_checked()[1] is True
