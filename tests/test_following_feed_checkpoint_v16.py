"""Following Feed checkpoint 一致性 V16。

P1：部分候选仍需要重试时不得推进 checkpoint（否则失败动态永久漏采）。
全 mock / 零真实 Bilibili：
A  全成功 → checkpoint 推进到本轮最新
B  部分 retryable（正文不可读取）→ 保留旧 checkpoint
B2 终态安全 skip（404/充电/非抽奖/详情提取失败）不阻塞推进
C  下一轮失败项再次进入 pipeline；完整成功后推进
D  重扫幂等安全（真实 pipeline DB 去重 + 真实 participation 防重链，不 mock 掉参与层）
E  pipeline 内 risk → 不推进、继续传播（不吞）
F  feed 请求整体失败 → checkpoint 不变
G  无新数据 → checkpoint 不变
H  Profile 隔离：A/B 各自 checkpoint
I  unreadable 与 terminal skip 不被混为一类
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src import following_feed as ff

_IDS = [f"20000000000000000{n:02d}" for n in range(1, 41)]
_A, _B, _C, _D, _E, _F = _IDS[:6]


class _FakeClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_json(self, url, params=None, *, referer=None, retries=0):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "account-a" / "following_feed_state.json"
    monkeypatch.setattr(ff, "_state_path", lambda: target)
    return target


def _payload(items=None, code=None):
    payload = {"code": code if code is not None else 0, "data": {"items": items}}
    if code is not None:
        payload["data"] = {}
    return payload


def _items(ids=None):
    return [{"id_str": dynamic_id} for dynamic_id in (ids or _IDS[:10])]


def _pipeline(**attrs) -> SimpleNamespace:
    defaults = {
        "ok": True,
        "pipeline_skipped": False,
        "new_link_count": 0,
        "persisted_count": 0,
        "skip_reasons": {},
    }
    defaults.update(attrs)
    return SimpleNamespace(**defaults)


def _seed_checkpoint(path: Path, dynamic_id: str) -> None:
    ff._write_state({"last_seen_dynamic_id": dynamic_id})


def _run_scan(
    client: _FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    pipeline: SimpleNamespace | None = None,
) -> tuple[dict, list[str]]:
    captured: dict = {"urls": []}

    def fake_pipeline(urls, *, workers=1, scan_started_at=None, on_progress=None):
        captured["urls"] = list(urls)
        if pipeline is not None:
            return pipeline
        return _pipeline(new_link_count=len(urls), persisted_count=1)

    monkeypatch.setattr(ff, "run_new_links_pipeline", fake_pipeline)
    result = ff.scan_following_feed(client_factory=lambda: client)
    return result, captured["urls"]


# ---------- A：全成功 → 推进到本轮最新 ----------


def test_all_success_advances_to_newest(state_path: Path, monkeypatch) -> None:
    _seed_checkpoint(state_path, _D)
    client = _FakeClient(payload=_payload(_items([_A, _B, _C, _D])))
    result, urls = _run_scan(
        client,
        monkeypatch,
        pipeline=_pipeline(new_link_count=3, persisted_count=2),
    )
    assert result["status"] == "success"
    assert urls == [ff.opus_link(i) for i in [_A, _B, _C]]
    assert ff.following_feed_info()["last_seen_dynamic_id"] == _A


# ---------- B：部分 retryable 失败 → 保留旧 checkpoint ----------


def test_retryable_unreadable_keeps_old_checkpoint(state_path: Path, monkeypatch) -> None:
    _seed_checkpoint(state_path, _D)
    client = _FakeClient(payload=_payload(_items([_A, _B, _C, _D])))
    result, _ = _run_scan(
        client,
        monkeypatch,
        pipeline=_pipeline(
            new_link_count=3,
            persisted_count=2,
            skip_reasons={"正文不可读取": 1},
        ),
    )
    assert result["status"] == "success"
    # 关键断言：不得写为 A，必须仍是进入本轮前的 D。
    assert ff.following_feed_info()["last_seen_dynamic_id"] == _D


def test_mixed_terminal_skips_still_advance(state_path: Path, monkeypatch) -> None:
    """404/充电/非抽奖/详情提取失败等终态安全 skip 不阻塞 checkpoint 推进。"""
    _seed_checkpoint(state_path, _D)
    client = _FakeClient(payload=_payload(_items([_A, _B, _C, _D])))
    result, _ = _run_scan(
        client,
        monkeypatch,
        pipeline=_pipeline(
            new_link_count=3,
            persisted_count=1,
            skip_reasons={
                "链接失效": 1,
                "充电抽奖": 1,
                "非抽奖活动": 1,
                "详情提取失败": 1,
            },
        ),
    )
    assert result["status"] == "success"
    assert ff.following_feed_info()["last_seen_dynamic_id"] == _A


# ---------- C：下一轮重试 + 完整成功后推进 ----------


def test_failed_candidate_reenters_pipeline_next_round_then_advances(
    state_path: Path, monkeypatch,
) -> None:
    _seed_checkpoint(state_path, _D)
    client = _FakeClient(payload=_payload(_items([_A, _B, _C, _D])))

    # 第一轮：B 正文不可读取 → 保留 D
    result1, urls1 = _run_scan(
        client,
        monkeypatch,
        pipeline=_pipeline(skip_reasons={"正文不可读取": 1}),
    )
    assert result1["status"] == "success"
    assert urls1 == [ff.opus_link(i) for i in [_A, _B, _C]]
    assert ff.following_feed_info()["last_seen_dynamic_id"] == _D

    # 第二轮：同样的窗口再次扫描 → B 必须再次进入 pipeline
    result2, urls2 = _run_scan(
        client,
        monkeypatch,
        pipeline=_pipeline(new_link_count=3, persisted_count=3),
    )
    assert result2["status"] == "success"
    assert urls2 == [ff.opus_link(i) for i in [_A, _B, _C]]
    # 完整成功后 checkpoint 推进到本轮最新
    assert ff.following_feed_info()["last_seen_dynamic_id"] == _A


# ---------- D：重扫幂等安全（真实链路） ----------


def test_rerun_pipeline_db_dedupe_never_reclassifies_persisted_items(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已成功入库的 A/C 在窗口重扫时被 DB 去重挡在 classify 之前（真实 pipeline 去重路径）。"""
    from src.pipeline import refresh_all_pipeline as pl

    monkeypatch.setattr(pl, "known_activity_ids", lambda: {_A, _C})
    monkeypatch.setattr(pl, "load_participations", lambda: {})

    class _NoopClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(pl, "BilibiliClient", _NoopClient)
    classified: list[str] = []

    def classify_one(client, dynamic_id):
        classified.append(dynamic_id)
        raise RuntimeError(f"无法获取动态正文: {dynamic_id}")

    monkeypatch.setattr(pl, "classify_new_link", classify_one)
    result = pl.run_new_links_pipeline(
        [ff.opus_link(_A), ff.opus_link(_B), ff.opus_link(_C)],
        workers=1,
    )
    # A/C 被 existing 去重：只有 B 进入 classify
    assert classified == [_B]
    assert result.existing_count == 2
    assert result.skip_reasons == {"正文不可读取": 1}


