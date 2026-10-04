from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import following_feed as ff
from src.message_api import _require_ok
from src.platform_risk import risk_code_of


CN_TZ = timezone(timedelta(hours=8))
NOW = int(datetime(2026, 10, 1, 20, 0, tzinfo=CN_TZ).timestamp())
IDS = [f"30000000000000{number:05d}" for number in range(1, 101)]


def item(dynamic_id: str, published_at: int) -> dict:
    return {
        "id_str": dynamic_id,
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {"module_author": {"mid": "123", "pub_ts": published_at}},
    }


def page(ids: list[str], page_no: int, *, published_at: int, has_more: bool = True) -> dict:
    return {
        "code": 0,
        "data": {
            "items": [item(dynamic_id, published_at) for dynamic_id in ids],
            "offset": f"offset-{page_no}",
            "has_more": has_more,
        },
    }


class QueueClient:
    def __init__(self, payloads: list[dict]):
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
    target = tmp_path / "profile" / "following_feed_state.json"
    monkeypatch.setattr(ff, "_state_path", lambda: target)
    monkeypatch.setattr(ff, "_cn_now_ts", lambda: NOW)
    monkeypatch.setattr(ff, "get_login_uid", lambda: "999")
    return target


@pytest.fixture
def idempotent_pipeline(monkeypatch: pytest.MonkeyPatch):
    inserted: set[str] = set()
    calls: list[list[str]] = []

    def run(urls, **kwargs):
        dynamic_ids = [str(url).rstrip("/").rsplit("/", 1)[-1] for url in urls]
        calls.append(dynamic_ids)
        fresh = [dynamic_id for dynamic_id in dynamic_ids if dynamic_id not in inserted]
        inserted.update(fresh)
        return SimpleNamespace(
            persisted_count=len(fresh),
            new_link_count=len(fresh),
            skip_reasons={},
            partial=False,
        )

    monkeypatch.setattr(ff, "run_new_links_pipeline", run)
    return inserted, calls


def hard_cap_pages(start: int = 0) -> list[dict]:
    return [
        page([IDS[start + index]], index + 1, published_at=NOW - index * 60)
        for index in range(ff.FOLLOW_FEED_MAX_PAGES)
    ]


def test_legacy_hard_cap_processes_candidates_and_seeds_history_cursor(
    state_path: Path, idempotent_pipeline
) -> None:
    legacy_checkpoint = IDS[-1]
    ff._write_state({"last_seen_dynamic_id": legacy_checkpoint})
    inserted, calls = idempotent_pipeline
    client = QueueClient(hard_cap_pages())

    result = ff.scan_following_feed(client_factory=lambda: client)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["status"] == "incomplete"
    assert result["stop_reason"] == "hard_cap_reached"
    assert result["processed"] == 20
    assert len(calls) == 1 and len(calls[0]) == 20
    assert len(inserted) == 20
    assert state["last_seen_dynamic_id"] == IDS[0]
    assert state["history_status"] == "pending"
    assert state["history_mode"] == "initial_15d"
    assert state["history_offset"] == "offset-20"
    assert state["history_next_page"] == 21
    assert state["baseline_at"] == NOW
    assert state["history_cutoff_at"] == NOW - 15 * 24 * 60 * 60
    assert state["last_catch_up_complete"] is False


