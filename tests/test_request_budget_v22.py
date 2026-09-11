"""V22 Bilibili 真实 HTTP attempt 预算窗口：计数 / 持久窗口 / 断点续扫 / 语义隔离。

CASE 1-25 覆盖：真实 attempt 计数、retry 计费、cache/LLM 0 计费、
reserve-before-request 硬上限、持久窗口重启/并发/Profile 隔离、时钟安全、
预算耗尽不是风控/LLM 故障、partial 不写 final、checkpoint 不误推进、
下一窗口续扫且不重复昂贵请求、V20 风控最高优先。
"""

from __future__ import annotations

import threading

import httpx
import pytest

from concurrent.futures import ThreadPoolExecutor

from src.bilibili_request_budget import (
    DEFAULT_BILIBILI_REQUEST_BUDGET,
    RequestBudgetExhausted,
    bilibili_request_budget_scope,
    get_bilibili_request_budget,
    reserve_bilibili_http_attempt,
    reserve_bilibili_request,
)
from src.pipeline.classify_step import ClassifyOutcome
from src.repost_cleanup import (
    CandidateAssessment,
    auto_maintain,
    cleanup_maintenance_cycle,
    scan_expired_reposts,
    sync_repost_history,
)
from src.repost_history import (
    RepostImportRecord,
    get_checkpoint,
    list_repost_assessments,
    mark_sync_needed,
    upsert_repost_records,
)

UID = "12345"
NOW = 1_800_000_000
WINDOW = 3600
DETAIL_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id=1"
NOTICE_URL = "https://api.vc.bilibili.com/lottery_svr/v1/lottery_svr/lottery_notice"
FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"


@pytest.fixture(autouse=True)
def _isolate_budget_dir(tmp_path, monkeypatch):
    """每个用例的 Profile 目录/预算文件隔离到 tmp_path（0 真实数据）。"""
    from src import data_paths

    monkeypatch.setattr(data_paths, "get_profile_dir", lambda profile_id=None: tmp_path)


def _original_id(index: int) -> str:
    return f"100000000000000{index:04d}"


def _repost_id(index: int) -> str:
    return f"200000000000000{index:04d}"


def _seed(count: int) -> None:
    upsert_repost_records(
        UID,
        [
            RepostImportRecord(
                repost_dynamic_id=_repost_id(index),
                original_dynamic_id=_original_id(index),
                reposted_at=100 + index,
                source="history_import",
            )
            for index in range(1, count + 1)
        ],
        seen_at=100,
    )


class _FakeDbPath:
    def resolve(self) -> "_FakeDbPath":
        return self

    def __str__(self) -> str:
        return "db-1"


def _login(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup.require_login", lambda: ("csrf-token", int(UID))
    )
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "profile-1")
    monkeypatch.setattr("src.repost_cleanup.db_path", lambda: _FakeDbPath())


def _patch_scope(monkeypatch, *, limit: int, now: int = NOW, window_seconds: int = WINDOW):
    import src.repost_cleanup as rc
    from src.bilibili_request_budget import (
        bilibili_request_budget_scope as real_scope,
    )

    def factory(*args, **kwargs):
        kwargs.setdefault("limit", limit)
        kwargs.setdefault("window_seconds", window_seconds)
        kwargs.setdefault("now_fn", lambda: now)
        return real_scope(*args, **kwargs)

    monkeypatch.setattr(rc, "bilibili_request_budget_scope", factory)


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


# ---------- CASE 1-4: 计数语义 ----------


def test_case1_real_attempts_hit_hard_limit(tmp_path) -> None:
    used = []
    for _ in range(5):
        snap = reserve_bilibili_request(
            now=NOW, limit=5, window_seconds=WINDOW, profile_dir=tmp_path
        )
        used.append(snap.used_requests)
    assert used == [1, 2, 3, 4, 5]

    with pytest.raises(RequestBudgetExhausted) as excinfo:
        reserve_bilibili_request(
            now=NOW, limit=5, window_seconds=WINDOW, profile_dir=tmp_path
        )
    assert excinfo.value.snapshot.used_requests == 5
    assert excinfo.value.snapshot.remaining_requests == 0
    # 硬上限：状态不再增长。
    assert (
        get_bilibili_request_budget(
            now=NOW, limit=5, window_seconds=WINDOW, profile_dir=tmp_path
        ).used_requests
        == 5
    )


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://api.bilibili.com")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )

    def json(self) -> dict:
        return self._payload


