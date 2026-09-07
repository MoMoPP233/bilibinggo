"""DS9 话题发现（M1）离线测试：全 mock，零真实 Bilibili/LLM/参与网络。

覆盖 A–N：
A 默认话题 + per-topic 独立状态
B Profile 隔离（配置与 checkpoint 各自独立）
C URL/topic_id 添加、去重、格式校验
D 原创动态 canonical + 公共链接 + checkpoint 推进
E 转发动态：canonical=orig，outer 仅来源元数据
F 转发缺 orig：不拿 outer 顶替，保持 incomplete/retryable
G 多话题同一动态只进一次活动集（batch 内去重；DB 去重由公共流水线兜底）
H partial failure：A 成功推进、B 失败保留旧 checkpoint、Job partial
I fingerprint 相同且无未完成项 → 不重复产出
J fingerprint 相同但存在 incomplete → 必须重试该话题
K 公共 pipeline 未完成（不调用 commit 钩子）→ checkpoint 不推进
L 风控 -352/-509/HTTP429：0 retry、停止后续话题请求、上抛不吞
M 一键全部数据源：DS9 注册进统一 Source 列表；DS9 普通失败不回滚其它 DS
N AutoScheduler refresh_all 走统一入口（无第二套调度）
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.sources import ds9_topics as ds9
from src.sources.common import CheckResult, commit_source_checkpoint

TOPIC_A = "1261901"
TOPIC_B = "1055656"
DID1 = "2000000000000000001"
DID2 = "2000000000000000002"
DID3 = "2000000000000000003"
DID4 = "2000000000000000004"
URL_TOPIC = "https://www.bilibili.com/v/topic/detail?topic_id=123456&spm_id_from=333.1387.collection.content.click"


@pytest.fixture(autouse=True)
def _isolated_runtime(isolated_home: Path):
    """DB/commit 钩子依赖的隔离运行时（每用例独立数据根 + 引擎）。"""
    return isolated_home


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "profile-a" / "ds9_topics.json"
    monkeypatch.setattr(ds9, "_state_path", lambda: target)
    return target


def _entry(outer: str, *, orig: str | None = None, dtype: int = 0) -> dict:
    desc: dict = {"dynamic_id": outer, "type": dtype}
    if orig is not None:
        desc["orig"] = {"id_str": orig}
    return {"desc": desc, "id_str": outer}


class _FakeTopicClient:
    """可编程 fake：每话题 topic_new 数据 + 可选的每话题异常/风险。"""

    def __init__(self, *, data_by_topic=None, errors_by_topic=None, history_by_topic=None):
        self.data_by_topic = dict(data_by_topic or {})
        self.errors_by_topic = dict(errors_by_topic or {})
        self.history_by_topic = dict(history_by_topic or {})
        self.calls: list[tuple[str, str]] = []
        self.history_calls: list[tuple[str, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _cards(self, ids):
        return {"code": 0, "data": {"hot_list": [{"desc": {"dynamic_id": did}} for did in ids]}}

    def get_topic_new(self, topic_id: str) -> dict:
        self.calls.append(("new", topic_id))
        error = self.errors_by_topic.get(topic_id)
        if error is not None:
            raise error
        payload = self.data_by_topic.get(topic_id, {"code": 0, "data": {}})
        return payload

    def get_topic_history(self, topic_id: str, *, offset_dynamic_id: str = "", page_size: int = 20) -> dict:
        self.history_calls.append((topic_id, offset_dynamic_id or ""))
        error = self.errors_by_topic.get(f"history:{topic_id}")
        if error is not None:
            raise error
        payload = self.history_by_topic.get(topic_id)
        if payload is None:
            return {"code": 0, "data": {}}
        return payload


def _original_payload(outer: str) -> dict:
    return {"code": 0, "data": {"hot_list": [_entry(outer)]}}


def _install_client(monkeypatch: pytest.MonkeyPatch, client: _FakeTopicClient) -> None:
    monkeypatch.setattr(ds9, "BilibiliClient", lambda: client)


# ---------- A：默认话题 + 独立状态 ----------


def test_default_two_builtin_topics_with_independent_state(state_path: Path) -> None:
    topics = ds9.list_topics()
    assert [t["topic_id"] for t in topics] == [TOPIC_A, TOPIC_B]
    assert [t["name"] for t in topics] == ["抽奖！！！", "转发动态抽奖！"]
    assert all(t["kind"] == "built-in" for t in topics)

    updated = ds9.set_topic_enabled(TOPIC_A, False)
    assert updated["enabled"] is False
    after = {t["topic_id"]: t for t in ds9.list_topics()}
    assert after[TOPIC_B]["enabled"] is True  # 彼此独立
    # built-in 不可删除，只可暂停
    with pytest.raises(ValueError, match="内置话题不可删除"):
        ds9.remove_topic(TOPIC_A)


# ---------- B：Profile 隔离 ----------


def test_profile_isolation_for_config_and_checkpoint(tmp_path: Path, monkeypatch) -> None:
    a = tmp_path / "account-a" / "ds9_topics.json"
    b = tmp_path / "account-b" / "ds9_topics.json"

    monkeypatch.setattr(ds9, "_state_path", lambda: a)
    ds9.add_topic("A 自定", "123456")
    assert any(t["topic_id"] == "123456" for t in ds9.list_topics())

    monkeypatch.setattr(ds9, "_state_path", lambda: b)
    assert not any(t["topic_id"] == "123456" for t in ds9.list_topics())

    # A 成功扫描（checkpoint 只进 A）
    client_a = _FakeTopicClient(data_by_topic={TOPIC_A: _original_payload(DID1)})
    _install_client(monkeypatch, client_a)
    monkeypatch.setattr(ds9, "_state_path", lambda: a)
    result = ds9.check_update()
    assert result.updated is True
    commit_source_checkpoint(result)
    record_a = {t["topic_id"]: t for t in ds9.list_topics()}
    assert record_a[TOPIC_A]["fp"] is not None

    # B 扫描不同内容，A 不受影响
    client_b = _FakeTopicClient(data_by_topic={TOPIC_A: _original_payload(DID2)})
    _install_client(monkeypatch, client_b)
    monkeypatch.setattr(ds9, "_state_path", lambda: b)
    ds9.check_update()
    record_b = {t["topic_id"]: t for t in ds9.list_topics()}
    assert record_b[TOPIC_A]["fp"] is None or record_b[TOPIC_A]["fp"] != record_a[TOPIC_A]["fp"]

    monkeypatch.setattr(ds9, "_state_path", lambda: a)
    assert {t["topic_id"]: t for t in ds9.list_topics()}[TOPIC_A]["fp"] == record_a[TOPIC_A]["fp"]


# ---------- C：URL 添加 / 去重 / 校验 ----------


def test_add_topic_from_url_dedup_and_validation(state_path: Path) -> None:
    topic = ds9.add_topic("我的话题", URL_TOPIC)
    assert topic["topic_id"] == "123456"
    assert topic["kind"] == "custom"
    assert topic["enabled"] is True

    with pytest.raises(ValueError, match="已存在"):
        ds9.add_topic("重复", "123456")

    with pytest.raises(ValueError, match="格式无效"):
        ds9.add_topic("x", "not-a-topic")
    with pytest.raises(ValueError, match="格式无效"):
        ds9.add_topic("x", "https://www.bilibili.com/video/BV1xx")


# ---------- D：原创动态 → 公共候选 → checkpoint ----------


def test_original_dynamic_goes_to_pipeline_then_checkpoint(state_path: Path, monkeypatch) -> None:
    client = _FakeTopicClient(data_by_topic={TOPIC_A: _original_payload(DID1)})
    _install_client(monkeypatch, client)
    result = ds9.check_update()
    assert result.updated is True
    assert result.activity_links == [f"https://www.bilibili.com/opus/{DID1}"]

    # 未提交前 fp 不推进（K 的正面：提交钩子即“流水线成功后”的提交点）
    record_before = {t["topic_id"]: t for t in ds9.list_topics()}
    assert record_before[TOPIC_A]["fp"] is None

    commit_source_checkpoint(result)  # 公共流水线成功后的提交点
    record = {t["topic_id"]: t for t in ds9.list_topics()}
    assert record[TOPIC_A]["fp"] is not None
    assert record[TOPIC_A]["last_success_at"] is not None
    assert record[TOPIC_A]["last_error"] == ""


# ---------- E：转发动态 canonical=orig ----------


def test_repost_canonical_is_orig_not_outer(state_path: Path, monkeypatch) -> None:
    payload = {
        "code": 0,
        "data": {"hot_list": [_entry(DID3, orig=DID1, dtype=1)]},
    }
    client = _FakeTopicClient(data_by_topic={TOPIC_A: payload})
    _install_client(monkeypatch, client)
    result = ds9.check_update()
    assert f"opus/{DID3}" not in result.activity_links  # outer 不进参与链
    assert result.activity_links == [f"https://www.bilibili.com/opus/{DID1}"]
    commit_source_checkpoint(result)
    record = {t["topic_id"]: t for t in ds9.list_topics()}
    assert record[TOPIC_A]["incomplete"] == []


# ---------- F：转发缺 orig → incomplete，绝不拿 outer 顶替 ----------


def test_repost_without_orig_is_incomplete_retryable(state_path: Path, monkeypatch) -> None:
    payload = {"code": 0, "data": {"hot_list": [_entry(DID3, dtype=1)]}}
    client = _FakeTopicClient(data_by_topic={TOPIC_A: payload})
    _install_client(monkeypatch, client)
    result = ds9.check_update()
    assert result.activity_links == []  # outer 不入参与链
    assert result.updated is True  # 有状态变化需要提交
    commit_source_checkpoint(result)
    record = {t["topic_id"]: t for t in ds9.list_topics()}
    assert record[TOPIC_A]["incomplete"] == [DID3]
    assert record[TOPIC_A]["fp"] is not None


# ---------- G：两话题发现同一动态，batch 内只产生一个链接 ----------


def test_same_dynamic_from_two_topics_deduplicated(state_path: Path, monkeypatch) -> None:
    client = _FakeTopicClient(
        data_by_topic={
            TOPIC_A: _original_payload(DID1),
            TOPIC_B: _original_payload(DID1),
        }
    )
    _install_client(monkeypatch, client)
    result = ds9.check_update()
    assert result.activity_links == [f"https://www.bilibili.com/opus/{DID1}"]
    assert len(result.activity_links) == 1
    # 最终活动库去重由公共 run_new_links_pipeline 的 known_activity_ids 兜底。


# ---------- H：partial failure ----------


def test_partial_failure_keeps_failed_topic_checkpoint(state_path: Path, monkeypatch) -> None:
    client = _FakeTopicClient(
        data_by_topic={TOPIC_A: _original_payload(DID1)},
        errors_by_topic={TOPIC_B: RuntimeError("网络请求失败: connect timeout")},
    )
    _install_client(monkeypatch, client)
    result = ds9.check_update()
    assert result.updated is True
    assert result.activity_links == [f"https://www.bilibili.com/opus/{DID1}"]
    summary = result.ds9_summary
    assert summary["partial"] is True
    assert summary["failed_topics"] == [TOPIC_B]

    commit_source_checkpoint(result)
    record = {t["topic_id"]: t for t in ds9.list_topics()}
    assert record[TOPIC_A]["fp"] is not None  # 成功话题推进
    assert record[TOPIC_B]["fp"] is None  # 失败话题保留旧 checkpoint
    assert record[TOPIC_B]["last_error"] != ""
    # 失败话题不会被伪装成“0 条新动态”：下次重试其候选
    client2 = _FakeTopicClient(data_by_topic={TOPIC_B: _original_payload(DID2)})
    _install_client(monkeypatch, client2)
    result2 = ds9.check_update()
    assert result2.activity_links == [f"https://www.bilibili.com/opus/{DID2}"]


# ---------- I：fingerprint 相同 → 不重复产出 ----------


def test_fingerprint_no_change_skips_duplicate_work(state_path: Path, monkeypatch) -> None:
    client = _FakeTopicClient(
        data_by_topic={TOPIC_A: _original_payload(DID1)},
        history_by_topic={TOPIC_A: {"code": 0, "data": {"cards": [_entry(DID2)]}}},
    )
    _install_client(monkeypatch, client)
    result = ds9.check_update()
    assert result.updated is True
    commit_source_checkpoint(result)
    fp1 = {t["topic_id"]: t for t in ds9.list_topics()}[TOPIC_A]["fp"]

    # 第二轮完全一致：无新产出、不再抓历史页
    client2 = _FakeTopicClient(
        data_by_topic={TOPIC_A: _original_payload(DID1)},
        history_by_topic={TOPIC_A: {"code": 0, "data": {"cards": [_entry(DID2)]}}},
    )
    _install_client(monkeypatch, client2)
    result2 = ds9.check_update()
    assert result2.updated is False
    assert result2.activity_links == []
    assert client2.history_calls == []

    record = {t["topic_id"]: t for t in ds9.list_topics()}
    assert record[TOPIC_A]["fp"] == fp1


# ---------- J：fingerprint 相同但存在 incomplete → 继续处理 ----------


def test_fingerprint_same_with_unresolved_still_retries(state_path: Path, monkeypatch) -> None:
    payload_outer = {"code": 0, "data": {"hot_list": [_entry(DID3, dtype=1)]}}
    client = _FakeTopicClient(data_by_topic={TOPIC_A: payload_outer})
    _install_client(monkeypatch, client)
    result = ds9.check_update()
    assert result.updated is True
    commit_source_checkpoint(result)

    # 第二轮内容相同（fp 不变）但上轮 incomplete 仍存在 → 必须重试该话题
    client2 = _FakeTopicClient(data_by_topic={TOPIC_A: payload_outer})
    _install_client(monkeypatch, client2)
    result2 = ds9.check_update()
    assert result2.updated is True  # 仍产出（允许重试 incomplete）
    assert client2.history_calls  # 有限历史补抓被重新触发

    # 第三轮 orig 补全后：canonical 进入候选且 incomplete 清空
    resolved = {"code": 0, "data": {"hot_list": [_entry(DID3, orig=DID1, dtype=1)]}}
    client3 = _FakeTopicClient(data_by_topic={TOPIC_A: resolved})
    _install_client(monkeypatch, client3)
    result3 = ds9.check_update()
    assert f"https://www.bilibili.com/opus/{DID1}" in result3.activity_links
    commit_source_checkpoint(result3)
    record = {t["topic_id"]: t for t in ds9.list_topics()}
    assert record[TOPIC_A]["incomplete"] == []


# ---------- K：pipeline 未完成 → 不推进 checkpoint ----------


def test_no_checkpoint_when_pipeline_not_committed(state_path: Path, monkeypatch) -> None:
    client = _FakeTopicClient(data_by_topic={TOPIC_A: _original_payload(DID1)})
    _install_client(monkeypatch, client)
    result = ds9.check_update()
    assert result.updated is True
    # 模拟公共 pipeline 失败：不调用 commit_source_checkpoint
    record = {t["topic_id"]: t for t in ds9.list_topics()}
    assert record[TOPIC_A]["fp"] is None

    # 下一轮同样内容仍会重新产出（不会因旧 fp 假象而漏掉）
    client2 = _FakeTopicClient(data_by_topic={TOPIC_A: _original_payload(DID1)})
    _install_client(monkeypatch, client2)
    result2 = ds9.check_update()
    assert result2.updated is True
    assert result2.activity_links == [f"https://www.bilibili.com/opus/{DID1}"]


# ---------- L：风控 0 retry + 停止后续话题 + 上抛 ----------


@pytest.mark.parametrize("error", [
    RuntimeError("API error -352: 风控校验失败"),
    RuntimeError("API error -509: 访问过于频繁"),
    httpx.HTTPStatusError(
        "429",
        request=httpx.Request("GET", "https://api.vc.bilibili.com/topic_svr/v1/topic_svr/topic_new"),
        response=httpx.Response(429, request=httpx.Request("GET", "https://api.vc.bilibili.com/topic_svr/v1/topic_svr/topic_new")),
    ),
])
def test_structured_risk_stops_all_topic_requests(state_path: Path, monkeypatch, error) -> None:
    client = _FakeTopicClient(
        data_by_topic={TOPIC_B: _original_payload(DID2)},
        errors_by_topic={TOPIC_A: error},
    )
    _install_client(monkeypatch, client)
    with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
        ds9.check_update()
    # 0 retry：每话题 topic_new 最多一次；风险话题后续请求 0
    assert client.calls.count(("new", TOPIC_A)) == 1
    assert client.calls.count(("new", TOPIC_B)) == 0  # 后续话题不再发起远程请求
    assert client.history_calls == []
    record = {t["topic_id"]: t for t in ds9.list_topics()}
    assert record[TOPIC_A]["fp"] is None  # 不推进


# ---------- M：统一 Source 注册 + 失败不回滚其它 DS ----------


def test_ds9_registered_in_unified_ds_handlers() -> None:
    import web.actions as actions

    assert any(source_id == "DS-9" for source_id, _, _ in actions.DS_HANDLERS)
    assert "DS-9" in actions.DS_HANDLER_BY_ID


def test_ds9_failure_does_not_rollback_other_ds(isolated_home: Path, monkeypatch) -> None:
    import web.actions as actions
    from src.sources.common import CheckResult as CR

    sequence: list[str] = []

    def ok_check(*, force=False, **kwargs) -> CR:
        sequence.append("check:DS-1")
        return CR(
            source_id="DS-1", updated=True, container_url="u", container_id="i",
            title="t", published_at=1, previous_container_url=None,
            activity_links=[], checked_at=1,
        )

    def ok_save(_result) -> Path:
        return Path("/tmp/out")

    def ds9_check(*, force=False, **kwargs) -> CR:
        sequence.append("check:DS-9")
        raise RuntimeError("话题接口返回异常（code=-1：普通失败）")

    def fake_pipeline(ds_results, *, workers=4, on_progress=None, **kwargs):
        sequence.append(f"pipeline:{ds_results[0].source_id}")
        from src.pipeline.refresh_all_pipeline import PipelineResult

        return PipelineResult(
            ok=True, pipeline_skipped=False, raw_link_count=1, new_link_count=0,
            classified_count=0, skipped_count=1, enriched_count=0,
            persisted_count=0, message="ok",
        )

    monkeypatch.setattr(actions, "run_refresh_all_pipeline", fake_pipeline)
    original = actions.DS_HANDLERS
    actions.DS_HANDLERS = [("DS-1", ok_check, ok_save), ("DS-9", ds9_check, ok_save)]
    try:
        payload = actions.run_action(
            "update_all_datasources", {}, on_progress=lambda **k: None, cancel_event=None
        )
    finally:
        actions.DS_HANDLERS = original
    assert payload["ok"] is True
    result = payload["result"]["update_all"]
    states = {entry["source_id"]: entry for entry in result["sources"]}
    assert states["DS-1"]["status"] == "success"  # 不回滚已成功源
    assert states["DS-9"]["status"] == "failed"
    assert sequence == ["check:DS-1", "pipeline:DS-1", "check:DS-9"]


# ---------- N：AutoScheduler refresh_all 走统一入口 ----------


def test_scheduler_refresh_batch_uses_unified_refresh_all(isolated_home: Path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    import web.actions as actions
    from web.auto_scheduler import AutoScheduler

    calls: list[str] = []
    original = actions.DS_HANDLERS
    actions.DS_HANDLERS = [("DS-9", ds9.check_update, ds9.save_result)]
    try:
        def fake(action, *, pipeline_index=None):
            calls.append(action)
            return {"skipped": False}

        scheduler = AutoScheduler(job_runner=MagicMock())
        scheduler._click_and_wait = fake
        scheduler._run_refresh_batch("2026-07-17-6")
    finally:
        actions.DS_HANDLERS = original
    assert calls[0] == "refresh_all"  # refresh_all 阶段第一入口
    assert "refresh_all" in calls
    assert "2026-07-17-6" in scheduler._done_refresh
    # 不新建第二套 scheduler：refresh_all 内 DS_HANDLERS 已含 DS-9
    assert any(source_id == "DS-9" for source_id, _, _ in actions.DS_HANDLERS)
