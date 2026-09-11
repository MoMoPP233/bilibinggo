"""V21 LLM resilience：错误分类 / response normalization / 连接测试真实验证 / V20 隔离。"""

from __future__ import annotations

import httpx
import pytest

from pathlib import Path
from unittest.mock import MagicMock, patch

import src.llm_client as llm
from src.llm_config import LlmConfig
from src.llm_resilience import (
    LlmProviderBreaker,
    LlmProviderUnavailable,
    bind_llm_provider_breaker,
    reset_llm_provider_breaker,
)
from src.pipeline.classify_step import ClassifyOutcome
from src.pipeline.refresh_all_pipeline import PipelineResult, run_new_links_pipeline
import src.pipeline.refresh_all_pipeline as pipeline_mod


def _cfg() -> LlmConfig:
    return LlmConfig(api_key="sk-test", base_url="https://llm.test/v1", model_name="m")


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://llm.test/v1/chat/completions")
    return httpx.HTTPStatusError(str(status), request=request, response=httpx.Response(status, request=request))


def _good() -> dict:
    return {"choices": [{"message": {"content": '{"ok": true}'}}]}


# ---------- CASE 1-7: normalization ----------


def test_case1_string_content_ok() -> None:
    assert llm._normalize_chat_response(_good()) == '{"ok": true}'


def test_case2_text_parts_ok() -> None:
    data = {"choices": [{"message": {"content": [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}]}}]}
    assert llm._normalize_chat_response(data) == "hello world"


def test_case3_html_protocol_error() -> None:
    data = {"choices": [{"message": {"content": "<html>bad gateway</html>"}}]}
    with pytest.raises(llm.LlmProtocolError):
        llm._normalize_chat_response(data)


def test_case4_empty_protocol_error() -> None:
    for data in ({"choices": [{"message": {"content": ""}}]}, {"choices": [{"message": {"content": "   "}}]}, {}):
        with pytest.raises(llm.LlmProtocolError):
            llm._normalize_chat_response(data)


def test_case5_error_object_protocol_error() -> None:
    with pytest.raises(llm.LlmProtocolError):
        llm._normalize_chat_response({"error": {"message": "bad"}})


def test_case6_missing_choices_protocol_error() -> None:
    with pytest.raises(llm.LlmProtocolError):
        llm._normalize_chat_response({"choices": []})


def test_case7_invalid_content_no_attributeerror() -> None:
    with pytest.raises(llm.LlmProtocolError):
        llm._normalize_chat_response({"choices": [{"message": {"content": {"weird": 1}}}]})
    with pytest.raises(llm.LlmProtocolError):
        llm._normalize_chat_response({"choices": ["not-a-dict"]})


def test_case7b_reasoning_only_not_answer() -> None:
    data = {"choices": [{"message": {"reasoning_content": "thinking...", "content": None}}]}
    with pytest.raises(llm.LlmProtocolError):
        llm._normalize_chat_response(data)


# ---------- CASE 8-11: taxonomy ----------


@pytest.mark.parametrize("status,expected", [(401, llm.LlmConfigError), (403, llm.LlmConfigError), (404, llm.LlmConfigError), (429, llm.LlmTemporaryError), (500, llm.LlmTemporaryError), (503, llm.LlmTemporaryError)])
def test_case8_9_http_taxonomy(status, expected, monkeypatch) -> None:
    monkeypatch.setattr(llm, "_post_chat_completion", lambda **kw: (_ for _ in ()).throw(_status_error(status)))
    with pytest.raises(expected):
        llm.chat_json(system="s", user="u", config=_cfg())


def test_case10_timeout_is_temporary(monkeypatch) -> None:
    monkeypatch.setattr(
        llm, "_post_chat_completion",
        lambda **kw: (_ for _ in ()).throw(httpx.TimeoutException("t", request=httpx.Request("POST", "https://x"))),
    )
    with pytest.raises(llm.LlmTemporaryError):
        llm.chat_json(system="s", user="u", config=_cfg())


