from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.app_logging import get_logger, setup_logging
from src.app_paths import __version__, ensure_user_dirs
from src.bilibili_login import QR_IMAGE_PATH
from src.data_paths import get_data_root, get_runtime_profile_id, get_selected_profile_id
from src.llm_client import test_llm_connection
from src.llm_settings import (
    build_llm_config_from_inputs,
    get_llm_settings_public,
    load_llm_values,
    mark_llm_test_passed,
    save_llm_settings,
)
from src.sources.common import is_valid_dynamic_id, load_previous_output
from src.state_store import get_watch_last_synced_at
from src.profile_manager import create_profile, delete_profile, list_profiles, set_active_profile
from src.restart_control import (
    RestartPendingError,
    RestartUnavailableError,
    restart_control,
)
from src.user_settings import (
    DEFAULT_PARTICIPATE_FALLBACK_TEXT,
    DEFAULT_PARTICIPATE_TEXT,
    DEFAULT_PARTICIPATE_TEXT_MODE,
    get_participate_fallback_text,
    get_participate_text,
    get_participate_text_mode,
    set_participate_fallback_text,
    set_participate_text,
    set_participate_text_mode,
)
from src.watch_sync import MAX_WINDOW_SECONDS, OUTPUT_PATH as WATCH_OUTPUT_PATH, compute_sync_window
from src.watch_users import add_watch_user, get_watch_users_payload, remove_watch_user, seed_from_candidates_if_empty
from web.account_service import ack_at_unread_notice, clear_login_cookie, get_account_extras, get_account_profile
from web.activity_service import (
    ACTIVITY_PAGE_SIZE,
    get_summary,
    list_activities,
    summarize_triple_participate_targets,
)
from web.api_contract import API_CONTRACT_VERSION, ApiContractMiddleware, patch_openapi_schema
from web.api_errors import AppError, ErrorCode, register_exception_handlers, require_llm_ready, require_login
from web.auto_scheduler import auto_scheduler
from web.job_runner import runner
from web.schemas import (
    ALLOWED_JOB_ACTIONS,
    AckAtUnreadRequest,
    DiagnosticsBundleOut,
    DiagnosticsLogsOut,
    JobRequest,
    JobStartOut,
    JobStatusOut,
    LlmSettingsRequest,
    OkResponse,
    ParticipateTextRequest,
    RepostCleanupDeleteRequest,
    RepostDeferRequest,
    UpdatesCheckOut,
    WatchUserRequest,
)

ensure_user_dirs()
setup_logging(console=False)
logger = get_logger("api")
try:
    from src.config_health import log_config_health

    log_config_health()
except Exception:
    logger.exception("配置自检失败（已忽略，不阻断启动）")
runner.recover_on_startup()

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
DIST_DIR = STATIC_DIR / "dist"

