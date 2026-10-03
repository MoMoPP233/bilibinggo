from __future__ import annotations

import contextvars
import io
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Callable, Mapping

import httpx

from src.app_logging import get_logger
from src.bilibili_client import BilibiliClient
from src.bilibili_request_budget import RequestBudgetExhausted
from src.bilibili_login import login_with_qrcode
from src.log_span import PhaseSpanTracker, log_span
from src.lottery_actions import ActionResult, ParticipationRiskBreaker, bind_participation_risk_breaker, current_participation_risk_breaker, reset_participation_risk_breaker
from src.lottery_classifier import PARTICIPATABLE_TYPES
from src.participation import participate_activity
from src.participation_log import (
    TRIPLE_LOCAL_FAILURE_BACKOFF_SECONDS,
    ParticipationActionRecord,
    append_action_record,
    participation_succeeded,
)
from src.platform_risk import risk_code_of as _risk_code_of
from src.pipeline.refresh_all_pipeline import PipelineResult, run_new_links_pipeline, run_refresh_all_pipeline
from src.sources import (
    ds1_xiaozhuli,
    ds2_fanqiao,
    ds3_gongjuren,
    ds4_junming,
    ds5_hudong,
    ds6_nuomi,
    ds7_dajinli,
    ds9_topics,
)
from src.sources.common import CheckResult, commit_source_checkpoint, is_valid_dynamic_id
from src.state_store import set_last_pipeline_persisted, set_watch_last_synced_at
from src.status_refresh import refresh_local_activity_statuses
from web.activity_service import (
    PARTICIPATE_TRIPLE_LIMIT,
    SOURCE_LABELS,
    build_triple_progress_plan,
    build_triple_target_preview,
    invalidate_activity_cache,
    lookup_lottery_type,
    participate_step_budget,
    pick_triple_participate_targets,
    resolve_participate_lottery_type,
)
from src.watch_sync import save_watch_result, sync_watch_forwards
from web.user_messages import format_participation_log, sanitize_log

logger = get_logger("job")

PARTICIPATE_TRIPLE_WORKERS = PARTICIPATE_TRIPLE_LIMIT
REFRESH_ALL_PIPELINE_SUBSTEPS = 3
REFRESH_WATCH_PIPELINE_SUBSTEPS = 3
REFRESH_WATCH_TOTAL = 1 + REFRESH_WATCH_PIPELINE_SUBSTEPS
PARTICIPATION_DEDUP_SKIP_REASONS = frozenset({
    "already_joined", "participation_busy", "repost_pending", "repost_unknown",
    "repost_suspected", "platform_joined",
})