class _FakeTransport:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.calls = 0

    def get(self, url, params=None, headers=None):
        self.calls += 1
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, **kwargs):
        return self.get(url, **kwargs)

    def close(self) -> None:
        pass


def _make_client(monkeypatch, results) -> tuple[object, _FakeTransport]:
    from src import bilibili_client as bc

    monkeypatch.setattr(bc, "acquire_bilibili_request_slot", lambda: None)
    monkeypatch.setattr(bc.time, "sleep", lambda *_: None)
    client = bc.BilibiliClient(warmup=False)
    transport = _FakeTransport(results)
    client._client = transport
    return client, transport


def test_case2_http_retry_consumes_budget_each_attempt(tmp_path, monkeypatch) -> None:
    client, transport = _make_client(
        monkeypatch,
        [
            httpx.ConnectError("reset", request=httpx.Request("GET", FEED_URL)),
            httpx.ConnectError("reset", request=httpx.Request("GET", FEED_URL)),
            _FakeResponse({"code": 0, "data": {}}),
        ],
    )
    with bilibili_request_budget_scope(
        limit=10, window_seconds=WINDOW, now_fn=lambda: NOW, profile_dir=tmp_path
    ) as scope:
        client.get_json(FEED_URL, retries=3)

    assert transport.calls == 3
    assert scope.total_attempts == 3
    assert scope.category_counts == {"feed": 3}
    assert (
        get_bilibili_request_budget(
            now=NOW, limit=10, window_seconds=WINDOW, profile_dir=tmp_path
        ).used_requests
        == 3
    )


def test_request_categories_are_counted(tmp_path) -> None:
    with bilibili_request_budget_scope(
        limit=10, window_seconds=WINDOW, now_fn=lambda: NOW, profile_dir=tmp_path
    ) as scope:
        reserve_bilibili_http_attempt("https://api.bilibili.com/x/web-interface/nav")
        reserve_bilibili_http_attempt(FEED_URL)
        reserve_bilibili_http_attempt(DETAIL_URL)
        reserve_bilibili_http_attempt(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail?id=1"
        )
        reserve_bilibili_http_attempt(NOTICE_URL)
        reserve_bilibili_http_attempt("https://api.bilibili.com/other")

    assert scope.category_counts == {
        "nav": 1,
        "feed": 1,
        "detail": 1,
        "opus": 1,
        "notice": 1,
        "other": 1,
    }
    assert scope.total_attempts == 6


def test_case3_cache_hit_consumes_zero_budget(isolated_home, monkeypatch) -> None:
    _seed(1)
    _login(monkeypatch)
    _patch_scope(monkeypatch, limit=10)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])

    def local_only(client, *, original_id, uid, now_ts, activities_map):
        # 纯本地评估（等价 cache hit）：0 远程请求。
        return CandidateAssessment(
            "excluded", "明确非抽奖活动", "non_lottery", classification_source="activities"
        )

    monkeypatch.setattr("src.repost_cleanup._assess_original", local_only)
    result = scan_expired_reposts(now_ts=NOW, client_factory=lambda: _FakeClient())

    assert result["request_attempts"] == 0
    assert result["cache_hits"] == 1
    assert (
        get_bilibili_request_budget(
            now=NOW, limit=10, window_seconds=WINDOW
        ).used_requests
        == 0
    )


def test_case4_llm_call_consumes_zero_bilibili_budget(tmp_path, monkeypatch) -> None:
    from src import llm_client as llm

    monkeypatch.setattr(
        llm, "_post_chat_completion", lambda **kwargs: {"choices": [{"message": {"content": '{"ok": true}'}}]}
    )
    with bilibili_request_budget_scope(
        limit=5, window_seconds=WINDOW, now_fn=lambda: NOW, profile_dir=tmp_path
    ):
        assert llm.chat_json(system="s", user="u", config=llm.LlmConfig(api_key="k", base_url="https://x", model_name="m")) == {"ok": True}

    assert (
        get_bilibili_request_budget(
            now=NOW, limit=5, window_seconds=WINDOW, profile_dir=tmp_path
        ).used_requests
        == 0
    )