app = FastAPI(
    title="Binggo 本地控制台 API",
    version=__version__,
    description=f"契约代见 X-Api-Contract / API_CONTRACT_VERSION；当前={API_CONTRACT_VERSION}",
)
class AssetCacheMiddleware:
    """为 hashed /assets/* 加长缓存；纯 ASGI，不缓冲 body。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path") or "").startswith("/assets/"):
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "public, max-age=31536000, immutable"
            await send(message)

        await self.app(scope, receive, send_wrapper)


# 纯 ASGI 中间件：勿用 BaseHTTPMiddleware，以免缓冲/打断 SSE
app.add_middleware(AssetCacheMiddleware)
app.add_middleware(ApiContractMiddleware)
register_exception_handlers(app)

_JOB_REQUIRES_LOGIN = frozenset(
    {
        "participate",
        "participate_triple",
        "refresh_all",
        "refresh_source",
        "refresh_status",
        "refresh_watch",
        "update_all_datasources",
        "following_feed_scan",
        "cleanup_auto_maintain",
        "scan_expired_reposts",
        "sync_repost_history",
    }
)
_JOB_REQUIRES_LLM = frozenset(
    {
        "participate",
        "participate_triple",
        "refresh_all",
        "refresh_source",
        "refresh_watch",
        "update_all_datasources",
        "following_feed_scan",
    }
)


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    app.openapi_schema = patch_openapi_schema(schema)
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]


@app.get("/api/watch-users", tags=["stable"])
def api_watch_users() -> dict[str, Any]:
    seed_from_candidates_if_empty()
    payload = get_watch_users_payload(ensure_seeded=False)
    now = int(time.time())
    last_synced_at = get_watch_last_synced_at()
    window_start, window_end = compute_sync_window(now=now, last_synced_at=last_synced_at)
    payload["last_synced_at"] = last_synced_at
    payload["max_window_seconds"] = MAX_WINDOW_SECONDS
    payload["next_window"] = {"start": window_start, "end": window_end}
    watch_data = load_previous_output(WATCH_OUTPUT_PATH) or {}
    payload["last_scan_link_count"] = int(watch_data.get("link_count") or 0)
    return payload


@app.post("/api/watch-users", tags=["stable"])
def api_add_watch_user(request: WatchUserRequest) -> dict[str, Any]:
    account = get_account_profile()
    require_login(account, message="请先扫码登录后再管理监控用户")
    try:
        user = add_watch_user(mid=request.mid)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    except OSError as exc:
        raise AppError(ErrorCode.INTERNAL, f"保存监控用户失败：{exc}") from exc
    name_fallback = str(request.mid) == user.name
    return {"ok": True, "user": user.to_dict(), "name_fallback": name_fallback}


@app.delete("/api/watch-users/{mid}", tags=["stable"])
def api_remove_watch_user(mid: int) -> OkResponse:
    account = get_account_profile()
    require_login(account, message="请先扫码登录后再管理监控用户")
    if mid <= 0:
        raise AppError(ErrorCode.VALIDATION_ERROR, "MID 无效")
    try:
        removed = remove_watch_user(mid=mid)
    except OSError as exc:
        raise AppError(ErrorCode.INTERNAL, f"删除监控用户失败：{exc}") from exc
    if not removed:
        raise AppError(ErrorCode.NOT_FOUND, "用户不在监控列表中")
    return OkResponse(ok=True)


@app.get("/api/account", tags=["stable"])
def api_account() -> dict[str, Any]:
    return get_account_profile()


def _require_profile_operations_idle(*, switching: bool = False) -> None:
    auto_running = auto_scheduler.get_status().get("state") == "running"
    if runner.is_running() or auto_running:
        if switching:
            message = "当前有任务正在运行，请等待任务结束后再切换账号。"
        else:
            message = "任务或自动调度正在运行，暂时不能切换或删除账号 Profile"
        raise AppError(
            ErrorCode.JOB_BUSY,
            message,
        )


def _raise_restart_pending() -> None:
    raise AppError(
        ErrorCode.JOB_BUSY,
        "Binggo 正在切换账号并重新启动，请稍候。",
    )


def _raise_profile_value_error(exc: ValueError) -> None:
    message = str(exc)
    code = ErrorCode.NOT_FOUND if "不存在" in message else ErrorCode.VALIDATION_ERROR
    raise AppError(code, message) from exc


@app.get("/api/profiles", tags=["stable"])
def api_profiles() -> dict[str, Any]:
    runtime_profile_id = get_runtime_profile_id()
    active_profile_id = get_selected_profile_id()
    return {
        "runtime_profile_id": runtime_profile_id,
        "active_profile_id": active_profile_id,
        "restart_required": active_profile_id != runtime_profile_id,
        "data_root": str(get_data_root()),
        "profiles": list_profiles(),
    }


@app.post("/api/profiles", tags=["stable"])
def api_create_profile() -> dict[str, Any]:
    try:
        with restart_control.new_work_guard():
            profile = create_profile()
    except RestartPendingError:
        _raise_restart_pending()
    except OSError as exc:
        raise AppError(ErrorCode.INTERNAL, f"创建账号 Profile 失败：{exc}") from exc
    return {"ok": True, "profile": profile}


@app.post("/api/profiles/{profile_id}/activate", tags=["stable"])
def api_activate_profile(profile_id: str) -> dict[str, Any]:
    try:
        with restart_control.new_work_guard():
            _require_profile_operations_idle()
            profile = set_active_profile(profile_id)
    except RestartPendingError:
        _raise_restart_pending()
    except ValueError as exc:
        _raise_profile_value_error(exc)
    except OSError as exc:
        raise AppError(ErrorCode.INTERNAL, f"切换账号 Profile 失败：{exc}") from exc
    return {
        "ok": True,
        "profile": profile,
        "restart_required": get_selected_profile_id() != get_runtime_profile_id(),
    }


@app.post("/api/profiles/{profile_id}/switch", tags=["stable"])
def api_switch_profile(profile_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Persist the target Profile, then gracefully stop for supervisor respawn."""

    runtime_profile_id = get_runtime_profile_id()

    # Restoring the already-running Profile only clears an older pending manual
    # selection.  There is no reason to bounce the process in that case.
    if profile_id == runtime_profile_id:
        try:
            with restart_control.new_work_guard():
                _require_profile_operations_idle(switching=True)
                profile = set_active_profile(profile_id)
        except RestartPendingError:
            _raise_restart_pending()
        except ValueError as exc:
            _raise_profile_value_error(exc)
        except OSError as exc:
            raise AppError(ErrorCode.INTERNAL, f"切换账号 Profile 失败：{exc}") from exc
        return {
            "ok": True,
            "profile": profile,
            "runtime_profile_id": runtime_profile_id,
            "active_profile_id": get_selected_profile_id(),
            "restart_requested": False,
        }

    def transition() -> dict[str, str]:
        _require_profile_operations_idle(switching=True)
        return set_active_profile(profile_id)

    try:
        profile = restart_control.begin_restart(transition)
    except RestartPendingError:
        _raise_restart_pending()
    except RestartUnavailableError as exc:
        raise AppError(ErrorCode.INTERNAL, str(exc), status_code=503) from exc
    except ValueError as exc:
        _raise_profile_value_error(exc)
    except OSError as exc:
        raise AppError(ErrorCode.INTERNAL, f"切换账号 Profile 失败：{exc}") from exc

    # Starlette executes response background tasks after sending the response
    # body.  Only then tell Uvicorn to enter its normal graceful shutdown path.
    background_tasks.add_task(restart_control.request_restart)
    return {
        "ok": True,
        "profile": profile,
        "runtime_profile_id": runtime_profile_id,
        "active_profile_id": get_selected_profile_id(),
        "restart_requested": True,
    }


