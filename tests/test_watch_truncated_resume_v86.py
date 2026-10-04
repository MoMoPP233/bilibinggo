"""Offline WATCH action -> durable resume -> new client -> Pipeline regressions."""
from __future__ import annotations

import importlib
import json
import threading
from collections import Counter
from pathlib import Path

import httpx
import pytest

from src import watch_feed as feed, watch_resume as resume, watch_sync as sync
from src.pipeline.refresh_all_pipeline import PipelineResult
from src.platform_risk import risk_code_of
from src.state_store import get_watch_last_synced_at, set_watch_last_synced_at
from src.watch_users import WatchUser
from web import actions

NOW = 1_780_000_000
START = NOW - 1000


def item(uid, number, ts):
    return {
        "id_str": str(1300000000000000000 + uid * 10000 + number),
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {"module_author": {"pub_ts": ts}},
        "orig": {"id_str": str(1200000000000000000 + uid * 10000 + number)},
    }


class Space:
    """Opaque stable offsets: adding a top page never renumbers old cursors."""
    def __init__(self):
        self.pages = {}
        self.requests = []
        self.faults = {}
        self.after_request = None
        self.clients = []

    def add(self, uid, count):
        self.pages[uid] = [
            (f"p{n}", item(uid, n, NOW - 100 - n)) for n in range(1, count + 1)
        ]

    def client(self, **kwargs):
        space = self

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get_json(self, url, *, params, **kwargs):
                assert url == feed.SPACE_FEED_URL
                uid, offset = params["host_mid"], params["offset"]
                pages = space.pages[uid]
                index = 0 if not offset else next(i + 1 for i, (key, _) in enumerate(pages) if key == offset)
                key, entry = pages[index]
                space.requests.append((uid, key))
                fault = space.faults.get((uid, offset))
                if isinstance(fault, Exception):
                    raise fault
                if fault is not None:
                    return fault
                if space.after_request:
                    space.after_request(uid, key)
                more = index < len(pages) - 1
                return {"code": 0, "data": {"items": [entry], "offset": key if more else "", "has_more": more}}

        client = Client()
        self.clients.append(client)
        return client


@pytest.fixture
def env(isolated_home, monkeypatch):
    space = Space()
    users = []
    clock = [NOW]
    batches = []
    outcome = {"partial": False, "ok": True, "error": None}
    monkeypatch.setattr(sync, "BilibiliClient", space.client)
    monkeypatch.setattr(sync, "list_watch_users", lambda: list(users))
    monkeypatch.setattr(sync.time, "time", lambda: clock[0])
    monkeypatch.setattr(sync.time, "sleep", lambda *_: None)
    monkeypatch.setattr(actions, "invalidate_activity_cache", lambda: None)

    def pipeline(links, **kwargs):
        batches.append(list(links))
        if outcome["error"]:
            raise outcome["error"]
        return PipelineResult(
            ok=outcome["ok"], partial=outcome["partial"], pipeline_skipped=False,
            raw_link_count=len(links), new_link_count=len(links), classified_count=len(links),
            skipped_count=0, enriched_count=0, persisted_count=len(links), message="offline pipeline",
        )

    monkeypatch.setattr(actions, "run_new_links_pipeline", pipeline)
    set_watch_last_synced_at(START)

    def configure(counts):
        for uid, count in counts.items():
            space.add(uid, count)
        users[:] = [WatchUser(uid, f"用户{uid}") for uid in counts]

    return space, users, clock, batches, outcome, configure


def run(**kwargs):
    return actions.run_action("refresh_watch", {}, **kwargs)


def user_state(uid=2):
    return resume.load_state()["users"][str(uid)]


def test_one_page_commits_after_pipeline(env):
    space, _, _, batches, _, configure = env
    configure({2: 1})
    payload = run()
    assert payload["result"]["watch"]["source_complete"]
    assert get_watch_last_synced_at() == NOW
    assert not resume.load_state()["active"]
    assert space.requests == [(2, "p1")]
    assert len(batches[0]) == 1


def test_page_30_reaches_lower_boundary_is_complete(env):
    space, _, _, _, _, configure = env
    configure({2: 35})
    space.pages[2][29][1]["modules"]["module_author"]["pub_ts"] = START - 1
    run()
    assert len(space.requests) == 30
    assert get_watch_last_synced_at() == NOW
    assert not resume.load_state()["active"]