# ---------- CASE 5-6: 语义隔离 ----------


def test_case5_budget_exhausted_writes_no_platform_cooldown(isolated_home, monkeypatch) -> None:
    recorded: list[dict] = []
    monkeypatch.setattr(
        "web.auto_remote_state.record_auto_remote_risk",
        lambda **kwargs: recorded.append(kwargs),
    )
    _seed(2)
    _login(monkeypatch)
    _patch_scope(monkeypatch, limit=1)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])

    def remote(client, *, original_id, uid, now_ts, activities_map):
        reserve_bilibili_http_attempt(DETAIL_URL)
        return CandidateAssessment("safe", "ok", "safe_official_notice")

    monkeypatch.setattr("src.repost_cleanup._assess_original", remote)
    result = scan_expired_reposts(now_ts=NOW, client_factory=lambda: _FakeClient())

    assert result["budget_exhausted"] is True
    assert result["rate_limited"] is False
    assert result["risk_code"] is None
    assert recorded == []


def test_case6_budget_exhausted_is_not_llm_failure() -> None:
    from src.llm_resilience import is_config_failure, is_provider_failure

    snapshot = get_bilibili_request_budget(now=NOW, limit=1, window_seconds=WINDOW)
    exc = RequestBudgetExhausted(snapshot)
    assert is_provider_failure(exc) is False
    assert is_config_failure(exc) is False


# ---------- CASE 7-10: partial / 断点续扫 ----------


def test_case7_8_partial_candidate_not_final_completed_kept(isolated_home, monkeypatch) -> None:
    _seed(2)
    _login(monkeypatch)
    _patch_scope(monkeypatch, limit=2)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])
    calls: list[str] = []

    def assess(client, *, original_id, uid, now_ts, activities_map):
        calls.append(original_id)
        reserve_bilibili_http_attempt(DETAIL_URL)
        if original_id == _original_id(1):
            return CandidateAssessment("safe", "ok", "safe_official_notice")
        # 第一窗口：NOTICE reserve 耗尽并上抛；下一窗口：成功返回。
        reserve_bilibili_http_attempt(NOTICE_URL)
        return CandidateAssessment("safe", "ok", "safe_official_notice")

    monkeypatch.setattr("src.repost_cleanup._assess_original", assess)
    result = scan_expired_reposts(now_ts=NOW, client_factory=lambda: _FakeClient())

    assert result["budget_exhausted"] is True
    assert result["stop_reason"] == "REQUEST_BUDGET_EXHAUSTED"
    persisted = list_repost_assessments(UID)
    assert _original_id(1) in persisted
    assert _original_id(2) not in persisted  # 半处理候选绝不写 final
    assert result["pending_originals"] == 1
    assert calls == [_original_id(1), _original_id(2)]


def test_case9_10_next_window_resumes_without_repeating_completed(isolated_home, monkeypatch) -> None:
    _seed(2)
    _login(monkeypatch)
    _patch_scope(monkeypatch, limit=2, now=NOW)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])
    calls: list[str] = []

    def assess(client, *, original_id, uid, now_ts, activities_map):
        calls.append(original_id)
        reserve_bilibili_http_attempt(DETAIL_URL)
        if original_id == _original_id(1):
            return CandidateAssessment("safe", "ok", "safe_official_notice")
        # 第一窗口：NOTICE reserve 耗尽；下一窗口：成功返回。
        reserve_bilibili_http_attempt(NOTICE_URL)
        return CandidateAssessment("safe", "ok", "safe_official_notice")

    monkeypatch.setattr("src.repost_cleanup._assess_original", assess)
    first = scan_expired_reposts(now_ts=NOW, client_factory=lambda: _FakeClient())
    assert first["budget_exhausted"] is True

    # 下一窗口：时间前进到窗口过期后，预算自动恢复。
    _patch_scope(monkeypatch, limit=2, now=NOW + WINDOW + 1)
    second = scan_expired_reposts(now_ts=NOW + WINDOW + 1, client_factory=lambda: _FakeClient())

    assert second["budget_exhausted"] is False
    # 已完成候选不再进入评估（不重复昂贵请求）。
    assert calls == [_original_id(1), _original_id(2), _original_id(2)]
    assert _original_id(1) in list_repost_assessments(UID)
    assert _original_id(2) in list_repost_assessments(UID)


