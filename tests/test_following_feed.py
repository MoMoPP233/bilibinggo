from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from web.auto_scheduler import AutoScheduler
from src import following_feed as ff

_IDS = [f"20000000000000000{n:02d}" for n in range(1, 41)]


class _FakeClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_json(self, url, params=None, *, referer=None, retries=0):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "account-a" / "following_feed_state.json"
    monkeypatch.setattr(ff, "_state_path", lambda: target)
    return target


def _payload(items=None, code=None):
    payload = {"code": code if code is not None else 0, "data": {"items": items}}
    if code is not None:
        payload["data"] = {}
    return payload


def _items(ids=None):
    return [{"id_str": dynamic_id} for dynamic_id in (ids or _IDS[:10])]


def _pipeline_result(new_link=1, persisted=1):
    return SimpleNamespace(new_link_count=new_link, persisted_count=persisted)


def _run_scan(client, monkeypatch, items=None, pipeline=None):
    if pipeline is None:
        pipeline = _pipeline_result
    captured = {}

    def fake_pipeline(urls, *, workers=1, scan_started_at=None, on_progress=None):
        captured["urls"] = list(urls)
        captured["scan_started_at"] = scan_started_at
        return pipeline(len(urls), 1)

    monkeypatch.setattr(ff, "run_new_links_pipeline", fake_pipeline)
    result = ff.scan_following_feed(client_factory=lambda: client)
    return result, captured


def test_scan_runs_without_any_switch(state_path: Path, monkeypatch) -> None:
    client = _FakeClient(payload=_payload(_items(_IDS[:10])))
    result, captured = _run_scan(client, monkeypatch)
    assert result["status"] == "success"
    assert result["found"] == 10
    assert captured["scan_started_at"] is not None
    assert "enabled" not in result
    info = ff.following_feed_info()
    assert info["last_seen_dynamic_id"] == _IDS[0]
    assert info["last_status"] == "success"


def test_legacy_enabled_false_does_not_block(state_path: Path, monkeypatch) -> None:
    ff._write_state({"enabled": False, "last_scan_at": None})
    client = _FakeClient(payload=_payload(_items(_IDS[:5])))
    result, _ = _run_scan(client, monkeypatch)
    assert result["status"] == "success"
    assert result["found"] == 5


def test_scan_one_page_respects_cap(state_path: Path, monkeypatch) -> None:
    client = _FakeClient(payload=_payload(_items(_IDS[:40])))
    result, _ = _run_scan(client, monkeypatch)
    assert result["found"] == ff.FOLLOW_FEED_MAX_ITEMS


def test_checkpoint_prevents_reprocessing(state_path: Path, monkeypatch) -> None:
    client = _FakeClient(payload=_payload(_items(_IDS[:10])))
    _, captured = _run_scan(client, monkeypatch)
    assert len(captured["urls"]) == 10
    _, captured2 = _run_scan(client, monkeypatch)
    assert captured2["urls"] == []


def test_in_page_duplicates_dedup(state_path: Path, monkeypatch) -> None:
    items = [{"id_str": _IDS[0]}, {"id_str": _IDS[0]}, {"id_str": _IDS[1]}]
    client = _FakeClient(payload=_payload(items))
    _, captured = _run_scan(client, monkeypatch)
    assert len(captured["urls"]) == 2


def test_risk_raises_and_checkpoint_not_advanced(state_path: Path, monkeypatch) -> None:
    client = _FakeClient(error=RuntimeError("API error -352: 风控校验失败"))
    with pytest.raises(RuntimeError, match="-352"):
        ff.scan_following_feed(client_factory=lambda: client)
    assert ff.following_feed_info()["last_scan_at"] is None


def test_risk_body_is_not_empty_feed(state_path: Path) -> None:
    client = _FakeClient(payload=_payload(items=[], code=-352))
    with pytest.raises(RuntimeError, match="-352"):
        ff.scan_following_feed(client_factory=lambda: client)


def test_empty_feed_is_success_zero(state_path: Path, monkeypatch) -> None:
    client = _FakeClient(payload=_payload(items=[]))
    result, _ = _run_scan(client, monkeypatch)
    assert result["status"] == "success"
    assert result["found"] == 0


def test_structural_failures_are_failed_not_zero(state_path: Path) -> None:
    for payload in (
        {"code": 0, "data": {}},
        {"code": 0, "data": {"items": "oops"}},
        {"code": 0},
    ):
        client = _FakeClient(payload=payload)
        result = ff.scan_following_feed(client_factory=lambda: client)
        assert result["status"] == "failed"
        assert ff.following_feed_info()["last_status"] == "failed"
        assert ff.following_feed_info()["last_error"]