@app.delete("/api/profiles/{profile_id}", tags=["stable"])
def api_delete_profile(profile_id: str) -> dict[str, bool]:
    try:
        with restart_control.new_work_guard():
            _require_profile_operations_idle()
            delete_profile(profile_id)
    except RestartPendingError:
        _raise_restart_pending()
    except ValueError as exc:
        _raise_profile_value_error(exc)
    except OSError as exc:
        raise AppError(ErrorCode.INTERNAL, f"删除账号 Profile 失败：{exc}") from exc
    return {"ok": True}


@app.get("/api/account/extras", tags=["stable"])
def api_account_extras() -> dict[str, Any]:
    try:
        return get_account_extras()
    except RuntimeError as exc:
        raise AppError(ErrorCode.AUTH_REQUIRED, str(exc)) from exc


@app.post("/api/account/ack-at-unread", tags=["stable"])
def api_ack_at_unread(request: AckAtUnreadRequest) -> dict[str, Any]:
    try:
        return ack_at_unread_notice(request.current)
    except RuntimeError as exc:
        raise AppError(ErrorCode.AUTH_REQUIRED, str(exc)) from exc
    except OSError as exc:
        raise AppError(ErrorCode.INTERNAL, f"保存提醒状态失败：{exc}") from exc


@app.get("/api/summary", tags=["stable"])
def api_summary() -> dict[str, Any]:
    payload = get_summary()
    payload["job"] = runner.get_status().to_dict()
    return payload


@app.get("/api/activities", tags=["stable"])
def api_activities(
    status: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="type"),
    draw: str | None = Query(default=None),
    draw_window: str | None = Query(default=None),
    q: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    order: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=ACTIVITY_PAGE_SIZE, ge=1, le=ACTIVITY_PAGE_SIZE),
) -> dict[str, Any]:
    return list_activities(
        status=status,
        lottery_type=type,
        draw=draw,
        draw_window=draw_window,
        q=q,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
    )


@app.get("/api/activities/triple-targets", tags=["stable"])
def api_triple_participate_targets(
    status: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="type"),
    draw: str | None = Query(default=None),
    draw_window: str | None = Query(default=None),
    q: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    order: str | None = Query(default=None),
) -> dict[str, Any]:
    return summarize_triple_participate_targets(
        status=status,
        lottery_type=type,
        draw=draw,
        draw_window=draw_window,
        q=q,
        sort=sort,
        order=order,
    )