def test_case11_second_job_cannot_bypass_window(isolated_home, monkeypatch) -> None:
    _seed(2)
    _login(monkeypatch)
    _patch_scope(monkeypatch, limit=1)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])

    def remote(client, *, original_id, uid, now_ts, activities_map):
        reserve_bilibili_http_attempt(DETAIL_URL)
        return CandidateAssessment("safe", "ok", "safe_official_notice")

    monkeypatch.setattr("src.repost_cleanup._assess_original", remote)
    first = scan_expired_reposts(now_ts=NOW, client_factory=lambda: _FakeClient())
    assert first["budget_exhausted"] is True

    second = scan_expired_reposts(now_ts=NOW, client_factory=lambda: _FakeClient())
    assert second["budget_exhausted"] is True
    assert second["request_attempts"] == 0  # 窗口未恢复：0 真实请求
    assert second["evaluated_originals"] == 0


def test_case12_restart_does_not_reset_window(tmp_path) -> None:
    for _ in range(3):
        reserve_bilibili_request(
            now=NOW, limit=3, window_seconds=WINDOW, profile_dir=tmp_path
        )
    # 模拟重启：仅重新读取持久状态（无进程内缓存依赖）。
    snap = get_bilibili_request_budget(
        now=NOW, limit=3, window_seconds=WINDOW, profile_dir=tmp_path
    )
    assert snap.used_requests == 3
    with pytest.raises(RequestBudgetExhausted):
        reserve_bilibili_request(
            now=NOW, limit=3, window_seconds=WINDOW, profile_dir=tmp_path
        )


def test_case13_window_expiry_auto_recovers(tmp_path) -> None:
    for _ in range(3):
        reserve_bilibili_request(
            now=NOW, limit=3, window_seconds=WINDOW, profile_dir=tmp_path
        )
    snap = reserve_bilibili_request(
        now=NOW + WINDOW + 1, limit=3, window_seconds=WINDOW, profile_dir=tmp_path
    )
    assert snap.used_requests == 1
    assert snap.remaining_requests == 2


def test_case14_profiles_are_isolated(tmp_path) -> None:
    profile_a = tmp_path / "a"
    profile_b = tmp_path / "b"
    profile_a.mkdir()
    profile_b.mkdir()
    for _ in range(2):
        reserve_bilibili_request(
            now=NOW, limit=2, window_seconds=WINDOW, profile_dir=profile_a
        )
    with pytest.raises(RequestBudgetExhausted):
        reserve_bilibili_request(
            now=NOW, limit=2, window_seconds=WINDOW, profile_dir=profile_a
        )
    snap_b = reserve_bilibili_request(
        now=NOW, limit=2, window_seconds=WINDOW, profile_dir=profile_b
    )
    assert snap_b.used_requests == 1


def test_case15_concurrent_reserve_never_oversells(tmp_path) -> None:
    limit = 20
    barrier = threading.Barrier(8)

    def worker() -> bool:
        barrier.wait()
        try:
            reserve_bilibili_request(
                now=NOW, limit=limit, window_seconds=WINDOW, profile_dir=tmp_path
            )
            return True
        except RequestBudgetExhausted:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: worker(), range(40)))

    assert sum(results) == limit
    assert (
        get_bilibili_request_budget(
            now=NOW, limit=limit, window_seconds=WINDOW, profile_dir=tmp_path
        ).used_requests
        == limit
    )


def test_case16_out_of_scope_requests_unaffected(tmp_path, monkeypatch) -> None:
    # 先把窗口耗尽。
    reserve_bilibili_request(now=NOW, limit=1, window_seconds=WINDOW, profile_dir=tmp_path)
    with pytest.raises(RequestBudgetExhausted):
        reserve_bilibili_request(
            now=NOW, limit=1, window_seconds=WINDOW, profile_dir=tmp_path
        )

    # 无 scope（Winner/Following/普通 DS/登录等）不受预算影响。
    client, transport = _make_client(
        monkeypatch, [_FakeResponse({"code": 0, "data": {}})]
    )
    client.get_json(FEED_URL, retries=0)
    assert transport.calls == 1


