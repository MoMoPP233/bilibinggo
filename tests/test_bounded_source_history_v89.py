"""V89 article sources: bounded one-page catch-up, offline only."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import httpx
import pytest

from src.sources.common import commit_source_checkpoint, opus_link
from src.platform_risk import risk_code_of
from src.state_store import get_last_container, get_last_cv_id, save_state


ARTICLE_SOURCES = ("DS-2", "DS-4", "DS-5", "DS-6", "DS-7")
MODULES = {
    "DS-2": "src.sources.ds2_fanqiao",
    "DS-4": "src.sources.ds4_junming",
    "DS-5": "src.sources.ds5_hudong",
    "DS-6": "src.sources.ds6_nuomi",
    "DS-7": "src.sources.ds7_dajinli",
}


def _article(cv: int) -> dict:
    return {"id": cv, "title": f"专栏 {cv}", "publish_time": cv * 100}


def _opus_id(cv: int) -> str:
    return str(1_200_000_000_000_000_000 + cv)


def _dynamic_url(cv: int) -> str:
    return opus_link(str(1_300_000_000_000_000_000 + cv))


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    source_id: str,
    entries: list[int],
    *,
    checkpoint_cv: int | None = 100,
    failure: dict[int, Exception] | None = None,
    link_for_cv=None,
):
    import src.sources.recent_articles as recent

    module = import_module(MODULES[source_id])
    kind = "cv" if source_id in {"DS-2", "DS-4"} else "opus"
    if checkpoint_cv is None:
        save_state({"sources": {}})
    else:
        old_url = (
            f"https://www.bilibili.com/read/cv{checkpoint_cv}"
            if kind == "cv" else opus_link(_opus_id(checkpoint_cv))
        )
        save_state({"sources": {source_id: {"container_url": old_url, "cv_id": str(checkpoint_cv)}}})

    list_calls: list[int] = []
    detail_calls: list[int] = []
    errors = failure if failure is not None else {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get_recent_articles(self, mid: int, *, limit: int = 2):
            assert mid == module.MID
            list_calls.append(limit)
            return [_article(cv) for cv in entries]

        def get_article_detail(self, cv: int):
            detail_calls.append(cv)
            if cv in errors:
                raise errors.pop(cv)
            return {
                "cv_id": cv, "title": f"专栏 {cv}", "publish_time": cv * 100,
                "opus": {"opus_id": _opus_id(cv)},
            }

    def fake_extract(detail: dict, **kwargs):
        cv = detail["cv_id"]
        if kind == "opus":
            assert kwargs["container_opus_id"] == _opus_id(cv)
        if source_id == "DS-4":
            assert kwargs["exclude_link_text"] is module.EXCLUDE_LINK_TEXT_RE
        return [link_for_cv(cv) if link_for_cv else _dynamic_url(cv)], {}

    monkeypatch.setattr(module, "BilibiliClient", FakeClient)
    monkeypatch.setattr(recent, "extract_opus_links_with_hints", fake_extract)
    return module, list_calls, detail_calls


@pytest.mark.parametrize("source_id", ARTICLE_SOURCES)
def test_three_intervening_containers_reach_existing_result_and_checkpoint(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, source_id: str,
) -> None:
    module, list_calls, detail_calls = _setup(monkeypatch, source_id, [103, 102, 101, 100])
    old_checkpoint = get_last_container(source_id)
    result = module.check_update()
    assert result.updated and result.source_complete
    assert result.activity_links == [_dynamic_url(cv) for cv in (101, 102, 103)]
    assert detail_calls == [103, 102, 101]
    assert list_calls == [5]  # formerly one ps=1 list; still one list request
    assert get_last_container(source_id) == old_checkpoint  # source check is not a checkpoint commit
    module.save_result(result)
    commit_source_checkpoint(result)  # normally only after the shared pipeline succeeds
    expected = f"https://www.bilibili.com/read/cv103" if source_id in {"DS-2", "DS-4"} else opus_link(_opus_id(103))
    assert get_last_container(source_id) == expected
    if source_id in {"DS-5", "DS-6", "DS-7"}:
        assert get_last_cv_id(source_id) == "103"

    again = module.check_update()
    assert not again.updated
    assert detail_calls == [103, 102, 101]  # next refresh: no repeat detail/body
    assert list_calls == [5, 5]


@pytest.mark.parametrize("source_id", ["DS-2", "DS-5"])
def test_one_new_container_keeps_old_behavior(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, source_id: str,
) -> None:
    module, list_calls, detail_calls = _setup(monkeypatch, source_id, [101, 100])
    result = module.check_update()
    assert result.source_complete
    assert result.activity_links == [_dynamic_url(101)]
    assert list_calls == [5] and detail_calls == [101]


@pytest.mark.parametrize("source_id", ["DS-2", "DS-5"])
def test_first_run_without_checkpoint_stays_latest_only(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, source_id: str,
) -> None:
    module, list_calls, detail_calls = _setup(monkeypatch, source_id, [103, 102, 101, 100], checkpoint_cv=None)
    result = module.check_update()
    assert result.source_complete
    assert result.activity_links == [_dynamic_url(103)]
    assert list_calls == [5] and detail_calls == [103]


def test_legacy_snapshot_without_new_cache_field_still_catches_up(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.db.snapshots import save_ds_check_dict

    module, list_calls, detail_calls = _setup(monkeypatch, "DS-2", [103, 102, 101, 100])
    save_ds_check_dict({
        "source_id": "DS-2", "updated": True,
        "container_url": "https://www.bilibili.com/read/cv100", "container_id": "100",
        "previous_container_url": "https://www.bilibili.com/read/cv99",
        "activity_links": [_dynamic_url(100)], "link_hints": {}, "checked_at": 1,
    })
    result = module.check_update()
    assert result.source_complete
    assert result.activity_links == [_dynamic_url(cv) for cv in (101, 102, 103)]
    assert list_calls == [5] and detail_calls == [103, 102, 101]


def test_checkpoint_outside_hard_cap_never_advances_or_pages(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, list_calls, detail_calls = _setup(monkeypatch, "DS-2", list(range(108, 99, -1)))
    previous = get_last_container("DS-2")
    result = module.check_update()
    assert not result.source_complete
    assert detail_calls == [108, 107, 106, 105, 104]
    assert list_calls == [5]
    module.save_result(result)
    commit_source_checkpoint(result)
    assert get_last_container("DS-2") == previous
    again = module.check_update()
    assert not again.source_complete
    assert list_calls == [5, 5]
    assert detail_calls == [108, 107, 106, 105, 104]  # persisted cache avoids re-reading


def test_middle_failure_keeps_successes_and_retries_only_failure(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, list_calls, detail_calls = _setup(
        monkeypatch, "DS-2", [103, 102, 101, 100], failure={102: RuntimeError("网络请求失败: TLS EOF")},
    )
    old_checkpoint = get_last_container("DS-2")
    first = module.check_update()
    assert not first.source_complete
    assert first.source_incomplete_containers == ["102"]
    assert first.activity_links == [_dynamic_url(101), _dynamic_url(103)]
    module.save_result(first)
    commit_source_checkpoint(first)
    assert get_last_container("DS-2") == old_checkpoint
    second = module.check_update()
    assert second.source_complete
    assert second.activity_links == [_dynamic_url(cv) for cv in (101, 102, 103)]
    assert detail_calls == [103, 102, 101, 102]
    assert list_calls == [5, 5]
    module.save_result(second)
    commit_source_checkpoint(second)
    assert get_last_container("DS-2") == "https://www.bilibili.com/read/cv103"


@pytest.mark.parametrize("code", [-352, -509])
def test_formal_risk_stops_later_detail_and_preserves_success_snapshot(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, code: int,
) -> None:
    from src.sources.common import load_previous_output

    module, list_calls, detail_calls = _setup(
        monkeypatch, "DS-2", [103, 102, 101, 100], failure={102: RuntimeError(f"API error {code}: 平台返回")},
    )
    old_checkpoint = get_last_container("DS-2")
    with pytest.raises(RuntimeError, match=f"API error {code}"):
        module.check_update()
    assert detail_calls == [103, 102]  # 101 must not be requested after formal risk
    assert list_calls == [5]
    assert get_last_container("DS-2") == old_checkpoint
    snapshot = load_previous_output(module.OUTPUT_PATH)
    assert snapshot and snapshot["source_complete"] is False
    assert snapshot["activity_links"] == [_dynamic_url(103)]


def test_refresh_all_preserves_article_http_429_risk_envelope(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import web.actions as actions

    request = httpx.Request("GET", "https://api.bilibili.com/x/article/view")
    error = httpx.HTTPStatusError("rate limited", request=request, response=httpx.Response(429, request=request))
    module, _, detail_calls = _setup(
        monkeypatch, "DS-2", [103, 102, 101, 100], failure={102: error},
    )
    monkeypatch.setattr(actions, "DS_HANDLERS", [("DS-2", module.check_update, module.save_result)])
    with pytest.raises(httpx.HTTPStatusError) as caught:
        actions.run_action("refresh_all", {})
    assert risk_code_of(caught.value) == 429
    assert detail_calls == [103, 102]


def test_transport_text_containing_risk_token_stays_ordinary_partial(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("GET", "https://api.bilibili.com/x/article/view")
    try:
        raise httpx.ConnectError("TLS EOF", request=request)
    except httpx.ConnectError as cause:
        error = RuntimeError("网络请求失败: old log API error -352: text")
        error.__cause__ = cause
    module, _, detail_calls = _setup(monkeypatch, "DS-2", [103, 102, 101, 100], failure={102: error})
    result = module.check_update()
    assert not result.source_complete
    assert result.activity_links == [_dynamic_url(101), _dynamic_url(103)]
    assert detail_calls == [103, 102, 101]


def test_cross_container_duplicate_dynamic_is_emitted_once(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _, detail_calls = _setup(
        monkeypatch, "DS-2", [103, 102, 101, 100], link_for_cv=lambda cv: _dynamic_url(101),
    )
    result = module.check_update()
    assert result.activity_links == [_dynamic_url(101)]
    assert detail_calls == [103, 102, 101]


def test_three_new_containers_enter_existing_refresh_pipeline_once(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import web.actions as actions
    from src.pipeline.refresh_all_pipeline import PipelineResult

    module, list_calls, detail_calls = _setup(monkeypatch, "DS-2", [103, 102, 101, 100])
    monkeypatch.setitem(actions.DS_HANDLER_BY_ID, "DS-2", (module.check_update, module.save_result))
    pipeline_inputs: list[list[str]] = []

    def fake_pipeline(results, **_kwargs):
        pipeline_inputs.append(results[0].activity_links)
        return PipelineResult(
            ok=True, pipeline_skipped=False, raw_link_count=3, new_link_count=3,
            classified_count=3, skipped_count=0, enriched_count=3, persisted_count=3,
            message="流水线完成",
        )

    monkeypatch.setattr(actions, "run_refresh_all_pipeline", fake_pipeline)
    monkeypatch.setattr(actions, "invalidate_activity_cache", lambda: None)
    payload = actions.run_action("refresh_source", {"source_id": "DS-2"})
    assert payload["ok"] is True
    assert pipeline_inputs == [[_dynamic_url(cv) for cv in (101, 102, 103)]]
    assert get_last_container("DS-2") == "https://www.bilibili.com/read/cv103"
    assert list_calls == [5] and detail_calls == [103, 102, 101]


@pytest.mark.parametrize("error", [ValueError("unexpected payload shape"), RuntimeError("unexpected internal failure")])
def test_unknown_detail_exception_is_not_swallowed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    module, _, detail_calls = _setup(
        monkeypatch, "DS-2", [103, 102, 101, 100], failure={102: error},
    )
    with pytest.raises(type(error), match="unexpected"):
        module.check_update()
    assert detail_calls == [103, 102]
    assert get_last_container("DS-2") == "https://www.bilibili.com/read/cv100"


def test_out_of_order_list_never_advances_checkpoint(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _, detail_calls = _setup(monkeypatch, "DS-2", [101, 103, 102, 100])
    with pytest.raises(RuntimeError, match="顺序异常"):
        module.check_update()
    assert detail_calls == []
    assert get_last_container("DS-2") == "https://www.bilibili.com/read/cv100"