def test_page_30_server_end_is_complete(env):
    space, _, _, _, _, configure = env
    configure({2: 30})
    run()
    assert len(space.requests) == 30
    assert get_watch_last_synced_at() == NOW


def test_35_pages_two_rounds_resume_not_repeat(env):
    space, _, _, batches, _, configure = env
    configure({2: 35})
    first = run()
    assert len(space.requests) == 30
    assert not first["result"]["watch"]["source_complete"]
    assert get_watch_last_synced_at() == START
    assert user_state()["offset"] == "p30"
    assert resume.load_state()["pending_links"] == []  # Pipeline acknowledged this batch
    assert len(batches[0]) == 30
    space.requests.clear()
    run()
    assert space.requests == [(2, "p1")] + [(2, f"p{n}") for n in range(31, 36)]
    assert len(batches[1]) == 5
    assert get_watch_last_synced_at() == NOW
    assert not resume.load_state()["active"]


def test_abc_restart_chain_keeps_complete_users_and_resumes_b(env, monkeypatch):
    space, users, _, batches, _, configure = env
    configure({1: 1, 2: 35, 3: 1})
    run()
    assert Counter(uid for uid, _ in space.requests) == {1: 1, 2: 30, 3: 1}
    assert user_state(1)["complete"] and user_state(3)["complete"]
    assert not user_state(2)["complete"]
    assert len(batches[0]) == 32
    assert get_watch_last_synced_at() == START
    # Reload service modules: no in-memory cursor survives. A fresh client is created.
    importlib.reload(resume)
    importlib.reload(sync)
    monkeypatch.setattr(sync, "BilibiliClient", space.client)
    monkeypatch.setattr(sync, "list_watch_users", lambda: list(users))
    monkeypatch.setattr(actions, "sync_watch_forwards", sync.sync_watch_forwards)
    space.requests.clear()
    run()
    assert space.requests == [(1, "p1"), (2, "p1")] + [(2, f"p{n}") for n in range(31, 36)] + [(3, "p1")]
    assert len(space.clients) == 2 and space.clients[0] is not space.clients[1]
    assert len(batches[1]) == 5
    assert get_watch_last_synced_at() == NOW
    assert not resume.load_state()["active"]


def test_new_top_page_during_catchup_is_discovered_and_window_then_closes(env):
    space, _, clock, batches, _, configure = env
    configure({2: 35})
    run()
    clock[0] += 100
    top = item(2, 1001, NOW + 50)
    space.pages[2].insert(0, ("new-top", top))
    space.requests.clear()
    run()
    assert space.requests == [(2, "new-top")] + [(2, f"p{n}") for n in range(31, 36)]
    assert any(top["orig"]["id_str"] in link for link in batches[1])
    assert len(batches[1]) == 6
    assert get_watch_last_synced_at() == NOW  # NOT now+100
    assert resume.load_state()["next_window_start"] == NOW
    space.requests.clear()
    run()
    assert space.requests == [(2, "new-top"), (2, "p1")]
    assert get_watch_last_synced_at() == NOW + 100
    assert resume.load_state() == {"version": 1, "active": False, "next_window_start": None}


def test_multiple_rounds_95_pages_always_advance_and_keep_budget(env):
    space, _, _, batches, _, configure = env
    configure({2: 95})
    for end in (30, 59, 88):
        space.requests.clear()
        run()
        assert len(space.requests) == 30
        assert user_state()["offset"] == f"p{end}"
        assert get_watch_last_synced_at() == START
    space.requests.clear()
    run()
    assert space.requests == [(2, "p1")] + [(2, f"p{n}") for n in range(89, 96)]
    assert get_watch_last_synced_at() == NOW
    assert sum(map(len, batches)) == 95