def test_second_round_processes_new_live_item_and_continues_history_without_duplicates(
    state_path: Path, idempotent_pipeline
) -> None:
    inserted, calls = idempotent_pipeline
    ff._write_state({"last_seen_dynamic_id": IDS[-1]})
    ff.scan_following_feed(client_factory=lambda: QueueClient(hard_cap_pages()))
    baseline = json.loads(state_path.read_text(encoding="utf-8"))["baseline_at"]

    new_id = IDS[50]
    live = page([new_id, IDS[0]], 1, published_at=NOW + 60)
    history = [
        page([IDS[20 + index]], 21 + index, published_at=NOW - 3600 - index * 60)
        for index in range(ff.FOLLOW_FEED_HISTORY_PAGES_PER_RUN)
    ]
    client = QueueClient([live, *history])
    result = ff.scan_following_feed(client_factory=lambda: client)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert calls[-2] == [new_id]
    assert calls[-1] == IDS[20:25]
    assert result["added"] == 6
    assert result["history_pages_fetched"] == 5
    assert state["history_offset"] == "offset-25"
    assert state["history_next_page"] == 26
    assert state["baseline_at"] == baseline
    assert len(inserted) == 26

    repeat_client = QueueClient(
        [
            page([new_id, IDS[0]], 1, published_at=NOW + 60),
            *[
                page([IDS[20 + index]], 26 + index, published_at=NOW - 4000)
                for index in range(ff.FOLLOW_FEED_HISTORY_PAGES_PER_RUN)
            ],
        ]
    )
    repeat = ff.scan_following_feed(client_factory=lambda: repeat_client)
    assert repeat["added"] == 0
    assert len(inserted) == 26


def test_history_cutoff_completes_after_restart_without_requesting_older_page(
    state_path: Path, idempotent_pipeline
) -> None:
    ff._write_state({"last_seen_dynamic_id": IDS[-1]})
    ff.scan_following_feed(client_factory=lambda: QueueClient(hard_cap_pages()))
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    baseline = persisted["baseline_at"]
    cutoff = persisted["history_cutoff_at"]

    client = QueueClient(
        [
            page([IDS[0]], 1, published_at=NOW),
            page([IDS[30], IDS[31]], 21, published_at=cutoff - 1),
        ]
    )
    result = ff.scan_following_feed(client_factory=lambda: client)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(client.calls) == 2
    assert state["baseline_at"] == baseline
    assert state["history_cutoff_at"] == cutoff
    assert state["history_status"] == "complete"
    assert state["history_mode"] == "idle"
    assert state["history_completed_at"] == NOW
    assert state["history_offset"] is None
    assert result["history_stop_reason"] == "history_cutoff_reached"


def test_long_downtime_hard_cap_opens_gap_but_processes_live_window(
    state_path: Path, idempotent_pipeline
) -> None:
    old_checkpoint = IDS[-1]
    ff._write_state(
        {
            "state_version": 2,
            "baseline_at": NOW - 30 * 24 * 60 * 60,
            "history_cutoff_at": NOW - 45 * 24 * 60 * 60,
            "history_status": "complete",
            "history_mode": "idle",
            "history_completed_at": NOW - 20 * 24 * 60 * 60,
            "last_seen_dynamic_id": old_checkpoint,
            "anchors": [old_checkpoint],
        }
    )
    inserted, _ = idempotent_pipeline

    result = ff.scan_following_feed(
        client_factory=lambda: QueueClient(hard_cap_pages())
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["processed"] == 20
    assert len(inserted) == 20
    assert state["history_status"] == "pending"
    assert state["history_mode"] == "gap"
    assert state["history_target_checkpoint"] == old_checkpoint
    assert state["history_offset"] == "offset-20"
    assert state["history_cutoff_at"] == NOW - 45 * 24 * 60 * 60
    assert state["gap_cutoff_at"] == NOW - 15 * 24 * 60 * 60
    assert state["coverage_gap"] is True


def test_fresh_profile_uses_cn_timezone_baseline_and_starts_bounded_backfill(
    state_path: Path, idempotent_pipeline
) -> None:
    client = QueueClient([page([IDS[0]], 1, published_at=NOW)])

    result = ff.scan_following_feed(client_factory=lambda: client)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    baseline_cn = datetime.fromtimestamp(state["baseline_at"], CN_TZ)
    cutoff_cn = datetime.fromtimestamp(state["history_cutoff_at"], CN_TZ)
    assert baseline_cn == datetime(2026, 10, 1, 20, 0, tzinfo=CN_TZ)
    assert baseline_cn - cutoff_cn == timedelta(days=15)
    assert state["history_status"] == "pending"
    assert state["history_offset"] == "offset-1"
    assert state["history_next_page"] == 2
    assert result["processed"] == 1


def test_profile_state_remains_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idempotent_pipeline) -> None:
    monkeypatch.setattr(ff, "_cn_now_ts", lambda: NOW)
    monkeypatch.setattr(ff, "get_login_uid", lambda: "999")
    first = tmp_path / "a" / "following_feed_state.json"
    second = tmp_path / "b" / "following_feed_state.json"
    monkeypatch.setattr(ff, "_state_path", lambda: first)
    ff.scan_following_feed(
        client_factory=lambda: QueueClient(
            [page([IDS[0]], 1, published_at=NOW, has_more=False)]
        )
    )
    monkeypatch.setattr(ff, "_state_path", lambda: second)
    assert ff.following_feed_info()["baseline_at"] is None
    assert not second.exists()


