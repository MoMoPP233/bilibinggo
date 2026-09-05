"""Job helpers: start actions and wait until terminal (G2)."""

from __future__ import annotations

import asyncio
from typing import Any

from binggo_mcp.client import BinggoApiError, BinggoClient, is_terminal_job_state

POLL_INTERVAL_SEC = 1.0
JOB_WAIT_TIMEOUT_SEC = 3600.0
QR_READY_TIMEOUT_SEC = 90.0


async def get_current_job(client: BinggoClient) -> dict[str, Any]:
    data = await client.get_json("/api/jobs/current")
    return data if isinstance(data, dict) else {}


async def wait_until_idle_or_terminal(
    client: BinggoClient,
    *,
    timeout_sec: float = JOB_WAIT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """If a job is already running (e.g. from UI), wait until it finishes."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_sec
    while True:
        job = await get_current_job(client)
        state = str(job.get("state") or "idle")
        if state != "running":
            return job
        if loop.time() >= deadline:
            raise BinggoApiError(
                f"等待已有任务结束超时（当前 action={job.get('action') or '—'}）。"
            )
        await asyncio.sleep(POLL_INTERVAL_SEC)


async def start_job(
    client: BinggoClient,
    action: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"action": action}
    if params:
        body["params"] = params
    data = await client.post_json("/api/jobs", json_body=body)
    return data if isinstance(data, dict) else {"ok": True, "job": data}


async def wait_job_terminal(
    client: BinggoClient,
    *,
    expect_job_id: int | None = None,
    expect_action: str | None = None,
    timeout_sec: float = JOB_WAIT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """等待指定任务到达终态。

    带 expect_job_id 时通过 /api/jobs/{job_id} 按精确身份轮询：
    同 action 的先后两个 Job 绝不会互相串结果（A 等待期间 B 结束也不影响 A）。
    expect_job_id 为空时退化为按当前槽位等待（兼容调用方/旧行为）。
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_sec
    last: dict[str, Any] = {}

    async def fetch_once() -> dict[str, Any]:
        if expect_job_id is not None:
            job = await client.get_json(f"/api/jobs/{expect_job_id}")
            return job if isinstance(job, dict) else {}
        return await get_current_job(client)

    while True:
        last = await fetch_once()
        job_id = last.get("id")
        state = str(last.get("state") or "idle")
        action = str(last.get("action") or "")
        if expect_job_id is not None:
            if job_id is None or int(job_id) != expect_job_id:
                # 身份不匹配：继续按原 job_id 轮询，绝不拿别的任务当结果。
                last = {}
            elif is_terminal_job_state(state):
                return last
        else:
            if state == "idle" and not action:
                return last
            if is_terminal_job_state(state):
                if expect_action and action and action != expect_action:
                    # Finished some other job; keep waiting for ours if still needed.
                    pass
                else:
                    return last
        if loop.time() >= deadline:
            raise BinggoApiError(
                f"等待任务结束超时（job_id={expect_job_id or '—'}, "
                f"action={expect_action or action or '—'}, state={state}）。"
            )
        await asyncio.sleep(POLL_INTERVAL_SEC)


async def run_job_to_terminal(
    client: BinggoClient,
    action: str,
    params: dict[str, Any] | None = None,
    *,
    timeout_sec: float = JOB_WAIT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """等待已有任务结束 → 启动新任务 → 按精确 job_id 等待该任务到达终态。

    同 action 的先后多个 Job 不会串结果：等待期间任何其它任务结束都不影响本任务。
    """
    await wait_until_idle_or_terminal(client, timeout_sec=timeout_sec)
    started = await start_job(client, action, params)
    job = started.get("job") if isinstance(started.get("job"), dict) else {}
    job_id = job.get("id")
    # If server returned a snapshot already terminal (unlikely), use it.
    if job_id is not None and is_terminal_job_state(str(job.get("state") or "")):
        return {"ok": True, "job": job, "started": started}
    final = await wait_job_terminal(
        client,
        expect_job_id=int(job_id) if job_id is not None else None,
        expect_action=action,
        timeout_sec=timeout_sec,
    )
    return {"ok": True, "job": final, "started": started}


def qrcode_ready(job: dict[str, Any]) -> bool:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    if result.get("qrcode_refreshed_at"):
        return True
    phase = str(result.get("login_phase") or "")
    return phase in {"waiting", "scanned", "confirming"}


async def start_login_until_qrcode(
    client: BinggoClient,
    *,
    timeout_sec: float = QR_READY_TIMEOUT_SEC,
) -> tuple[dict[str, Any], bytes]:
    """
    Login exception to G2: return as soon as QR image is available so the user can scan.
    The login job keeps running on the server; observe with job_get.
    """
    await wait_until_idle_or_terminal(client)
    await start_job(client, "login")
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_sec
    last: dict[str, Any] = {}
    while True:
        last = await get_current_job(client)
        state = str(last.get("state") or "")
        if is_terminal_job_state(state):
            raise BinggoApiError(
                f"登录在二维码就绪前已结束：state={state}, message={last.get('message') or ''}"
            )
        if qrcode_ready(last) or state == "running":
            try:
                png = await client.get_bytes("/api/login/qrcode")
                if png:
                    return last, png
            except BinggoApiError:
                pass
        if loop.time() >= deadline:
            raise BinggoApiError("等待登录二维码超时。")
        await asyncio.sleep(POLL_INTERVAL_SEC)


async def cancel_login_only(client: BinggoClient) -> dict[str, Any]:
    job = await get_current_job(client)
    if str(job.get("state") or "") != "running" or str(job.get("action") or "") != "login":
        raise BinggoApiError(
            "当前没有进行中的扫码登录（关闭扫码仅用于取消 login，不能取消其它任务）。"
        )
    data = await client.post_json("/api/jobs/cancel")
    return data if isinstance(data, dict) else {"ok": True, "job": data}