@pytest.mark.parametrize("error", [httpx.ConnectError("offline"), httpx.ReadTimeout("timeout"), httpx.ConnectError("TLS failure")])
def test_network_failure_preserves_known_good_resume_and_candidates(env, error):
    space, _, _, _, _, configure = env
    configure({2: 35})
    run()
    space.faults[(2, "p31")] = error
    with pytest.raises(RuntimeError):
        run()
    assert user_state()["offset"] == "p31"
    assert len(resume.load_state()["pending_links"]) == 1
    assert get_watch_last_synced_at() == START
    assert risk_code_of(error) is None
    space.faults.clear()
    space.requests.clear()
    run()
    assert space.requests == [(2, "p1")] + [(2, f"p{n}") for n in range(32, 36)]
    assert get_watch_last_synced_at() == NOW


@pytest.mark.parametrize("bad", [
    {"items": [], "offset": "p31", "has_more": True},
    {"items": [item(2, 31, NOW - 131)], "offset": {}, "has_more": True},
    {"items": [item(2, 31, NOW - 131)], "offset": "", "has_more": True},
    {"items": [item(2, 31, NOW - 131)], "offset": "p31", "has_more": "true"},
    {"items": None, "offset": "p31", "has_more": True},
    {"items": [{}], "offset": "p31", "has_more": True},
    {"items": [item(2, 31, NOW - 131)]},
    {"items": [item(2, 31, NOW - 131)], "offset": "p31", "has_more": None},
])
def test_malformed_page_does_not_advance(env, bad):
    space, _, _, _, _, configure = env
    configure({2: 35})
    run()
    space.faults[(2, "p30")] = {"code": 0, "data": bad}
    with pytest.raises(ValueError):
        run()
    assert user_state()["offset"] == "p30"
    assert get_watch_last_synced_at() == START


@pytest.mark.parametrize("offset", ["p30", "p10"])
def test_repeated_cursor_including_prior_round_stops_without_loop(env, offset):
    space, _, _, _, _, configure = env
    configure({2: 35})
    run()
    space.requests.clear()
    space.faults[(2, "p30")] = {"code": 0, "data": {
        "items": [item(2, 31, NOW - 131)], "offset": offset, "has_more": True,
    }}
    with pytest.raises(ValueError, match="重复"):
        run()
    assert space.requests == [(2, "p1"), (2, "p31")]
    assert user_state()["offset"] == "p30"


def test_cancel_retains_partial_page_batch_without_checkpoint(env):
    space, _, _, batches, _, configure = env
    configure({1: 1, 2: 35, 3: 1})
    event = threading.Event()
    space.after_request = lambda uid, key: event.set() if (uid, key) == (2, "p10") else None
    with pytest.raises(ValueError, match="取消"):
        run(cancel_event=event)
    assert space.requests[-1] == (2, "p10")
    assert not any(uid == 3 for uid, _ in space.requests)
    assert user_state()["offset"] == "p10"
    assert len(resume.load_state()["pending_links"]) == 11
    assert not batches
    assert get_watch_last_synced_at() == START


@pytest.mark.parametrize("code", [-352, -509, 429])
@pytest.mark.parametrize("offset", ["", "p30"])
def test_formal_risk_stops_exactly_once_no_other_user_requests(env, code, offset):
    space, _, _, _, _, configure = env
    configure({1: 1, 2: 35, 3: 1})
    if offset:
        run()
    response = httpx.Response(429, request=httpx.Request("GET", feed.SPACE_FEED_URL))
    fault = (
        httpx.HTTPStatusError("HTTP 429", request=response.request, response=response)
        if code == 429 else {"code": code, "message": "平台返回"}
    )
    space.faults[(2, offset)] = fault
    space.requests.clear()
    with pytest.raises(Exception) as caught:
        run()
    assert risk_code_of(caught.value) == code
    assert space.requests == ([(1, "p1"), (2, "p1"), (2, "p31")] if offset else [(1, "p1"), (2, "p1")])
    if code == 429:
        assert caught.value is fault
    assert get_watch_last_synced_at() == START


def test_removed_b_does_not_block_completion(env):
    space, users, _, _, _, configure = env
    configure({1: 1, 2: 35, 3: 1})
    run()
    users[:] = [user for user in users if user.mid != 2]
    space.requests.clear()
    run()
    assert space.requests == [(1, "p1"), (3, "p1")]
    assert get_watch_last_synced_at() == NOW
    assert not resume.load_state()["active"]