def test_checkpoint_and_fallback_anchors_keep_fast_stop_behavior(
    state_path: Path, idempotent_pipeline
) -> None:
    checkpoint = IDS[40]
    ff._write_state({"last_seen_dynamic_id": checkpoint})
    checkpoint_client = QueueClient(
        [
            page([IDS[0]], 1, published_at=NOW),
            page([IDS[1], checkpoint, IDS[2]], 2, published_at=NOW - 60),
        ]
    )
    checkpoint_result = ff.scan_following_feed(
        client_factory=lambda: checkpoint_client
    )
    assert checkpoint_result["stop_reason"] == "checkpoint_reached"
    assert checkpoint_result["live_pages_fetched"] == 2

    anchors = IDS[50:54]
    ff._write_state(
        {
            "last_seen_dynamic_id": IDS[60],
            "anchors": [IDS[60], *anchors],
        }
    )
    anchor_client = QueueClient(
        [
            page([IDS[3]], 1, published_at=NOW),
            page(anchors[:3], 2, published_at=NOW - 60),
        ]
    )
    anchor_result = ff.scan_following_feed(client_factory=lambda: anchor_client)
    assert anchor_result["stop_reason"] == "anchor_reached"
    assert anchor_result["live_pages_fetched"] == 2


def test_repeated_live_offset_remains_fail_closed(
    state_path: Path, idempotent_pipeline
) -> None:
    ff._write_state({"last_seen_dynamic_id": IDS[-1]})
    first = page([IDS[0]], 1, published_at=NOW)
    second = page([IDS[1]], 2, published_at=NOW - 60)
    second["data"]["offset"] = first["data"]["offset"]
    client = QueueClient([first, second])

    result = ff.scan_following_feed(client_factory=lambda: client)

    assert result["status"] == "incomplete"
    assert result["stop_reason"] == "repeated_offset"
    assert idempotent_pipeline[1] == []
    assert ff.following_feed_info()["last_seen_dynamic_id"] == IDS[-1]


def test_history_repeated_offset_does_not_advance_cursor_or_block_live_candidate(
    state_path: Path, idempotent_pipeline
) -> None:
    checkpoint = IDS[0]
    ff._write_state(
        {
            "state_version": 2,
            "baseline_at": NOW,
            "history_cutoff_at": NOW - 15 * 24 * 60 * 60,
            "history_status": "pending",
            "history_mode": "initial_15d",
            "history_offset": "offset-20",
            "history_next_page": 21,
            "last_seen_dynamic_id": checkpoint,
            "anchors": [checkpoint],
        }
    )
    new_id = IDS[50]
    repeated = page([IDS[30]], 21, published_at=NOW - 3600)
    repeated["data"]["offset"] = "offset-20"
    client = QueueClient(
        [page([new_id, checkpoint], 1, published_at=NOW), repeated]
    )

    result = ff.scan_following_feed(client_factory=lambda: client)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert new_id in idempotent_pipeline[0]
    assert state["last_seen_dynamic_id"] == new_id
    assert state["history_offset"] == "offset-20"
    assert state["history_next_page"] == 21
    assert state["history_stop_reason"] == "history_repeated_offset"
    assert result["status"] == "incomplete"