# ---------- CASE 17-21: V20/V21 语义 ----------


@pytest.mark.parametrize("code", [-352, -509])
def test_case17_18_platform_risk_wins_over_budget(isolated_home, monkeypatch, code) -> None:
    recorded: list[dict] = []
    monkeypatch.setattr(
        "web.auto_remote_state.record_auto_remote_risk",
        lambda **kwargs: recorded.append(kwargs),
    )
    _seed(1)
    _login(monkeypatch)
    _patch_scope(monkeypatch, limit=10)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])

    from src.repost_cleanup import AssessmentRateLimited

    def risky(client, *, original_id, uid, now_ts, activities_map):
        reserve_bilibili_http_attempt(DETAIL_URL)
        raise AssessmentRateLimited(f"API error {code}: 平台风控")

    monkeypatch.setattr("src.repost_cleanup._assess_original", risky)
    result = scan_expired_reposts(now_ts=NOW, client_factory=lambda: _FakeClient())

    assert result["rate_limited"] is True
    assert result["risk_code"] == code
    assert result["budget_exhausted"] is False
    assert recorded and recorded[0]["code"] == str(code)


def test_case19_bilibili_http_429_wins_over_budget(isolated_home, monkeypatch) -> None:
    recorded: list[dict] = []
    monkeypatch.setattr(
        "web.auto_remote_state.record_auto_remote_risk",
        lambda **kwargs: recorded.append(kwargs),
    )
    _seed(1)
    _login(monkeypatch)
    _patch_scope(monkeypatch, limit=10)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])

    request = httpx.Request("GET", DETAIL_URL)
    http_429 = httpx.HTTPStatusError(
        "429", request=request, response=httpx.Response(429, request=request)
    )

    def risky(client, *, original_id, uid, now_ts, activities_map):
        reserve_bilibili_http_attempt(DETAIL_URL)
        raise http_429

    monkeypatch.setattr("src.repost_cleanup._assess_original", risky)
    result = scan_expired_reposts(now_ts=NOW, client_factory=lambda: _FakeClient())

    assert result["rate_limited"] is True
    assert result["risk_code"] == 429
    assert result["budget_exhausted"] is False
    assert recorded and recorded[0]["code"] == "429"


def test_case20_llm_429_does_not_touch_bilibili_budget(tmp_path, monkeypatch) -> None:
    from src import llm_client as llm

    recorded: list[dict] = []
    monkeypatch.setattr(
        "web.auto_remote_state.record_auto_remote_risk",
        lambda **kwargs: recorded.append(kwargs),
    )
    request = httpx.Request("POST", "https://llm.test/v1/chat/completions")
    status_error = httpx.HTTPStatusError(
        "429", request=request, response=httpx.Response(429, request=request)
    )
    monkeypatch.setattr(
        llm, "_post_chat_completion", lambda **kwargs: (_ for _ in ()).throw(status_error)
    )
    with bilibili_request_budget_scope(
        limit=5, window_seconds=WINDOW, now_fn=lambda: NOW, profile_dir=tmp_path
    ):
        with pytest.raises(llm.LlmTemporaryError):
            llm.chat_json(
                system="s",
                user="u",
                config=llm.LlmConfig(api_key="k", base_url="https://llm.test/v1", model_name="m"),
            )

    assert (
        get_bilibili_request_budget(
            now=NOW, limit=5, window_seconds=WINDOW, profile_dir=tmp_path
        ).used_requests
        == 0
    )
    assert recorded == []


def test_case21_minus_799_stays_temporary_restriction() -> None:
    from src.platform_risk import is_platform_risk_code

    assert is_platform_risk_code(-799) is False
    assert is_platform_risk_code(-352) is True


# ---------- CASE 22-23: 结果与 checkpoint ----------