def test_case11_connection_error_is_temporary(monkeypatch) -> None:
    monkeypatch.setattr(
        llm, "_post_chat_completion",
        lambda **kw: (_ for _ in ()).throw(httpx.ConnectError("reset", request=httpx.Request("POST", "https://x"))),
    )
    with pytest.raises(llm.LlmTemporaryError):
        llm.chat_json(system="s", user="u", config=_cfg())


def test_case28_llm_429_never_writes_bilibili_cooldown(monkeypatch) -> None:
    recorded: list = []
    monkeypatch.setattr("web.auto_remote_state.record_auto_remote_risk", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(llm, "_post_chat_completion", lambda **kw: (_ for _ in ()).throw(_status_error(429)))
    with pytest.raises(llm.LlmTemporaryError):
        llm.chat_json(system="s", user="u", config=_cfg())
    assert recorded == []


def test_case20_minus_799_not_llm_concern() -> None:
    from src.platform_risk import is_platform_risk_code

    assert is_platform_risk_code(-799) is False


# ---------- CASE 22-24: connection test ----------


def test_case22_connection_ok(monkeypatch) -> None:
    monkeypatch.setattr(llm, "_post_chat_completion", lambda **kw: {"choices": [{"message": {"content": "ok"}}]})
    assert "m @ https://llm.test/v1" == llm.test_llm_connection(_cfg())


def test_case23_connection_html_fails(monkeypatch) -> None:
    monkeypatch.setattr(llm, "_post_chat_completion", lambda **kw: {"choices": [{"message": {"content": "<html>"}}]})
    with pytest.raises(llm.LlmProtocolError):
        llm.test_llm_connection(_cfg())


def test_case24_connection_missing_choices_fails(monkeypatch) -> None:
    monkeypatch.setattr(llm, "_post_chat_completion", lambda **kw: {"choices": []})
    with pytest.raises(llm.LlmProtocolError):
        llm.test_llm_connection(_cfg())


def test_connection_401_human_message(monkeypatch) -> None:
    monkeypatch.setattr(llm, "_post_chat_completion", lambda **kw: (_ for _ in ()).throw(_status_error(401)))
    with pytest.raises(llm.LlmConfigError, match="API Key 无效或权限不足"):
        llm.test_llm_connection(_cfg())


def test_no_secret_leak_in_error(monkeypatch) -> None:
    monkeypatch.setattr(llm, "_post_chat_completion", lambda **kw: (_ for _ in ()).throw(_status_error(500)))
    with pytest.raises(llm.LlmTemporaryError) as excinfo:
        llm.test_llm_connection(_cfg())
    text = str(excinfo.value)
    assert "sk-test" not in text and "Authorization" not in text


# ---------- V21 stage-2: assessment retryable + backoff ----------


def _assessment_with(reason_code: str):
    import src.repost_cleanup as rc

    return rc.CandidateAssessment(
        "blocked", "tmp", reason_code, classification_source="public_classifier"
    )


def test_case19_llm_temporary_assessment_is_retryable_not_final() -> None:
    import src.repost_cleanup as rc

    assert rc._assessment_status_for(_assessment_with("llm_temporary")) == "retryable_unknown"
    assert "llm_temporary" in rc.RETRYABLE_ASSESSMENT_REASON_CODES


def test_case20_classifier_maps_llm_errors_to_retryable(monkeypatch) -> None:
    import src.repost_cleanup as rc

    def _boom(client, dynamic_id):
        raise llm.LlmTemporaryError("429")

    monkeypatch.setattr(rc, "classify_for_cleanup", _boom)
    result = rc._assess_via_public_classifier(MagicMock(), original_id="1", uid="1", now_ts=0)
    assert result.reason_code == "llm_temporary"
    assert rc._assessment_status_for(result) == "retryable_unknown"

    def _boom_proto(client, dynamic_id):
        raise llm.LlmProtocolError("html")

    monkeypatch.setattr(rc, "classify_for_cleanup", _boom_proto)
    result2 = rc._assess_via_public_classifier(MagicMock(), original_id="1", uid="1", now_ts=0)
    assert result2.reason_code == "llm_temporary"

    def _boom_cfg(client, dynamic_id):
        raise llm.LlmConfigError("401")

    monkeypatch.setattr(rc, "classify_for_cleanup", _boom_cfg)
    result3 = rc._assess_via_public_classifier(MagicMock(), original_id="1", uid="1", now_ts=0)
    assert result3.reason_code == "llm_temporary"


def test_case24_business_final_stays_final() -> None:
    import src.repost_cleanup as rc

    assert rc._assessment_status_for(_assessment_with("classify_failed")) == "final"
    assert rc._assessment_status_for(_assessment_with("non_lottery")) == "final"


def test_case21_22_23_backoff_window_defined() -> None:
    import src.repost_cleanup as rc

    assert rc.RETRYABLE_ASSESSMENT_BACKOFF_SECONDS == 15 * 60


def test_case29_llm429_provider_failure_only() -> None:
    import src.repost_cleanup as rc

    err = llm._map_http_error(_status_error(429))
    assert isinstance(err, llm.LlmTemporaryError)
    assert rc._risk_code_from_message(llm.LlmTemporaryError("LLM 429")) is None


# ---------- V21 stage-3: candidate isolation / breaker / partial / config ----------


_ID_BASE = 1224962472871460880


def _url(index: int) -> str:
    return f"https://www.bilibili.com/opus/{_ID_BASE + index}"


def _ids(count: int) -> list[str]:
    return [str(_ID_BASE + index) for index in range(count)]


class _PipelineClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _GoodActivity:
    skipped = False

    def __init__(self, dynamic_id: str) -> None:
        self.dynamic_id = dynamic_id

    def to_dict(self) -> dict:
        return {"dynamic_id": self.dynamic_id}


def _patch_pipeline(monkeypatch, classify, *, known=None):
    persisted: list[dict] = []
    monkeypatch.setattr(pipeline_mod, "known_activity_ids", lambda: set(known or ()))
    monkeypatch.setattr(pipeline_mod, "load_participations", lambda: {})
    monkeypatch.setattr(pipeline_mod, "BilibiliClient", _PipelineClient)
    monkeypatch.setattr(pipeline_mod, "classify_new_link", classify)
    monkeypatch.setattr(
        pipeline_mod,
        "enrich_activity",
        lambda client, dynamic_id, lottery_type, **kwargs: _GoodActivity(dynamic_id),
    )
    monkeypatch.setattr(pipeline_mod, "apply_initial_status", lambda row: row)
    monkeypatch.setattr(
        pipeline_mod,
        "append_activities",
        lambda rows: (persisted.extend(rows), len(rows))[1],
    )
    return persisted


def test_case34_single_candidate_temporary_failure_continues(monkeypatch) -> None:
    calls: list[str] = []

    def classify(client, dynamic_id):
        calls.append(dynamic_id)
        if dynamic_id == _ids(2)[0]:
            raise llm.LlmTemporaryError("LLM 服务暂时不可用（HTTP 500）")
        return ClassifyOutcome(dynamic_id, "互动抽奖", False, llm_called=True)

    persisted = _patch_pipeline(monkeypatch, classify)
    result = run_new_links_pipeline([_url(0), _url(1)], workers=1)

    assert calls == _ids(2)
    assert result.partial is True
    assert result.provider_unavailable is False
    assert result.provider_failed_count == 1
    assert [row["dynamic_id"] for row in persisted] == [_ids(2)[1]]
    assert result.persisted_count == 1


def test_case35_single_candidate_protocol_failure_continues(monkeypatch) -> None:
    calls: list[str] = []

    def classify(client, dynamic_id):
        calls.append(dynamic_id)
        if dynamic_id == _ids(2)[0]:
            raise llm.LlmProtocolError("LLM 响应缺少 choices（协议不兼容）")
        return ClassifyOutcome(dynamic_id, "互动抽奖", False, llm_called=True)

    persisted = _patch_pipeline(monkeypatch, classify)
    result = run_new_links_pipeline([_url(0), _url(1)], workers=1)

    assert calls == _ids(2)
    assert result.provider_unavailable is False
    assert result.provider_failed_count == 1
    assert [row["dynamic_id"] for row in persisted] == [_ids(2)[1]]


def test_case36_three_consecutive_provider_failures_open_breaker(monkeypatch) -> None:
    calls: list[str] = []

    def classify(client, dynamic_id):
        calls.append(dynamic_id)
        raise llm.LlmTemporaryError("down")

    _patch_pipeline(monkeypatch, classify)
    result = run_new_links_pipeline([_url(i) for i in range(4)], workers=1)

    assert len(calls) == 3
    assert result.provider_unavailable is True
    assert result.partial is True
    assert result.provider_failed_count == 3
    assert result.unprocessed_count == 1


def test_case37_breaker_blocks_further_llm_requests(monkeypatch) -> None:
    from src import forward_parser

    chat_calls: list[dict] = []
    monkeypatch.setattr(forward_parser, "get_cached_classify", lambda *a, **k: None)
    monkeypatch.setattr(forward_parser, "get_cached_parse", lambda *a, **k: None)
    monkeypatch.setattr(
        forward_parser,
        "chat_json",
        lambda **kwargs: chat_calls.append(kwargs) or {"is_lottery": True},
    )
    breaker = LlmProviderBreaker()
    for _ in range(3):
        breaker.note_provider_failure(llm.LlmTemporaryError("down"))
    token = bind_llm_provider_breaker(breaker)
    try:
        with pytest.raises(LlmProviderUnavailable):
            forward_parser.classify_forward_lottery(
                "1224962472871460899", "V21 breaker 正文内容唯一串"
            )
        with pytest.raises(LlmProviderUnavailable):
            forward_parser.parse_forward_content(
                "1224962472871460898", "V21 breaker 解析正文内容唯一串"
            )
    finally:
        reset_llm_provider_breaker(token)

    assert chat_calls == []


def test_case38_success_resets_provider_failure_streak(monkeypatch) -> None:
    sequence = ["fail", "fail", "ok", "fail", "fail"]
    calls: list[str] = []

    def classify(client, dynamic_id):
        kind = sequence[len(calls)]
        calls.append(dynamic_id)
        if kind == "fail":
            raise llm.LlmTemporaryError("down")
        return ClassifyOutcome(dynamic_id, "互动抽奖", False, llm_called=True)

    _patch_pipeline(monkeypatch, classify)
    result = run_new_links_pipeline([_url(i) for i in range(5)], workers=1)

    assert len(calls) == 5
    assert result.provider_unavailable is False
    assert result.partial is True
    assert result.provider_failed_count == 4


def test_case39_partial_keeps_successful_candidates(monkeypatch) -> None:
    calls: list[str] = []

    def classify(client, dynamic_id):
        index = len(calls)
        calls.append(dynamic_id)
        if 30 <= index < 33:
            raise llm.LlmTemporaryError("down")
        return ClassifyOutcome(dynamic_id, "互动抽奖", False, llm_called=True)

    persisted = _patch_pipeline(monkeypatch, classify)
    result = run_new_links_pipeline([_url(i) for i in range(35)], workers=1)

    assert len(calls) == 33
    assert len(persisted) == 30
    assert result.persisted_count == 30
    assert result.provider_unavailable is True
    assert result.unprocessed_count == 2


def test_case40_partial_does_not_commit_source_checkpoint(
    isolated_home: Path, monkeypatch
) -> None:
    from src.sources.common import CheckResult
    from src.state_store import get_last_container, save_state
    from web.actions import run_action

    _ = isolated_home
    save_state(
        {"sources": {"DS-3": {"container_url": "https://www.bilibili.com/read/cv888001"}}}
    )
    check_result = CheckResult(
        source_id="DS-3",
        updated=True,
        container_url="https://www.bilibili.com/read/cv999001",
        container_id="999001",
        title="新专栏",
        published_at=1_700_000_000,
        previous_container_url="https://www.bilibili.com/read/cv888001",
        activity_links=[_url(0)],
        checked_at=1_700_000_100,
        link_hints={},
        cv_id=None,
    )
    partial_pipeline = PipelineResult(
        ok=True,
        pipeline_skipped=False,
        raw_link_count=40,
        new_link_count=40,
        classified_count=30,
        skipped_count=0,
        enriched_count=30,
        persisted_count=30,
        partial=True,
        provider_unavailable=True,
        provider_failed_count=3,
        unprocessed_count=7,
        partial_reason="LLM 服务连续异常",
        message="部分完成",
    )

    def fake_run_ds_check(index, source_id, check_update, save_result):
        payload = {
            "source_id": source_id,
            "updated": True,
            "link_count": 40,
            "saved": True,
            "status_text": "发现新专栏，已爬取",
        }
        return index, payload, "log", check_result

    with (
        patch("web.actions._run_ds_check", side_effect=fake_run_ds_check),
        patch("web.actions.run_refresh_all_pipeline", return_value=partial_pipeline),
        patch("web.actions.invalidate_activity_cache"),
        patch("web.actions.set_last_pipeline_persisted"),
    ):
        payload = run_action("refresh_source", {"source_id": "DS-3"})

    assert payload["ok"] is True
    assert "部分完成" in payload["message"]
    assert get_last_container("DS-3") == "https://www.bilibili.com/read/cv888001"


def test_case41_unprocessed_candidates_recover_next_round(monkeypatch) -> None:
    state = {"round": 0}
    calls: list[str] = []

    def classify(client, dynamic_id):
        calls.append(dynamic_id)
        if state["round"] == 0 and dynamic_id in _ids(3):
            raise llm.LlmTemporaryError("down")
        return ClassifyOutcome(dynamic_id, "互动抽奖", False, llm_called=True)

    persisted = _patch_pipeline(monkeypatch, classify)
    first = run_new_links_pipeline([_url(i) for i in range(4)], workers=1)
    assert first.partial is True
    assert first.provider_unavailable is True
    assert persisted == []

    state["round"] = 1
    second = run_new_links_pipeline([_url(i) for i in range(4)], workers=1)
    assert second.partial is False
    assert len(persisted) == 4


def test_case42_persisted_candidates_skip_expensive_processing(monkeypatch) -> None:
    calls: list[str] = []

    def classify(client, dynamic_id):
        calls.append(dynamic_id)
        return ClassifyOutcome(dynamic_id, "互动抽奖", False, llm_called=True)

    _patch_pipeline(monkeypatch, classify, known=[_ids(2)[0]])
    result = run_new_links_pipeline([_url(0), _url(1)], workers=1)

    assert calls == [_ids(2)[1]]
    assert result.existing_count == 1
    assert result.persisted_count == 1


def test_case43_config_error_stops_source_immediately(monkeypatch) -> None:
    calls: list[str] = []

    def classify(client, dynamic_id):
        calls.append(dynamic_id)
        raise llm.LlmConfigError("API Key 无效或权限不足")

    _patch_pipeline(monkeypatch, classify)
    result = run_new_links_pipeline([_url(0), _url(1)], workers=1)

    assert calls == [_ids(2)[0]]
    assert result.config_unavailable is True
    assert result.provider_unavailable is True
    assert result.partial is True
    assert result.provider_failed_count == 0


def test_case44_config_error_writes_no_bilibili_cooldown(monkeypatch) -> None:
    recorded: list[dict] = []
    monkeypatch.setattr(
        "web.auto_remote_state.record_auto_remote_risk",
        lambda **kwargs: recorded.append(kwargs),
    )

    def classify(client, dynamic_id):
        raise llm.LlmConfigError("API Key 无效或权限不足")

    _patch_pipeline(monkeypatch, classify)
    result = run_new_links_pipeline([_url(0)], workers=1)

    assert result.partial is True
    assert recorded == []


def test_case45_config_unchanged_skips_repeat_provider_call(monkeypatch) -> None:
    calls: list[dict] = []

    def boom(**kwargs):
        calls.append(kwargs)
        raise _status_error(401)

    monkeypatch.setattr(llm, "_post_chat_completion", boom)
    with pytest.raises(llm.LlmConfigError):
        llm.chat_json(system="s", user="u", config=_cfg())
    with pytest.raises(llm.LlmConfigUnavailableError):
        llm.chat_json(system="s", user="u", config=_cfg())

    assert len(calls) == 1


def test_case46_config_change_recovers_llm(monkeypatch) -> None:
    calls: list[dict] = []

    def first(**kwargs):
        calls.append(kwargs)
        raise _status_error(401)

    monkeypatch.setattr(llm, "_post_chat_completion", first)
    with pytest.raises(llm.LlmConfigError):
        llm.chat_json(system="s", user="u", config=_cfg())

    def good(**kwargs):
        calls.append(kwargs)
        return _good()

    monkeypatch.setattr(llm, "_post_chat_completion", good)
    changed = LlmConfig(api_key="sk-other", base_url="https://llm.test/v1", model_name="m")
    assert llm.chat_json(system="s", user="u", config=changed) == {"ok": True}
    assert len(calls) == 2


def test_case47_config_failure_not_business_final(monkeypatch) -> None:
    import src.repost_cleanup as rc

    def boom(client, dynamic_id):
        raise llm.LlmConfigUnavailableError("llm_configuration_unavailable")

    monkeypatch.setattr(rc, "classify_for_cleanup", boom)
    result = rc._assess_via_public_classifier(MagicMock(), original_id="1", uid="1", now_ts=0)

    assert result.reason_code == "llm_temporary"
    assert rc._assessment_status_for(result) == "retryable_unknown"


def test_case48_temporary_assessment_keeps_backoff() -> None:
    import src.repost_cleanup as rc

    assert rc.RETRYABLE_ASSESSMENT_BACKOFF_SECONDS == 15 * 60
    assert rc._assessment_status_for(_assessment_with("llm_temporary")) == "retryable_unknown"


@pytest.mark.parametrize("code", [-352, -509, 429])
def test_case49_50_51_bilibili_risk_penetrates_v21(monkeypatch, code) -> None:
    calls: list[str] = []

    def classify(client, dynamic_id):
        calls.append(dynamic_id)
        raise RuntimeError(f"API error {code}: 分类阶段命中平台风控（{dynamic_id}）")

    _patch_pipeline(monkeypatch, classify)
    with pytest.raises(RuntimeError, match=f"API error {code}"):
        run_new_links_pipeline([_url(0), _url(1)], workers=1)

    assert calls == [_ids(2)[0]]


def test_case52_llm429_only_provider_failure(monkeypatch) -> None:
    recorded: list[dict] = []
    monkeypatch.setattr(
        "web.auto_remote_state.record_auto_remote_risk",
        lambda **kwargs: recorded.append(kwargs),
    )
    err = llm._map_http_error(_status_error(429))

    def classify(client, dynamic_id):
        raise err

    _patch_pipeline(monkeypatch, classify)
    result = run_new_links_pipeline([_url(0)], workers=1)

    assert result.provider_failed_count == 1
    assert result.partial is True
    assert recorded == []


def test_case53_minus_799_temporary_restriction_unchanged() -> None:
    from src.platform_risk import is_platform_risk_code, risk_code_of

    assert is_platform_risk_code(-799) is False
    assert risk_code_of("API error -799: 请求过于频繁") is None
    assert is_platform_risk_code(-352) is True

