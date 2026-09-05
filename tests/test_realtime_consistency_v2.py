"""任务实时状态一致性 V2（后端部分）：EventHub overflow 顺序、SSE 快照/订阅窗口、
按 job_id 查询接口、同 action 多 Job 身份隔离。全 mock / 零真实 Bilibili。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.app import app
from web.event_hub import EventHub
from web.sse import iter_sse_frames
from src.job_store import finish_job, insert_running_job


def _collect(gen, *, max_frames: int = 80) -> list[bytes]:
    frames: list[bytes] = []
    for _ in range(max_frames):
        try:
            frame = next(gen)
        except StopIteration:
            break
        frames.append(frame)
        if b"job.terminal" in frame:
            break
    gen.close()
    return frames


def test_eventhub_overflow_keeps_retained_seq_strictly_increasing() -> None:
    hub = EventHub(queue_maxsize=8)
    sub = hub.subscribe()
    # 大量普通 log/progress 洪水 + 一条 terminal + 最新 auto.log
    hub.publish("job.created", {"id": 1, "state": "running", "action": "refresh_all"})
    for i in range(300):
        hub.publish("job.progress", {"id": 1, "step": i, "total": 300, "message": f"p{i}"})
    hub.publish("job.terminal", {"id": 1, "state": "success", "action": "refresh_all", "message": "done"})
    for i in range(300):
        hub.publish("job.log", {"id": 1, "chunk": f"log-{i}"})
    hub.publish("auto.log", {"level": "info", "message": "auto-now"})

    drained: list = []
    deadline = time.time() + 1.0
    while time.time() < deadline:
        try:
            item = sub.queue.get_nowait()
        except Exception:
            if drained:
                break
            continue
        if item is None:
            break
        drained.append(item)

    seqs = [item.seq for item in drained]
    # 保留事件 seq 严格递增（允许丢弃，不允许重排/倒灌）。
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert seqs[-1] == max(seqs)
    # terminal 是 protected：洪水后仍应保留。
    terminal = [item for item in drained if item.event == "job.terminal"]
    assert terminal and terminal[0].data["state"] == "success"
    # 普通 log/progress 未全部保留但新 auto.log 仍在。
    assert any(item.event == "auto.log" for item in drained)
    assert len(drained) <= 8
    hub.unsubscribe(sub)


def test_eventhub_latest_seq_is_process_monotonic() -> None:
    hub = EventHub()
    assert hub.latest_seq() == 0
    first = hub.publish("job.progress", {"id": 1, "step": 1})
    second = hub.publish("job.terminal", {"id": 1, "state": "success"})
    assert second > first
    assert hub.latest_seq() == second


def test_sse_subscribes_before_snapshot_and_keeps_monotonic_stream() -> None:
    """生产实现：先订阅再读快照。订阅后发布的事件全部到达，且 seq 单调。"""
    hub = EventHub(queue_maxsize=64)
    gen = iter_sse_frames(
        hub=hub,
        job_snapshot=lambda: {"id": 1, "state": "running", "action": "refresh_all",
                              "message": "running", "log": "", "progress_step": 0},
    )
    snapshot_frame = next(gen)
    assert b"job.snapshot" in snapshot_frame
    # 快照之后发布的事件（模拟订阅建立后立即发生的事件）不能丢：
    hub.publish("job.progress", {"id": 1, "step": 1, "total": 2, "message": "m"})
    hub.publish("job.log", {"id": 1, "chunk": "line-1"})
    hub.publish(
        "job.terminal",
        {"id": 1, "state": "success", "action": "refresh_all", "message": "done", "log": "line-1"},
    )
    frames = _collect(gen)
    assert any(b"job.terminal" in f for f in frames)

    import json

    seqs: list[int] = []
    for frame in [snapshot_frame, *frames]:
        payload = frame.split(b"data: ", 1)[1].split(b"\n", 1)[0]
        seq = json.loads(payload).get("seq")
        if seq is not None:
            seqs.append(int(seq))
    assert seqs == sorted(seqs)


def test_sse_snapshot_carries_watermark_for_frontend_gate() -> None:
    hub = EventHub()
    # 模拟已有历史事件（seq=1..3），新连接快照读取时 watermark 应为 3。
    hub.publish("job.progress", {"id": 9, "step": 1, "total": 3, "message": "a"})
    hub.publish("job.progress", {"id": 9, "step": 2, "total": 3, "message": "b"})
    hub.publish("job.progress", {"id": 9, "step": 3, "total": 3, "message": "c"})

    gen = iter_sse_frames(
        hub=hub,
        job_snapshot=lambda: {"id": 9, "state": "running", "action": "refresh_all",
                              "message": "c", "log": "", "progress_step": 3},
    )
    frame = next(gen)
    import json

    payload_text = frame.split(b"data: ", 1)[1].split(b"\n", 1)[0]
    data = json.loads(payload_text)
    assert data["seq"] == 3
    assert data["snapshot_seq"] == 3
    gen.close()


def test_job_by_id_endpoint_keeps_identity_per_job(isolated_home: Path) -> None:
    _ = isolated_home
    now = int(time.time())
    first = insert_running_job(
        action="refresh_status",
        label="刷新任务状态",
        source="ui",
        params=None,
        message="start",
        now=now,
    )
    finish_job(
        first,
        state="success",
        message="done-1",
        log_summary="ok-1",
        finished_at=now + 1,
    )
    second = insert_running_job(
        action="refresh_status",
        label="刷新任务状态",
        source="ui",
        params=None,
        message="start",
        now=now + 2,
    )
    finish_job(
        second,
        state="success",
        message="done-2",
        log_summary="ok-2",
        finished_at=now + 3,
    )

    client = TestClient(app)
    resp_first = client.get(f"/api/jobs/{first}")
    assert resp_first.status_code == 200
    body_first = resp_first.json()
    assert body_first["id"] == first
    assert body_first["state"] == "success"
    assert body_first["message"] == "done-1"

    resp_second = client.get(f"/api/jobs/{second}")
    assert resp_second.status_code == 200
    body_second = resp_second.json()
    assert body_second["id"] == second
    assert body_second["state"] == "success"
    assert body_second["message"] == "done-2"
    # 两个同 action Job 身份不串。
    assert body_first["id"] != body_second["id"]

    missing = client.get("/api/jobs/999999999")
    assert missing.status_code == 404