def test_case22_budget_exhausted_is_partial_not_complete(isolated_home, monkeypatch, caplog) -> None:
    _seed(3)
    _login(monkeypatch)
    _patch_scope(monkeypatch, limit=1)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])

    def remote(client, *, original_id, uid, now_ts, activities_map):
        reserve_bilibili_http_attempt(DETAIL_URL)
        return CandidateAssessment("safe", "ok", "safe_official_notice")

    monkeypatch.setattr("src.repost_cleanup._assess_original", remote)
    with caplog.at_level("WARNING", logger="repost_cleanup"):
        result = scan_expired_reposts(now_ts=NOW, client_factory=lambda: _FakeClient())

    assert result["partial"] is True
    assert result["budget_exhausted"] is True
    assert result["stopped_early"] is True
    assert result["stop_reason"] == "REQUEST_BUDGET_EXHAUSTED"
    assert "预算" in result["message"]
    # 避免刷屏：预算耗尽主 warning 只记录一次。
    budget_warnings = [
        record for record in caplog.records if "预算已用完" in record.getMessage()
    ]
    assert len(budget_warnings) == 1


def _feed_item(*, dynamic_id: str, ts: int, original_id: str | None = None) -> dict:
    item = {
        "type": "DYNAMIC_TYPE_FORWARD" if original_id else "DYNAMIC_TYPE_NOTE",
        "id_str": dynamic_id,
        "modules": {"module_author": {"mid": UID, "name": "me", "pub_ts": ts}},
    }
    if original_id:
        item["orig"] = {
            "id_str": original_id,
            "modules": {"module_author": {"mid": "999", "name": "up"}},
        }
    return item


def _patch_sync(monkeypatch, pages: list[dict]) -> None:
    import src.repost_cleanup as rc
    from src import watch_feed

    monkeypatch.setattr(rc, "require_login", lambda: ("csrf-token", int(UID)))
    monkeypatch.setattr(
        rc, "_require_verified_login", lambda client: ("csrf-token", int(UID))
    )
    monkeypatch.setattr(rc, "PAGE_REQUEST_DELAY", 0)
    monkeypatch.setattr(watch_feed, "PAGE_REQUEST_DELAY", 0)

    remaining = list(pages)

    def fake_page(client, *, mid, offset, retries=0):
        reserve_bilibili_http_attempt(FEED_URL)
        if not remaining:
            raise AssertionError("不应继续分页")
        return remaining.pop(0)

    monkeypatch.setattr(rc, "fetch_space_feed_page", fake_page)


def test_case23_budget_exhaustion_does_not_advance_sync_checkpoint(
    isolated_home, monkeypatch
) -> None:
    old_head = _repost_id(99)
    mark_sync_needed(UID)
    from src.repost_history import save_checkpoint

    save_checkpoint(
        UID,
        head_dynamic_id=old_head,
        head_published_at=50,
        full_scan_completed=False,
        last_synced_at=60,
    )
    _patch_sync(
        monkeypatch,
        [
            {
                "items": [_feed_item(dynamic_id=_repost_id(1), ts=100, original_id=_original_id(1))],
                "offset": "next",
            }
        ],
    )
    _patch_scope(monkeypatch, limit=1)

    result = sync_repost_history(client_factory=lambda: _FakeClient())

    assert result["budget_exhausted"] is True
    assert result["sync_complete"] is False
    assert result["partial"] is True
    # 已导入的页保留，但 checkpoint 不推进（剩余记录下一窗口继续）。
    assert get_checkpoint(UID).head_dynamic_id == old_head
    assert result["sync_needed_cleared"] is False


