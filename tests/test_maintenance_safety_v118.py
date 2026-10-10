from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from src.platform_risk import risk_code_of


def _risk(code):
    if code != 429:
        return RuntimeError(f"API error {code}: test")
    response = httpx.Response(429, request=httpx.Request("GET", "https://invalid.test"))
    return httpx.HTTPStatusError("limited", request=response.request, response=response)


@pytest.mark.parametrize("script", ["backfill_missing_details", "rebuild_all_activity_details", "retry_failed_rebuild"])
@pytest.mark.parametrize("code", [-352, -509, 429])
def test_maintenance_risk_stops_next_item_and_preserves_success(isolated_home, monkeypatch, script, code):
    module = importlib.import_module(f"scripts.{script}")
    tasks = [("10001", "转发抽奖"), ("10002", "转发抽奖"), ("10003", "转发抽奖")]
    if script == "backfill_missing_details":
        monkeypatch.setattr(module, "list_missing_enrich_tasks", lambda: tasks)
    else:
        monkeypatch.setattr(module, "list_all_enrichable_tasks", lambda: tasks)
    monkeypatch.setattr(module, "_load_existing_activities", lambda: {})
    monkeypatch.setattr(module, "load_participations", lambda: {})
    monkeypatch.setattr(module, "reset_detail_api_state", lambda: None)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module, "_all_enriched_activities", lambda **kwargs: list(kwargs["existing_by_id"].values()))
    monkeypatch.setattr(module, "_build_result", lambda rows, **kwargs: {"rows": rows, **kwargs})
    saved = Mock()
    monkeypatch.setattr(module, "save_enriched", saved)
    row = SimpleNamespace(skipped=False, to_dict=lambda: {"dynamic_id": "10001"})
    enrich = Mock(side_effect=[row, _risk(code), AssertionError("must stop")])
    monkeypatch.setattr(module, "_enrich_one", enrich)
    if script == "retry_failed_rebuild":
        path = module.failure_path()
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(json.dumps([{"dynamic_id": key, "lottery_type": kind} for key, kind in tasks]), encoding="utf-8")
    with pytest.raises(Exception) as error:
        module._main_impl()
    assert risk_code_of(error.value) == code
    assert enrich.call_count == 2
    saved.assert_called_once()
    assert saved.call_args.args[0]["rows"] == [{"dynamic_id": "10001"}]
    assert saved.call_args.args[0]["complete"] is False


@pytest.mark.parametrize("script, function", [("discover_watch_users", "measure_forward_ratio"), ("replenish_watch_users", "forward_ratio")])
@pytest.mark.parametrize("code", [-352, -509, 429])
def test_watch_maintenance_ratio_cannot_swallow_risk(isolated_home, script, function, code):
    module = importlib.import_module(f"scripts.{script}")
    client = Mock()
    client.get_json.side_effect = _risk(code)
    with pytest.raises(Exception) as error:
        getattr(module, function)(client, 123)
    assert risk_code_of(error.value) == code
    client.get_json.assert_called_once()


def test_replenish_reaction_risk_stops_all_later_requests(isolated_home, monkeypatch):
    from scripts import replenish_watch_users as module
    path = module.candidates_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"users":[]}', encoding="utf-8")
    monkeypatch.setattr(module, "collect_lottery_ids", lambda _: ["10001", "10002"])
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.get_json.side_effect = _risk(-352)
    monkeypatch.setattr(module, "BilibiliClient", lambda **kwargs: client)
    with pytest.raises(RuntimeError):
        module._main_impl()
    client.get_json.assert_called_once()
    assert json.loads(path.read_text(encoding="utf-8")) == {"users": []}


def test_maintenance_profile_paths_and_inputs_are_independent(tmp_path, monkeypatch):
    from src import data_paths
    from scripts import replenish_watch_users as replenish, discover_watch_users as discover, retry_failed_rebuild as retry
    root = tmp_path / "data-root"
    for profile, key in [("account-1", "1251573729479098372"), ("account-2", "1251573729479098373")]:
        target = root / "profiles" / profile
        (target / "output").mkdir(parents=True)
        (target / "profile.json").write_text(json.dumps({"profile_id": profile}), encoding="utf-8")
        (target / "output/ds2_latest.json").write_text(json.dumps({"activity_links": [f"https://t.bilibili.com/{key}"]}), encoding="utf-8")
    monkeypatch.setenv("BINGGO_DATA_ROOT", str(root))
    for profile, key in [("account-1", "1251573729479098372"), ("account-2", "1251573729479098373")]:
        (root / "active_profile.json").write_text(json.dumps({"profile_id": profile}), encoding="utf-8")
        data_paths.reset_runtime_profile_for_tests()
        from src.db.engine import reset_engine_for_tests
        from src.db.schema import init_db
        from src.db.snapshots import save_ds_check_dict
        reset_engine_for_tests()
        init_db()
        save_ds_check_dict({"source_id": "DS-2", "activity_links": [f"https://t.bilibili.com/{key}"]})
        assert replenish.candidates_path().is_relative_to(root / "profiles" / profile)
        assert retry.failure_path().is_relative_to(root / "profiles" / profile)
        assert discover.collect_lottery_dynamic_ids(lottery_limit=10) == [key]
        assert replenish.collect_lottery_ids(10) == [key]
    reset_engine_for_tests()


@pytest.mark.parametrize("code", [-315, -412])
def test_ordinary_maintenance_error_is_not_formal(isolated_home, code):
    from scripts import replenish_watch_users as module
    client = Mock()
    client.get_json.side_effect = _risk(code)
    assert module.forward_ratio(client, 123) == 0
    assert risk_code_of(_risk(code)) is None