class ParticipationItemFailure(RuntimeError):
    """A single candidate failed without requesting batch cancellation."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)
        super().__init__(str(payload.get("message") or "参与失败"))


def _deserialize_payload_actions(payload: dict[str, Any]) -> list[ActionResult]:
    actions: list[ActionResult] = []
    for item in payload.get("actions") or []:
        if not isinstance(item, dict):
            continue
        action_name = str(item.get("action") or "").strip()
        if not action_name:
            continue
        actions.append(
            ActionResult(
                action=action_name,  # type: ignore[arg-type]
                ok=bool(item.get("ok")),
                detail=str(item.get("detail") or ""),
            )
        )
    return actions


def _payload_joined_success(payload: dict[str, Any]) -> bool:
    if str(payload.get("status") or "") != "joined":
        return False
    lottery_type = str(payload.get("lottery_type") or "")
    actions = _deserialize_payload_actions(payload)
    if not actions:
        return False
    if lottery_type in ("互动抽奖", "转发抽奖", "预约抽奖"):
        return participation_succeeded(actions, lottery_type=lottery_type)
    return all(item.ok for item in actions)


def _require_participate_success(payload: dict[str, Any]) -> None:
    status = str(payload.get("status") or "")
    if status == "deferred" or payload.get("result_class") == "deferred_comment_limit":
        return
    if status == "skipped":
        raise RuntimeError(str(payload.get("message") or "该活动不可参与"))
    if status != "joined" or not _payload_joined_success(payload):
        raise ParticipationItemFailure(payload)


def _is_participation_dedup_skip(payload: dict[str, Any]) -> bool:
    return (
        payload.get("status") == "skipped"
        and payload.get("skip_reason") in PARTICIPATION_DEDUP_SKIP_REASONS
        and not payload.get("actions")
    )


def _normalize_participate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if _is_participation_dedup_skip(payload):
        return {**payload, "skipped": True}
    _require_participate_success(payload)
    normalized = dict(payload)
    if str(normalized.get("status") or "") == "deferred" and not normalized.get("result_class"):
        normalized["result_class"] = "deferred_comment_limit"
    return normalized


def _triple_transport_unknown(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.HTTPError, json.JSONDecodeError)):
        return True
    return isinstance(exc, RuntimeError) and isinstance(
        exc.__cause__, (httpx.HTTPError, json.JSONDecodeError)
    )


def _triple_global_stop_reason(
    exc: BaseException,
    cancel_event: threading.Event | None,
) -> str | None:
    if cancel_event is not None and cancel_event.is_set():
        return "user_cancel"
    if _risk_code_of(exc) is not None:
        return "platform_risk"
    if isinstance(exc, RequestBudgetExhausted):
        return "budget_exhausted"
    # Structured auth failures are global; ordinary RuntimeError messages are
    # deliberately not treated as auth by substring matching.
    code = getattr(exc, "code", None)
    if code in {-101, 401}:
        return "auth"
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 401:
        return "auth"
    text = str(exc)
    if text.startswith("profile_writer_busy:") or any(
        marker in text
        for marker in (
            "参与期间登录账号发生变化",
            "运行期间 Profile 发生变化",
            "运行中的 Profile 已变化",
            "运行时 Profile 不存在",
        )
    ):
        return "authority"
    return None


def _triple_failure_payload(
    *,
    dynamic_id: str,
    lottery_type: str,
    message: str,
    result_class: str,
    unknown_action: str | None = None,
) -> dict[str, Any]:
    recorded_at = int(time.time())
    snapshot: dict[str, Any] = {"result_class": result_class}
    if result_class == "failed_local":
        snapshot["backoff_until"] = recorded_at + TRIPLE_LOCAL_FAILURE_BACKOFF_SECONDS
    if unknown_action:
        snapshot["unknown_action"] = unknown_action
    return {
        "dynamic_id": dynamic_id,
        "lottery_type": lottery_type,
        "status": "failed",
        "message": sanitize_log(message)[:240] or "参与失败",
        "action_text": "",
        "actions": [],
        "context_snapshot": snapshot,
        "result_class": result_class,
    }


def _persist_triple_failure(
    payload: dict[str, Any],
    *,
    recorded_at: int | None = None,
) -> None:
    """Persist a worker-level failure when the guarded service had no result."""
    snapshot = payload.get("context_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}
    result_class = str(payload.get("result_class") or snapshot.get("result_class") or "")
    if result_class not in {"failed_local", "unknown"}:
        return
    stamp = int(recorded_at if recorded_at is not None else time.time())
    snapshot = dict(snapshot)
    if result_class == "failed_local":
        snapshot.setdefault("backoff_until", stamp + TRIPLE_LOCAL_FAILURE_BACKOFF_SECONDS)
    payload["context_snapshot"] = snapshot
    payload["result_class"] = result_class
    try:
        append_action_record(
            ParticipationActionRecord(
                recorded_at=stamp,
                dynamic_id=str(payload.get("dynamic_id") or ""),
                lottery_type=str(payload.get("lottery_type") or ""),
                status="failed",
                message=str(payload.get("message") or "参与失败"),
                action_text="",
                actions=list(payload.get("actions") or []),
                context_snapshot=snapshot,
            )
        )
    except Exception as exc:
        # A local logging failure must not turn an item-local outcome into a
        # batch cancellation. The normal service path persists under its DB
        # lock; this fallback only covers worker-level exceptions in tests or
        # unexpected wrappers.
        logger.warning(
            "记录三连单项失败结果失败 dynamic_id=%s error=%s",
            payload.get("dynamic_id"),
            sanitize_log(str(exc))[:160],
        )


def _participate_dynamic_payload(
    dynamic_id: str,
    on_step: Callable[[int, int, str, str], None],
    *,
    lottery_type_hint: str | None = None,
    client: BilibiliClient | None = None,
) -> dict[str, Any]:
    resolved_type = resolve_participate_lottery_type(dynamic_id, hint=lottery_type_hint)
    # Client creation and live preflight belong behind the shared local guard.
    # Do not redirect process-global stdout from parallel participation workers.
    payload = _execute_participate(
        dynamic_id,
        on_step,
        lottery_type=resolved_type,
        client=client,
    )
    return _normalize_participate_payload(payload)


def _list_filter_params(params: dict[str, Any]) -> dict[str, str | None]:
    return {
        "status": str(params.get("status") or "").strip() or None,
        "lottery_type": str(params.get("lottery_type") or params.get("type") or "").strip() or None,
        "draw": str(params.get("draw") or "").strip() or None,
        "draw_window": str(params.get("draw_window") or "").strip() or None,
        "q": str(params.get("q") or "").strip() or None,
        "sort": str(params.get("sort") or "").strip() or None,
        "order": str(params.get("order") or "").strip() or None,
    }


def _execute_participate(
    dynamic_id: str,
    on_step: Callable[[int, int, str, str], None],
    *,
    lottery_type: str | None = None,
    client: BilibiliClient | None = None,
) -> dict[str, Any]:
    dynamic_id = str(dynamic_id or "").strip()
    if not is_valid_dynamic_id(dynamic_id):
        raise ValueError(f"活动 ID 无效: {dynamic_id}")
    resolved_type = str(lottery_type or "").strip()
    if resolved_type not in PARTICIPATABLE_TYPES:
        resolved_type = resolve_participate_lottery_type(dynamic_id)

    result = participate_activity(
        client,
        dynamic_id=dynamic_id,
        lottery_type=resolved_type,
        dry_run=False,
        persist=True,
        preflight=True,
        on_step=on_step,
    )
    payload = result.to_dict()
    payload["lottery_type"] = resolved_type
    return payload


def _append_log_detail(log_lines: list[str], detail: str) -> None:
    cleaned = sanitize_log(detail.strip())
    if cleaned:
        log_lines.append(cleaned)


DS_HANDLERS: list[tuple[str, Callable[..., Any], Callable[[Any], Any]]] = [
    ("DS-1", ds1_xiaozhuli.check_update, ds1_xiaozhuli.save_result),
    ("DS-2", ds2_fanqiao.check_update, ds2_fanqiao.save_result),
    ("DS-3", ds3_gongjuren.check_update, ds3_gongjuren.save_result),
    ("DS-4", ds4_junming.check_update, ds4_junming.save_result),
    ("DS-5", ds5_hudong.check_update, ds5_hudong.save_result),
    ("DS-6", ds6_nuomi.check_update, ds6_nuomi.save_result),
    ("DS-7", ds7_dajinli.check_update, ds7_dajinli.save_result),
    # DS9 话题发现：故意没有 DS8；作为统一数据源参与 refresh_all / refresh_source。
    ("DS-9", ds9_topics.check_update, ds9_topics.save_result),
]
DS_HANDLER_BY_ID: dict[str, tuple[Callable[..., Any], Callable[[Any], Any]]] = {
    source_id: (check_update, save_result)
    for source_id, check_update, save_result in DS_HANDLERS
}

REFRESH_ALL_TOTAL = len(DS_HANDLERS) + REFRESH_ALL_PIPELINE_SUBSTEPS
REFRESH_SOURCE_TOTAL = 1 + REFRESH_ALL_PIPELINE_SUBSTEPS
ProgressCallback = Callable[..., None]


_SUBPROGRESS_SUFFIX_RE = re.compile(r"(?:\s*\(\s*\d+\s*/\s*\d+\s*\))+\s*$")


def _format_subprogress_message(message: str, done: int, total: int) -> str:
    base = _SUBPROGRESS_SUFFIX_RE.sub("", str(message or "").strip()).strip()
    if total <= 0:
        return base
    return f"{base} ({done}/{total})"


def _pipeline_substep_index(message: str) -> int:
    text = str(message or "")
    if "入库" in text or "落库" in text or "写入活动库" in text:
        return 3
    if "详情进度" in text or "活动详情" in text:
        return 2
    if "分类" in text or "新链接" in text:
        return 1
    return 1


_PIPELINE_PHASE_BY_SUBSTEP = {
    1: ("pipeline_classify", "pipeline_classify"),
    2: ("pipeline_detail", "pipeline_detail"),
    3: ("pipeline_persist", "pipeline_persist"),
}


def _make_refresh_all_pipeline_progress(
    progress: ProgressCallback,
    *,
    ds_count: int,
    span_tracker: PhaseSpanTracker | None = None,
) -> Callable[[int, int, str], None]:
    def on_pipeline_progress(done: int, total: int, message: str) -> None:
        substep = _pipeline_substep_index(message)
        if span_tracker is not None:
            phase_name = _PIPELINE_PHASE_BY_SUBSTEP.get(substep)
            if phase_name:
                phase, span_name = phase_name
                span_tracker.set_phase(phase, name=span_name)
        progress(
            step=ds_count + substep,
            total=REFRESH_ALL_TOTAL,
            message=_format_subprogress_message(message, done, total),
        )

    return on_pipeline_progress


def _make_refresh_watch_pipeline_progress(
    progress: ProgressCallback,
    *,
    span_tracker: PhaseSpanTracker | None = None,
) -> Callable[[int, int, str], None]:
    def on_pipeline_progress(done: int, total: int, message: str) -> None:
        substep = _pipeline_substep_index(message)
        if span_tracker is not None:
            phase_name = _PIPELINE_PHASE_BY_SUBSTEP.get(substep)
            if phase_name:
                phase, span_name = phase_name
                span_tracker.set_phase(phase, name=span_name)
        progress(
            step=1 + substep,
            total=REFRESH_WATCH_TOTAL,
            message=_format_subprogress_message(message, done, total),
        )

    return on_pipeline_progress


def _noop_progress(**_kwargs: Any) -> None:
    return None


def _capture_output(func: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        result = func(*args, **kwargs)
    return result, buffer.getvalue()


def _pipeline_log_lines(result: PipelineResult) -> list[str]:
    if result.pipeline_skipped:
        return [
            f"【本轮扫描】发现 {result.raw_link_count} 条，数据库已有 "
            f"{result.existing_count} 条，批内重复 {result.duplicate_link_count} 条，"
            f"无效链接 {result.invalid_link_count} 条，新候选 {result.new_link_count} 条",
            f"【流水线】{result.message}",
        ]
    skip_detail = ""
    if result.skip_reasons:
        parts = [f"{reason} {count}" for reason, count in result.skip_reasons.items()]
        skip_detail = f"，跳过 {result.skipped_count} 条（{' / '.join(parts)}）"
    return [
        f"【本轮扫描】发现 {result.raw_link_count} 条，数据库已有 "
        f"{result.existing_count} 条，批内重复 {result.duplicate_link_count} 条，"
        f"无效链接 {result.invalid_link_count} 条，新候选 {result.new_link_count} 条",
        f"【新链接流水线】分类通过 {result.classified_count} 条，"
        f"详情 {result.enriched_count} 条，"
        f"过期过滤 {result.expired_skipped_count} 条，"
        f"实际新增 {result.persisted_count} 条{skip_detail}"
    ]


def _run_ds_check(
    index: int,
    source_id: str,
    check_update: Callable[..., Any],
    save_result: Callable[[Any], Any],
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> tuple[int, dict[str, Any], str, CheckResult]:
    source_log_lines: list[str] = []

    def on_source_progress(done: int, total: int, message: str) -> None:
        cleaned = sanitize_log(str(message or "").strip())
        if not cleaned:
            return
        source_log_lines.append(f"【{source_id}】{cleaned}")
        if on_progress is not None:
            on_progress(done, total, cleaned)

    with log_span(
        f"ds_check:{source_id}",
        logger=logger,
        component="ds",
        source_id=source_id,
        phase="ds_check",
    ):
        check_kwargs: dict[str, Any] = {"force": False}
        if source_id == "DS-3":
            check_kwargs["on_progress"] = on_source_progress
        result, detail = _capture_output(check_update, **check_kwargs)
        out_path = save_result(result)
    status_text = "发现新专栏，已爬取" if result.updated else "同一专栏，已跳过"
    log_line = f"【{source_id}】{status_text}，共 {len(result.activity_links)} 条链接"
    payload = {
        "source_id": source_id,
        "updated": result.updated,
        "link_count": len(result.activity_links),
        "saved": bool(out_path),
        "log_line": log_line,
        "detail": detail.strip(),
        "status_text": status_text,
        "source_log_lines": source_log_lines,
    }
    return index, payload, log_line, result


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ValueError("任务已取消")


def _cleanup_progress_adapter(
    progress: ProgressCallback,
    cancel_event: threading.Event | None,
    *,
    log_stride: int = 1,
) -> Callable[..., None]:
    """Bridge cleanup services onto the existing JobRunner progress protocol."""

    last_logged = -max(1, log_stride)

    def report(done: int, total: int, message: str, log_line: str | None = None) -> None:
        nonlocal last_logged
        _raise_if_cancelled(cancel_event)
        step = max(0, int(done or 0))
        normalized_total = max(step, int(total or 0), 1)
        cleaned_message = str(message or "正在处理…").strip()
        should_log = (
            log_stride <= 1
            or step == 0
            or step - last_logged >= log_stride
            or (int(total or 0) > 0 and step >= int(total))
        )
        if should_log:
            last_logged = step
        progress(
            step=step,
            total=normalized_total,
            message=cleaned_message,
            log_append=str(log_line or cleaned_message).strip() if should_log else None,
        )

    return report


def _make_ds_source_progress(
    progress: ProgressCallback,
    *,
    source_id: str,
    step: int,
    total: int,
    cancel_event: threading.Event | None,
) -> Callable[[int, int, str], None]:
    def on_source_progress(_done: int, _total: int, message: str) -> None:
        _raise_if_cancelled(cancel_event)
        progress(
            step=step,
            total=total,
            message=f"{source_id}：{message}",
            log_append=f"【{source_id}】{message}",
        )

    return on_source_progress


# ===== 一键更新全部数据源：编排层，串行复用单数据源逻辑 =====

_DS_PLATFORM_RISK_MARKERS = (
    "-352",
    "-509",
    "-799",
    "429",
)

# 仅匹配结构化 code 文本令牌（-352/-509/-799/429）——Binggo 正式平台风控只认
# -352/-509/HTTP429（platform_risk.risk_code_of）。-799 是临时反爬/频率受限，不是正式风控；
# 正文字样（“风控/限流/too many…”等）一律不参与判断。本组只用于“保守停止后续只读数据源”
# 的本地兼容行为：不写 global cooldown / 不写 risk pause / 不触发 mutation breaker。

# 内部导入阶段提示（出现在单源 refresh_source 的流水线消息里）
_DS_IMPORT_PHASE_MARKERS = (
    "正在分类",
    "分类进度",
    "正在拉取",
    "详情进度",
    "正在写入活动库",
    "入库完成",
    "正在入库",
    "落库",
    "正在导入",
)


def _looks_like_platform_risk(message: object) -> bool:
    lowered = str(message or "").lower()
    return any(marker in lowered for marker in _DS_PLATFORM_RISK_MARKERS)


def _ds_phase_from_message(message: str) -> str | None:
    text = str(message or "")
    if not text:
        return None
    if any(marker in text for marker in _DS_IMPORT_PHASE_MARKERS):
        return "importing"
    if any(marker in text for marker in ("正在检查", "正在读取", "正在分析", "跳过 ", "无新专栏")):
        return "fetching"
    return None


_SOURCE_SCAN_COUNT_KEYS = (
    "discovered_count",
    "invalid_link_count",
    "duplicate_link_count",
    "existing_count",
    "candidate_count",
    "non_lottery_count",
    "other_skipped_count",
    "processing_failed_count",
    "expired_skipped_count",
    "persist_skipped_count",
    "persisted_count",
)


def _source_payload_counts(payload: dict[str, Any]) -> dict[str, Any]:
    """从单源返回值读取本轮严格计数；不使用活动总库存，也不做额外查询。"""
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    pipeline = result.get("pipeline") if isinstance(result.get("pipeline"), dict) else {}
    source = result.get("source") if isinstance(result.get("source"), dict) else {}
    discovered = int(
        source.get(
            "link_count",
            pipeline.get("discovered_count", pipeline.get("raw_link_count", 0)),
        )
        or 0
    )
    candidate = int(
        pipeline.get("candidate_count", pipeline.get("new_link_count", 0)) or 0
    )
    return {
        "updated": bool(source.get("updated")),
        "pipeline_skipped": bool(
            result.get("pipeline_skipped", pipeline.get("pipeline_skipped", False))
        ),
        "discovered_count": discovered,
        "invalid_link_count": int(pipeline.get("invalid_link_count", 0) or 0),
        "duplicate_link_count": int(pipeline.get("duplicate_link_count", 0) or 0),
        "existing_count": int(pipeline.get("existing_count", 0) or 0),
        "candidate_count": candidate,
        # 向后兼容旧前端/测试字段；语义与 candidate_count 相同。
        "new_link_count": candidate,
        "non_lottery_count": int(pipeline.get("non_lottery_count", 0) or 0),
        "other_skipped_count": int(pipeline.get("other_skipped_count", 0) or 0),
        "processing_failed_count": int(
            pipeline.get("failed_count", 0) or 0
        )
        + int(pipeline.get("provider_failed_count", 0) or 0),
        "persisted_count": int(
            result.get("persisted_count", pipeline.get("persisted_count", 0)) or 0
        ),
        "expired_skipped_count": int(
            result.get("expired_skipped_count", pipeline.get("expired_skipped_count", 0))
            or 0
        ),
        "persist_skipped_count": int(pipeline.get("persist_skipped_count", 0) or 0),
    }


def _format_source_scan_counts(counts: Mapping[str, Any]) -> str:
    if not bool(counts.get("updated")):
        return (
            f"本轮源返回 {int(counts.get('discovered_count') or 0)} 条链接，"
            "容器未变更，未进入候选处理"
        )
    duplicate_or_invalid = int(counts.get("duplicate_link_count") or 0) + int(
        counts.get("invalid_link_count") or 0
    )
    safe_skipped = int(counts.get("non_lottery_count") or 0) + int(
        counts.get("other_skipped_count") or 0
    )
    parts = [
        f"本轮发现 {int(counts.get('discovered_count') or 0)} 条",
        f"数据库已有 {int(counts.get('existing_count') or 0)} 条",
    ]
    if duplicate_or_invalid:
        parts.append(f"重复/无效 {duplicate_or_invalid} 条")
    parts.extend(
        [
            f"新候选 {int(counts.get('candidate_count') or 0)} 条",
            f"过期过滤 {int(counts.get('expired_skipped_count') or 0)} 条",
            f"非抽奖/其他跳过 {safe_skipped} 条",
            f"处理失败 {int(counts.get('processing_failed_count') or 0)} 条",
        ]
    )
    persist_skipped = int(counts.get("persist_skipped_count") or 0)
    if persist_skipped:
        parts.append(f"写入时已存在 {persist_skipped} 条")
    parts.append(f"实际新增 {int(counts.get('persisted_count') or 0)} 条")
    return "，".join(parts)


def _run_source_refresh(
    source_id: str,
    check_update: Callable[..., Any],
    save_result: Callable[[Any], Any],
    *,
    progress: ProgressCallback,
    cancel_event: threading.Event | None,
    pass_ds3_source_progress: bool = True,
    scan_started_at: int | None = None,
) -> dict[str, Any]:
    """复用现有单数据源「抓取→分类→详情→入库→检查点落库」完整链路。

    与 run_action(refresh_source) 保持完全一致的语义；普通业务失败向上抛出，
    由调用方决定是「单源失败」还是「整体风控停止」。这里不复制任何 DS 抓取逻辑。
    """
    log_lines: list[str] = []

    progress(
        step=0,
        total=REFRESH_SOURCE_TOTAL,
        message=f"正在检查 {source_id}…",
    )
    _raise_if_cancelled(cancel_event)
    source_progress_kwargs: dict[str, Any] = {}
    if source_id == "DS-3" and pass_ds3_source_progress:
        source_progress_kwargs["on_progress"] = _make_ds_source_progress(
            progress,
            source_id=source_id,
            step=1,
            total=REFRESH_SOURCE_TOTAL,
            cancel_event=cancel_event,
        )
    _, payload, log_line, check_result = _run_ds_check(
        1, source_id, check_update, save_result, **source_progress_kwargs
    )
    _raise_if_cancelled(cancel_event)
    log_lines.extend(payload.get("source_log_lines") or [])
    log_lines.append(log_line)
    progress(
        step=1,
        total=REFRESH_SOURCE_TOTAL,
        message=f"{source_id}：{payload['status_text']}",
        log_append=log_line,
    )

    if not check_result.updated:
        skip_line = f"【跳过流水线】{source_id} 为同一专栏，跳过后续步骤"
        log_lines.append(skip_line)
        progress(
            step=REFRESH_SOURCE_TOTAL,
            total=REFRESH_SOURCE_TOTAL,
            message="无新专栏，已跳过流水线",
            log_append=skip_line,
        )
        logger.info("%s 无新专栏，跳过流水线", source_id)
        set_last_pipeline_persisted(action="refresh_source", persisted_count=0)
        skipped_counts = {
            "updated": False,
            "discovered_count": int(payload["link_count"] or 0),
        }
        return {
            "ok": True,
            "message": f"{source_id} 检查完成：{_format_source_scan_counts(skipped_counts)}",
            "result": {
                "source_id": source_id,
                "source": {
                    "source_id": payload["source_id"],
                    "updated": payload["updated"],
                    "link_count": payload["link_count"],
                    "saved": payload["saved"],
                },
                "pipeline_skipped": True,
                "discovered_count": int(payload["link_count"] or 0),
                "candidate_count": 0,
                "new_link_count": 0,
                "persisted_count": 0,
            },
            "log": sanitize_log("\n".join(log_lines).strip()),
        }

    _raise_if_cancelled(cancel_event)
    progress(step=2, total=REFRESH_SOURCE_TOTAL, message="正在分类新链接…")
    pipeline_spans = PhaseSpanTracker(logger=logger, component="pipeline")
    pipeline_error: str | None = None
    try:
        pipeline_result = run_refresh_all_pipeline(
            [check_result],
            on_progress=_make_refresh_all_pipeline_progress(
                progress,
                ds_count=1,
                span_tracker=pipeline_spans,
            ),
            scan_started_at=scan_started_at,
        )
    except Exception as exc:
        pipeline_error = type(exc).__name__
        raise
    finally:
        pipeline_spans.close(error_kind=pipeline_error)
    _raise_if_cancelled(cancel_event)
    if pipeline_result.partial:
        # V21：partial/breaker 时禁止提交最终 checkpoint，避免剩余候选被永久跳过。
        # 已成功结果已由 pipeline 落库；下一轮从同一专栏重新进入，由 DB 去重与
        # classification cache 挡下已完成候选。
        partial_line = (
            "【部分完成】LLM 服务异常，未推进数据源检查点；"
            "已成功结果已保存，剩余候选将在后续刷新继续"
        )
        log_lines.append(partial_line)
    else:
        commit_source_checkpoint(check_result)
    invalidate_activity_cache()
    for line in _pipeline_log_lines(pipeline_result):
        log_lines.append(line)
    progress(
        step=REFRESH_SOURCE_TOTAL,
        total=REFRESH_SOURCE_TOTAL,
        message=pipeline_result.message or "流水线完成",
        log_append=log_lines[-1] if log_lines else "",
    )

    logger.info(
        "%s 更新完成：发现 %s，新候选 %s，实际新增 %s",
        source_id,
        pipeline_result.raw_link_count,
        pipeline_result.new_link_count,
        pipeline_result.persisted_count,
    )
    set_last_pipeline_persisted(
        action="refresh_source",
        persisted_count=pipeline_result.persisted_count,
    )

    result_payload = {
        "source_id": source_id,
        "source": {
            "source_id": payload["source_id"],
            "updated": payload["updated"],
            "link_count": payload["link_count"],
            "saved": payload["saved"],
        },
        "pipeline": pipeline_result.to_dict(),
    }
    counts = _source_payload_counts({"result": result_payload})
    message = f"{source_id} 更新完成：{_format_source_scan_counts(counts)}"
    if pipeline_result.partial:
        message += "（部分完成：LLM 服务异常，剩余候选待后续刷新）"
    return {
        "ok": True,
        "message": message,
        "result": result_payload,
        "log": sanitize_log("\n".join(log_lines).strip()),
    }


def _update_all_datasources(
    *,
    progress: ProgressCallback,
    cancel_event: threading.Event | None,
) -> dict[str, Any]:
    """一键更新全部数据源：严格串行编排层。

    对每个数据源复用 _run_source_refresh（即单源“更新此源”的完整链路）。
    一个源只有「抓取→分类→详情→入库→检查点落库」全部返回后才进入下一个源。
    普通失败记录后继续；平台风控（-352/-509/429/限流）或用户取消立即停止后续源。
    不新建第二套 DS 抓取/导入逻辑；DS1~DS7 之间绝不并发。
    """
    log_lines: list[str] = []
    total = len(DS_HANDLERS)
    if total == 0:
        return {
            "ok": True,
            "message": "没有可更新的数据源",
            "result": {"update_all": {"phase": "done", "total": 0, "sources": []}},
            "log": "没有可更新的数据源",
        }
    # 整个一键任务（DS1→DS7）只取一次固定扫描基准时间，避免标准漂移。
    scan_started_at = int(time.time())

    states: list[dict[str, Any]] = [
        {
            "source_id": source_id,
            "name": SOURCE_LABELS.get(source_id, source_id),
            "status": "waiting",
            "phase": "waiting",
            "message": "等待",
            "error": None,
            "updated": False,
            "pipeline_skipped": False,
            "discovered_count": 0,
            "invalid_link_count": 0,
            "duplicate_link_count": 0,
            "existing_count": 0,
            "candidate_count": 0,
            "new_link_count": 0,
            "non_lottery_count": 0,
            "other_skipped_count": 0,
            "processing_failed_count": 0,
            "persisted_count": 0,
            "expired_skipped_count": 0,
            "persist_skipped_count": 0,
        }
        for source_id, _, _ in DS_HANDLERS
    ]

    def state_snapshot() -> list[dict[str, Any]]:
        return [
            {
                key: entry[key]
                for key in (
                    "source_id",
                    "name",
                    "status",
                    "phase",
                    "message",
                    "error",
                    "updated",
                    "pipeline_skipped",
                    "discovered_count",
                    "invalid_link_count",
                    "duplicate_link_count",
                    "existing_count",
                    "candidate_count",
                    "new_link_count",
                    "non_lottery_count",
                    "other_skipped_count",
                    "processing_failed_count",
                    "persisted_count",
                    "expired_skipped_count",
                    "persist_skipped_count",
                )
            }
            for entry in states
        ]

    def aggregate_counts() -> dict[str, int]:
        totals = {
            key: sum(int(entry.get(key) or 0) for entry in states)
            for key in _SOURCE_SCAN_COUNT_KEYS
        }
        totals["safe_skipped_count"] = (
            totals["non_lottery_count"] + totals["other_skipped_count"]
        )
        totals["duplicate_or_invalid_count"] = (
            totals["duplicate_link_count"] + totals["invalid_link_count"]
        )
        totals["finished_sources"] = sum(
            entry["status"] in {"success", "failed", "risk", "stopped"}
            for entry in states
        )
        return totals

    def emit(phase: str, current_index: int | None, *, log_append: str | None = None) -> None:
        progress(
            step=(current_index + 1 if current_index is not None else total),
            total=total,
            message=_current_status_text(),
            log_append=log_append,
            result_patch={
                "update_all": {
                    "phase": phase,
                    "current_index": current_index,
                    "total": total,
                    "totals": aggregate_counts(),
                    "sources": state_snapshot(),
                }
            },
        )

    def _current_status_text() -> str:
        running = [
            entry
            for entry in states
            if entry["status"] in {"running", "success", "failed", "risk", "stopped"}
        ]
        if not running:
            return f"准备更新 {total} 个数据源…"
        running_text = running[-1]
        if running_text["status"] == "success":
            totals = aggregate_counts()
            return (
                f"已更新 {running_text['source_id']}：累计发现 "
                f"{totals['discovered_count']} 条，过期过滤 "
                f"{totals['expired_skipped_count']} 条，实际新增 "
                f"{totals['persisted_count']} 条"
            )
        if running_text["status"] in {"failed", "risk", "stopped"}:
            return f"{running_text['source_id']}：{running_text['message']}"
        return f"正在更新 {running_text['source_id']}（{running_text['name']}）…"

    def make_source_progress(index: int, source_id: str) -> Callable[..., None]:
        emitted_phase: dict[str, str] = {"phase": "fetching"}

        def on_source_progress(
            step: int,
            total: int,
            message: str,
            log_append: str | None = None,
            **_extra: Any,
        ) -> None:
            _raise_if_cancelled(cancel_event)
            if _looks_like_platform_risk(message):
                states[index]["_risk_seen"] = True
            phase = _ds_phase_from_message(message)
            if phase is not None and phase != emitted_phase["phase"]:
                emitted_phase["phase"] = phase
                states[index]["phase"] = phase
                emit("running", index, log_append=log_append)
                return
            if message:
                progress(
                    step=step,
                    total=total,
                    message=message,
                    log_append=log_append,
                )

        return on_source_progress

    success_count = 0
    failed_count = 0
    risk_stopped = False
    cancelled_stop = False
    stopped_index: int | None = None

    for index, (source_id, check_update, save_result_func) in enumerate(DS_HANDLERS):
        if cancel_event is not None and cancel_event.is_set():
            cancelled_stop = True
            stopped_index = index
            break

        states[index]["status"] = "running"
        states[index]["phase"] = "fetching"
        states[index]["message"] = f"正在更新 {source_id}…"
        states[index]["error"] = None
        states[index]["_risk_seen"] = False
        emit("running", index, log_append=f"【一键更新】开始 {source_id}…")
        log_lines.append(f"【一键更新】开始 {source_id}…")

        try:
            payload = _run_source_refresh(
                source_id,
                check_update,
                save_result_func,
                progress=make_source_progress(index, source_id),
                cancel_event=cancel_event,
                scan_started_at=scan_started_at,
            )
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            if cancel_event is not None and cancel_event.is_set() or "任务已取消" in message:
                cancelled_stop = True
                states[index]["status"] = "stopped"
                states[index]["phase"] = "stopped"
                states[index]["message"] = "已取消"
                stopped_index = index
                emit("done", index, log_append=f"【一键更新】{source_id} 已取消")
                break
            risk_code = _risk_code_of(exc)
            if risk_code is not None:
                # 正式结构化平台风控（-352/-509/HTTP429）：保守停止后续只读源
                risk_stopped = True
                states[index]["status"] = "risk"
                states[index]["phase"] = "risk"
                states[index]["message"] = f"平台风控（{risk_code}），已停止后续数据源刷新"
                states[index]["error"] = message
                stopped_index = index
                emit("done", index, log_append=f"一键更新：{source_id} 平台风控停止")
                break
            if _looks_like_platform_risk(message):
                # 文本含 -799/429 等 code 令牌但未确认正式风控：source-local 保守停止
                # （禁止升级为 platform risk / global cooldown / mutation breaker）
                risk_stopped = True
                states[index]["status"] = "risk"
                states[index]["phase"] = "risk"
                states[index]["message"] = "疑似平台风控 code（未确认正式风控信封），保守停止后续只读数据源；不写全局冷却"
                states[index]["error"] = message
                stopped_index = index
                emit("done", index, log_append=f"一键更新：{source_id} 疑似临时受限，保守停止")
                break
            failed_count += 1
            states[index]["status"] = "failed"
            states[index]["phase"] = "failed"
            states[index]["message"] = "更新失败"
            states[index]["error"] = message
            log_lines.append(f"【一键更新】{source_id} 失败：{message}")
            emit("running", index, log_append=f"【一键更新】{source_id} 失败：{message}")
            continue

        log_chunk = payload.get("log")
        if log_chunk:
            log_lines.append(str(log_chunk))

        counts = _source_payload_counts(payload)
        states[index]["updated"] = counts["updated"]
        states[index]["pipeline_skipped"] = counts["pipeline_skipped"]
        for key in _SOURCE_SCAN_COUNT_KEYS:
            states[index][key] = counts[key]
        states[index]["new_link_count"] = counts["candidate_count"]

        if states[index].get("_risk_seen"):
            # DS 源内部出现 -352/-509 等 code 文本：保守停止后续只读源；
            # 未确认正式结构化风控，不写 global cooldown，仅供本轮展示。
            risk_stopped = True
            states[index]["status"] = "stopped"
            states[index]["phase"] = "stopped"
            states[index]["message"] = "疑似源内临时受限（未确认正式平台风控），保守停止后续只读数据源；不写全局冷却"
            states[index]["error"] = "源内出现风控/限流 code 文本"
            stopped_index = index
            log_lines.append(
                f"一键更新：{source_id} 疑似源内临时受限，保守停止后续只读源（不写全局冷却）"
            )
            emit("done", index, log_append=f"一键更新：{source_id} 疑似受限保守停止")
            break

        states[index]["status"] = "success"
        states[index]["phase"] = "success"
        states[index]["message"] = str(payload.get("message") or "完成")
        success_count += 1
        log_lines.append(f"【一键更新】{source_id} 完成：{states[index]['message']}")
        emit("running", index, log_append=f"【一键更新】{source_id} 完成")

    totals = aggregate_counts()
    total_scan_text = _format_source_scan_counts({"updated": True, **totals})
    if cancelled_stop or risk_stopped:
        # 已结束或未执行的源统一标记，不允许并发/自动补跑。
        start = stopped_index if stopped_index is not None else total
        for entry in states[start:]:
            if entry["status"] in {"waiting", "running"}:
                entry["status"] = "not_run"
                entry["phase"] = "not_run"
                entry["message"] = "未执行"
        if risk_stopped:
            summary_text = f"检测到平台风控，已停止后续数据源（成功 {success_count}，本次停止 {failed_count} 个失败源）"
        else:
            summary_text = f"已停止更新（成功 {success_count}，失败 {failed_count}）"
    else:
        summary_text = (
            f"全部数据源更新完成：成功 {success_count} 个，失败 {failed_count} 个，"
            f"{total_scan_text}"
        )

    if cancelled_stop or risk_stopped:
        summary_text += (
            f"；已完成部分：{total_scan_text}；其余标记为未执行"
        )

    log_lines.append(summary_text)
    emit("done", None, log_append=summary_text)

    return {
        "ok": not cancelled_stop and not risk_stopped,
        "cancelled": cancelled_stop,
        "message": summary_text,
        "result": {
            "update_all": {
                "phase": "done",
                "total": total,
                "cancelled": cancelled_stop,
                "risk_stopped": risk_stopped,
                "success_count": success_count,
                "failed_count": failed_count,
                "persisted_count": totals["persisted_count"],
                "expired_skipped_count": totals["expired_skipped_count"],
                "totals": totals,
                "summary": summary_text,
                "sources": state_snapshot(),
            }
        },
        "log": sanitize_log("\n".join(log_lines).strip()),
    }


# M5：写型 Job（会写 DB / Profile 状态 / Bilibili mutation）才需要 Profile WriterLease。
# 只读性质的 Job 不在此集合中，不会被占用锁而失败。
WRITER_LEASE_ACTIONS = frozenset({
    "login",
    "refresh_all",
    "refresh_source",
    "refresh_watch",
    "refresh_status",
    "update_all_datasources",
    "following_feed_scan",
    "participate",
    "participate_triple",
    "cleanup_auto_maintain",
    "scan_expired_reposts",
    "cleanup_auto_delete",
    "cleanup_maintenance_cycle",
    "sync_repost_history",
    "sync_assess_repost_history",
    "winner_deep_scan",
    "delete_expired_reposts",
})


def run_action(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    on_progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """写型 action 在统一入口外层持有 per-Profile WriterLease（只锁最外层一次）。"""
    if action in WRITER_LEASE_ACTIONS:
        from src.profile_writer_lease import ProfileWriterLeaseBusy, profile_writer_lease

        try:
            with profile_writer_lease(owner=f"job:{action}"):
                return _run_action_impl(
                    action,
                    params,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                )
        except ProfileWriterLeaseBusy as exc:
            logger.warning("写型 Job 被占用 action=%s (%s)", action, exc)
            raise RuntimeError("profile_writer_busy: 当前 Profile 正在被另一个写入任务使用，请稍后重试。") from exc
    return _run_action_impl(action, params, on_progress=on_progress, cancel_event=cancel_event)


def _raise_assessment_platform_risk(result: object) -> None:
    """评估结果若为正式平台风控：结构化上抛（Job 失败 + 全局冷却已由核心层记录）。"""
    if not isinstance(result, dict) or not result.get("rate_limited"):
        return
    code = result.get("risk_code")
    if code in (-352, -509, 429):
        raise RuntimeError(f"API error {code}: 平台风控已停止当前评估（已完成结果已保留）")


def _run_action_impl(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    on_progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    params = params or {}
    progress = on_progress or _noop_progress
    log_lines: list[str] = []
    _raise_if_cancelled(cancel_event)

    if action == "login":
        qrcode_refresh_count = 0

        def on_qrcode_ready() -> None:
            nonlocal qrcode_refresh_count
            qrcode_refresh_count += 1
            refreshed_at = int(time.time())
            if qrcode_refresh_count == 1:
                progress(
                    step=1,
                    total=1,
                    message="请使用哔哩哔哩 App 扫码登录",
                    log_append="已生成登录二维码",
                    login_phase="waiting",
                    qrcode_refreshed_at=refreshed_at,
                )
            else:
                progress(
                    step=1,
                    total=1,
                    message="二维码已刷新，请重新扫码",
                    log_append="二维码已自动刷新",
                    login_phase="waiting",
                    qrcode_refreshed_at=refreshed_at,
                )

        def on_login_status(phase: str, message: str) -> None:
            log_append = {
                "scanned": "已在手机扫码，等待确认",
                "confirming": "用户已确认，正在写入登录信息",
                "refreshing": "二维码已过期，请重新扫码",
            }.get(phase)
            progress(
                step=1,
                total=1,
                message=message,
                log_append=log_append,
                login_phase=phase,
            )

        progress(step=1, total=1, message="正在生成登录二维码…", login_phase="waiting")
        _, log = _capture_output(
            login_with_qrcode,
            open_image=False,
            auto_refresh_on_expire=False,
            cancel_event=cancel_event,
            on_qrcode_ready=on_qrcode_ready,
            on_status_change=on_login_status,
        )
        progress(step=1, total=1, message="登录成功，账号已就绪", login_phase="success")
        return {
            "ok": True,
            "message": "登录成功，账号已就绪",
            "log": sanitize_log(log) or "登录成功",
            "result": {"login_phase": "success"},
        }

    if action == "refresh_all":
        ds_results: list[dict[str, Any]] = []
        sources_updated = 0
        progress(
            step=0,
            total=REFRESH_ALL_TOTAL,
            message=f"正在并行检查 {len(DS_HANDLERS)} 个数据源…",
        )
        _raise_if_cancelled(cancel_event)

        ds_payloads: list[tuple[int, dict[str, Any], str, CheckResult]] = []
        with ThreadPoolExecutor(max_workers=len(DS_HANDLERS)) as executor:
            # 每个 future 各自 copy_context：同一 Context 不能被多线程并发 enter
            futures = {
                executor.submit(
                    contextvars.copy_context().run,
                    _run_ds_check,
                    index,
                    source_id,
                    check_update,
                    save_result,
                    **({
                        "on_progress": _make_ds_source_progress(
                            progress,
                            source_id=source_id,
                            step=0,
                            total=REFRESH_ALL_TOTAL,
                            cancel_event=cancel_event,
                        )
                    } if source_id == "DS-3" and on_progress is not None else {}),
                ): source_id
                for index, (source_id, check_update, save_result) in enumerate(DS_HANDLERS, start=1)
            }
            for future in as_completed(futures):
                _raise_if_cancelled(cancel_event)
                source_id = futures[future]
                try:
                    ds_payloads.append(future.result())
                except Exception as exc:
                    raise RuntimeError(f"{source_id} 检查失败：{exc}") from exc

        ds_payloads.sort(key=lambda item: item[0])
        ds_check_results: list[CheckResult] = []
        for index, payload, log_line, check_result in ds_payloads:
            _raise_if_cancelled(cancel_event)
            ds_check_results.append(check_result)
            if payload.get("updated"):
                sources_updated += 1
            ds_results.append(
                {
                    "source_id": payload["source_id"],
                    "updated": payload["updated"],
                    "link_count": payload["link_count"],
                    "saved": payload["saved"],
                }
            )
            # JobRunner 完成后以 payload.log 覆盖实时日志；保留来源阶段的
            # 细节和部分失败警告，避免成功状态将它们清空。
            log_lines.extend(payload.get("source_log_lines") or [])
            log_lines.append(log_line)
            progress(
                step=index,
                total=REFRESH_ALL_TOTAL,
                message=f"{payload['source_id']}：{payload['status_text']}",
                log_append=log_line,
            )

        if sources_updated == 0:
            skip_line = f"【跳过流水线】{len(DS_HANDLERS)} 个数据源均为同一专栏，跳过后续步骤"
            log_lines.append(skip_line)
            progress(
                step=REFRESH_ALL_TOTAL,
                total=REFRESH_ALL_TOTAL,
                message="均无新专栏，已跳过整个流水线",
                log_append=skip_line,
            )
            logger.info("一键更新：%s 个数据源均无新专栏，跳过流水线", len(DS_HANDLERS))
            set_last_pipeline_persisted(action="refresh_all", persisted_count=0)
            return {
                "ok": True,
                "message": f"检查完成：{len(DS_HANDLERS)} 个数据源均无新专栏，已跳过整个流水线",
                "result": {
                    "sources": ds_results,
                    "sources_updated": 0,
                    "pipeline_skipped": True,
                    "new_link_count": 0,
                    "persisted_count": 0,
                },
                "log": sanitize_log("\n".join(log_lines).strip()),
            }

        _raise_if_cancelled(cancel_event)
        pipeline_step = len(DS_HANDLERS) + 1
        progress(step=pipeline_step, total=REFRESH_ALL_TOTAL, message="正在分类新链接…")

        pipeline_spans = PhaseSpanTracker(logger=logger, component="pipeline")
        pipeline_error: str | None = None
        try:
            pipeline_result = run_refresh_all_pipeline(
                ds_check_results,
                on_progress=_make_refresh_all_pipeline_progress(
                    progress,
                    ds_count=len(DS_HANDLERS),
                    span_tracker=pipeline_spans,
                ),
            )
        except Exception as exc:
            pipeline_error = type(exc).__name__
            raise
        finally:
            pipeline_spans.close(error_kind=pipeline_error)
        _raise_if_cancelled(cancel_event)
        if pipeline_result.partial:
            # V21：partial/breaker 时禁止提交任何最终 checkpoint，避免剩余候选被永久跳过。
            partial_line = (
                "【部分完成】LLM 服务异常，未推进数据源检查点；"
                "已成功结果已保存，剩余候选将在后续刷新继续"
            )
            log_lines.append(partial_line)
        else:
            for check_result in ds_check_results:
                commit_source_checkpoint(check_result)
        invalidate_activity_cache()
        for line in _pipeline_log_lines(pipeline_result):
            log_lines.append(line)
        progress(
            step=REFRESH_ALL_TOTAL,
            total=REFRESH_ALL_TOTAL,
            message=pipeline_result.message or "流水线完成",
            log_append=log_lines[-1] if log_lines else "",
        )

        summary_parts = [
            f"检查 {len(DS_HANDLERS)} 个数据源，{sources_updated} 个有新专栏",
            f"新链接 {pipeline_result.new_link_count} 条",
            f"新入库 {pipeline_result.persisted_count} 条",
        ]
        if pipeline_result.skip_reasons:
            summary_parts.append(f"跳过 {pipeline_result.skipped_count} 条")
        if pipeline_result.partial:
            summary_parts.append("部分完成（LLM 服务异常，剩余候选待后续刷新）")

        logger.info(
            "一键更新完成：新链接 %s，入库 %s",
            pipeline_result.new_link_count,
            pipeline_result.persisted_count,
        )
        set_last_pipeline_persisted(
            action="refresh_all",
            persisted_count=pipeline_result.persisted_count,
        )

        return {
            "ok": True,
            "message": "一键更新完成：" + "，".join(summary_parts),
            "result": {
                "sources": ds_results,
                "sources_updated": sources_updated,
                "pipeline": pipeline_result.to_dict(),
            },
            "log": sanitize_log("\n".join(log_lines).strip()),
        }

    if action == "refresh_source":
        source_id = str(params.get("source_id") or "").strip()
        handler = DS_HANDLER_BY_ID.get(source_id)
        if not handler:
            raise ValueError(f"未知数据源：{source_id}")
        check_update, save_result = handler
        # 单源 Job 启动时记录一次固定扫描基准时间，整轮 DS3 使用同一个时间。
        scan_started_at = int(time.time())
        return _run_source_refresh(
            source_id,
            check_update,
            save_result,
            progress=progress,
            cancel_event=cancel_event,
            pass_ds3_source_progress=on_progress is not None,
            scan_started_at=scan_started_at,
        )

    if action == "update_all_datasources":
        if params:
            raise ValueError("一键更新全部数据源不接受额外参数")
        return _update_all_datasources(progress=progress, cancel_event=cancel_event)

    if action == "winner_deep_scan":
        # M3：中奖深检（@我 / 回复我 / 未读私信）。三类来源独立 checkpoint；
        # 普通来源失败以 partial 呈现（不伪装空结果），平台风控沿现有风险通道上抛。
        from src.winner_scan.service import service as winner_scan_service

        if params:
            raise ValueError("中奖深检不接受额外参数")
        _raise_if_cancelled(cancel_event)
        progress(step=0, total=4, message="正在读取 @我的 / 回复我的 / 未读私信…")
        result = winner_scan_service.run(cancel_check=(lambda: bool(cancel_event and cancel_event.is_set())))
        _raise_if_cancelled(cancel_event)
        sources = result.get("sources") or {}
        found = sum(int(item.get("found") or 0) for item in sources.values())
        failed = [name for name, item in sources.items() if item.get("status") != "success"]
        auto_reply_skipped = int(result.get("auto_reply_skipped_count") or 0)
        if result.get("partial"):
            message = f"深检完成（部分来源失败：{','.join(failed)}，本轮新增线索 {found} 条）"
        else:
            message = f"中奖深检完成：新增疑似中奖线索 {found} 条"
        if auto_reply_skipped:
            message += f"；跳过 {auto_reply_skipped} 条关注后自动回复"
        progress(step=4, total=4, message=message, log_append=message)
        return {
            "ok": True,
            "message": message,
            "result": result,
            "log": sanitize_log(message),
        }

    if action == "following_feed_scan":
        if params:
            raise ValueError("关注动态补漏不接受额外参数")
        from src.following_feed import scan_following_feed

        last_logged_stage = ""
        last_logged_classified = -1

        def _stage_for_feed_message(message: str, done: int, total: int) -> tuple[str, int, int, int]:
            text = str(message or "")
            if "读取关注动态 Feed" in text:
                return "获取 Feed", 0, 0, 1
            if "候选" in text and "流水线" in text:
                return "准备候选", 1, max(0, done), max(1, total)
            if "写入活动库" in text or "入库完成" in text:
                return "保存", 3, max(0, done), max(1, total)
            if "详情" in text or "分类" in text or "LLM" in text:
                return "分类中", 2, max(0, done), max(1, total)
            return "分类中", 2, max(0, done), max(1, total)

        def on_feed_progress(done: int, total: int, message: str) -> None:
            nonlocal last_logged_stage, last_logged_classified
            _raise_if_cancelled(cancel_event)
            stage, step, current, stage_total = _stage_for_feed_message(message, done, total)
            display = str(message or "关注动态扫描中…")
            if stage == "分类中":
                display = f"分类动态 {current}/{stage_total}"
            log_append: str | None = None
            should_log = stage != last_logged_stage
            if stage == "分类中":
                stride = max(10, stage_total // 20)
                should_log = should_log or current in {0, stage_total} or current - last_logged_classified >= stride
            if should_log:
                log_append = display
                last_logged_stage = stage
                if stage == "分类中":
                    last_logged_classified = current
            progress(
                step=step,
                total=4,
                message=display,
                stage=stage,
                current=current,
                stage_total=stage_total,
                log_append=log_append,
            )

        progress(step=0, total=4, message="开始关注动态补漏扫描…", stage="获取 Feed", current=0, stage_total=1)
        result = scan_following_feed(on_progress=on_feed_progress)
        # 结构解析失败（普通失败，非风控）：必须体现为 Job 失败，不能 success。
        # 明确风控以异常向上抛：由 JobRunner（auto）统一记录 global cooldown。
        if str(result.get("status") or "") == "failed":
            raise RuntimeError(str(result.get("message") or "关注动态补漏扫描失败"))
        message = str(result.get("message") or "关注动态补漏完成")
        progress(step=4, total=4, message=message, stage="完成", current=1, stage_total=1, log_append=message)
        return {"ok": True, "message": message, "result": result, "log": sanitize_log(message)}

    if action == "refresh_watch":
        log_lines: list[str] = []
        progress(step=0, total=REFRESH_WATCH_TOTAL, message="准备扫描监控用户动态…")
        _raise_if_cancelled(cancel_event)

        def on_watch_progress(done: int, total: int, message: str) -> None:
            _raise_if_cancelled(cancel_event)
            progress(step=1, total=REFRESH_WATCH_TOTAL, message=message)

        with log_span("watch_scan", logger=logger, component="pipeline", phase="watch_scan"):
            watch_result = sync_watch_forwards(on_progress=on_watch_progress)
        _raise_if_cancelled(cancel_event)
        watch_line = (
            f"【监控扫描】窗口内 {watch_result.link_count} 条活动链接，"
            f"成功扫描 {watch_result.users_ok}/{watch_result.users_total} 人"
        )
        log_lines.append(watch_line)
        progress(
            step=1,
            total=REFRESH_WATCH_TOTAL,
            message=f"扫描完成：窗口 {watch_result.link_count} 条链接",
            log_append=watch_line,
        )

        pipeline_step = 2
        progress(step=pipeline_step, total=REFRESH_WATCH_TOTAL, message="正在分类新链接…")
        _raise_if_cancelled(cancel_event)

        pipeline_spans = PhaseSpanTracker(logger=logger, component="pipeline")
        pipeline_error: str | None = None
        try:
            pipeline_result = run_new_links_pipeline(
                watch_result.activity_links,
                on_progress=_make_refresh_watch_pipeline_progress(
                    progress,
                    span_tracker=pipeline_spans,
                ),
                source_observations=[
                    {
                        "source_id": "refresh_watch",
                        "dynamic_id": link,
                        "page_index": None,
                    }
                    for link in watch_result.activity_links
                ],
            )
        except Exception as exc:
            pipeline_error = type(exc).__name__
            raise
        finally:
            pipeline_spans.close(error_kind=pipeline_error)
        _raise_if_cancelled(cancel_event)
        invalidate_activity_cache()
        for line in _pipeline_log_lines(pipeline_result):
            log_lines.append(line)
        progress(
            step=REFRESH_WATCH_TOTAL,
            total=REFRESH_WATCH_TOTAL,
            message=pipeline_result.message or "流水线完成",
            log_append=log_lines[-1] if log_lines else "",
        )

        if pipeline_result.partial:
            # V21：partial 时保留监控窗口 checkpoint，下一轮重扫同一窗口；
            # 已成功结果由 DB 去重与 classification cache 挡下。
            log_lines.append(
                "【部分完成】LLM 服务异常，未推进监控窗口检查点；剩余候选将在后续刷新继续"
            )
        else:
            set_watch_last_synced_at(watch_result.synced_at)
        save_watch_result(watch_result)
        set_last_pipeline_persisted(
            action="refresh_watch",
            persisted_count=pipeline_result.persisted_count,
            synced_at=watch_result.synced_at,
        )

        summary_parts = [
            f"扫描 {watch_result.users_total} 人，提取 {watch_result.link_count} 条链接",
            f"新链接 {pipeline_result.new_link_count} 条",
            f"新入库 {pipeline_result.persisted_count} 条",
        ]
        if pipeline_result.partial:
            summary_parts.append("部分完成（LLM 服务异常，剩余候选待后续刷新）")
        logger.info(
            "监控用户更新完成：窗口 %s 条，入库 %s",
            watch_result.link_count,
            pipeline_result.persisted_count,
        )
        return {
            "ok": True,
            "message": "监控用户动态更新完成：" + "，".join(summary_parts),
            "result": {
                "watch": watch_result.to_dict(),
                "pipeline": pipeline_result.to_dict(),
            },
            "log": sanitize_log("\n".join(log_lines).strip()),
        }

    if action == "refresh_status":
        progress(step=0, total=1, message="正在刷新活动状态…")
        _raise_if_cancelled(cancel_event)
        with log_span(
            "status_refresh",
            logger=logger,
            component="pipeline",
            phase="status_refresh",
        ):
            result = refresh_local_activity_statuses()
        _raise_if_cancelled(cancel_event)
        invalidate_activity_cache()
        if result.get("skipped"):
            message = "没有需要刷新的进行中活动"
        else:
            message = (
                f"状态刷新完成：检查 {result.get('scanned', 0)} 条，"
                f"更新 {result.get('updated', 0)} 条，"
                f"新结束 {result.get('ended_marked', 0)} 条，"
                f"即将开奖 {result.get('soon_marked', 0)} 条"
            )
        progress(step=1, total=1, message=message, log_append=message)
        logger.info(
            "刷新任务状态完成：检查 %s，更新 %s",
            result.get("scanned", 0),
            result.get("updated", 0),
        )
        return {
            "ok": True,
            "message": message,
            "result": result,
            "log": sanitize_log(message),
        }

    if action == "sync_repost_history":
        from src.repost_cleanup import sync_repost_history

        if params:
            raise ValueError("同步个人转发历史不接受额外参数")
        progress(step=0, total=1, message="正在核对当前账号并读取个人动态…")
        result = sync_repost_history(
            on_progress=_cleanup_progress_adapter(progress, cancel_event, log_stride=10),
            cancel_check=(lambda: bool(cancel_event and cancel_event.is_set())),
            enforce_request_budget=False,
        )
        _raise_if_cancelled(cancel_event)
        imported = int(result.get("imported_count") or result.get("imported") or 0)
        discovered = int(result.get("found_reposts") or result.get("discovered") or 0)
        if result.get("budget_exhausted"):
            message = str(
                result.get("message")
                or "Bilibili 请求预算已用完，同步已安全暂停；额度恢复后请手动再次执行，系统会从已有进度继续"
            )
        else:
            message = f"转发历史同步完成：发现 {discovered} 条，本次新增 {imported} 条"
        reconciliation = result.get("guard_reconciliation")
        resolved = (
            int(reconciliation.get("resolved") or 0)
            if isinstance(reconciliation, dict)
            else 0
        )
        if resolved:
            message += f"；已根据本地转发历史自动确认 {resolved} 条历史参与记录"
        progress(step=1, total=1, message=message, log_append=message)
        return {"ok": True, "message": message, "result": result, "log": sanitize_log(message)}

    if action == "sync_assess_repost_history":
        # 页面主流程：同一个 Job、同一层 WriterLease 内先同步，再运行 M4 唯一评估引擎。
        # 保留独立 sync / assessment action 作为兼容入口，但不从前端要求用户点两次。
        from src.repost_cleanup import scan_expired_reposts, sync_repost_history

        if params:
            raise ValueError("同步/评估转发历史不接受额外参数")
        _raise_if_cancelled(cancel_event)
        progress(step=0, total=2, message="正在同步转发历史…", log_append="同步转发历史开始")

        def sync_progress(**event: Any) -> None:
            progress(
                step=0,
                total=2,
                message=str(event.get("message") or "正在同步转发历史…"),
                stage="同步转发历史",
                current=int(event.get("step") or 0),
                log_append=event.get("log_append"),
            )

        synced = sync_repost_history(
            on_progress=_cleanup_progress_adapter(sync_progress, cancel_event, log_stride=10),
            cancel_check=(lambda: bool(cancel_event and cancel_event.is_set())),
            enforce_request_budget=False,
        )
        _raise_if_cancelled(cancel_event)
        if synced.get("budget_exhausted"):
            # V22：同步已耗尽预算 → 0 assessment 请求，返回结构化 paused 结果。
            message = str(
                synced.get("message")
                or "Bilibili 请求预算已用完，本轮同步/评估已安全暂停；额度恢复后请手动再次执行，系统会从已有进度继续"
            )
            progress(step=2, total=2, message=message, log_append=message)
            return {
                "ok": True,
                "message": message,
                "result": {
                    "sync": synced,
                    "assessment": {
                        "skipped": True,
                        "reason": "request_budget_exhausted",
                        "message": message,
                        "budget_exhausted": True,
                        "partial": True,
                    },
                },
                "log": sanitize_log(f"同步转发历史完成\n{message}"),
            }
        progress(step=1, total=2, message="同步完成，正在连续智能评估…", log_append="同步转发历史完成；智能评估开始")
        assessed = scan_expired_reposts(
            on_progress=_cleanup_progress_adapter(progress, cancel_event, log_stride=10),
            cancel_check=(lambda: bool(cancel_event and cancel_event.is_set())),
            enforce_request_budget=False,
        )
        _raise_if_cancelled(cancel_event)
        _raise_assessment_platform_risk(assessed)
        message = str(assessed.get("message") or "同步/评估转发历史完成")
        progress(step=2, total=2, message=message, log_append=message)
        return {
            "ok": True,
            "message": message,
            "result": {"sync": synced, "assessment": assessed},
            "log": sanitize_log(f"同步转发历史完成\n{message}"),
        }

    if action == "cleanup_auto_maintain":
        from src.repost_cleanup import auto_maintain

        if params:
            raise ValueError("自动维护不接受额外参数")
        progress(step=0, total=1, message="正在执行清理数据自动维护…")
        result = auto_maintain(
            on_progress=_cleanup_progress_adapter(progress, cancel_event),
            cancel_check=(lambda: bool(cancel_event and cancel_event.is_set())),
        )
        _raise_if_cancelled(cancel_event)
        message = str(result.get("message") or "自动维护完成")
        progress(step=1, total=1, message=message, log_append=message)
        return {"ok": True, "message": message, "result": result, "log": sanitize_log(message)}

    if action == "cleanup_auto_delete":
        # M6：strict-safe 自动清理（每批 ≤20 自动连续处理 frozen scope）。
        # 开关默认关闭；0 DELETE 状态/开关关闭时返回原因而非报错。WriterLease 由外层 wrapper 提供。
        from src.repost_cleanup import auto_cleanup_delete

        if params:
            raise ValueError("自动安全清理不接受额外参数")
        _raise_if_cancelled(cancel_event)
        progress(step=0, total=1, message="正在执行 strict-safe 自动安全清理…")
        result = auto_cleanup_delete(
            on_progress=_cleanup_progress_adapter(progress, cancel_event),
            cancel_check=(lambda: bool(cancel_event and cancel_event.is_set())),
        )
        _raise_if_cancelled(cancel_event)
        message = str(result.get("message") or "自动安全清理完成")
        progress(step=1, total=1, message=message, log_append=message)
        return {"ok": True, "message": message, "result": result, "log": sanitize_log(message)}

    if action == "cleanup_maintenance_cycle":
        # AutoScheduler 的 M8 单 Job 编排入口；直接调用核心函数，绝不嵌套 run_action/WriterLease。
        from src.repost_cleanup import cleanup_maintenance_cycle

        if params:
            raise ValueError("清理维护周期不接受额外参数")
        _raise_if_cancelled(cancel_event)
        result = cleanup_maintenance_cycle(
            on_progress=_cleanup_progress_adapter(progress, cancel_event),
            cancel_check=(lambda: bool(cancel_event and cancel_event.is_set())),
        )
        _raise_if_cancelled(cancel_event)
        # V24：任一阶段即使以结构化 result 返回正式风控（如身份校验路径），
        # 也必须走统一风险上抛，不能降级成 ok=True/普通 success。
        phases = result.get("phases") if isinstance(result, dict) else None
        if isinstance(phases, list):
            for phase in phases:
                if not isinstance(phase, dict):
                    continue
                stage_result = phase.get("result")
                if isinstance(stage_result, dict):
                    _raise_assessment_platform_risk(stage_result)
        message = str(result.get("message") or "清理维护周期完成")
        return {"ok": True, "message": message, "result": result, "log": sanitize_log(message)}

    if action == "scan_expired_reposts":
        from src.repost_cleanup import scan_expired_reposts

        force_original_ids = params.get("force_original_ids") if params else None
        progress(step=0, total=1, message="正在串行评估历史抽奖…")
        result = scan_expired_reposts(
            on_progress=_cleanup_progress_adapter(progress, cancel_event, log_stride=10),
            cancel_check=(lambda: bool(cancel_event and cancel_event.is_set())),
            force_original_ids=force_original_ids,
            enforce_request_budget=False,
        )
        _raise_if_cancelled(cancel_event)
        _raise_assessment_platform_risk(result)
        candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
        message = str(result.get("message") or "历史抽奖评估完成")
        progress(step=1, total=1, message=message, log_append=message)
        return {"ok": True, "message": message, "result": result, "log": sanitize_log(message)}

    if action == "delete_expired_reposts":
        from src.repost_cleanup import delete_reposts

        raw_ids = params.get("repost_dynamic_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("没有选择要删除的转发动态")
        repost_ids: list[str] = []
        seen_ids: set[str] = set()
        for raw_id in raw_ids:
            repost_id = str(raw_id or "").strip()
            if not is_valid_dynamic_id(repost_id):
                raise ValueError("转发动态 ID 无效")
            if repost_id not in seen_ids:
                seen_ids.add(repost_id)
                repost_ids.append(repost_id)
        if len(repost_ids) > 20:
            raise ValueError("单次最多删除 20 条转发动态")
        progress(step=0, total=len(repost_ids), message=f"准备逐条验证并删除 {len(repost_ids)} 条转发动态…")
        result = delete_reposts(
            repost_ids,
            on_progress=_cleanup_progress_adapter(progress, cancel_event),
            cancel_check=(lambda: bool(cancel_event and cancel_event.is_set())),
            manual_review_confirmed=bool(params.get("manual_review_confirmed")),
        )
        _raise_if_cancelled(cancel_event)
        deleted = int(result.get("deleted_count") or result.get("deleted") or 0)
        failed = int(result.get("failed_count") or result.get("delete_failed") or 0)
        unknown = int(result.get("unknown_count") or result.get("unknown") or 0)
        message = f"删除处理完成：成功 {deleted} 条，失败 {failed} 条，结果未知 {unknown} 条"
        progress(step=len(repost_ids), total=len(repost_ids), message=message, log_append=message)
        return {"ok": True, "message": message, "result": result, "log": sanitize_log(message)}

    if action == "participate":
        dynamic_id = str(params.get("dynamic_id") or "").strip()
        if not is_valid_dynamic_id(dynamic_id):
            raise ValueError("活动 ID 无效")
        _raise_if_cancelled(cancel_event)
        try:
            lottery_type = lookup_lottery_type(dynamic_id)
        except RuntimeError as exc:
            logger.warning("参与失败：未找到活动类型 %s — %s", dynamic_id, exc)
            raise ValueError(f"未找到活动 {dynamic_id} 的类型信息，请先一键更新") from exc
        total_steps = participate_step_budget(lottery_type, dynamic_id=dynamic_id)
        progress(step=0, total=total_steps, message="正在检查活动状态…", log_append="正在打开活动链接检查状态…")
        logger.debug("开始参与活动 %s (%s)", dynamic_id, lottery_type)

        def on_step(step: int, total: int, message: str, _action_name: str) -> None:
            _raise_if_cancelled(cancel_event)
            progress(step=step, total=total, message=message, log_append=message)

        _raise_if_cancelled(cancel_event)
        _breaker_token = bind_participation_risk_breaker(ParticipationRiskBreaker())
        try:
            payload = _participate_dynamic_payload(
                dynamic_id,
                on_step,
                lottery_type_hint=lottery_type,
            )
        finally:
            reset_participation_risk_breaker(_breaker_token)

        if _is_participation_dedup_skip(payload):
            message = str(payload.get("message") or "参与已跳过")
            progress(step=total_steps, total=total_steps, message=message, log_append=message)
            invalidate_activity_cache()
            return {"ok": True, "message": message, "result": payload, "log": format_participation_log(payload)}

        refresh_local_activity_statuses()
        logger.debug("参与活动成功 %s", dynamic_id)
        action_log = format_participation_log(payload)
        invalidate_activity_cache()
        return {
            "ok": True,
            "message": str(payload.get("message") or "参与成功"),
            "result": payload,
            "log": action_log,
        }

    if action == "participate_triple":
        _raise_if_cancelled(cancel_event)

        filters = _list_filter_params(params)
        targets = pick_triple_participate_targets(**filters)
        from_auto = bool(params.get("from_auto"))
        if not targets:
            return {
                "ok": True,
                "message": "当前没有可参与的未参加活动，已跳过",
                "log": "无可参与目标，本次三连参与已跳过",
                "result": {
                    "skipped": True,
                    "from_auto": from_auto,
                    "joined": 0,
                    "failed": 0,
                    "unknown": 0,
                    "not_attempted": 0,
                    "skipped_count": 0,
                    "stop_reason": None,
                    "items": [],
                    "targets": [],
                },
            }

        target_titles = [str(item.get("activity_title") or item.get("dynamic_id") or "") for item in targets]
        total_steps, progress_plan = build_triple_progress_plan(targets)
        if total_steps <= 0:
            return {
                "ok": True,
                "message": "当前没有可参与的未参加活动，已跳过",
                "log": "进度计划为空，本次三连参与已跳过",
                "result": {
                    "skipped": True,
                    "from_auto": from_auto,
                    "joined": 0,
                    "failed": 0,
                    "unknown": 0,
                    "not_attempted": 0,
                    "skipped_count": 0,
                    "stop_reason": None,
                    "items": [],
                    "targets": [],
                },
            }

        logger.debug("三连参与开始：%s", ", ".join(target_titles))
        triple_target_items = build_triple_target_preview(targets)["items"]
        progress(
            step=0,
            total=total_steps,
            message=f"准备并行三连参与 {len(targets)} 个活动…",
            log_append="目标活动：" + "、".join(target_titles),
            result_patch={"targets": triple_target_items},
        )

        progress_lock = threading.Lock()
        task_states: dict[str, str] = {
            str(item.get("dynamic_id") or ""): "等待开始…" for item in targets
        }
        task_step_progress: dict[str, int] = {
            str(item.get("dynamic_id") or ""): 0 for item in targets
        }
        completed_target_ids: set[str] = set()
        target_ids = [str(item.get("dynamic_id") or "") for item in targets]

        def _overall_progress_step() -> int:
            total_done = 0
            for dynamic_id in target_ids:
                plan_entry = progress_plan.get(dynamic_id)
                if not plan_entry:
                    continue
                _, budget = plan_entry
                total_done += min(task_step_progress.get(dynamic_id, 0), budget)
            return min(total_done, total_steps)

        def _emit_triple_progress(*, log_append: str | None = None) -> None:
            progress(
                step=_overall_progress_step(),
                total=total_steps,
                message=_progress_snapshot(),
                log_append=log_append,
            )

        def _progress_snapshot() -> str:
            parts: list[str] = []
            for target in targets:
                dynamic_id = str(target.get("dynamic_id") or "")
                title = str(target.get("activity_title") or dynamic_id[-6:])
                parts.append(f"{title}: {task_states.get(dynamic_id, '等待开始…')}")
            return " | ".join(parts)

        def _report_task_progress(dynamic_id: str, step: int, _total: int, message: str) -> None:
            if cancel_event and cancel_event.is_set():
                raise ValueError("任务已取消")
            if dynamic_id not in progress_plan:
                return
            with progress_lock:
                task_states[dynamic_id] = message
                task_step_progress[dynamic_id] = max(task_step_progress.get(dynamic_id, 0), step)
            title = next(
                (
                    str(item.get("activity_title") or dynamic_id)
                    for item in targets
                    if str(item.get("dynamic_id") or "") == dynamic_id
                ),
                dynamic_id,
            )
            _emit_triple_progress(log_append=f"{title}：{message}")

        def _mark_other_tasks_stopped(failed_id: str, *, reason: str) -> None:
            skip_markers = ("失败", "参与成功", "完成", "成功", "已停止", "已取消")
            with progress_lock:
                for other_id in target_ids:
                    if other_id in completed_target_ids or (failed_id and other_id == failed_id):
                        continue
                    state = task_states.get(other_id, "")
                    if any(marker in state for marker in skip_markers):
                        continue
                    task_states[other_id] = reason

        def _participate_triple_target(target: dict[str, Any]) -> dict[str, Any]:
            if cancel_event and cancel_event.is_set():
                raise ValueError("任务已取消")
            active_breaker = current_participation_risk_breaker()
            if active_breaker is not None:
                # 兄弟 worker 已命中平台风控：不再开始本目标的远程动作。
                active_breaker.check()

            dynamic_id = str(target.get("dynamic_id") or "")
            title = str(target.get("activity_title") or dynamic_id)
            target_lottery_type = str(target.get("lottery_type") or "").strip() or None

            with progress_lock:
                task_states[dynamic_id] = "正在检查活动状态…"
            _emit_triple_progress(log_append=f"{title}：正在检查活动状态…")

            def on_step(step: int, total: int, message: str, _action_name: str) -> None:
                _report_task_progress(dynamic_id, step, total, message)

            try:
                payload = _participate_dynamic_payload(
                    dynamic_id,
                    on_step,
                    lottery_type_hint=target_lottery_type,
                )
            except ParticipationItemFailure as exc:
                payload = dict(exc.payload)
                result_class = str(
                    payload.get("result_class")
                    or (payload.get("context_snapshot") or {}).get("result_class")
                    or "failed_local"
                )
                if result_class not in {"invalid_activity", "unknown", "failed_local"}:
                    result_class = "failed_local"
                payload["result_class"] = result_class
                if result_class != "invalid_activity":
                    _persist_triple_failure(payload)
            except Exception as exc:
                global_stop = _triple_global_stop_reason(exc, cancel_event)
                if global_stop is not None:
                    if active_breaker is not None and global_stop != "user_cancel":
                        if global_stop == "platform_risk":
                            code = _risk_code_of(exc)
                            if code is not None:
                                active_breaker.trigger(code)
                        else:
                            active_breaker.trigger_global_stop(exc)
                    raise
                result_class = "unknown" if _triple_transport_unknown(exc) else "failed_local"
                payload = _triple_failure_payload(
                    dynamic_id=dynamic_id,
                    lottery_type=target_lottery_type or "",
                    message=str(exc),
                    result_class=result_class,
                )
                _persist_triple_failure(payload)

            skipped = _is_participation_dedup_skip(payload)
            result_class = str(payload.get("result_class") or "")
            if skipped:
                result_class = "skipped"
            elif not result_class:
                result_class = "joined" if str(payload.get("status") or "") == "joined" else "failed_local"
            payload["result_class"] = result_class
            with progress_lock:
                if skipped:
                    state_message = str(payload.get("message") or "已跳过")
                elif result_class == "deferred_comment_limit" or payload.get("status") == "deferred":
                    state_message = str(payload.get("message") or "评论发送已暂停")
                elif result_class == "unknown":
                    state_message = str(payload.get("message") or "结果未知，已锁定")
                elif result_class == "failed_local" or payload.get("status") == "failed":
                    state_message = str(payload.get("message") or "单项失败")
                else:
                    state_message = str(payload.get("message") or "参与成功")
                task_states[dynamic_id] = state_message
                completed_target_ids.add(dynamic_id)
            plan_entry = progress_plan.get(dynamic_id)
            if plan_entry:
                _, budget = plan_entry
                task_step_progress[dynamic_id] = budget
            _emit_triple_progress(log_append=f"{title}：{task_states[dynamic_id]}")
            return {
                "dynamic_id": dynamic_id,
                "activity_title": title,
                "lottery_type": str(payload.get("lottery_type") or target.get("lottery_type") or ""),
                "payload": payload,
                "result_class": result_class,
            }

        results: list[dict[str, Any]] = []
        _emit_triple_progress()
        breaker = ParticipationRiskBreaker()
        _breaker_token = bind_participation_risk_breaker(breaker)
        try:
            with ThreadPoolExecutor(max_workers=PARTICIPATE_TRIPLE_WORKERS) as executor:
                # 每个 future 各自 copy_context：同一 Context 不能被多线程并发 enter
                future_to_id = {
                    executor.submit(
                        contextvars.copy_context().run,
                        _participate_triple_target,
                        target,
                    ): str(target.get("dynamic_id") or "")
                    for target in targets
                }
                global_failures: dict[str, Exception] = {}
                try:
                    for future in as_completed(future_to_id):
                        dynamic_id = future_to_id[future]
                        if future.cancelled():
                            continue
                        if cancel_event and cancel_event.is_set():
                            raise ValueError("任务已取消")
                        try:
                            results.append(future.result())
                        except Exception as exc:
                            global_stop = _triple_global_stop_reason(exc, cancel_event)
                            if global_stop is None:
                                # Worker-level exceptions are converted inside
                                # _participate_triple_target. Keep this guard
                                # for an unexpected wrapper so one bad item
                                # still cannot cancel its siblings.
                                payload = _triple_failure_payload(
                                    dynamic_id=dynamic_id,
                                    lottery_type="",
                                    message=str(exc),
                                    result_class=(
                                        "unknown" if _triple_transport_unknown(exc) else "failed_local"
                                    ),
                                )
                                _persist_triple_failure(payload)
                                results.append({
                                    "dynamic_id": dynamic_id,
                                    "activity_title": dynamic_id,
                                    "lottery_type": "",
                                    "payload": payload,
                                    "result_class": payload["result_class"],
                                })
                                with progress_lock:
                                    task_states[dynamic_id] = str(payload["message"])
                                completed_target_ids.add(dynamic_id)
                                _emit_triple_progress(log_append=f"{dynamic_id}：{payload['message']}")
                            else:
                                global_failures[dynamic_id] = exc
                                # Future.cancel() only removes work that has
                                # not started. Running workers remain governed
                                # by the shared risk breaker before mutation.
                                for pending in future_to_id:
                                    if pending is not future:
                                        pending.cancel()
                                with progress_lock:
                                    task_states[dynamic_id] = f"已停止：{global_stop}"
                                _emit_triple_progress(log_append=f"{dynamic_id}：{global_stop}")
                finally:
                    if cancel_event and cancel_event.is_set():
                        _emit_triple_progress()
                if global_failures:
                    # Risk has priority when multiple workers observe the same
                    # breaker. Do not set cancel_event here: that event belongs
                    # exclusively to user/scheduler cancellation.
                    risk_error = next(
                        (exc for exc in global_failures.values() if _risk_code_of(exc) is not None),
                        None,
                    )
                    raise risk_error if risk_error is not None else next(iter(global_failures.values()))
        finally:
            reset_participation_risk_breaker(_breaker_token)

        results.sort(key=lambda item: target_ids.index(item["dynamic_id"]))
        log_blocks = [format_participation_log(item["payload"]) for item in results]
        skipped_count = sum(_is_participation_dedup_skip(item["payload"]) for item in results)
        unknown_count = sum(item.get("result_class") == "unknown" for item in results)
        deferred_count = sum(item.get("result_class") == "deferred_comment_limit" for item in results)
        deleted_count = sum(item.get("result_class") == "invalid_activity" for item in results)
        requeued_count = sum(
            bool(
                item.get("result_class") == "failed_local"
                and isinstance(item.get("payload", {}).get("context_snapshot"), dict)
                and item["payload"]["context_snapshot"].get("requeue")
            )
            for item in results
        )
        failed_count = sum(
            item.get("result_class") == "failed_local" and not (
                isinstance(item.get("payload", {}).get("context_snapshot"), dict)
                and item["payload"]["context_snapshot"].get("requeue")
            )
            for item in results
        )
        joined_count = sum(item.get("result_class") == "joined" for item in results)
        if joined_count:
            refresh_local_activity_statuses()
        message = (
            f"三连参与完成：成功 {joined_count} 个，删除 {deleted_count} 个，"
            f"重新排队 {requeued_count} 个，失败 {failed_count} 个，"
            f"暂缓 {deferred_count} 个，跳过 {skipped_count} 个，结果未知 {unknown_count} 个"
        )
        detail_message = f"{message} | {_progress_snapshot()}"
        progress(step=total_steps, total=total_steps, message=detail_message, log_append=message)
        logger.debug(
            "三连参与完成 joined=%s skipped=%s failed=%s unknown=%s",
            joined_count,
            skipped_count,
            failed_count,
            unknown_count,
        )
        invalidate_activity_cache()
        return {
            "ok": True,
            "message": detail_message,
            "result": {
                "targets": [
                    {
                        "dynamic_id": item["dynamic_id"],
                        "activity_title": item["activity_title"],
                        "lottery_type": item["lottery_type"],
                    }
                    for item in results
                ],
                "items": [item["payload"] for item in results],
                "joined": joined_count,
                "skipped_count": skipped_count,
                "failed": failed_count,
                "deleted": deleted_count,
                "requeued": requeued_count,
                "unknown": unknown_count,
                "not_attempted": 0,
                "stop_reason": None,
                "skipped": bool(results) and skipped_count == len(results),
                "from_auto": from_auto,
            },
            "log": "\n\n".join(log_blocks).strip(),
        }

    raise ValueError(f"未知操作: {action}")
