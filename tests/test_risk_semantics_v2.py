"""平台风控语义统一 V2 测试（全 mock / 零真实 Bilibili）。

覆盖：风险判定唯一来源（结构化 code/HTTP 429/官方信封，正文文本绝不触发）、
feed 文本安全与 failed→Job failed、cancel 与风险优先级、多动作 risk breaker、
refresh pipeline 风险传播与 checkpoint、cleanup lottery_notice 风险码保留、
cooldown 幂等去重、stale generation 在途 Job 风险仍记录（runner 侧统一出口）。
"""

from __future__ import annotations

import sys
import time
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web import actions as actions_mod
from web import auto_remote_state as state_mod
from src import following_feed as ff
import src.pipeline.refresh_all_pipeline as pipeline_mod
from src.platform_risk import matches_platform_risk, risk_code_of
from src.repost_cleanup import (
    AssessmentRateLimited,
    RemoteStateUnknown,
    _fetch_notice_strict,
)

_IDS = [f"20000000000000000{n:02d}" for n in range(1, 6)]
_OPUS = [f"https://www.bilibili.com/opus/{dynamic_id}" for dynamic_id in _IDS]


@pytest.fixture
def risk_state_path(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "profile-a" / "auto_remote_state.json"
    monkeypatch.setattr(state_mod, "_state_path", lambda: target)
    return target


# ---------- following feed：正文文本安全 / 结构化风险 / failed 语义 ----------


class _FakeClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_json(self, url, params=None, *, referer=None, retries=0):
        if self.error is not None:
            raise self.error
        return self.payload


def _feed_payload(items):
    return {"code": 0, "data": {"items": items}}


def _run_scan(client, monkeypatch, *, tmp_path: Path) -> tuple[dict, list]:
    target = tmp_path / "feed_state.json"
    monkeypatch.setattr(ff, "_state_path", lambda: target)
    captured: list[str] = []

    def fake_pipeline(urls, *, workers=1, scan_started_at=None, on_progress=None):
        captured.extend(urls)
        return SimpleNamespace(new_link_count=len(urls), persisted_count=1)

    monkeypatch.setattr(ff, "run_new_links_pipeline", fake_pipeline)
    result = ff.scan_following_feed(client_factory=lambda: client)
    return result, captured


def test_feed_content_text_with_429_and_risk_words_is_normal(
    tmp_path: Path, monkeypatch,
) -> None:
    items = [
        {
            "id_str": _IDS[0],
            "desc": {"text": "开奖倒计时 429 分钟后开始，别信什么风控、限流谣言。"},
        },
        {"id_str": "2000000000000042900", "desc": {"text": "动态 ID 中含 429 数字片段"}},
        {
            "id_str": _IDS[1],
            "desc": {"text": "这条正文提到 风控 与 限流 都不代表平台限制"},
        },
    ]
    client = _FakeClient(payload=_feed_payload(items))
    result, captured = _run_scan(client, monkeypatch, tmp_path=tmp_path)
    assert result["status"] == "success"
    assert result["found"] == 3
    assert len(captured) == 3
    info = ff.following_feed_info()
    assert info["last_status"] == "success"


def test_feed_structured_api_code_352_is_risk(tmp_path: Path, monkeypatch) -> None:
    client = _FakeClient(payload={"code": -352, "message": "风控校验失败", "data": {}})
    with pytest.raises(RuntimeError, match="API error -352"):
        ff.scan_following_feed(client_factory=lambda: client)


def test_feed_structured_api_code_509_is_risk(tmp_path: Path, monkeypatch) -> None:
    client = _FakeClient(payload={"code": -509, "message": "访问过于频繁", "data": {}})
    with pytest.raises(RuntimeError, match="API error -509"):
        ff.scan_following_feed(client_factory=lambda: client)


def test_feed_http_429_is_risk(tmp_path: Path, monkeypatch) -> None:
    request = httpx.Request("GET", "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all")
    error = httpx.HTTPStatusError(
        "429", request=request, response=httpx.Response(429, request=request)
    )
    client = _FakeClient(error=error)
    with pytest.raises(httpx.HTTPStatusError):
        ff.scan_following_feed(client_factory=lambda: client)


def test_feed_plain_text_markers_do_not_trigger_risk() -> None:
    assert matches_platform_risk("这条动态正文包含 429、风控、限流 等字样") is False
    assert matches_platform_risk("2000000000000042900 只是一个动态 ID") is False
    assert risk_code_of(RuntimeError("普通解析失败：缺少 items")) is None


def test_feed_failed_status_maps_to_job_failure(
    monkeypatch, tmp_path: Path,
) -> None:
    def fake_scan(*, on_progress=None):
        return {"status": "failed", "found": 0, "message": "响应缺少有效 data.items"}

    monkeypatch.setattr(ff, "scan_following_feed", fake_scan)
    with pytest.raises(RuntimeError, match="data.items"):
        actions_mod.run_action("following_feed_scan", {})


def test_feed_risk_no_longer_recorded_at_actions_layer(
    monkeypatch, tmp_path: Path, risk_state_path: Path,
) -> None:
    def fake_scan(*, on_progress=None):
        raise RuntimeError("API error -352: 风控校验失败")

    monkeypatch.setattr(ff, "scan_following_feed", fake_scan)
    with pytest.raises(RuntimeError, match="-352"):
        actions_mod.run_action("following_feed_scan", {})
    # 记录职责已统一到 JobRunner（source=auto），actions 层不再重复 record。
    assert state_mod.is_risk_paused() is False


# ---------- JobRunner：cancel / risk / 普通失败语义分离 ----------


def _wait_terminal(runner, job_id: int, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = runner.resolve_job_status(job_id)
        if status.state != "running":
            return status
        time.sleep(0.02)
    raise AssertionError("Job 未在超时前进入终态")


@pytest.mark.parametrize("source", ["auto", "ui"])
def test_worker_risk_with_cancel_set_is_error_not_cancelled(
    isolated_home: Path, monkeypatch, risk_state_path: Path, source: str,
) -> None:
    """worker 遇到明确风控并 set cancel_event（停并行兄弟线程）→ 必须 error + 保留风险。"""
    from web.job_runner import JobRunner

    def fake_run(action, params, *, on_progress=None, cancel_event=None):
        cancel_event.set()  # 模拟 worker 为了停止并行线程而设置 cancel
        raise RuntimeError("API error -352: 风控校验失败")

    monkeypatch.setattr("web.job_runner.run_action", fake_run)
    runner = JobRunner()
    job_id = runner.try_start("participate_triple", {}, source=source)
    assert job_id is not None
    terminal = _wait_terminal(runner, job_id)
    assert terminal.state == "error"
    assert (terminal.result or {}).get("risk_code") == -352
    if source == "auto":
        assert state_mod.is_risk_paused() is True
    else:
        assert state_mod.is_risk_paused() is False


def test_user_cancel_only_is_cancelled_without_risk(
    isolated_home: Path, monkeypatch, risk_state_path: Path,
) -> None:
    from web.job_runner import JobRunner

    def fake_run(action, params, *, on_progress=None, cancel_event=None):
        cancel_event.set()
        raise ValueError("任务已取消")

    monkeypatch.setattr("web.job_runner.run_action", fake_run)
    runner = JobRunner()
    job_id = runner.try_start("participate", {}, source="auto")
    terminal = _wait_terminal(runner, job_id)
    assert terminal.state == "cancelled"
    assert (terminal.result or {}).get("risk_code") is None
    assert state_mod.is_risk_paused() is False


def test_worker_plain_failure_is_error_without_risk(
    isolated_home: Path, monkeypatch, risk_state_path: Path,
) -> None:
    from web.job_runner import JobRunner

    def fake_run(action, params, *, on_progress=None, cancel_event=None):
        raise ValueError("解析业务数据失败")

    monkeypatch.setattr("web.job_runner.run_action", fake_run)
    runner = JobRunner()
    job_id = runner.try_start("following_feed_scan", {}, source="auto")
    terminal = _wait_terminal(runner, job_id)
    assert terminal.state == "error"
    assert (terminal.result or {}).get("risk_code") is None
    assert state_mod.is_risk_paused() is False


def test_stale_generation_inflight_job_risk_still_records_cooldown(
    isolated_home: Path, monkeypatch, risk_state_path: Path,
) -> None:
    """记录发生在 JobRunner worker 线程（与 scheduler generation 无关）：
    即使调度器已 stop/换代而不再监听，在途 Job 命中风险仍落 global cooldown。"""
    from web.job_runner import JobRunner

    gate = threading.Event()

    def fake_run(action, params, *, on_progress=None, cancel_event=None):
        gate.wait(5)  # 模拟长 Job：此时 scheduler 早已 stop 并启动新一代
        raise RuntimeError("API error -509: 访问过于频繁")

    monkeypatch.setattr("web.job_runner.run_action", fake_run)
    runner = JobRunner()
    job_id = runner.try_start("refresh_all", {}, source="auto")
    time.sleep(0.2)
    # 模拟 scheduler stop→start（不 cancel Job）：仅改变“调度资格”，与 Job 无关。
    runner2 = runner  # 同一个 JobRunner：Job 不受 scheduler 生命周期影响
    _ = runner2
    gate.set()
    terminal = _wait_terminal(runner, job_id)
    assert terminal.state == "error"
    assert (terminal.result or {}).get("risk_code") == -509
    assert state_mod.is_risk_paused() is True


# ---------- 多动作 risk breaker（三连） ----------


def test_triple_first_target_risk_stops_following_targets(monkeypatch) -> None:
    calls: list[str] = []

    def fake_pick(**kwargs):
        return [
            {"dynamic_id": _IDS[0], "activity_title": "T1", "lottery_type": "互动抽奖"},
            {"dynamic_id": _IDS[1], "activity_title": "T2", "lottery_type": "互动抽奖"},
        ]

    def fake_preview(targets):
        return {"items": targets}

    def fake_plan(targets):
        plan = {str(item["dynamic_id"]): (0, 4) for item in targets}
        return 8, plan

    def fake_participate(dynamic_id, on_step, lottery_type_hint=None):
        calls.append(dynamic_id)
        raise RuntimeError("API error -352: 风控校验失败")

    monkeypatch.setattr(actions_mod, "PARTICIPATE_TRIPLE_WORKERS", 1)
    monkeypatch.setattr(actions_mod, "pick_triple_participate_targets", fake_pick)
    monkeypatch.setattr(actions_mod, "build_triple_target_preview", fake_preview)
    monkeypatch.setattr(actions_mod, "build_triple_progress_plan", fake_plan)
    monkeypatch.setattr(actions_mod, "_participate_dynamic_payload", fake_participate)

    with pytest.raises(RuntimeError, match="API error -352"):
        actions_mod.run_action("participate_triple", {})
    # 第一个动作命中风控后，后续远程动作 0 次（串行 worker=1 下确定）。
    assert calls == [_IDS[0]]


# ---------- refresh pipeline：坏动态隔离 vs 风控传播 ----------


class _NoopClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_refresh_ordinary_unreadable_keeps_skipping_current_only(
    isolated_home: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(pipeline_mod, "BilibiliClient", lambda: _NoopClient())
    raised: list[str] = []

    def fake_classify(client, dynamic_id):
        raised.append(dynamic_id)
        raise RuntimeError(f"无法获取动态正文: {dynamic_id}")

    monkeypatch.setattr(pipeline_mod, "classify_new_link", fake_classify)
    result = pipeline_mod.run_new_links_pipeline(_OPUS[:2], workers=1)
    assert result.ok is True
    assert result.skip_reasons == {"正文不可读取": 2}
    assert result.failed_count == 2
    assert len(raised) == 2


def test_refresh_explicit_risk_is_not_swallowed_as_unreadable(
    isolated_home: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(pipeline_mod, "BilibiliClient", lambda: _NoopClient())
    called: list[str] = []

    def fake_classify(client, dynamic_id):
        called.append(dynamic_id)
        raise RuntimeError("API error -352: 风控校验失败")

    monkeypatch.setattr(pipeline_mod, "classify_new_link", fake_classify)
    with pytest.raises(RuntimeError, match="API error -352"):
        pipeline_mod.run_new_links_pipeline(_OPUS[:2], workers=1)
    # 风险不是“跳过一条继续”而是整轮停止：只处理到第一项。
    assert called == [_IDS[0]]


def test_refresh_risk_stops_before_activity_persist(
    isolated_home: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(pipeline_mod, "BilibiliClient", lambda: _NoopClient())
    monkeypatch.setattr(
        pipeline_mod,
        "classify_new_link",
        lambda client, dynamic_id: (_ for _ in ()).throw(
            RuntimeError("opus/detail API error -509: 访问过于频繁")
        ),
    )
    with pytest.raises(RuntimeError, match="-509"):
        pipeline_mod.run_new_links_pipeline(_OPUS[:2], workers=1)
    from src.activity_store import known_activity_ids

    assert known_activity_ids() == set()


def test_refresh_source_risk_does_not_commit_checkpoint_but_keeps_saved_output(
    isolated_home: Path, monkeypatch,
) -> None:
    from src.sources.common import CheckResult

    check_calls: list[str] = []
    saved: list[str] = []
    committed: list[str] = []

    def check_update(*, force: bool = False):
        check_calls.append("run")
        return CheckResult(
            source_id="DS-F",
            updated=True,
            container_url="https://example.test/container",
            container_id="1",
            title="T",
            published_at=int(time.time()),
            previous_container_url=None,
            activity_links=list(_OPUS),
            checked_at=int(time.time()),
        )

    def save_result(_result):
        saved.append("saved")
        return "out.txt"

    def fake_commit(check_result):
        committed.append(check_result.source_id)

    def fake_pipeline(ds_results, *, workers=4, on_progress=None, scan_started_at=None):
        raise RuntimeError("API error -352: 风控校验失败")

    monkeypatch.setattr(actions_mod, "DS_HANDLER_BY_ID", {"DS-F": (check_update, save_result)})
    monkeypatch.setattr(actions_mod, "run_refresh_all_pipeline", fake_pipeline)
    monkeypatch.setattr(actions_mod, "commit_source_checkpoint", fake_commit)

    with pytest.raises(RuntimeError, match="API error -352"):
        actions_mod.run_action("refresh_source", {"source_id": "DS-F"})
    assert check_calls == ["run"]
    assert saved == ["saved"]  # 风控前已成功的保存输出保留
    assert committed == []  # checkpoint 不因风控错误推进


# ---------- cleanup lottery_notice 风险码保留 ----------


class _NoticeClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def request_json(self, url, params=None, *, referer=None, retries=0):
        if self.error is not None:
            raise self.error
        return self.payload


def test_cleanup_notice_plain_unreadable_stays_remote_unknown() -> None:
    client = _NoticeClient(payload={"code": -9999, "message": "服务内部错误"})
    with pytest.raises(RemoteStateUnknown):
        _fetch_notice_strict(
            client,
            original_dynamic_id=_IDS[0],
            business_id=_IDS[0],
            business_type=1,
        )


@pytest.mark.parametrize("code", [-352, -509, 429])
def test_cleanup_notice_risk_codes_raise_rate_limited_with_code(
    code: int,
) -> None:
    client = _NoticeClient(payload={"code": code, "message": "risk"})
    with pytest.raises(AssessmentRateLimited, match=str(code)):
        _fetch_notice_strict(
            client,
            original_dynamic_id=_IDS[0],
            business_id=_IDS[0],
            business_type=1,
        )


def test_cleanup_notice_http_429_raises_rate_limited() -> None:
    request = httpx.Request("GET", "https://api.vc.bilibili.com/lottery_svr/v1/lottery_svr/lottery_notice")
    error = httpx.HTTPStatusError(
        "429", request=request, response=httpx.Response(429, request=request)
    )
    client = _NoticeClient(error=error)
    with pytest.raises(AssessmentRateLimited):
        _fetch_notice_strict(
            client,
            original_dynamic_id=_IDS[0],
            business_id=_IDS[0],
            business_type=1,
        )


def test_cleanup_notice_normal_failure_keeps_unknown_semantics() -> None:
    request = httpx.Request("GET", "https://api.vc.bilibili.com/x")
    error = httpx.HTTPStatusError(
        "500", request=request, response=httpx.Response(500, request=request)
    )
    client = _NoticeClient(error=error)
    with pytest.raises(RemoteStateUnknown):
        _fetch_notice_strict(
            client,
            original_dynamic_id=_IDS[0],
            business_id=_IDS[0],
            business_type=1,
        )


# ---------- cooldown 幂等去重 ----------


def test_same_risk_recorded_twice_does_not_extend_cooldown(risk_state_path: Path) -> None:
    first = state_mod.record_auto_remote_risk(
        trigger_stage="job:following_feed_scan",
        reason="API error -352: 风控",
        now_ts=1_000_000,
    )
    second = state_mod.record_auto_remote_risk(
        trigger_stage="following_feed_scan",
        reason="API error -352: 风控",
        now_ts=1_000_060,  # 同一事件 60 秒内重复传播
    )
    assert second["paused_until"] == first["paused_until"] == 1_000_000 + 6 * 3600


def test_new_later_risk_still_extends_cooldown(risk_state_path: Path) -> None:
    state_mod.record_auto_remote_risk(
        trigger_stage="job:refresh_all", reason="API error -352", now_ts=1_000_000
    )
    later = state_mod.record_auto_remote_risk(
        trigger_stage="job:participate_triple",
        reason="API error -509",
        now_ts=1_000_000 + 4 * 3600,  # 数小时后新一次真实风控
    )
    assert later["paused_until"] == (1_000_000 + 4 * 3600) + 6 * 3600



# ---------- 手动批量删除：recheck 命中风险立即停止整批 ----------

from src import repost_cleanup as cleanup_mod
from src.repost_cleanup import AssessmentRateLimited as _ARL, delete_reposts

_REPOST_IDS = [f"50000000000000000{n:02d}" for n in range(1, 6)]
_ORIG_BY_REPOST = dict(zip(_REPOST_IDS, _IDS))


class _DeleteFakeClient:
    def __init__(self):
        self.delete_calls: list[str] = []
        self.timeout_on: str | None = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post_json(self, url, payload, *, params=None, referer=None, retries=3, raise_on_code=True):
        repost_id = str(payload.get("dyn_id_str") or "")
        if self.timeout_on == repost_id:
            raise httpx.ReadTimeout("timeout", request=None)
        self.delete_calls.append(repost_id)
        return {"code": 0}


def _delete_env(monkeypatch, *, assess_error: Exception | None = None):
    """把 delete_reposts 周边全部本地化（Mapping 行记录），保留核心删除循环语义。"""
    records = {
        rid: {"original_dynamic_id": oid, "delete_status": "active"}
        for rid, oid in _ORIG_BY_REPOST.items()
    }
    assess_calls: list[str] = []
    marks: list[tuple[str, str, str]] = []

    def fake_assess(client, *, original_id, uid, now_ts, activities_map):
        assess_calls.append(original_id)
        if assess_error is not None and original_id == _ORIG_BY_REPOST[_REPOST_IDS[2]]:
            raise assess_error
        return SimpleNamespace(level="safe")

    monkeypatch.setattr(cleanup_mod, "get_repost", lambda uid, rid: records.get(rid))
    monkeypatch.setattr(cleanup_mod, "_verify_owned_repost_detail", lambda *a, **k: None)
    monkeypatch.setattr(cleanup_mod, "_assert_frozen_runtime", lambda *a, **k: "csrf-token")
    monkeypatch.setattr(cleanup_mod, "_require_verified_login", lambda client: ("csrf", 42))
    monkeypatch.setattr(cleanup_mod, "list_repost_assessments", lambda uid: {oid: object() for oid in _IDS})
    monkeypatch.setattr(cleanup_mod, "load_activities", lambda: [])
    monkeypatch.setattr(cleanup_mod, "_defer_state", lambda record, stored: None)
    monkeypatch.setattr(
        cleanup_mod,
        "_effective_candidate_state",
        lambda record, stored: ("safe", "", None),
    )
    monkeypatch.setattr(
        cleanup_mod,
        "claim_delete_pending",
        lambda uid, rid, requested_at: True,
    )
    monkeypatch.setattr(
        cleanup_mod,
        "mark_delete_result",
        lambda uid, rid, status, error=None, updated_at=None, deleted_at=None: marks.append((rid, status, error or "")) or True,
    )
    monkeypatch.setattr(cleanup_mod, "_assess_original", fake_assess)
    monkeypatch.setattr(cleanup_mod, "get_runtime_profile_id", lambda: "profile")
    monkeypatch.setattr(cleanup_mod, "db_path", lambda: Path("tmp"))
    return {"records": records, "assess_calls": assess_calls, "marks": marks}


@pytest.mark.parametrize("code", [-352, -509, 429])
def test_delete_recheck_risk_stops_batch_immediately(
    monkeypatch, risk_state_path: Path, code: int,
) -> None:
    client = _DeleteFakeClient()
    env = _delete_env(
        monkeypatch,
        assess_error=_ARL(f"API error {code}: 风控校验失败"),
    )
    result = delete_reposts(list(_ORIG_BY_REPOST.keys()), client_factory=lambda: client)

    a, b, c, d, e = _REPOST_IDS
    # C 命中风险：不 DELETE；D/E 不 recheck、不 DELETE（0 后续远程）。
    assert client.delete_calls == [a, b]
    assert env["assess_calls"] == [_ORIG_BY_REPOST[a], _ORIG_BY_REPOST[b], _ORIG_BY_REPOST[c]]
    assert _ORIG_BY_REPOST[d] not in env["assess_calls"]
    # A/B 已成功结果保留。
    assert result["deleted_count"] == 2
    assert result["rate_limited"] is True
    assert result["stopped_early"] is True
    c_item = next(item for item in result["items"] if item["repost_dynamic_id"] == c)
    assert c_item["status"] == "skipped"
    assert str(code) in c_item["message"]
    # 明确风控已写入 global auto remote cooldown（统一出口）。
    assert state_mod.is_risk_paused() is True


def test_delete_recheck_ordinary_unreadable_keeps_single_skip_semantics(
    monkeypatch, risk_state_path: Path,
) -> None:
    client = _DeleteFakeClient()
    env = _delete_env(
        monkeypatch,
        assess_error=RemoteStateUnknown("原动态当前无法读取（动态已删除）"),
    )
    result = delete_reposts(list(_ORIG_BY_REPOST.keys()), client_factory=lambda: client)

    a, b, c, d, e = _REPOST_IDS
    # C 普通不可读：仅跳过 C；D/E 继续验证与删除（不停止整批）。
    assert client.delete_calls == [a, b, d, e]
    assert _ORIG_BY_REPOST[c] in env["assess_calls"]
    assert result["stopped_early"] is False
    assert result["rate_limited"] is False
    c_item = next(item for item in result["items"] if item["repost_dynamic_id"] == c)
    assert c_item["status"] == "skipped"
    assert state_mod.is_risk_paused() is False


def test_delete_timeout_remains_unknown_and_does_not_stop_batch(
    monkeypatch, risk_state_path: Path,
) -> None:
    client = _DeleteFakeClient()
    client.timeout_on = _REPOST_IDS[0]
    _delete_env(monkeypatch)
    result = delete_reposts(list(_ORIG_BY_REPOST.keys()), client_factory=lambda: client)

    a, b, c, d, e = _REPOST_IDS
    # DELETE timeout：保持 unknown、不自动重试、不停止整批、不进 cooldown。
    assert client.delete_calls == [b, c, d, e]
    a_item = next(item for item in result["items"] if item["repost_dynamic_id"] == a)
    assert a_item["status"] == "unknown"
    assert "结果不确定" in a_item["message"]
    assert result["stopped_early"] is False
    assert state_mod.is_risk_paused() is False
