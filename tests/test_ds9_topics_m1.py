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

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.sources import ds9_topics as ds9
from src.sources.common import CheckResult, commit_source_checkpoint

TOPIC_A = "1261901"
TOPIC_B = "1055656"
TOPIC_C = "47801"
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
    """可编程 fake：每话题现代 feed/topic 数据 + 可选异常/offset 页。"""

    def __init__(self, *, data_by_topic=None, errors_by_topic=None, history_by_topic=None):
        self.data_by_topic = dict(data_by_topic or {})
        self.errors_by_topic = dict(errors_by_topic or {})
        self.history_by_topic = dict(history_by_topic or {})
        self.calls: list[tuple[str, str, str]] = []
        self.history_calls: list[tuple[str, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _cards(self, ids):
        return {"code": 0, "data": {"hot_list": [{"desc": {"dynamic_id": did}} for did in ids]}}

    def get_topic_feed(
        self,
        topic_id: str,
        *,
        offset: str = "",
        page_size: int = 20,
        sort_by: int = 3,
        source: str = "Web",
    ) -> dict:
        self.calls.append(("feed", topic_id, offset))
        error = self.errors_by_topic.get(f"history:{topic_id}" if offset else topic_id)
        if error is not None:
            raise error
        if offset:
            self.history_calls.append((topic_id, offset))
            payload = self.history_by_topic.get(topic_id, {"code": 0, "data": {}})
        else:
            payload = self.data_by_topic.get(topic_id, {"code": 0, "data": {}})
        return payload


def _original_payload(outer: str) -> dict:
    return {"code": 0, "data": {"hot_list": [_entry(outer)]}}


def _modern_payload(entries: list[dict], *, offset: str = "", has_more: bool = False) -> dict:
    return {
        "code": 0,
        "message": "0",
        "data": {
            "related_topics": [],
            "topic_card_list": {
                "has_more": has_more,
                "offset": offset,
                "items": [
                    {"dynamic_card_item": entry, "topic_type": "DYNAMIC"}
                    for entry in entries
                ],
            },
        },
    }


def _modern_entry(outer: str, *, author_mid: str, orig: str | None = None) -> dict:
    entry = {
        "id_str": outer,
        "type": "DYNAMIC_TYPE_FORWARD" if orig else "DYNAMIC_TYPE_DRAW",
        "modules": {"module_author": {"mid": int(author_mid)}},
        "visible": True,
    }
    if orig:
        entry["orig"] = {
            "id_str": orig,
            "type": "DYNAMIC_TYPE_DRAW",
            "modules": {"module_author": {"mid": 99999}},
            "visible": True,
        }
    return entry


def _install_client(monkeypatch: pytest.MonkeyPatch, client: _FakeTopicClient) -> None:
    monkeypatch.setattr(ds9, "BilibiliClient", lambda: client)


# ---------- A：默认话题 + 独立状态 ----------


def test_default_three_builtin_topics_with_independent_state(state_path: Path) -> None:
    topics = ds9.list_topics()
    assert [t["topic_id"] for t in topics] == [TOPIC_A, TOPIC_B, TOPIC_C]
    assert [t["name"] for t in topics] == ["抽奖！！！", "转发动态抽奖！", "抽奖号的日常"]
    assert all(t["kind"] == "built-in" for t in topics)

    updated = ds9.set_topic_enabled(TOPIC_A, False)
    assert updated["enabled"] is False
    after = {t["topic_id"]: t for t in ds9.list_topics()}
    assert after[TOPIC_B]["enabled"] is True  # 彼此独立
    # built-in 不可删除，只可暂停
    with pytest.raises(ValueError, match="内置话题不可删除"):
        ds9.remove_topic(TOPIC_A)


def test_legacy_profile_gains_third_builtin_without_overwriting_state(state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "topics": [
                    {
                        "topic_id": TOPIC_A,
                        "name": "抽奖！！！",
                        "kind": "built-in",
                        "enabled": False,
                        "fp": "old-a",
                        "incomplete": ["outer-a"],
                        "last_success_at": 11,
                        "last_scan_at": 12,
                        "last_found": 3,
                        "last_error": "old error",
                    },
                    {
                        "topic_id": TOPIC_B,
                        "name": "转发动态抽奖！",
                        "kind": "built-in",
                        "enabled": True,
                        "fp": "old-b",
                        "incomplete": [],
                        "last_success_at": 21,
                        "last_scan_at": 22,
                        "last_found": 4,
                        "last_error": "",
                    },
                    {
                        "topic_id": "123456",
                        "name": "已有自定义",
                        "kind": "custom",
                        "enabled": False,
                        "fp": "custom-fp",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    topics = ds9.list_topics()
    assert [item["topic_id"] for item in topics] == [TOPIC_A, TOPIC_B, TOPIC_C, "123456"]
    by_id = {item["topic_id"]: item for item in topics}
    assert by_id[TOPIC_A]["enabled"] is False
    assert by_id[TOPIC_A]["fp"] == "old-a"
    assert by_id[TOPIC_A]["last_scan_at"] == 12
    assert by_id[TOPIC_C]["name"] == "抽奖号的日常"
    assert by_id[TOPIC_C]["kind"] == "built-in"
    assert by_id["123456"]["name"] == "已有自定义"


# ---------- B：Profile 隔离 ----------


def test_profile_isolation_for_config_and_checkpoint(tmp_path: Path, monkeypatch) -> None:
    a = tmp_path / "account-a" / "ds9_topics.json"
    b = tmp_path / "account-b" / "ds9_topics.json"

    monkeypatch.setattr(ds9, "_state_path", lambda: a)
    ds9.add_topic("A 自定", "123456")
    assert any(t["topic_id"] == "123456" for t in ds9.list_topics())
    ds9.set_topic_enabled(TOPIC_C, False)

    monkeypatch.setattr(ds9, "_state_path", lambda: b)
    assert not any(t["topic_id"] == "123456" for t in ds9.list_topics())
    assert {t["topic_id"]: t for t in ds9.list_topics()}[TOPIC_C]["enabled"] is True

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
    assert {t["topic_id"]: t for t in ds9.list_topics()}[TOPIC_C]["enabled"] is False


# ---------- C：URL 添加 / 去重 / 校验 ----------


def test_add_topic_from_url_dedup_and_validation(state_path: Path) -> None:
    topic = ds9.add_topic("我的话题", URL_TOPIC)
    assert topic["topic_id"] == "123456"
    assert topic["kind"] == "custom"
    assert topic["enabled"] is True

    with pytest.raises(ValueError, match="已存在"):
        ds9.add_topic("重复", "123456")
    with pytest.raises(ValueError, match="已存在"):
        ds9.add_topic("重复内置", TOPIC_C)

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
    assert result.activity_source_context[DID1] == {
        "topic_id": TOPIC_A,
        "topic_name": "抽奖！！！",
    }
    # 最终活动库去重由公共 run_new_links_pipeline 的 known_activity_ids 兜底。


def test_all_three_builtin_topics_carry_source_context(state_path: Path, monkeypatch) -> None:
    ids = (DID1, DID2, "1000000000000000003")
    client = _FakeTopicClient(
        data_by_topic={
            TOPIC_A: _original_payload(ids[0]),
            TOPIC_B: _original_payload(ids[1]),
            TOPIC_C: _original_payload(ids[2]),
        }
    )
    _install_client(monkeypatch, client)
    result = ds9.check_update()
    assert {item["topic_id"] for item in result.activity_source_context.values()} == {
        TOPIC_A,
        TOPIC_B,
        TOPIC_C,
    }
    assert result.activity_source_context[ids[2]]["topic_name"] == "抽奖号的日常"


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


def test_third_builtin_partial_failure_does_not_affect_first_two(
    state_path: Path, monkeypatch
) -> None:
    client = _FakeTopicClient(
        data_by_topic={
            TOPIC_A: _original_payload(DID1),
            TOPIC_B: _original_payload(DID2),
        },
        errors_by_topic={TOPIC_C: RuntimeError("网络请求失败: connect timeout")},
    )
    _install_client(monkeypatch, client)
    result = ds9.check_update()
    assert result.activity_links == [
        f"https://www.bilibili.com/opus/{DID1}",
        f"https://www.bilibili.com/opus/{DID2}",
    ]
    assert result.ds9_summary["partial"] is True
    assert result.ds9_summary["failed_topics"] == [TOPIC_C]

    commit_source_checkpoint(result)
    record = {t["topic_id"]: t for t in ds9.list_topics()}
    assert record[TOPIC_A]["fp"] is not None
    assert record[TOPIC_B]["fp"] is not None
    assert record[TOPIC_C]["fp"] is None


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
    payload_outer = _modern_payload(
        [_modern_entry(DID3, author_mid="10001") | {"type": "DYNAMIC_TYPE_FORWARD"}],
        offset="page-2",
        has_more=True,
    )
    empty_history = _modern_payload([], has_more=False)
    client = _FakeTopicClient(
        data_by_topic={TOPIC_A: payload_outer},
        history_by_topic={TOPIC_A: empty_history},
    )
    _install_client(monkeypatch, client)
    result = ds9.check_update()
    assert result.updated is True
    commit_source_checkpoint(result)

    # 第二轮内容相同（fp 不变）但上轮 incomplete 仍存在 → 必须重试该话题
    client2 = _FakeTopicClient(
        data_by_topic={TOPIC_A: payload_outer},
        history_by_topic={TOPIC_A: empty_history},
    )
    _install_client(monkeypatch, client2)
    result2 = ds9.check_update()
    assert result2.updated is True  # 仍产出（允许重试 incomplete）
    assert client2.history_calls  # 有限历史补抓被重新触发

    # 第三轮 orig 补全后：canonical 进入候选且 incomplete 清空
    resolved = _modern_payload([_modern_entry(DID3, author_mid="10001", orig=DID1)])
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
    assert client.calls.count(("feed", TOPIC_A, "")) == 1
    assert client.calls.count(("feed", TOPIC_B, "")) == 0  # 后续话题不再发起远程请求
    assert client.history_calls == []
    record = {t["topic_id"]: t for t in ds9.list_topics()}
    assert record[TOPIC_A]["fp"] is None  # 不推进


# ---------- M：统一 Source 注册 + 失败不回滚其它 DS ----------


def test_ds9_registered_in_unified_ds_handlers() -> None:
    import web.actions as actions

    assert any(source_id == "DS-9" for source_id, _, _ in actions.DS_HANDLERS)
    assert "DS-9" in actions.DS_HANDLER_BY_ID


def test_modern_topic_fixture_parses_multiple_authors_original_and_repost() -> None:
    fixture = Path(__file__).parent / "fixtures" / "ds9_topic_feed_modern.json"
    payload = __import__("json").loads(fixture.read_text(encoding="utf-8"))
    entries, offset, has_more = ds9._payload_topic_page(payload)

    assert len(entries) == 3
    assert offset == "574748481266496"
    assert has_more is True
    authors = {str(item["modules"]["module_author"]["mid"]) for item in entries}
    assert authors == {"10001", "20002", "30003"}  # 同一 topic 下多个不同 UP 主
    resolved, incomplete, outers = ds9.normalize_topic_entries(entries)
    assert resolved == [DID1, DID2, DID4]
    assert incomplete == []
    assert outers == [DID1, DID3, DID4]


def test_modern_topic_offset_pagination_is_bounded(state_path: Path, monkeypatch) -> None:
    first = _modern_payload(
        [_modern_entry(DID1, author_mid="10001")],
        offset="page-2",
        has_more=True,
    )
    second = _modern_payload(
        [_modern_entry(DID3, author_mid="20002", orig=DID2)],
        offset="",
        has_more=False,
    )
    client = _FakeTopicClient(
        data_by_topic={TOPIC_A: first},
        history_by_topic={TOPIC_A: second},
    )
    _install_client(monkeypatch, client)

    result = ds9.check_update()

    assert result.activity_links == [
        f"https://www.bilibili.com/opus/{DID1}",
        f"https://www.bilibili.com/opus/{DID2}",
    ]
    assert client.history_calls == [(TOPIC_A, "page-2")]


@pytest.mark.parametrize("code", [-352, -509, 429])
def test_raw_modern_payload_risk_stops_without_second_topic(
    state_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
) -> None:
    client = _FakeTopicClient(
        data_by_topic={
            TOPIC_A: {"code": code, "message": "risk", "data": {}},
            TOPIC_B: _modern_payload([_modern_entry(DID2, author_mid="20002")]),
        }
    )
    _install_client(monkeypatch, client)

    with pytest.raises(RuntimeError, match=f"API error {code}"):
        ds9.check_update()

    assert client.calls == [("feed", TOPIC_A, "")]


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