def _require_runtime_bilibili_uid() -> str:
    from src.bilibili_auth import require_login as require_bilibili_login

    try:
        _csrf, uid = require_bilibili_login()
    except RuntimeError as exc:
        raise AppError(ErrorCode.AUTH_REQUIRED, "请先扫码登录后再管理转发历史") from exc
    return str(uid)


@app.get("/api/repost-cleanup/history", tags=["stable"])
def api_repost_cleanup_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    """Read only the current runtime Profile's local repost index."""

    from dataclasses import asdict

    from src.repost_history import get_checkpoint, list_repost_history

    account = get_account_profile()
    require_login(account, message="请先扫码登录后再查看转发历史")
    uid = _require_runtime_bilibili_uid()
    normalized_status = str(status or "").strip() or None
    try:
        rows, total = list_repost_history(
            uid,
            page=page,
            page_size=page_size,
            status=normalized_status,
        )
        checkpoint = get_checkpoint(uid)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    return {
        "ok": True,
        "uid": uid,
        "items": [asdict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "checkpoint": asdict(checkpoint) if checkpoint is not None else None,
    }


@app.get("/api/repost-cleanup/candidates", tags=["stable"])
def api_repost_cleanup_candidates(
    show_deleted: int = Query(default=0, ge=0, le=1),
) -> dict[str, Any]:
    """从本地评估结果恢复三级候选与计数；不触发远程接口或历史重新同步。"""

    from src.repost_cleanup import repost_cleanup_summary

    account = get_account_profile()
    require_login(account, message="请先扫码登录后再查看删除候选")
    uid = _require_runtime_bilibili_uid()
    try:
        summary = repost_cleanup_summary(uid, show_deleted=bool(show_deleted))
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    return {"ok": True, "uid": uid, **summary}


@app.get("/api/repost-cleanup/health", tags=["stable"])
def api_repost_cleanup_health() -> dict[str, Any]:
    """当前 runtime Profile 的清理维护健康检查。

    纯本地：0 Bilibili 请求 / 0 LLM / 0 DELETE。只读本地库与本地文件，
    不自动恢复 pending、不自动重试 unknown/delete_failed、不自动重新删除。
    """

    from src.repost_cleanup import cleanup_health_check
    from web.auto_config import CLEANUP_MAINTAIN_INTERVAL_HOURS, cleanup_maintain_enabled

    account = get_account_profile()
    require_login(account, message="请先扫码登录后再查看维护状态")
    uid = _require_runtime_bilibili_uid()
    job = runner.get_status().to_dict()
    delete_job_in_progress = bool(
        job.get("state") == "running" and job.get("action") == "delete_expired_reposts"
    )
    payload = cleanup_health_check(
        uid, delete_job_in_progress=delete_job_in_progress
    )
    payload["maintenance"]["enabled"] = cleanup_maintain_enabled()
    payload["maintenance"]["interval_hours"] = CLEANUP_MAINTAIN_INTERVAL_HOURS
    return {"ok": True, "uid": uid, **payload}


def _defer_common(request: RepostDeferRequest, *, restore: bool) -> dict[str, Any]:
    from src.repost_cleanup import defer_candidate, repost_cleanup_summary, restore_candidate

    account = get_account_profile()
    require_login(account, message="请先扫码登录后再管理转发清理")
    uid = _require_runtime_bilibili_uid()
    repost_ids: list[str] = []
    seen_ids: set[str] = set()
    for raw_id in request.repost_dynamic_ids:
        repost_id = str(raw_id or "").strip()
        if not is_valid_dynamic_id(repost_id):
            raise AppError(ErrorCode.VALIDATION_ERROR, "转发动态 ID 无效")
        if repost_id not in seen_ids:
            seen_ids.add(repost_id)
            repost_ids.append(repost_id)
    if not repost_ids:
        raise AppError(ErrorCode.VALIDATION_ERROR, "没有选择要处理的转发动态")
    try:
        for repost_id in repost_ids:
            if restore:
                restore_candidate(uid, repost_id)
            else:
                defer_candidate(uid, repost_id)
        summary = repost_cleanup_summary(uid)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    return {"ok": True, "uid": uid, **summary}


@app.post("/api/repost-cleanup/defer", tags=["stable"])
def api_repost_cleanup_defer(request: RepostDeferRequest) -> dict[str, Any]:
    """用户“暂不删除”：本地 per-repost 标记，0 Bilibili 远程请求。"""
    return _defer_common(request, restore=False)


@app.post("/api/repost-cleanup/restore", tags=["stable"])
def api_repost_cleanup_restore(request: RepostDeferRequest) -> dict[str, Any]:
    """恢复用户暂缓：本地清除 per-repost 标记，0 Bilibili 远程请求。"""
    return _defer_common(request, restore=True)


@app.post(
    "/api/repost-cleanup/delete",
    response_model=JobStartOut,
    tags=["stable"],
)
def api_delete_expired_reposts(request: RepostCleanupDeleteRequest) -> dict[str, Any]:
    """Start a manually confirmed delete job; never accepts original IDs."""

    account = get_account_profile()
    require_login(account, message="请先扫码登录后再删除转发动态")
    _require_runtime_bilibili_uid()
    if request.confirmed is not True:
        raise AppError(ErrorCode.VALIDATION_ERROR, "请先确认删除选中的转发动态")

    repost_ids: list[str] = []
    seen_ids: set[str] = set()
    for raw_id in request.repost_dynamic_ids:
        repost_id = str(raw_id or "").strip()
        if not is_valid_dynamic_id(repost_id):
            raise AppError(ErrorCode.VALIDATION_ERROR, "转发动态 ID 无效")
        if repost_id not in seen_ids:
            seen_ids.add(repost_id)
            repost_ids.append(repost_id)
    if not repost_ids:
        raise AppError(ErrorCode.VALIDATION_ERROR, "没有选择要删除的转发动态")

    params = {
        "repost_dynamic_ids": repost_ids,
        "manual_review_confirmed": request.manual_review_confirmed,
    }
    if runner.try_start("delete_expired_reposts", params, source="ui") is None:
        if restart_control.is_restart_pending():
            _raise_restart_pending()
        raise AppError(ErrorCode.JOB_BUSY, "已有任务正在运行")
    return {"ok": True, "job": runner.get_status().to_dict()}


@app.post("/api/jobs", response_model=JobStartOut, tags=["stable"])
def api_start_job(request: JobRequest) -> dict[str, Any]:
    if request.action not in ALLOWED_JOB_ACTIONS:
        raise AppError(ErrorCode.UNSUPPORTED_ACTION, "暂不支持该操作")
    account = get_account_profile()
    if request.action in _JOB_REQUIRES_LOGIN:
        require_login(account, message="请先扫码登录后再执行此操作")
    if request.action in _JOB_REQUIRES_LLM:
        require_llm_ready()
    params = request.params or {}
    if request.action == "sync_repost_history" and params:
        raise AppError(ErrorCode.VALIDATION_ERROR, "该操作不接受额外参数")
    if request.action == "update_all_datasources" and params:
        raise AppError(ErrorCode.VALIDATION_ERROR, "该操作不接受额外参数")
    if request.action == "following_feed_scan" and params:
        raise AppError(ErrorCode.VALIDATION_ERROR, "该操作不接受额外参数")
    if request.action == "scan_expired_reposts" and not set(params) <= {"force_original_ids"}:
        raise AppError(ErrorCode.VALIDATION_ERROR, "该操作只接受 force_original_ids 参数")
    if request.action == "refresh_source":
        from web.actions import DS_HANDLER_BY_ID

        source_id = str(params.get("source_id") or "").strip()
        if source_id not in DS_HANDLER_BY_ID:
            raise AppError(ErrorCode.VALIDATION_ERROR, "数据源 ID 无效")
    if request.action == "participate":
        dynamic_id = str(params.get("dynamic_id") or "").strip()
        if not is_valid_dynamic_id(dynamic_id):
            raise AppError(ErrorCode.VALIDATION_ERROR, "活动 ID 无效")
    if runner.try_start(request.action, params, source="ui") is None:
        if restart_control.is_restart_pending():
            _raise_restart_pending()
        raise AppError(ErrorCode.JOB_BUSY, "已有任务正在运行")
    return {"ok": True, "job": runner.get_status().to_dict()}


@app.post("/api/jobs/cancel", response_model=JobStartOut, tags=["stable"])
def api_cancel_job() -> dict[str, Any]:
    if not runner.cancel():
        raise AppError(ErrorCode.JOB_NOT_CANCELLABLE, "当前没有可取消的任务")
    return {"ok": True, "job": runner.get_status().to_dict()}


@app.get("/api/jobs/current", response_model=JobStatusOut, tags=["stable"])
def api_current_job() -> dict[str, Any]:
    return runner.get_status().to_dict()


@app.get("/api/runtime", tags=["stable"])
def api_runtime() -> dict[str, Any]:
    from src.config_health import run_config_health_checks

    report = run_config_health_checks()
    payload = report.to_dict()
    payload["ok"] = True
    return payload


@app.post(
    "/api/updates/check",
    response_model=UpdatesCheckOut,
    tags=["stable"],
)
def api_updates_check() -> dict[str, Any]:
    """手动检查 GitHub Releases；网络失败也返回 200 + ok=false。"""
    from src.update_check import check_for_updates

    return check_for_updates().to_dict()


@app.get(
    "/api/diagnostics/logs",
    response_model=DiagnosticsLogsOut,
    tags=["internal"],
    include_in_schema=False,
)
def api_diagnostics_logs(
    job_id: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    from src.log_query import query_log_lines

    try:
        payload = query_log_lines(job_id=job_id, limit=limit)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    return {
        "ok": True,
        "files": payload.get("files") or [],
        "count": int(payload.get("count") or 0),
        "lines": payload.get("lines") or [],
    }


@app.get(
    "/api/diagnostics/bundle",
    response_model=DiagnosticsBundleOut,
    tags=["internal"],
    include_in_schema=False,
)
def api_diagnostics_bundle(job_id: int | None = Query(default=None)) -> dict[str, Any]:
    from src.diagnostics import build_diagnostics_bundle

    bundle = build_diagnostics_bundle(
        job_id=job_id,
        current_job=runner.get_status().to_dict(),
        auto_status=auto_scheduler.get_status(),
    )
    return {"ok": True, "filename": bundle["filename"], "text": bundle["text"]}


@app.get("/api/events", tags=["streaming"])
def api_events():
    """SSE：job.* + auto.* 进程级事件流（协议见方向二）。"""
    from web.sse import sse_response

    # 快照以 callable 注入：在流启动、订阅建立之后再读取，
    # 关闭“读快照 → 订阅”之间的事件丢失窗口。
    return sse_response(
        job_snapshot=lambda: runner.get_status().to_dict(),
        auto_snapshot=lambda: auto_scheduler.get_status(),
    )


@app.get("/api/jobs/{job_id}", response_model=JobStatusOut, tags=["stable"])
def api_job_by_id(job_id: int) -> dict[str, Any]:
    """按精确 job_id 读取任务状态（供 MCP/客户端等待指定 Job，与当前槽位解耦）。"""
    from src.job_store import get_job
    from web.job_runner import _status_from_row

    row = get_job(job_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"任务不存在：{job_id}")
    return _status_from_row(row).to_dict()


@app.get("/api/auto/status", tags=["stable"])
def api_auto_status() -> dict[str, Any]:
    return auto_scheduler.get_status()


@app.post("/api/auto/start", tags=["stable"])
def api_auto_start() -> dict[str, Any]:
    try:
        return auto_scheduler.start()
    except RestartPendingError:
        _raise_restart_pending()
    except RuntimeError as exc:
        text = str(exc)
        if "已在运行" in text:
            raise AppError(ErrorCode.AUTO_ALREADY_RUNNING, text) from exc
        raise AppError(ErrorCode.INTERNAL, text) from exc


@app.post("/api/auto/stop", tags=["stable"])
def api_auto_stop() -> dict[str, Any]:
    """只停止定时点击调度器，不会取消抽奖端正在运行的任务。"""
    return auto_scheduler.stop(reason="用户在监视面板停止")


@app.get("/api/auto/following-feed", tags=["stable"])
def api_auto_following_feed_status() -> dict[str, Any]:
    """读取关注动态补漏最近状态与下次自动扫描时间（本地 0 远程，纯只读）。

    统一 Scheduler 决定执行时机：本接口无开关、无触发副作用。
    """
    from src.following_feed import following_feed_summary

    account = get_account_profile()
    require_login(account, message="请先扫码登录后再查看关注动态补漏")
    uid = _require_runtime_bilibili_uid()
    return {"ok": True, "uid": uid, **following_feed_summary()}


@app.get("/api/auto/cleanup-maintain", tags=["stable"])
def api_auto_cleanup_maintain_status() -> dict[str, Any]:
    from src.repost_cleanup import maintenance_risk_state
    from web.auto_config import CLEANUP_MAINTAIN_INTERVAL_HOURS, cleanup_maintain_enabled

    uid = _require_runtime_bilibili_uid()
    return {
        "ok": True,
        "enabled": cleanup_maintain_enabled(),
        "interval_hours": CLEANUP_MAINTAIN_INTERVAL_HOURS,
        **maintenance_risk_state(uid),
    }


@app.post("/api/auto/cleanup-maintain", tags=["stable"])
async def api_auto_cleanup_maintain_toggle(request: Request) -> dict[str, Any]:
    from web.auto_config import CLEANUP_MAINTAIN_INTERVAL_HOURS, set_cleanup_maintain_enabled

    body = await request.json() or {}
    enabled = bool(body.get("enabled", True))
    return {
        "ok": True,
        "enabled": set_cleanup_maintain_enabled(enabled),
        "interval_hours": CLEANUP_MAINTAIN_INTERVAL_HOURS,
    }


@app.post("/api/auto/cleanup-maintain/recover", tags=["stable"])
def api_auto_cleanup_maintain_recover() -> dict[str, Any]:
    """人工恢复自动维护：只清除本地风控暂停，0 远程，恢复后不立即执行维护。"""
    from src.repost_cleanup import maintenance_risk_state, recover_auto_maintenance

    account = get_account_profile()
    require_login(account, message="请先扫码登录后再恢复自动维护")
    uid = _require_runtime_bilibili_uid()
    recover_auto_maintenance(uid)
    return {"ok": True, "uid": uid, **maintenance_risk_state(uid)}


def _build_settings_payload() -> dict[str, Any]:
    account = get_account_profile()
    llm = get_llm_settings_public()
    logged_in = bool(account.get("logged_in"))
    return {
        "participate_text": get_participate_text(),
        "default_participate_text": DEFAULT_PARTICIPATE_TEXT,
        "participate_fallback_text": get_participate_fallback_text(),
        "default_participate_fallback_text": DEFAULT_PARTICIPATE_FALLBACK_TEXT,
        "participate_text_mode": get_participate_text_mode(),
        "default_participate_text_mode": DEFAULT_PARTICIPATE_TEXT_MODE,
        "llm": llm,
        "setup_complete": logged_in and bool(llm.get("ready")),
    }


@app.get("/api/settings", tags=["stable"])
def api_settings() -> dict[str, Any]:
    return _build_settings_payload()


@app.get("/api/settings/llm", tags=["stable"])
def api_get_llm_settings() -> dict[str, Any]:
    account = get_account_profile()
    llm = get_llm_settings_public()
    logged_in = bool(account.get("logged_in"))
    return {
        "llm": llm,
        "setup_complete": logged_in and bool(llm.get("ready")),
    }


@app.post("/api/settings/llm/test", tags=["stable"])
def api_test_llm_settings(request: LlmSettingsRequest) -> dict[str, Any]:
    account = get_account_profile()
    require_login(account, message="请先扫码登录后再测试 LLM")
    try:
        saved = load_llm_values()
        test_key = request.api_key.strip() or saved.get("LLM_API_KEY", "").strip()
        test_base = (request.base_url or "").strip().rstrip("/")
        test_model = request.model_name.strip() or saved.get("LLM_MODEL_NAME", "").strip()
        config = build_llm_config_from_inputs(
            api_key=test_key,
            base_url=test_base,
            model_name=test_model,
        )
        endpoint = test_llm_connection(config)
        llm = mark_llm_test_passed(
            api_key=test_key,
            base_url=test_base,
            model_name=test_model,
        )
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    except RuntimeError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    logged_in = bool(account.get("logged_in"))
    return {
        "ok": True,
        "message": f"连接成功：{endpoint}",
        "llm": llm,
        "setup_complete": logged_in and bool(llm.get("ready")),
    }


@app.post("/api/settings/llm", tags=["stable"])
@app.put("/api/settings/llm", tags=["stable"], deprecated=True)
def api_update_llm_settings(request: LlmSettingsRequest) -> dict[str, Any]:
    account = get_account_profile()
    require_login(account, message="请先扫码登录后再配置 LLM")
    try:
        llm = save_llm_settings(
            api_key=request.api_key.strip() or None,
            base_url=request.base_url,
            model_name=request.model_name,
        )
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    except OSError as exc:
        raise AppError(ErrorCode.INTERNAL, f"保存 LLM 配置失败：{exc}") from exc
    logged_in = bool(account.get("logged_in"))
    return {
        "llm": llm,
        "setup_complete": logged_in and bool(llm.get("ready")),
    }


@app.put("/api/settings/participate-text", tags=["stable"])
@app.post("/api/settings/participate-text", tags=["stable"], deprecated=True)
def api_update_participate_text(request: ParticipateTextRequest) -> dict[str, Any]:
    account = get_account_profile()
    require_login(account, message="请先扫码登录后再修改参与文案")
    payload: dict[str, Any] = {}
    try:
        if request.participate_text_mode is not None:
            payload["participate_text_mode"] = set_participate_text_mode(request.participate_text_mode)
        if request.participate_text is not None:
            payload["participate_text"] = set_participate_text(request.participate_text)
        if request.participate_fallback_text is not None:
            payload["participate_fallback_text"] = set_participate_fallback_text(
                request.participate_fallback_text
            )
    except OSError as exc:
        raise AppError(ErrorCode.INTERNAL, f"保存参与文案失败：{exc}") from exc
    if not payload:
        raise AppError(ErrorCode.VALIDATION_ERROR, "未提供可保存的设置")
    return payload


@app.put("/api/settings/participate-text-mode", tags=["stable"], deprecated=True)
@app.post("/api/settings/participate-text-mode", tags=["stable"], deprecated=True)
def api_update_participate_text_mode(request: ParticipateTextRequest) -> dict[str, str]:
    account = get_account_profile()
    require_login(account, message="请先扫码登录后再修改参与文案模式")
    if request.participate_text_mode is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "缺少 participate_text_mode")
    try:
        value = set_participate_text_mode(request.participate_text_mode)
    except OSError as exc:
        raise AppError(ErrorCode.INTERNAL, f"保存参与文案模式失败：{exc}") from exc
    return {"participate_text_mode": value}


@app.post("/api/logout", response_model=OkResponse, tags=["stable"])
def api_logout() -> dict[str, Any]:
    try:
        clear_login_cookie()
    except OSError as exc:
        raise AppError(ErrorCode.INTERNAL, f"退出登录失败：{exc}") from exc
    return {"ok": True}


@app.get("/api/login/qrcode", tags=["stable"])
def api_login_qrcode() -> FileResponse:
    if not QR_IMAGE_PATH.exists():
        raise AppError(ErrorCode.NOT_FOUND, "二维码尚未生成")
    return FileResponse(
        QR_IMAGE_PATH,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.get("/app.js", tags=["internal"], include_in_schema=False)
@app.get("/styles.css", tags=["internal"], include_in_schema=False)
def api_legacy_static_gone() -> None:
    raise AppError(
        ErrorCode.NOT_FOUND,
        "旧静态入口已移除，请使用构建产物 web/static/dist（先 npm run build）",
        status_code=410,
    )


@app.get("/favicon.svg", tags=["internal"], include_in_schema=False)
def api_favicon() -> FileResponse:
    favicon = DIST_DIR / "favicon.svg"
    if not favicon.exists():
        favicon = STATIC_DIR / "favicon.svg"
    if not favicon.exists():
        raise AppError(ErrorCode.NOT_FOUND, "favicon 不存在")
    return FileResponse(favicon, media_type="image/svg+xml")


if not (DIST_DIR / "index.html").exists():
    logger.error(
        "未找到 web/static/dist。开发请另开: cd web/frontend && npm run dev；"
        "生产请先: cd web/frontend && npm ci && npm run build"
    )


@app.get("/", tags=["internal"], include_in_schema=False)
def spa_index() -> FileResponse:
    index_path = DIST_DIR / "index.html"
    if not index_path.exists():
        raise AppError(
            ErrorCode.INTERNAL,
            "前端未构建：请先执行 cd web/frontend && npm ci && npm run build",
        )
    return FileResponse(
        index_path,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


# E2E 钩子仅在 BINGGO_E2E=1 时安装（生产 run_dashboard 不得设置该变量）
from web.e2e_hooks import e2e_enabled, install_e2e_hooks

if e2e_enabled():
    install_e2e_hooks(app)
    logger.warning("BINGGO_E2E=1：已安装测试钩子（仅 127.0.0.1 /api/testing/e2e-state）")

_assets_dir = DIST_DIR / "assets"
if _assets_dir.is_dir():
    app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")
else:
    logger.error("未找到 web/static/dist/assets，静态资源将无法加载")