def test_case24_sync_resumes_next_window(isolated_home, monkeypatch) -> None:
    old_head = _repost_id(99)
    from src.repost_history import save_checkpoint

    save_checkpoint(
        UID,
        head_dynamic_id=old_head,
        head_published_at=50,
        full_scan_completed=False,
        last_synced_at=60,
    )
    _patch_sync(
        monkeypatch,
        [
            {
                "items": [_feed_item(dynamic_id=_repost_id(1), ts=100, original_id=_original_id(1))],
                "offset": "next",
            }
        ],
    )
    _patch_scope(monkeypatch, limit=1, now=NOW)
    first = sync_repost_history(client_factory=lambda: _FakeClient())
    assert first["budget_exhausted"] is True

    # 下一窗口：预算恢复，安全续扫并推进 checkpoint。
    _patch_sync(
        monkeypatch,
        [
            {
                "items": [_feed_item(dynamic_id=_repost_id(1), ts=100, original_id=_original_id(1))],
                "offset": "next",
            },
            {"items": [_feed_item(dynamic_id=old_head, ts=50)], "offset": ""},
        ],
    )
    _patch_scope(monkeypatch, limit=5, now=NOW + WINDOW + 1)
    second = sync_repost_history(client_factory=lambda: _FakeClient())
    assert second["budget_exhausted"] is False
    assert second["sync_complete"] is True
    assert get_checkpoint(UID).head_dynamic_id == _repost_id(1)


# ---------- CASE 25 + 链路口径 ----------


def test_case25_clock_anomalies_fail_safe(tmp_path) -> None:
    from src.data_paths import _write_json_atomic

    state_path = tmp_path / "bilibili_request_budget.json"
    # 未来起点（时钟回拨）：不永久锁死、不无限放大 → 重建有界窗口。
    _write_json_atomic(
        state_path,
        {
            "window_started_at": NOW + 10_000_000,
            "window_expires_at": NOW + 10_003_600,
            "used_requests": 999,
            "budget_limit": 999,
        },
    )
    snap = reserve_bilibili_request(
        now=NOW, limit=5, window_seconds=WINDOW, profile_dir=tmp_path
    )
    assert snap.used_requests == 1
    assert snap.budget_limit == 5
    assert snap.remaining_seconds >= 0

    # 未来异常到期时间：夹到合法窗口长度。
    _write_json_atomic(
        state_path,
        {
            "window_started_at": NOW - 10,
            "window_expires_at": NOW + 10**9,
            "used_requests": 0,
            "budget_limit": 5,
        },
    )
    snap2 = reserve_bilibili_request(
        now=NOW, limit=5, window_seconds=WINDOW, profile_dir=tmp_path
    )
    assert snap2.window_expires_at <= NOW - 10 + WINDOW
    assert snap2.remaining_seconds >= 0


def test_auto_maintain_budget_exhausted_skips_assessment(isolated_home, monkeypatch) -> None:
    mark_sync_needed(UID)
    _login(monkeypatch)
    import src.repost_cleanup as rc

    monkeypatch.setattr(
        rc,
        "sync_repost_history",
        lambda **kwargs: {
            "uid": UID,
            "synced": False,
            "budget_exhausted": True,
            "partial": True,
            "budget_limit": 1,
            "used_requests": 1,
            "remaining_requests": 0,
            "message": "本轮 Bilibili 安全请求预算已用完",
        },
    )
    monkeypatch.setattr(
        rc,
        "_assess_original",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应评估")),
    )
    result = auto_maintain(client_factory=lambda: _FakeClient())

    assert result["budget_exhausted"] is True
    assert result["evaluated_originals"] == 0
    assert result["stopped_early"] is True


def test_maintenance_cycle_skips_delete_when_sync_budget_exhausted(
    isolated_home, monkeypatch
) -> None:
    import src.repost_cleanup as rc

    monkeypatch.setattr(
        rc,
        "auto_maintain",
        lambda **kwargs: {
            "uid": UID,
            "synced": False,
            "budget_exhausted": True,
            "message": "预算已用完",
        },
    )
    monkeypatch.setattr(
        rc,
        "auto_cleanup_delete",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("不应删除")),
    )
    result = cleanup_maintenance_cycle()

    phases = result["phases"]
    assert phases[0]["result"]["budget_exhausted"] is True
    assert phases[1]["result"]["reason"] == "request_budget_exhausted"
    assert phases[2]["result"]["reason"] == "request_budget_exhausted"
    assert "预算" in result["message"]


def test_default_budget_constant_is_conservative() -> None:
    # 安全依据：真实事故 ~413 requests 后出现 -352；默认显著低于该量级。
    assert DEFAULT_BILIBILI_REQUEST_BUDGET == 120
    assert DEFAULT_BILIBILI_REQUEST_BUDGET < 200
