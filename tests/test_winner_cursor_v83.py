"""Offline, multi-run feed fixtures with opaque server continuation tokens."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from src.notification.service import NotificationLocalError
from src.platform_risk import risk_code_of
from src.winner_scan import service as winmod


class Notify:
    def __init__(self):
        self.calls = []

    def dispatch(self, event_id, **kwargs):
        self.calls.append(event_id)
        return {}


class FeedApi:
    """Cursor tokens designate continuations, not sortable message IDs.

    Insertion at the top changes page endings but leaves a previously issued
    continuation usable. Both production list_feed and normalization run here.
    """

    page_size = 2

    def __init__(self):
        self.items = {"at": [], "reply": []}
        self.calls = []
        self.faults = {}
        self.serial = 0

    @staticmethod
    def token(item):
        serial = item["serial"]
        return (100000 - serial * 17, 500000 + serial * 31)

    def prepend(self, kind, count):
        added = []
        for _ in range(count):
            self.serial += 1
            added.append({
                "serial": self.serial,
                "id": f"event-{self.serial}",
                "reply_time": self.serial + 100,
                "user": {"mid": 7, "nickname": "发送者"},
                "item": {"source_content": "恭喜中奖，请填写地址"},
            })
        self.items[kind][0:0] = list(reversed(added))

    def head(self, kind):
        return self.token(self.items[kind][self.page_size - 1])

    def request_json(self, url, params=None, **kwargs):
        if "get_sessions" in url:
            self.calls.append(("dm", (0, 0)))
            return {"code": 0, "data": {"session_list": []}}
        kind = url.rsplit("/", 1)[-1]
        assert kind in self.items, f"unexpected remote request: {url}"
        params = params or {}
        cursor = (int(params.get("id", 0)), int(params.get("reply_time", 0)))
        self.calls.append((kind, cursor))
        fault = self.faults.get((kind, cursor))
        if fault is not None:
            if isinstance(fault, Exception):
                raise fault
            if callable(fault):
                return fault()
            return copy.deepcopy(fault)
        items = self.items[kind]
        start = 0
        if cursor != (0, 0):
            start = next(i + 1 for i, item in enumerate(items) if self.token(item) == cursor)
        page = items[start:start + self.page_size]
        end = start + len(page) >= len(items)
        token = self.token(page[-1]) if page else (0, 0)
        return {"code": 0, "data": {
            "items": copy.deepcopy(page),
            "cursor": {"is_end": end, "id": token[0], "time": token[1]},
        }}

    def requests(self, kind):
        return [cursor for source, cursor in self.calls if source == kind]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(winmod, "get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(winmod, "require_login", lambda: ("csrf", 101))
    api = FeedApi()
    notify = Notify()
    return api, notify


def source(kind):
    return "mention" if kind == "at" else "reply"


def state(kind):
    return winmod._load_state()[source(kind)]


def completed(kind):
    current = state(kind)
    return current["cursor_id"], current["cursor_time"]


def run(api, notify, **kwargs):
    # A fresh service instance every time also exercises persisted restart state.
    return winmod.WinnerScanService(notification=notify).run(api, **kwargs)


def baseline(api, notify, kind):
    api.prepend(kind, 10)
    run(api, notify)
    assert completed(kind) == api.head(kind)
    api.calls.clear()
    return completed(kind)


@pytest.mark.parametrize("kind", ["at", "reply"])
def test_completed_boundary_moves_and_idle_round_reads_one_page(setup, kind):
    api, notify = setup
    old = baseline(api, notify, kind)
    api.prepend(kind, 2)
    head = api.head(kind)
    result = run(api, notify)
    assert result["sources"][source(kind)]["advance"] is True
    assert len(api.requests(kind)) == 2
    assert completed(kind) == head != old
    count = len(notify.calls)
    api.calls.clear()
    run(api, notify)
    assert api.requests(kind) == [(0, 0)]
    assert completed(kind) == head
    assert len(notify.calls) == count


@pytest.mark.parametrize("kind", ["at", "reply"])
@pytest.mark.parametrize("pages_per_round", [1, 5])
def test_ten_consecutive_rounds_have_bounded_page_counts(setup, kind, pages_per_round):
    api, notify = setup
    baseline(api, notify, kind)
    counts = []
    for _ in range(10):
        api.prepend(kind, api.page_size * pages_per_round)
        head = api.head(kind)
        api.calls.clear()
        result = run(api, notify)
        counts.append(len(api.requests(kind)))
        assert result["sources"][source(kind)]["advance"] is True
        assert completed(kind) == head
        assert state(kind)["catch_up"] is None
    assert counts == [pages_per_round + 1] * 10
    assert len(notify.calls) == len(set(notify.calls)) == 2 + 20 * pages_per_round


@pytest.mark.parametrize("kind", ["at", "reply"])
def test_page_endings_shift_without_sorting_or_comparing_message_ids(setup, kind):
    api, notify = setup
    baseline(api, notify, kind)
    for added in [1, 3] * 5:
        api.prepend(kind, added)
        api.calls.clear()
        result = run(api, notify)
        assert result["sources"][source(kind)]["advance"] is True
        assert len(api.requests(kind)) <= 3
        assert completed(kind) == api.head(kind)


@pytest.mark.parametrize("kind", ["at", "reply"])
@pytest.mark.parametrize("new_top_pages", [0, 5])
@pytest.mark.parametrize("legacy", [False, True])
def test_30_page_backlog_resumes_after_restart_and_then_reads_new_top(setup, kind, new_top_pages, legacy):
    api, notify = setup
    old = baseline(api, notify, kind)
    if legacy:
        old_state = winmod._load_state()
        old_state[source(kind)].pop("checkpoint_keys")
        old_state[source(kind)].pop("catch_up")
        winmod._save_state(old_state)
    api.prepend(kind, 60)
    frozen_head = api.head(kind)
    first = run(api, notify)
    pending = state(kind)["catch_up"]
    resume = tuple(pending["resume_cursor"][key] for key in ("id", "time"))
    first_requests = api.requests(kind)
    assert len(first_requests) == 20
    assert first["sources"][source(kind)]["status"] == "incomplete"
    assert first["sources"][source(kind)]["advance"] is False
    assert first["sources"][source(kind)]["found"] == 40
    assert completed(kind) == old
    assert pending["head_cursor"] == {"id": frozen_head[0], "time": frozen_head[1]}

    api.prepend(kind, new_top_pages * api.page_size)
    api.calls.clear()
    second = run(api, notify)
    assert api.requests(kind)[0] == resume
    assert len(api.requests(kind)) == 11
    assert set(api.requests(kind)).isdisjoint(first_requests)
    assert second["sources"][source(kind)]["advance"] is True
    assert completed(kind) == frozen_head
    assert state(kind)["catch_up"] is None

    api.calls.clear()
    run(api, notify)
    assert len(api.requests(kind)) == new_top_pages + 1
    assert completed(kind) == api.head(kind)
    assert len(notify.calls) == len(set(notify.calls)) == 62 + 2 * new_top_pages


def _pending_backlog(api, notify, kind):
    baseline(api, notify, kind)
    api.prepend(kind, 60)
    run(api, notify)
    before = state(kind)
    cursor = before["catch_up"]["resume_cursor"]
    api.calls.clear()
    return before, (cursor["id"], cursor["time"])


@pytest.mark.parametrize("kind", ["at", "reply"])
@pytest.mark.parametrize("fault", [
    "network", "missing", "malformed", "repeated", "cross_batch_loop", "bad_items",
    "missing_flag", "boolean_cursor", "fractional_cursor", "malformed_end",
])
def test_bad_batch_preserves_completed_and_resume_positions(setup, kind, fault):
    api, notify = setup
    before, resume = _pending_backlog(api, notify, kind)
    cursor = {"is_end": False, "id": resume[0], "time": resume[1]}
    if fault == "network":
        payload = httpx.ConnectError("offline simulated interruption")
    elif fault == "missing":
        payload = {"code": 0, "data": {"items": []}}
    elif fault == "malformed":
        payload = {"code": 0, "data": {"items": [], "cursor": {"is_end": False, "id": "bad", "time": 1}}}
    elif fault == "bad_items":
        payload = {"code": 0, "data": {"items": [None], "cursor": {"is_end": True}}}
    elif fault in {"missing_flag", "boolean_cursor", "fractional_cursor", "malformed_end"}:
        if fault == "missing_flag":
            cursor.pop("is_end")
        elif fault == "boolean_cursor":
            cursor["id"] = True
        elif fault == "fractional_cursor":
            cursor["id"] = 1.5
        else:
            cursor = {"is_end": True, "id": "bad"}
        payload = {"code": 0, "data": {"items": [], "cursor": cursor}}
    else:
        if fault == "cross_batch_loop":
            cursor.update(before["catch_up"]["head_cursor"])
        payload = {"code": 0, "data": {"items": [], "cursor": cursor}}
    api.faults[(kind, resume)] = payload
    result = run(api, notify)
    assert result["sources"][source(kind)]["advance"] is False
    assert api.requests(kind) == [resume]
    after = state(kind)
    assert after["cursor_id"] == before["cursor_id"]
    assert after["cursor_time"] == before["cursor_time"]
    assert after["catch_up"] == before["catch_up"]
    api.faults.clear()
    assert run(api, notify)["sources"][source(kind)]["advance"] is True


@pytest.mark.parametrize("code", [-352, -509, 429])
def test_risk_during_resume_preserves_state_and_stops_other_sources(setup, code):
    api, notify = setup
    before, resume = _pending_backlog(api, notify, "at")
    if code == 429:
        request = httpx.Request("GET", "https://api.bilibili.com/x/msgfeed/at")
        fault = httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
    else:
        fault = {"code": code, "message": "平台拒绝"}
    api.faults[("at", resume)] = fault
    with pytest.raises(Exception) as caught:
        run(api, notify)
    assert risk_code_of(caught.value) == code
    assert api.calls == [("at", resume)]
    assert state("at") == before


def test_cancellation_between_pages_does_not_advance_resume(setup):
    api, notify = setup
    before, resume = _pending_backlog(api, notify, "at")
    with pytest.raises(RuntimeError, match="任务已取消"):
        run(api, notify, cancel_check=lambda: bool(api.requests("at")))
    assert api.calls == [("at", resume)]
    assert state("at")["catch_up"] == before["catch_up"]
    assert completed("at") == (before["cursor_id"], before["cursor_time"])


@pytest.mark.parametrize("stage", ["candidates", "notification"])
def test_safe_processing_must_finish_before_saving_resume(setup, monkeypatch, stage):
    api, notify = setup
    old = baseline(api, notify, "at")
    api.prepend("at", 60)

    def fail(*args, **kwargs):
        raise NotificationLocalError("simulated durable write failure")

    if stage == "candidates":
        monkeypatch.setattr(winmod, "_save_candidates", fail)
    else:
        monkeypatch.setattr(notify, "dispatch", fail)
    result = run(api, notify)
    assert result["sources"]["mention"]["advance"] is False
    assert completed("at") == old
    assert state("at")["catch_up"] is None


def test_old_profile_without_new_fields_keeps_its_completed_boundary(setup, tmp_path):
    api, notify = setup
    old = baseline(api, notify, "at")
    stored = json.loads((tmp_path / winmod._STATE_FILE).read_text(encoding="utf-8"))
    for kind in ("mention", "reply"):
        stored["sources"][kind].pop("checkpoint_keys")
        stored["sources"][kind].pop("catch_up")
    (tmp_path / winmod._STATE_FILE).write_text(json.dumps(stored), encoding="utf-8")
    assert completed("at") == old
    api.prepend("at", 2)
    assert run(api, notify)["sources"]["mention"]["advance"] is True
    assert len(api.requests("at")) == 2
    assert completed("at") == api.head("at")


def test_resume_is_profile_local(setup, tmp_path, monkeypatch):
    api, notify = setup
    before, resume = _pending_backlog(api, notify, "at")
    other = tmp_path / "other-profile"
    monkeypatch.setattr(winmod, "get_profile_dir", lambda: other)
    assert state("at")["catch_up"] is None
    assert completed("at") == (0, 0)
    run(api, notify)
    assert api.requests("at") == [(0, 0)]
    monkeypatch.setattr(winmod, "get_profile_dir", lambda: tmp_path)
    assert state("at")["catch_up"] == before["catch_up"]
    api.calls.clear()
    run(api, notify)
    assert api.requests("at")[0] == resume


@pytest.mark.parametrize("kind", ["at", "reply"])
def test_checkpoint_page_split_across_batches_keeps_remaining_exact_keys(setup, kind):
    api, notify = setup
    old = baseline(api, notify, kind)
    api.prepend(kind, 39)
    head = api.head(kind)
    result = run(api, notify)
    assert result["sources"][source(kind)]["advance"] is False
    assert len(api.requests(kind)) == 20
    assert len(state(kind)["catch_up"]["remaining_keys"]) == 1
    assert completed(kind) == old
    api.calls.clear()
    result = run(api, notify)
    assert result["sources"][source(kind)]["advance"] is True
    assert len(api.requests(kind)) == 1
    assert state(kind)["catch_up"] is None
    assert completed(kind) == head
    assert len(notify.calls) == len(set(notify.calls))