def test_history_risk_propagates_after_live_progress_is_committed(
    state_path: Path, idempotent_pipeline
) -> None:
    checkpoint = IDS[0]
    ff._write_state(
        {
            "state_version": 2,
            "baseline_at": NOW,
            "history_cutoff_at": NOW - 15 * 24 * 60 * 60,
            "history_status": "pending",
            "history_mode": "initial_15d",
            "history_offset": "offset-20",
            "history_next_page": 21,
            "last_seen_dynamic_id": checkpoint,
            "anchors": [checkpoint],
        }
    )
    new_id = IDS[50]
    client = QueueClient(
        [
            page([new_id, checkpoint], 1, published_at=NOW),
            RuntimeError("API error -352: 风控校验失败"),
        ]
    )

    with pytest.raises(RuntimeError, match="-352"):
        ff.scan_following_feed(client_factory=lambda: client)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert new_id in idempotent_pipeline[0]
    assert state["last_seen_dynamic_id"] == new_id
    assert state["history_offset"] == "offset-20"


@pytest.mark.parametrize("code", [-352, -509])
def test_history_classification_payload_risk_is_not_login_uid_error(
    state_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    checkpoint = IDS[0]
    ff._write_state(
        {
            "state_version": 2,
            "baseline_at": NOW,
            "history_cutoff_at": NOW - 15 * 24 * 60 * 60,
            "history_status": "pending",
            "history_mode": "initial_15d",
            "history_offset": "offset-20",
            "history_next_page": 21,
            "last_seen_dynamic_id": checkpoint,
            "anchors": [checkpoint],
        }
    )
    pipeline_calls: list[list[str]] = []

    def classify_batch(urls, **kwargs):
        if not urls:
            return SimpleNamespace(new_link_count=0, persisted_count=0, skip_reasons={}, partial=False)
        pipeline_calls.append(list(urls))
        _require_ok({"code": code, "message": "分类读取被拒绝"}, action="分类")

    monkeypatch.setattr(ff, "run_new_links_pipeline", classify_batch)
    client = QueueClient(
        [
            page([checkpoint], 1, published_at=NOW),
            page([IDS[1]], 21, published_at=NOW - 3600, has_more=False),
        ]
    )
    with pytest.raises(RuntimeError) as caught:
        ff.scan_following_feed(client_factory=lambda: client)

    assert risk_code_of(caught.value) == code
    assert len(client.calls) == 2
    assert pipeline_calls == [[ff.opus_link(IDS[1])]]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["history_stop_reason"] != "history_missing_login_uid"


def test_history_plain_runtime_error_keeps_existing_incomplete_semantics(
    state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = IDS[0]
    ff._write_state(
        {
            "state_version": 2,
            "baseline_at": NOW,
            "history_cutoff_at": NOW - 15 * 24 * 60 * 60,
            "history_status": "pending",
            "history_mode": "initial_15d",
            "history_offset": "offset-20",
            "history_next_page": 21,
            "last_seen_dynamic_id": checkpoint,
            "anchors": [checkpoint],
        }
    )

    def plain_failure(urls, **kwargs):
        if not urls:
            return SimpleNamespace(new_link_count=0, persisted_count=0, skip_reasons={}, partial=False)
        raise RuntimeError("普通分类失败")

    monkeypatch.setattr(ff, "run_new_links_pipeline", plain_failure)
    client = QueueClient(
        [
            page([checkpoint], 1, published_at=NOW),
            page([IDS[1]], 21, published_at=NOW - 3600, has_more=False),
        ]
    )
    result = ff.scan_following_feed(client_factory=lambda: client)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["status"] == "incomplete"
    assert state["history_stop_reason"] == "history_missing_login_uid"
    assert risk_code_of(state["last_error"]) is None


def test_scheduler_surfaces_history_progress_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src import data_paths
    from tests.scheduler_test_helpers import daytime_cn_clock
    from web.auto_scheduler import AutoScheduler

    monkeypatch.setattr(data_paths, "get_profile_dir", lambda profile_id=None: tmp_path)
    scheduler = AutoScheduler(job_runner=MagicMock(), clock=daytime_cn_clock)
    detail = "关注动态本轮已处理 20 条，新增 1 条；历史追赶尚未完成"
    scheduler._click_and_wait = lambda action, pipeline_index=None: {"message": detail}

    scheduler._run_following_scan("following-v74-message")

    assert scheduler.get_status()["message"] == detail
