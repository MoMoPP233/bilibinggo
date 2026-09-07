"""一键智能评估连续批次（M4）离线测试：全 mock，零真实网络/LLM。

A–W（按 M4 验收）：
A 0 条 success；B 1 条；C 120 一个业务批；D 121 自动 120+1；E 240 两批；
F 241 三批；G 615 全部 frozen backlog；H 运行中新增不纳入本 Job；
I/J 同一 original 同一 Job 最多一次（含 retryable_unknown 不重试）；
K 每批重新查 DB；M made_progress=0 停止；N/O/P -352/-509/429 停止所有后续批次；
Q cancel 停止；R Profile mismatch 停止；S stale generation 停止；
T progress step 单调、total 固定；V 完成后无额外 progress；W terminal 不伪装 total/total。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.pipeline.classify_step import ClassifyOutcome
from src.repost_cleanup import (
    CandidateAssessment,
    RemoteStateUnknown,
    scan_expired_reposts,
)
from src.repost_history import (
    RepostImportRecord,
    list_repost_assessments,
    upsert_repost_assessment,
    upsert_repost_records,
)

UID = "12345"


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _login(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])


def _seed(count: int, monkeypatch, *, batch_size: int = 120) -> None:
    _login(monkeypatch)
    monkeypatch.setattr("src.repost_cleanup.ASSESSMENT_BATCH_SIZE", batch_size)
    originals = [f"100000000000000000{n:02d}" for n in range(1, count + 1)]
    upsert_repost_records(
        UID,
        [
            RepostImportRecord(
                repost_dynamic_id=f"200000000000000000{n:02d}",
                original_dynamic_id=original,
                reposted_at=100 + n,
                source="history_import",
            )
            for n, original in enumerate(originals, 1)
        ],
        seen_at=100,
    )
    return originals


def _assess_plan(monkeypatch, *, fail_on=None, calls: dict | None = None):
    """可编程评估：默认 manual_review；可指定 raise RemoteStateUnknown/RuntimeError(risk)。"""
    counters = calls if calls is not None else {"n": 0, "by_id": {}}
    default = {"mode": "manual"}

    def assess(client, *, original_id, uid, now_ts, activities_map):
        counters["n"] += 1
        counters.setdefault("by_id", {})[original_id] = counters["by_id"].get(original_id, 0) + 1
        plan = fail_on and fail_on.get(original_id) or default
        mode = plan.get("mode", "manual")
        if mode == "remote_unknown":
            raise RemoteStateUnknown("remote-unavailable")
        if mode == "risk":
            raise RuntimeError(plan.get("message", "API error -352: 风控校验失败"))
        if mode == "add_more":
            plan.get("side_effect")(original_id)
        return CandidateAssessment(
            "manual_review", "确定为历史抽奖", "forward_lottery_manual",
            lottery_type="转发抽奖", classification_source="public_classifier",
        )

    monkeypatch.setattr("src.repost_cleanup._assess_original", assess)
    return counters


def _run(monkeypatch, **kwargs):
    progresses: list[tuple[int, int, str]] = []

    def on_progress(done, total, message, log_line=None):
        progresses.append((int(done), int(total), str(message)))

    result = scan_expired_reposts(
        on_progress=on_progress,
        now_ts=1_800_000_000,
        client_factory=lambda: _FakeClient(),
        **kwargs,
    )
    return result, progresses


# ---------- A/B/C/D/E/F/G ----------


def test_empty_backlog_success_no_assessments(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    calls = _assess_plan(monkeypatch)
    result, _ = _run(monkeypatch)
    assert result["evaluated_originals"] == 0
    assert result["pending_originals"] == 0
    assert result["message"].startswith("智能评估完成")
    assert calls["n"] == 0


def test_single_original_assessed_once(isolated_home, monkeypatch) -> None:
    _seed(1, monkeypatch)
    calls = _assess_plan(monkeypatch)
    result, _ = _run(monkeypatch)
    assert result["evaluated_originals"] == 1
    assert result["batches_done"] == 1
    assert calls["n"] == 1


def test_120_is_one_business_batch(isolated_home, monkeypatch) -> None:
    _seed(120, monkeypatch)
    calls = _assess_plan(monkeypatch)
    result, _ = _run(monkeypatch)
    assert result["evaluated_originals"] == 120
    assert result["batches_done"] == 1
    assert calls["n"] == 120


def test_121_auto_continues_120_plus_1(isolated_home, monkeypatch) -> None:
    _seed(121, monkeypatch)
    calls = _assess_plan(monkeypatch)
    result, _ = _run(monkeypatch)
    assert result["evaluated_originals"] == 121
    assert result["batches_done"] == 2
    assert calls["n"] == 121


def test_240_two_batches_one_job(isolated_home, monkeypatch) -> None:
    _seed(240, monkeypatch)
    calls = _assess_plan(monkeypatch)
    result, _ = _run(monkeypatch)
    assert result["evaluated_originals"] == 240
    assert result["batches_done"] == 2
    assert calls["n"] == 240


def test_241_three_batches_one_job(isolated_home, monkeypatch) -> None:
    _seed(241, monkeypatch)
    calls = _assess_plan(monkeypatch)
    result, _ = _run(monkeypatch)
    assert result["evaluated_originals"] == 241
    assert result["batches_done"] == 3
    assert calls["n"] == 241


def test_615_full_frozen_backlog_in_one_job(isolated_home, monkeypatch) -> None:
    _seed(615, monkeypatch)
    calls = _assess_plan(monkeypatch)
    result, progresses = _run(monkeypatch)
    assert result["evaluated_originals"] == 615
    assert result["pending_originals"] == 0
    assert result["batches_done"] == 6  # 5×120 + 15
    assert calls["n"] == 615
    # T：progress total 固定 615、step 单调递增
    assert all(p[1] == 615 for p in progresses)
    steps = [p[0] for p in progresses]
    assert steps == sorted(steps)
    assert steps[-1] == 615


# ---------- H：运行中新增不纳入本 Job ----------


def test_new_record_during_job_is_not_consumed(isolated_home, monkeypatch) -> None:
    originals = _seed(5, monkeypatch, batch_size=2)
    calls: dict = {"n": 0, "by_id": {}}
    new_original = "10000000000000000616"

    def side_effect(_oid):
        upsert_repost_records(
            UID,
            [
                RepostImportRecord(
                    repost_dynamic_id="20000000000000000616",
                    original_dynamic_id=new_original,
                    reposted_at=9999,
                    source="history_import",
                )
            ],
            seen_at=9999,
        )

    fail_on = {originals[0]: {"mode": "add_more", "side_effect": side_effect}}
    _assess_plan(monkeypatch, fail_on=fail_on, calls=calls)
    result, _ = _run(monkeypatch)
    # 本 Job 冻结 scope=5：新纪录不纳入
    assert result["evaluated_originals"] == 5
    assert new_original not in list_repost_assessments(UID)
    # 下一 Job 处理它
    upsert_repost_assessment  # noqa: B018  # noqa
    result2, _ = _run(monkeypatch)
    assert result2["evaluated_originals"] == 1
    assert new_original in list_repost_assessments(UID)


# ---------- I/J：同一 Job 同一 original 只评估一次（含 retryable_unknown） ----------


def test_retryable_unknown_never_reassessed_in_same_job(isolated_home, monkeypatch) -> None:
    originals = _seed(3, monkeypatch, batch_size=1)
    calls: dict = {"n": 0, "by_id": {}}
    fail_on = {originals[0]: {"mode": "remote_unknown"}}
    _assess_plan(monkeypatch, fail_on=fail_on, calls=calls)
    result, _ = _run(monkeypatch)
    assert result["evaluated_originals"] == 3
    # 失败的第一条只尝试一次（后续批次不再进入）
    assert calls["by_id"][originals[0]] == 1
    record = list_repost_assessments(UID).get(originals[0])
    assert getattr(record, "assessment_status") == "retryable_unknown"
    # 未来 Job 可再评估（M4 原则：同 Job 一次，跨 Job 允许）
    fail_on2 = {}
    _assess_plan(monkeypatch, fail_on=fail_on2, calls=calls)
    result2, _ = _run(monkeypatch)
    assert result2["evaluated_originals"] == 1


# ---------- K：每批重新查库 ----------


def test_each_batch_requeries_db(isolated_home, monkeypatch) -> None:
    originals = _seed(5, monkeypatch, batch_size=2)
    calls: dict = {"n": 0, "by_id": {}}
    late_original = originals[4]

    def side_effect(_oid):
        # 第一批处理期间，为第五个 original 写入终态（模拟外部/其它 Job 已处理）
        upsert_repost_assessment(
            UID,
            late_original,
            assessment_level="manual_review",
            assessment_status="final",
            reason_code="forward_lottery_manual",
            assessed_at=1_800_000_000,
        )

    fail_on = {originals[0]: {"mode": "add_more", "side_effect": side_effect}}
    _assess_plan(monkeypatch, fail_on=fail_on, calls=calls)
    result, _ = _run(monkeypatch)
    assert result["evaluated_originals"] == 4  # 第二批重新查库后发现 final 而跳过
    assert late_original not in calls["by_id"]


# ---------- M：made_progress=0 停止 ----------


def test_no_state_progress_stops_even_if_pending_remains(isolated_home, monkeypatch) -> None:
    originals = _seed(3, monkeypatch, batch_size=1)
    # 前两条已经是 retryable_unknown：再评估仍会得到相同签名（无语义进展）→ 停止
    for original in originals[:2]:
        upsert_repost_assessment(
            UID,
            original,
            assessment_level="blocked",
            assessment_status="retryable_unknown",
            reason_code="remote_unknown",
            reason="原动态状态无法可靠判断：remote-unavailable",
            classification_source="public_classifier",
            assessed_at=1_800_000_000,
        )
    fail_on = {original: {"mode": "remote_unknown"} for original in originals}
    calls: dict = {"n": 0, "by_id": {}}
    _assess_plan(monkeypatch, fail_on=fail_on, calls=calls)
    result, _ = _run(monkeypatch)
    assert result["stop_reason"] == "NO_STATE_PROGRESS"
    assert result["evaluated_originals"] == 1
    assert calls["n"] == 1


# ---------- N/O/P：风险停止所有后续批次 ----------


@pytest.mark.parametrize("risk_message", [
    "API error -352: 风控校验失败",
    "API error -509: 访问过于频繁",
    "API error 429: Too Many Requests",
])
def test_structured_risk_stops_all_following_batches(isolated_home, monkeypatch, risk_message) -> None:
    originals = _seed(7, monkeypatch, batch_size=2)
    fail_on = {originals[1]: {"mode": "risk", "message": risk_message}}
    calls: dict = {"n": 0, "by_id": {}}
    _assess_plan(monkeypatch, fail_on=fail_on, calls=calls)
    result, _ = _run(monkeypatch)
    assert result["rate_limited"] is True
    assert result["stopped_early"] is True
    # 第 2 次尝试即命中风险：第一条已保存，其余（含后续批次）均不再评估
    assert result["evaluated_originals"] == 1
    assert calls["n"] == 2
    assert result["pending_originals"] == 6


# ---------- Q：cancel ----------


def test_cancel_stops_without_following_batches(isolated_home, monkeypatch) -> None:
    originals = _seed(5, monkeypatch, batch_size=2)
    calls: dict = {"n": 0, "by_id": {}}
    _assess_plan(monkeypatch, calls=calls)
    persisted_ids: list[str] = []

    def cancel_check():
        return len(list_repost_assessments(UID)) >= 2

    with pytest.raises(RuntimeError, match="任务已取消"):
        scan_expired_reposts(
            now_ts=1_800_000_000,
            client_factory=lambda: _FakeClient(),
            cancel_check=cancel_check,
        )
    assert calls["n"] == 2
    assert len(list_repost_assessments(UID)) == 2


# ---------- R：Profile mismatch ----------


def test_profile_mismatch_stops_between_batches(isolated_home, monkeypatch) -> None:
    _seed(5, monkeypatch, batch_size=2)
    _assess_plan(monkeypatch)
    values = iter(["profile-a", "profile-b"])

    def flaky_profile():
        return next(values)

    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", flaky_profile)
    with pytest.raises(RuntimeError, match="Profile 发生变化"):
        _run(monkeypatch)


# ---------- S：stale generation（可注入 guard） ----------


def test_stale_generation_check_stops_next_batch(isolated_home, monkeypatch) -> None:
    _seed(5, monkeypatch, batch_size=2)
    calls: dict = {"n": 0, "by_id": {}}
    _assess_plan(monkeypatch, calls=calls)
    checks = {"n": 0}

    def stale_check():
        checks["n"] += 1
        return checks["n"] >= 2  # 第一批后判定过期

    with pytest.raises(RuntimeError, match="任务已过期"):
        scan_expired_reposts(
            now_ts=1_800_000_000,
            client_factory=lambda: _FakeClient(),
            stale_check=stale_check,
        )
    assert calls["n"] == 2  # 仅第一批两条被评估


# ---------- V：完成后无 progress ----------


def test_no_progress_after_completion(isolated_home, monkeypatch) -> None:
    _seed(4, monkeypatch, batch_size=2)
    _assess_plan(monkeypatch)
    result, progresses = _run(monkeypatch)
    assert result["evaluated_originals"] == 4
    last = progresses[-1]
    assert last[0] == 4  # 完成后 step=total，无更多回调（同步函数返回即结束）

# ---------- W：terminal 不伪装 total/total ----------


def _wait_terminal(runner, job_id: int, timeout: float = 5.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = runner.resolve_job_status(job_id)
        if status.state != "running":
            return status
        time.sleep(0.02)
    raise AssertionError("Job 未在超时前进入终态")


def test_terminal_error_keeps_real_progress_step(isolated_home, monkeypatch) -> None:
    from web.job_runner import JobRunner

    def fake_run(action, params, *, on_progress=None, cancel_event=None):
        on_progress(step=3, total=10, message="第1批…", log_append="log")
        raise RuntimeError("普通业务失败")

    monkeypatch.setattr("web.job_runner.run_action", fake_run)
    runner = JobRunner()
    job_id = runner.try_start("scan_expired_reposts", {}, source="ui")
    assert job_id is not None
    terminal = _wait_terminal(runner, job_id)
    assert terminal.state == "error"
    # error 不被伪装成 10/10
    assert terminal.progress_step == 3
    assert terminal.progress_total == 10


def test_terminal_success_completes_total(isolated_home, monkeypatch) -> None:
    from web.job_runner import JobRunner

    def fake_run(action, params, *, on_progress=None, cancel_event=None):
        on_progress(step=7, total=10, message="第1批…", log_append="log")
        return {"ok": True, "message": "完成", "result": None, "log": ""}

    monkeypatch.setattr("web.job_runner.run_action", fake_run)
    runner = JobRunner()
    job_id = runner.try_start("scan_expired_reposts", {}, source="ui")
    assert job_id is not None
    terminal = _wait_terminal(runner, job_id)
    assert terminal.state == "success"
    # 真正完成的 success 才补齐 total/total
    assert terminal.progress_step == 10
    assert terminal.progress_total == 10