def test_repersisted_duplicate_cannot_trigger_second_participation_mutation(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使某成功项重新出现在扫描窗口并被（假设）重复入库，真实的 participation 防重链
    也会在任何客户端构造/远程 mutation 之前把它挡下：已参加 + guard 三态全验证。"""
    from src import participation as flow
    from src import lottery_actions as actions
    from src.participation_guard import (
        mark_repost_suspected,
        mark_repost_unknown,
        record_pending,
    )
    from src.participation_store import get_participation, set_participation_unlocked

    DID = _A
    UID = "101"
    monkeypatch.setattr(flow, "require_login", lambda: ("test-csrf", int(UID)))
    monkeypatch.setattr(actions, "require_login", lambda: ("test-csrf", int(UID)))

    def no_client(*args, **kwargs):
        raise AssertionError("本地防重命中时必须连客户端都不构造")

    monkeypatch.setattr(flow, "BilibiliClient", no_client)

    def participate():
        return flow.participate_activity(
            None,
            dynamic_id=DID,
            lottery_type="转发抽奖",
            action_text="test",
            preflight=True,
            persist=False,
        )

    # 1) 已参加（前一轮成功 → DB 记录已参加）
    set_participation_unlocked(DID, "已参加", uid=UID)
    assert participate().to_dict()["skip_reason"] == "already_joined"
    assert get_participation(DID, uid=UID).user_status == "已参加"

    # 2) guard：confirmed 由流程写入（历史成功转发）
    from src.participation_guard import confirm_repost

    confirm_repost(UID, DID)
    for _ in range(2):
        assert participate().to_dict()["skip_reason"] == "already_joined"

    # 3) guard：pending / unknown / suspected 三态不 resend
    for status in ("pending", "unknown", "suspected"):
        DID2 = {"pending": _B, "unknown": _C, "suspected": _D}[status]
        if status == "suspected":
            mark_repost_suspected(UID, DID2)
        else:
            record_pending(UID, DID2)
            if status == "unknown":
                mark_repost_unknown(UID, DID2)
        result = flow.participate_activity(
            None,
            dynamic_id=DID2,
            lottery_type="转发抽奖",
            action_text="test",
            preflight=True,
            persist=False,
        )
        assert result.to_dict()["skip_reason"] == f"repost_{status}"
        assert get_participation(DID2, uid=UID) is None


# ---------- E：pipeline 内 risk 不推进、不吞 ----------


def test_pipeline_risk_propagates_without_checkpoint_write(
    state_path: Path, monkeypatch,
) -> None:
    _seed_checkpoint(state_path, _D)
    client = _FakeClient(payload=_payload(_items([_A, _B, _C, _D])))

    def risky_pipeline(urls, *, workers=1, scan_started_at=None, on_progress=None):
        raise RuntimeError("API error -509: 访问过于频繁")

    monkeypatch.setattr(ff, "run_new_links_pipeline", risky_pipeline)
    with pytest.raises(RuntimeError, match="-509"):
        ff.scan_following_feed(client_factory=lambda: client)
    info = ff.following_feed_info()
    assert info["last_seen_dynamic_id"] == _D
    assert info["last_status"] == "waiting"  # 未写任何成功状态
    assert info["last_scan_at"] is None


# ---------- F：feed 请求失败 → 不变 ----------


def test_feed_request_failure_never_changes_checkpoint(state_path: Path, monkeypatch) -> None:
    _seed_checkpoint(state_path, _D)
    client = _FakeClient(error=RuntimeError("网络请求失败: connection reset"))
    with pytest.raises(RuntimeError, match="网络请求失败"):
        ff.scan_following_feed(client_factory=lambda: client)
    info = ff.following_feed_info()
    assert info["last_seen_dynamic_id"] == _D
    assert info["last_scan_at"] is None


def test_feed_business_error_response_keeps_checkpoint(state_path: Path, monkeypatch) -> None:
    _seed_checkpoint(state_path, _D)
    client = _FakeClient(payload=_payload(items=[], code=-101))
    with pytest.raises(RuntimeError, match="code=-101"):
        ff.scan_following_feed(client_factory=lambda: client)
    assert ff.following_feed_info()["last_seen_dynamic_id"] == _D


# ---------- G：无新数据 → checkpoint 不变 ----------


def test_no_new_data_keeps_checkpoint(state_path: Path, monkeypatch) -> None:
    _seed_checkpoint(state_path, _A)
    # 页面上最老只到 checkpoint 本身（或更早）：没有任何新候选。
    client = _FakeClient(payload=_payload(_items([_A, _B, _C])))
    result, urls = _run_scan(
        client,
        monkeypatch,
        pipeline=_pipeline(new_link_count=0, persisted_count=0, pipeline_skipped=True),
    )
    assert result["status"] == "success"
    assert urls == []
    assert ff.following_feed_info()["last_seen_dynamic_id"] == _A


# ---------- H：Profile 隔离 ----------


def test_checkpoint_failure_does_not_leak_across_profiles(tmp_path: Path, monkeypatch) -> None:
    profile_a = tmp_path / "profile-a" / "following_feed_state.json"
    profile_b = tmp_path / "profile-b" / "following_feed_state.json"

    # Profile A：本轮 B 失败 → 保留 D
    monkeypatch.setattr(ff, "_state_path", lambda: profile_a)
    ff._write_state({"last_seen_dynamic_id": _D})
    client_a = _FakeClient(payload=_payload(_items([_A, _B, _C, _D])))

    def pipeline_a(urls, *, workers=1, scan_started_at=None, on_progress=None):
        return _pipeline(skip_reasons={"正文不可读取": 1})

    monkeypatch.setattr(ff, "run_new_links_pipeline", pipeline_a)
    ff.scan_following_feed(client_factory=lambda: client_a)
    assert ff.following_feed_info()["last_seen_dynamic_id"] == _D

    # Profile B：全新状态 + 全成功 → 推进到 _A；不读 A 的 checkpoint
    monkeypatch.setattr(ff, "_state_path", lambda: profile_b)
    client_b = _FakeClient(payload=_payload(_items([_A, _B, _C])))

    def pipeline_b(urls, *, workers=1, scan_started_at=None, on_progress=None):
        return _pipeline(new_link_count=len(urls), persisted_count=1)

    monkeypatch.setattr(ff, "run_new_links_pipeline", pipeline_b)
    ff.scan_following_feed(client_factory=lambda: client_b)
    assert ff.following_feed_info()["last_seen_dynamic_id"] == _A
    assert ff.following_feed_info()["last_status"] == "success"

    # A 未被 B 的成功改变
    monkeypatch.setattr(ff, "_state_path", lambda: profile_a)
    assert ff.following_feed_info()["last_seen_dynamic_id"] == _D


# ---------- I：unreadable（retryable）与 terminal skip 语义分离 ----------


def test_only_unreadable_counts_as_blocking_for_checkpoint(
    state_path: Path, monkeypatch,
) -> None:
    """正文不可读取 = 仍需重试；404 等 = 终态安全。同轮混合时只要有 unreadable 就保守不推进。"""
    _seed_checkpoint(state_path, _D)
    client = _FakeClient(payload=_payload(_items([_A, _B, _C, _D])))
    _, _ = _run_scan(
        client,
        monkeypatch,
        pipeline=_pipeline(
            skip_reasons={
                "链接失效": 2,  # 终态
                "非抽奖活动": 1,
                "正文不可读取": 1,  # 仍需重试
            },
        ),
    )
    assert ff.following_feed_info()["last_seen_dynamic_id"] == _D
