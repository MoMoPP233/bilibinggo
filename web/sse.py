"""SSE 编码与 StreamingResponse 生成器。

连接一致性（realtime-consistency-v2）：
- 先 subscribe，再读取快照（由调用方以 callable 注入，读取发生在订阅之后），
  关闭“读快照 → 订阅”之间的事件丢失窗口；
- 每条快照附带自己的 watermark（读取该快照前的最新全局 seq）：
  订阅队列里早于 watermark 的旧事件会被前端按 seq 判定为过期并忽略，
  不会在快照之后倒灌覆盖快照；
- 事件帧始终携带 HubEvent 的全局 seq（event_id + data.seq）。
"""

from __future__ import annotations

import json
import queue
import time
from collections.abc import Callable, Iterator
from typing import Any

from starlette.responses import StreamingResponse

from src.restart_control import restart_control
from web.event_hub import EventHub, HubEvent, Subscriber, event_hub

HEARTBEAT_INTERVAL_SEC = 15.0
QUEUE_GET_TIMEOUT_SEC = 1.0

SnapshotSource = dict[str, Any] | Callable[[], dict[str, Any] | None] | None


def format_sse(event: str, data: dict[str, Any], *, event_id: int | None = None) -> bytes:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    lines.append(f"data: {payload}")
    lines.append("")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _trim_auto_logs(status: dict[str, Any], *, limit: int = 30) -> dict[str, Any]:
    out = dict(status)
    logs = out.get("logs")
    if isinstance(logs, list) and len(logs) > limit:
        out["logs"] = logs[-limit:]
    return out


def _resolve(source: SnapshotSource) -> dict[str, Any] | None:
    if source is None:
        return None
    return source() if callable(source) else source


def _stamp_snapshot(frame: dict[str, Any], watermark: int) -> tuple[dict[str, Any], int]:
    """快照帧打上 watermark（读取该快照前的最新全局 seq）。

    前端约定：任何 seq <= watermark 的后续事件都是快照已包含的旧事件，应忽略。
    """
    data = dict(frame)
    data["seq"] = watermark
    data["snapshot_seq"] = watermark
    return data, watermark


def iter_sse_frames(
    *,
    hub: EventHub | None = None,
    job_snapshot: SnapshotSource = None,
    auto_snapshot: SnapshotSource = None,
    heartbeat_interval_sec: float = HEARTBEAT_INTERVAL_SEC,
) -> Iterator[bytes]:
    bus = hub or event_hub
    # 关键顺序：先订阅，再读取快照，避免订阅建立前的事件丢失。
    # watermark 在读取快照前一刻捕获：快照必然已包含 <= watermark 的事件；
    # watermark 之后发布的事件带更大 seq，会在快照后正常送达（幂等安全）。
    sub: Subscriber = bus.subscribe()
    job_watermark = bus.latest_seq()
    job_frame = _resolve(job_snapshot)
    auto_watermark = bus.latest_seq()
    auto_frame = _resolve(auto_snapshot)
    last_heartbeat = time.monotonic()
    try:
        if job_frame is not None:
            data, watermark = _stamp_snapshot(job_frame, job_watermark)
            yield format_sse("job.snapshot", data, event_id=watermark)
        if auto_frame is not None:
            data, watermark = _stamp_snapshot(_trim_auto_logs(auto_frame), auto_watermark)
            yield format_sse("auto.snapshot", data, event_id=watermark)
        while not restart_control.is_restart_pending():
            if sub.closed:
                break
            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_interval_sec:
                hb = {"ts": int(time.time())}
                yield format_sse("heartbeat", hb)
                last_heartbeat = now
            try:
                item: HubEvent | None = sub.queue.get(timeout=QUEUE_GET_TIMEOUT_SEC)
            except queue.Empty:
                continue
            if item is None or sub.closed:
                break
            yield format_sse(item.event, item.data, event_id=item.seq)
    finally:
        bus.unsubscribe(sub)


def sse_response(
    *,
    job_snapshot: SnapshotSource = None,
    auto_snapshot: SnapshotSource = None,
    hub: EventHub | None = None,
) -> StreamingResponse:
    generator = iter_sse_frames(
        hub=hub,
        job_snapshot=job_snapshot,
        auto_snapshot=auto_snapshot,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