def test_new_d_has_independent_fresh_window_and_cursor(env):
    space, users, clock, _, _, configure = env
    configure({2: 65})
    run()
    clock[0] += 100
    users.append(WatchUser(4, "新用户"))
    space.add(4, 1)
    space.requests.clear()
    run()
    assert space.requests[-1] == (4, "p1")
    assert user_state(4)["since_ts"] == START
    assert user_state(4)["until_ts"] == NOW + 100
    assert user_state(4)["complete"]
    assert user_state()["until_ts"] == NOW
    assert user_state()["offset"] == "p59"
    assert get_watch_last_synced_at() == START


def test_same_uid_profile_state_isolation(env, monkeypatch, tmp_path):
    _, _, _, _, _, configure = env
    configure({2: 35})
    # Production lookup uses the actual runtime Profile database directory.
    from src import data_paths
    from src.db.schema import init_db
    monkeypatch.setattr(data_paths, "get_database_path", lambda profile_id=None: data_paths.get_profile_dir("account-1") / "binggo.db")
    init_db()
    set_watch_last_synced_at(START)
    run()
    path_a = resume.state_path()
    assert user_state()["offset"] == "p30"
    monkeypatch.setattr(data_paths, "get_database_path", lambda profile_id=None: data_paths.get_profile_dir("account-2") / "binggo.db")
    init_db()
    set_watch_last_synced_at(START)
    assert resume.state_path() != path_a
    assert resume.load_state() is None
    run()
    assert user_state()["offset"] == "p30"
    assert json.loads(path_a.read_text(encoding="utf-8"))["users"]["2"]["offset"] == "p30"


def test_old_profile_only_checkpoint_keeps_existing_ten_day_window(env):
    _, _, _, _, _, configure = env
    configure({2: 35})
    set_watch_last_synced_at(NOW - 20 * 86400)
    assert resume.load_state() is None
    run()
    assert user_state()["since_ts"] == NOW - 10 * 86400
    assert get_watch_last_synced_at() == NOW - 20 * 86400


@pytest.mark.parametrize("mode", ["partial", "failed", "exception"])
def test_pipeline_not_complete_keeps_checkpoint_and_durable_candidates(env, mode):
    space, _, _, batches, outcome, configure = env
    configure({2: 35})
    if mode == "exception":
        outcome["error"] = RuntimeError("offline pipeline failure")
        with pytest.raises(RuntimeError, match="pipeline failure"):
            run()
    else:
        outcome["partial" if mode == "partial" else "ok"] = mode == "partial"
        run()
    assert get_watch_last_synced_at() == START
    assert user_state()["offset"] == "p30"
    assert len(resume.load_state()["pending_links"]) == 30
    outcome.update(partial=False, ok=True, error=None)
    space.requests.clear()
    run()
    assert len(space.requests) == 6
    assert len(batches[-1]) == 35
    assert get_watch_last_synced_at() == NOW


def test_source_complete_pipeline_partial_still_no_checkpoint(env):
    _, _, _, batches, outcome, configure = env
    configure({2: 1})
    outcome["partial"] = True
    run()
    assert user_state()["complete"]
    assert len(resume.load_state()["pending_links"]) == 1
    assert get_watch_last_synced_at() == START
    outcome["partial"] = False
    run()
    assert batches[0] == batches[1]
    assert get_watch_last_synced_at() == NOW


@pytest.mark.parametrize("corrupt", ["json", "missing", "offset", "visited", "version"])
def test_corrupt_state_fails_closed_before_remote_requests(env, corrupt):
    space, _, _, _, _, configure = env
    configure({2: 35})
    run()
    state = resume.load_state()
    if corrupt == "missing":
        del state["users"]["2"]["since_ts"]
    elif corrupt == "offset":
        state["users"]["2"]["offset"] = {"bad": 1}
    elif corrupt == "visited":
        state["users"]["2"]["offset"] = "p10"
    elif corrupt == "version":
        state["version"] = 999
    resume.state_path().write_text("{broken" if corrupt == "json" else json.dumps(state), encoding="utf-8")
    space.requests.clear()
    with pytest.raises(ValueError, match="状态损坏"):
        run()
    assert space.requests == []
    assert get_watch_last_synced_at() == START


