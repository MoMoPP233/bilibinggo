"""P1-1：参与命中平台风控的 fail-fast + 客户端不重试 + 三连共享 breaker（V15）。

全 mock / 零真实 Bilibili。覆盖：
A  当前目标 like 命中 -352 → follow/favorite/repost/comment 一律不调用（一次点赞请求）
B  客户端层 -352/-509/HTTP 429 均只发一次；transient 网络错误仍按历史 retry
C  三 worker（默认并行数不降级）breaker 时序：兄弟 worker 不再发起新的远程 mutation
D  风险 → JobRunner → 6h cooldown（沿用唯一出口，不新增第二套）
E  普通非风控失败（ActionResult ok=False）保持 V14 行为（继续后续动作，不 fail-fast）
F  participation_guard 的 pending/unknown/suspected 语义回归
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import lottery_actions as actions
from src.bilibili_client import BilibiliClient

DYNAMIC_ID_A = "1000000000000000001"
DYNAMIC_ID_B = "1000000000000000002"
UID = 42
REFERER = f"https://www.bilibili.com/opus/{DYNAMIC_ID_A}"


def _detail(*, liked=False, favorite=False, include_favorite=True):
    stat = {"like": {"status": liked}, "forward": {"count": 1}, "comment": {"count": 1}}
    if include_favorite:
        stat["favorite"] = {"status": favorite}
    return {
        "basic": {"comment_id_str": DYNAMIC_ID_A, "comment_type": 17},
        "modules": {"module_author": {"mid": 123}, "module_stat": stat},
    }


def _fresh_context(dynamic_id: str = DYNAMIC_ID_A) -> actions.DynamicContext:
    return actions.DynamicContext(
        dynamic_id=dynamic_id,
        sender_uid=123,
        referer=REFERER,
        comment_rid=dynamic_id,
        comment_type=17,
        liked=False,
        favorited=False,
        favorite_available=True,
        followed=False,
        reposted=False,
        commented=False,
    )


class _CallLog:
    def __init__(self) -> None:
        self.post_form: list[str] = []
        self.post_json: list[str] = []
        self.get_json: list[str] = []

    def record(self, kind: str, url: str) -> None:
        getattr(self, kind).append(url)


class _RawClient:
    """记录全部远程调用；mutation URL → 期望 payload/异常。"""

    def __init__(
        self,
        log: _CallLog,
        *,
        post_json_payloads: dict[str, Any] | None = None,
        post_form_payloads: dict[str, Any] | None = None,
        post_json_errors: dict[str, Exception] | None = None,
        post_form_errors: dict[str, Exception] | None = None,
        get_json_payloads: dict[str, Any] | None = None,
        like_block: threading.Event | None = None,
        like_entered: threading.Event | None = None,
        like_release: threading.Event | None = None,
    ) -> None:
        self.log = log
        self.post_json_payloads = post_json_payloads or {}
        self.post_form_payloads = post_form_payloads or {}
        self.post_json_errors = post_json_errors or {}
        self.post_form_errors = post_form_errors or {}
        self.get_json_payloads = get_json_payloads or {}
        self.like_block = like_block
        self.like_entered = like_entered
        self.like_release = like_release

    def _serve(self, url: str, *, json_mode: bool) -> dict:
        kind = "post_json" if json_mode else "post_form"
        getattr(self.log, kind).append(url)
        table = self.post_json_payloads if json_mode else self.post_form_payloads
        errors = self.post_json_errors if json_mode else self.post_form_errors
        if url in errors:
            raise errors[url]
        if url not in table:
            raise AssertionError(f"未配置的 mutation 响应: {url}")
        if url == actions.LIKE_URL and self.like_block is not None:
            if self.like_entered is not None:
                self.like_entered.set()
            if self.like_release is not None:
                assert self.like_release.wait(5), "like 等待放行超时"
        return table[url]

    def post_json(self, url, payload, *, params=None, referer=None, retries=3, raise_on_code=True):
        return self._serve(url, json_mode=True)

    def post_form(self, url, data, *, params=None, referer=None, retries=3, raise_on_code=True):
        return self._serve(url, json_mode=False)

    def get_json(self, url, params=None, *, referer=None, retries=0, wbi=False):
        self.log.get_json.append(url)
        if url not in self.get_json_payloads:
            raise AssertionError(f"未配置的读取响应: {url}")
        return self.get_json_payloads[url]

    def request_json(self, url, params=None, *, referer=None, retries=0):
        return self.get_json(url, params, referer=referer, retries=retries)

    def get_text(self, url, *, referer=None, retries=0):
        raise AssertionError(f"不应读取文本页: {url}")


def _execution_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions, "require_login", lambda: ("test-csrf", UID))
    monkeypatch.setattr(actions.time, "sleep", lambda _: None)


def _ok(code: int, message: str = "") -> dict:
    return {"code": code, "message": message, "data": {}}


# ---------- A：当前目标 fail-fast ----------


@pytest.mark.parametrize("code", [-352, -509, 429])
def test_like_risk_stops_current_target_with_single_mutation(monkeypatch, code: int) -> None:
    """like 命中明确结构化风控：like 只发一次，后续四个动作一个都不发起。"""
    _execution_env(monkeypatch)
    log = _CallLog()
    raw = _RawClient(
        log,
        post_json_payloads={actions.LIKE_URL: _ok(code, "风控校验失败")},
    )
    breaker = actions.ParticipationRiskBreaker()
    partial: list[actions.ActionResult] = []

    with pytest.raises(actions.ParticipationRiskError, match=f"API error {code}"):
        actions.execute_full_participation(
            raw,
            dynamic_id=DYNAMIC_ID_A,
            context=_fresh_context(),
            on_action=partial.append,
            risk_breaker=breaker,
        )

    # 命中的动作以异常中断，未及产出 ActionResult；关键断言是后续动作零调用。
    assert partial == []
    assert log.post_json == [actions.LIKE_URL]
    assert log.post_form == []
    assert breaker.triggered() is True


def test_follow_risk_stops_remaining_steps(monkeypatch) -> None:
    _execution_env(monkeypatch)
    log = _CallLog()
    raw = _RawClient(
        log,
        post_json_payloads={actions.LIKE_URL: _ok(0)},
        post_form_payloads={actions.FOLLOW_URL: _ok(-352)},
    )
    partial: list[actions.ActionResult] = []
    with pytest.raises(actions.ParticipationRiskError, match="API error -352"):
        actions.execute_full_participation(
            raw,
            dynamic_id=DYNAMIC_ID_A,
            context=_fresh_context(),
            on_action=partial.append,
            risk_breaker=actions.ParticipationRiskBreaker(),
        )
    assert [item.action for item in partial] == ["like"]
    assert log.post_json == [actions.LIKE_URL]
    assert log.post_form == [actions.FOLLOW_URL]


def test_comment_risk_stops_after_repost(isolated_home: Path, monkeypatch) -> None:
    _execution_env(monkeypatch)
    log = _CallLog()
    raw = _RawClient(
        log,
        post_json_payloads={
            actions.LIKE_URL: _ok(0),
            actions.COSMO_SIMPLE_ACTION_URL: _ok(75008, "已收藏"),
        },
        post_form_payloads={
            actions.FOLLOW_URL: _ok(0),
            actions.REPOST_URL: _ok(0),
            actions.COMMENT_URL: _ok(-509),
        },
        get_json_payloads={},
    )
    partial: list[actions.ActionResult] = []
    with pytest.raises(actions.ParticipationRiskError, match="API error -509"):
        actions.execute_full_participation(
            raw,
            dynamic_id=DYNAMIC_ID_A,
            context=_fresh_context(),
            on_action=partial.append,
            risk_breaker=actions.ParticipationRiskBreaker(),
        )
    assert [item.action for item in partial] == ["like", "follow", "favorite", "repost"]
    assert log.post_json == [actions.LIKE_URL, actions.COSMO_SIMPLE_ACTION_URL]
    assert log.post_form == [actions.FOLLOW_URL, actions.REPOST_URL, actions.COMMENT_URL]


# ---------- B：客户端不重试明确风控；transient 保持重试 ----------


def _client_with_fake_transport(monkeypatch, *, get_responses=None, post_responses=None):
    import src.bilibili_client as client_mod

    client = BilibiliClient(warmup=False)
    get_queue = list(get_responses or [])
    post_queue = list(post_responses or [])
    calls = {"get": 0, "post": 0}

    def fake_get(url, params=None, headers=None):
        calls["get"] += 1
        item = get_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def fake_post(url, params=None, data=None, json=None, headers=None):
        calls["post"] += 1
        item = post_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(client, "_http_get", fake_get)
    monkeypatch.setattr(client, "_http_post", fake_post)
    monkeypatch.setattr(client_mod.time, "sleep", lambda _: None)
    return client, calls


def _json_response(status: int, payload: dict) -> httpx.Response:
    request = httpx.Request("POST", "https://api.bilibili.com/x/action")
    return httpx.Response(
        status,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        request=request,
    )


@pytest.mark.parametrize("code", [-352, -509])
def test_client_post_risk_codes_never_retry(monkeypatch, code: int) -> None:
    client, calls = _client_with_fake_transport(
        monkeypatch, post_responses=[_json_response(200, _ok(code, "risk"))]
    )
    with pytest.raises(RuntimeError, match=f"API error {code}"):
        client.post_json("https://api.bilibili.com/x/action", {}, retries=3, raise_on_code=True)
    assert calls["post"] == 1

    client2, calls2 = _client_with_fake_transport(
        monkeypatch, post_responses=[_json_response(200, _ok(code, "risk"))]
    )
    payload = client2.post_json(
        "https://api.bilibili.com/x/action", {}, retries=3, raise_on_code=False
    )
    assert payload["code"] == code
    assert calls2["post"] == 1


def test_client_post_http_429_never_retries(monkeypatch) -> None:
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.bilibili.com/x/action"))
    client, calls = _client_with_fake_transport(monkeypatch, post_responses=[response])
    with pytest.raises(httpx.HTTPStatusError):
        client.post_form("https://api.bilibili.com/x/action", {}, retries=3)
    assert calls["post"] == 1


def test_client_get_json_risk_never_retries(monkeypatch) -> None:
    client, calls = _client_with_fake_transport(
        monkeypatch, get_responses=[_json_response(200, _ok(-352, "risk"))]
    )
    with pytest.raises(RuntimeError, match="API error -352"):
        client.get_json("https://api.bilibili.com/x/view", params={}, retries=3)
    assert calls["get"] == 1


def test_client_transient_network_error_keeps_historical_retry(monkeypatch) -> None:
    """普通可重试 transient 网络失败保持原 retry 语义（对照 B）。"""
    client, calls = _client_with_fake_transport(
        monkeypatch,
        post_responses=[
            httpx.ReadTimeout("timeout", request=httpx.Request("POST", "https://api.bilibili.com/x/action")),
            _json_response(200, {"code": 0, "data": {}}),
        ],
    )
    payload = client.post_json("https://api.bilibili.com/x/action", {}, retries=3)
    assert payload["code"] == 0
    assert calls["post"] == 2


def test_client_transient_http_500_keeps_historical_retry(monkeypatch) -> None:
    client, calls = _client_with_fake_transport(
        monkeypatch,
        get_responses=[
            httpx.Response(500, request=httpx.Request("GET", "https://api.bilibili.com/x/view")),
            _json_response(200, {"code": 0, "data": {}}),
        ],
    )
    payload = client.get_json("https://api.bilibili.com/x/view", params={}, retries=3)
    assert payload["code"] == 0
    assert calls["get"] == 2


# ---------- C：三连默认并行 worker + 共享 breaker 时序 ----------


def test_parallel_sibling_stops_before_next_mutation_after_risk(monkeypatch) -> None:
    """worker B 的 like 已在 in-flight（先于 breaker 建立）；A 命中风险并触发 breaker 后，
    B 的 follow/favorite/repost/comment 一律不再发起。不降 worker=1，不用 sleep 断言。"""
    _execution_env(monkeypatch)
    log = _CallLog()
    like_entered = threading.Event()
    like_release = threading.Event()

    raw_a = _RawClient(
        log,
        post_json_payloads={actions.LIKE_URL: _ok(-352, "风控校验失败")},
    )
    raw_b = _RawClient(
        log,
        post_json_payloads={actions.LIKE_URL: _ok(0)},
        like_block=True,
        like_entered=like_entered,
        like_release=like_release,
    )
    breaker = actions.ParticipationRiskBreaker()
    outcomes: list[Exception | None] = [None, None]
    barrier_errors: list[str] = []

    def run_a() -> None:
        try:
            actions.execute_full_participation(
                raw_a,
                dynamic_id=DYNAMIC_ID_A,
                context=_fresh_context(DYNAMIC_ID_A),
                risk_breaker=breaker,
            )
        except Exception as exc:  # noqa: BLE001 - 测试线程内收集
            outcomes[0] = exc

    def run_b() -> None:
        try:
            actions.execute_full_participation(
                raw_b,
                dynamic_id=DYNAMIC_ID_B,
                context=_fresh_context(DYNAMIC_ID_B),
                risk_breaker=breaker,
            )
            outcomes[1] = None
        except Exception as exc:  # noqa: BLE001 - 测试线程内收集
            outcomes[1] = exc

    thread_b = threading.Thread(target=run_b)
    thread_b.start()
    # B 的 like 请求进入 in-flight（阻塞在放行事件上，等价于已发出的 HTTP 请求在途）。
    assert like_entered.wait(5), "worker B 未进入 like 调用"

    thread_a = threading.Thread(target=run_a)
    thread_a.start()
    thread_a.join(10)
    if thread_a.is_alive():
        barrier_errors.append("worker A 超时未退出")
    assert breaker.triggered() is True
    # 放行 B 的 in-flight like：允许其自然完成（不撤回已发出的请求）。
    like_release.set()
    thread_b.join(10)
    if thread_b.is_alive():
        barrier_errors.append("worker B 超时未退出")
    assert not barrier_errors

    assert isinstance(outcomes[0], actions.ParticipationRiskError)
    assert outcomes[0].code == -352  # type: ignore[union-attr]
    assert isinstance(outcomes[1], actions.ParticipationRiskError)
    assert breaker.triggered() is True
    # B 只发出过 like 这一次 mutation；风险后的 follow/favorite/repost/comment 为 0。
    assert log.post_json == [actions.LIKE_URL, actions.LIKE_URL]
    assert log.post_form == []


# ---------- D：风险 → JobRunner → 6h cooldown 出口 ----------


def test_participation_risk_error_reaches_runner_cooldown(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from web import auto_remote_state as state_mod
    from web.job_runner import JobRunner

    target = isolated_home / "profile-a" / "auto_remote_state.json"
    monkeypatch.setattr(state_mod, "_state_path", lambda: target)

    def fake_run(action, params, *, on_progress=None, cancel_event=None):
        raise actions.ParticipationRiskError(-352, "API error -352: 风控校验失败")

    monkeypatch.setattr("web.job_runner.run_action", fake_run)
    runner = JobRunner()
    job_id = runner.try_start("participate_triple", {}, source="auto")
    assert job_id is not None
    import time

    started = time.monotonic()
    terminal = None
    while time.monotonic() - started < 5.0:
        status = runner.resolve_job_status(job_id)
        if status.state != "running":
            terminal = status
            break
        time.sleep(0.02)
    assert terminal is not None
    assert terminal.state == "error"
    assert (terminal.result or {}).get("risk_code") == -352
    assert state_mod.is_risk_paused() is True


# ---------- E：普通失败保持 V14 语义（不 fail-fast） ----------


def test_ordinary_action_failure_keeps_continuing_next_steps(
    isolated_home: Path, monkeypatch,
) -> None:
    """普通非风控 ActionResult(ok=False) 不允许扩大成 fail-fast：后续动作继续。"""
    _execution_env(monkeypatch)
    log = _CallLog()
    raw = _RawClient(
        log,
        post_json_payloads={
            actions.LIKE_URL: _ok(-1, "活动已关闭"),
            actions.COSMO_SIMPLE_ACTION_URL: _ok(75008, "已收藏"),
        },
        post_form_payloads={
            actions.FOLLOW_URL: _ok(0),
            actions.REPOST_URL: _ok(0),
            actions.COMMENT_URL: _ok(0),
        },
        get_json_payloads={},
    )
    partial: list[actions.ActionResult] = []
    breaker = actions.ParticipationRiskBreaker()
    actions_, context = actions.execute_full_participation(
        raw,
        dynamic_id=DYNAMIC_ID_A,
        context=_fresh_context(),
        on_action=partial.append,
        risk_breaker=breaker,
    )
    assert [item.action for item in partial] == ["like", "follow", "favorite", "repost", "comment"]
    assert partial[0].ok is False
    assert all(item.ok for item in partial[1:])
    assert breaker.triggered() is False
    assert context.dynamic_id == DYNAMIC_ID_A


# ---------- F：guard 语义回归 ----------


@pytest.mark.parametrize("status", ["pending", "unknown", "suspected"])
def test_guard_blocked_states_still_block_resend(isolated_home: Path, status: str) -> None:
    from src.participation_guard import (
        get_guard,
        mark_repost_suspected,
        record_pending,
    )

    if status == "suspected":
        mark_repost_suspected(UID, DYNAMIC_ID_A)
    else:
        record_pending(UID, DYNAMIC_ID_A)
        if status == "unknown":
            actions.mark_repost_unknown(UID, DYNAMIC_ID_A)
    raw = Mock()
    result = actions.repost_dynamic(
        raw, dynamic_id=DYNAMIC_ID_A, my_uid=UID,
        csrf="test", referer=REFERER, content="text",
    )
    assert result.ok is False
    raw.post_form.assert_not_called()
    assert get_guard(UID, DYNAMIC_ID_A).repost_status == status


def test_repost_risk_marks_unknown_then_raises(isolated_home: Path, monkeypatch) -> None:
    """转发命中 -352：已持久化防重状态保持 blocked（不自动重发），并上抛风控。"""
    from src.participation_guard import get_guard

    raw = Mock()
    raw.post_form.return_value = _ok(-352, "风控校验失败")
    with pytest.raises(actions.ParticipationRiskError, match="API error -352"):
        actions.repost_dynamic(
            raw, dynamic_id=DYNAMIC_ID_A, my_uid=UID,
            csrf="test", referer=REFERER, content="text",
        )
    raw.post_form.assert_called_once()
    assert get_guard(UID, DYNAMIC_ID_A).repost_status == "unknown"