def test_nonempty_items_without_valid_ids_fails(state_path: Path) -> None:
    items = [{"id_str": "abc"}, {"id_str": ""}, {}]
    client = _FakeClient(payload=_payload(items=items))
    result = ff.scan_following_feed(client_factory=lambda: client)
    assert result["status"] == "failed"
    assert "动态 ID" in ff.following_feed_info()["last_error"]


def test_profile_isolation(tmp_path: Path, monkeypatch) -> None:
    a = tmp_path / "account-a" / "following_feed_state.json"
    b = tmp_path / "account-b" / "following_feed_state.json"

    monkeypatch.setattr(ff, "_state_path", lambda: a)
    client = _FakeClient(payload=_payload(_items(_IDS[:3])))
    captured = {}

    def fake_pipeline(urls, *, workers=1, scan_started_at=None, on_progress=None):
        captured["urls"] = list(urls)
        return _pipeline_result(len(urls), 1)

    monkeypatch.setattr(ff, "run_new_links_pipeline", fake_pipeline)
    ff.scan_following_feed(client_factory=lambda: client)
    assert ff.following_feed_info()["last_scan_at"] is not None

    monkeypatch.setattr(ff, "_state_path", lambda: b)
    assert ff.following_feed_info()["last_scan_at"] is None


def test_scheduler_following_scan_runs_without_toggle(state_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake(action, *, pipeline_index=None):
        calls.append(action)
        return {}

    scheduler = AutoScheduler(job_runner=MagicMock())
    scheduler._click_and_wait = fake
    scheduler._run_following_scan("following-2026-07-17-06")
    assert calls == ["following_feed_scan"]
    assert "following-2026-07-17-06" in scheduler._done_following


def test_scheduler_cleanup_runs_without_toggle(state_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup.auto_maintenance_paused_state",
        lambda: (False, ""),
    )
    calls: list[str] = []

    def fake(action, *, pipeline_index=None):
        calls.append(action)
        return {"skipped": False}

    scheduler = AutoScheduler(job_runner=MagicMock())
    scheduler._click_and_wait = fake
    scheduler._run_maintenance("2026-07-17-06-maintain")
    assert calls == ["cleanup_auto_maintain"]


def test_next_scan_follows_cadence() -> None:
    now = int(time.time())
    result = ff.next_following_scan_at(now)
    assert result is not None and result > now
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromtimestamp(result, tz=timezone(timedelta(hours=8)))
    assert dt.minute == 3
    assert dt.hour % 6 == 1


def test_next_scan_hours_are_0103_0703_1303_1903() -> None:
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    samples = [
        datetime(2026, 7, 17, 0, 40, tzinfo=tz),
        datetime(2026, 7, 17, 6, 40, tzinfo=tz),
        datetime(2026, 7, 17, 12, 40, tzinfo=tz),
        datetime(2026, 7, 17, 18, 40, tzinfo=tz),
    ]
    for now in samples:
        result = ff.next_following_scan_at(int(now.timestamp()))
        dt = datetime.fromtimestamp(result, tz=tz)
        assert dt.minute == 3
        assert dt.hour in (1, 7, 13, 19)
        assert dt.hour % 6 == 1


def test_next_scan_crosses_day() -> None:
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    now = datetime(2026, 7, 17, 19, 40, tzinfo=tz)
    result = ff.next_following_scan_at(int(now.timestamp()))
    dt = datetime.fromtimestamp(result, tz=tz)
    assert dt.day == 18
    assert (dt.hour, dt.minute) == (1, 3)


def test_cadence_does_not_overlap_refresh_cleanup_triple() -> None:
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    now = datetime(2026, 7, 17, 11, 0, tzinfo=tz)
    for _ in range(3):
        result = ff.next_following_scan_at(int(now.timestamp()))
        dt = datetime.fromtimestamp(result, tz=tz)
        assert dt.hour % 6 == 1
        assert dt.minute == 3
        # 不落公共刷新整点/清理偶数整点/TRIPLE_MINUTES
        assert dt.hour not in {0, 3, 6, 9, 12, 15, 18, 21}
        assert dt.hour % 2 != 0
        assert dt.minute not in {5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}
        now = datetime.fromtimestamp(result + 1, tz=tz)