def test_long_catchup_does_not_clamp_away_unsampled_new_pages(env):
    space, _, clock, batches, _, configure = env
    configure({2: 35})
    run()
    # More new pages than the one-page discovery budget, while old work is owed.
    middle = item(2, 1001, NOW + 2 * 86400)
    latest = item(2, 1002, NOW + 11 * 86400)
    space.pages[2][:0] = [("latest", latest), ("middle", middle)]
    clock[0] = NOW + 12 * 86400
    run()
    assert get_watch_last_synced_at() == NOW
    assert resume.load_state()["next_window_start"] == NOW
    space.requests.clear()
    run()
    assert len(space.requests) == 3  # latest (outside fixed window), middle, lower-bound page
    assert any(middle["orig"]["id_str"] in link for link in batches[-1])
    assert get_watch_last_synced_at() == NOW + 10 * 86400
    run()
    assert get_watch_last_synced_at() == clock[0]
    assert not resume.load_state()["active"]
    assert resume.load_state()["next_window_start"] is None


def test_candidate_and_resume_write_failure_replays_page_not_skip(env, monkeypatch):
    space, _, _, batches, _, configure = env
    configure({2: 35})
    run()
    real_save = resume.save_state

    def fail_page_31(state):
        if state.get("users", {}).get("2", {}).get("offset") == "p31":
            raise OSError("offline disk failure")
        real_save(state)

    monkeypatch.setattr(resume, "save_state", fail_page_31)
    with pytest.raises(OSError, match="disk failure"):
        run()
    assert user_state()["offset"] == "p30"
    assert get_watch_last_synced_at() == START
    monkeypatch.setattr(resume, "save_state", real_save)
    space.requests.clear()
    run()
    assert space.requests == [(2, "p1")] + [(2, f"p{n}") for n in range(31, 36)]
    assert len(batches[-1]) == 5


def test_interruption_after_checkpoint_before_state_retirement_is_replay_safe(env, monkeypatch):
    _, _, _, batches, _, configure = env
    configure({2: 1})
    real_save = resume.save_state

    def fail_retirement(state):
        if not state["active"]:
            raise OSError("offline interruption")
        real_save(state)

    monkeypatch.setattr(resume, "save_state", fail_retirement)
    with pytest.raises(OSError, match="interruption"):
        run()
    assert get_watch_last_synced_at() == NOW
    assert user_state()["complete"]
    assert len(resume.load_state()["pending_links"]) == 1
    monkeypatch.setattr(resume, "save_state", real_save)
    run()
    assert batches[0] == batches[1]
    assert not resume.load_state()["active"]


def test_explicit_page_budget_cannot_exceed_30(env):
    space, _, _, _, _, configure = env
    configure({2: 100})
    result = feed.collect_forward_links_for_user(
        space.client(), 2, since_ts=START, until_ts=NOW, max_pages=100,
    )
    assert result.pages == len(space.requests) == 30
    assert not result.complete
    assert result.next_offset == "p30"


def test_short_page_with_legacy_offset_does_not_mean_end(env):
    space, _, _, _, _, configure = env
    configure({2: 2})
    space.faults[(2, "")] = {"code": 0, "data": {
        "items": [item(2, 1, NOW - 101)], "offset": "p1",
    }}
    run()
    assert space.requests == [(2, "p1"), (2, "p2")]
    assert get_watch_last_synced_at() == NOW


def test_empty_server_end_not_confused_with_user_removal(env):
    space, _, _, _, _, configure = env
    configure({2: 35})
    run()
    space.faults[(2, "p30")] = {"code": 0, "data": {"items": [], "has_more": False}}
    result = sync.sync_watch_forwards()
    assert result.source_complete
    assert "2" in resume.load_state()["users"]
    assert user_state()["complete"]
    assert get_watch_last_synced_at() == START  # still needs Pipeline acknowledgement


def test_head_to_resume_preserves_page_request_delay(env, monkeypatch):
    space, _, _, _, _, configure = env
    configure({2: 35})
    run()
    events = []
    space.after_request = lambda uid, key: events.append(key)
    monkeypatch.setattr(sync.time, "sleep", lambda delay: events.append(delay))
    run()
    assert events[:3] == ["p1", feed.PAGE_REQUEST_DELAY, "p31"]
