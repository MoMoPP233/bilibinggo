"""Risk semantics V13：-799 不进入正式 platform risk；Datasource 正文 marker 不升级全局风控。

A. -352 仍触发 platform risk（note_failure / risk_code_of）
B. -509 仍触发 platform risk
C. HTTP 429 仍触发 platform risk
D. -799 int 不再被归为 rate_limited / 不再立即 raise 平台暂停
E. -799 按真实局部语义走 blocked/失败计数（本地停止）
F. -799 路径绝不进入 DELETE（classify 阶段产出 blocked）
G. DS 正文（含“风控/限流”字样、无结构化 code）→ 不触发 platform risk 判断
H. timeout / 普通 5xx → 不触发 platform risk
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from src.platform_risk import risk_code_of


def _risk_exc(code: int) -> RuntimeError:
    return RuntimeError(f"API error {code}: 风控")


def test_a_minus_352_is_formal_platform_risk() -> None:
    assert risk_code_of(_risk_exc(-352)) == -352


def test_b_minus_509_is_formal_platform_risk() -> None:
    assert risk_code_of(_risk_exc(-509)) == -509


def test_c_http_429_is_formal_platform_risk() -> None:
    request = httpx.Request("GET", "https://api.bilibili.com/x")
    exc = httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
    assert risk_code_of(exc) == 429


def test_d_minus_799_not_formal_platform_risk() -> None:
    assert risk_code_of(RuntimeError("API error -799: 访问过于频繁")) is None
    assert risk_code_of(-799) is None


def test_e_breaker_minus_799_is_local_not_rate_limited() -> None:
    import src.repost_cleanup as rc

    breaker = rc._AssessmentBreaker()
    breaker.note_failure(-799)  # 不抛 AssessmentRateLimited；本地失败计数 +1
    assert breaker.rate_limited is False
    assert breaker.consecutive_failures == 1


def test_f_breaker_minus_352_still_stops_as_platform_risk() -> None:
    import src.repost_cleanup as rc

    breaker = rc._AssessmentBreaker()
    with pytest.raises(rc.AssessmentRateLimited):
        breaker.note_failure(-352)
    assert breaker.rate_limited is True


def test_classifier_minus_799_returns_blocked_not_risk_raise(monkeypatch) -> None:
    import src.repost_cleanup as rc

    outcome = MagicMock(skipped=False, classify_content="")
    monkeypatch.setattr(rc, "classify_for_cleanup", lambda client, dynamic_id: (outcome, -799))
    client = MagicMock()
    result = rc._assess_via_public_classifier(
        client, original_id="90001", uid="1", now_ts=0
    )
    assert result.level == "blocked"
    assert "-799" in str(result.reason) or "799" in str(result.reason)
    assert result.reason_code == "temporary_unavailable"


def test_classifier_minus_352_raises_assessment_rate_limited(monkeypatch) -> None:
    import src.repost_cleanup as rc

    outcome = MagicMock(skipped=False, classify_content="")
    monkeypatch.setattr(rc, "classify_for_cleanup", lambda client, dynamic_id: (outcome, -352))
    with pytest.raises(rc.AssessmentRateLimited):
        rc._assess_via_public_classifier(MagicMock(), original_id="90001", uid="1", now_ts=0)


def test_g_ds_body_words_do_not_trigger_risk_matcher() -> None:
    import web.actions as actions

    # 普通正文含“风控/限流/too many”等字样、无结构化 code：不再被识别
    assert actions._looks_like_platform_risk("该动态正文提到 风控 与 限流 词汇") is False
    assert actions._looks_like_platform_risk("rate limit reached in page text") is False
    # 结构化 code 文本令牌仍作为保守本地停止依据
    assert actions._looks_like_platform_risk("API error -352: 风控") is True
    assert actions._looks_like_platform_risk("访问过于频繁 -799") is True


def test_h_timeout_and_5xx_are_not_platform_risk() -> None:
    request = httpx.Request("GET", "https://api.bilibili.com/x")
    timeout = httpx.TimeoutException("timeout", request=request)
    assert risk_code_of(timeout) is None
    five = httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))
    assert risk_code_of(five) is None
