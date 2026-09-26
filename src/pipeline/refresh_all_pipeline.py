from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal, Mapping

from src.activity_store import append_activities, known_activity_ids
from src.app_logging import get_logger
from src.bilibili_client import BilibiliClient
from src.canonical_identity import CanonicalIdentity, resolve_dynamic_identity
from src.llm_resilience import (
    LlmProviderBreaker,
    LlmProviderUnavailable,
    bind_llm_provider_breaker,
    is_config_failure,
    is_provider_failure,
    reset_llm_provider_breaker,
)
from src.lottery_enricher import (
    ENRICH_SKIP_REASON,
    EnrichedActivity,
    EnrichSkippedError,
    enrich_activity,
    is_enrich_detail_skip_error,
)
from src.lottery_classifier import LotteryType
from src.participation_store import load_participations
from src.pipeline.classify_step import ClassifyOutcome, classify_new_link
from src.pipeline.status_step import apply_initial_status
from src.sources.common import CheckResult, normalize_activity_id
from src.dead_links import is_http_not_found
from src.invalid_activities import invalid_activity_ids

logger = get_logger("pipeline")

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class PipelineResult:
    ok: bool
    pipeline_skipped: bool
    raw_link_count: int
    new_link_count: int
    classified_count: int
    skipped_count: int
    enriched_count: int
    persisted_count: int
    skip_reasons: dict[str, int] = field(default_factory=dict)
    invalid_link_count: int = 0
    duplicate_link_count: int = 0
    existing_count: int = 0
    non_lottery_count: int = 0
    other_skipped_count: int = 0
    failed_count: int = 0
    persist_skipped_count: int = 0
    expired_skipped_count: int = 0
    message: str = ""
    # V21：LLM provider 单候选隔离 / breaker / partial 语义（未完成候选后续可恢复）。
    partial: bool = False
    provider_unavailable: bool = False
    config_unavailable: bool = False
    provider_failed_count: int = 0
    unprocessed_count: int = 0
    partial_reason: str = ""

    def to_dict(self) -> dict:
        payload = asdict(self)
        # 面向 Job/UI 使用更不易混淆的名称，同时保留原字段向后兼容。
        payload["discovered_count"] = self.raw_link_count
        payload["candidate_count"] = self.new_link_count
        return payload

    @property
    def discovered_count(self) -> int:
        return self.raw_link_count

    @property
    def candidate_count(self) -> int:
        return self.new_link_count


@dataclass(frozen=True)
class _DedupeResult:
    dynamic_ids: list[str]
    invalid_link_count: int
    duplicate_link_count: int
    existing_count: int


_FAILED_SKIP_REASONS = frozenset({"链接失效", "正文不可读取", ENRICH_SKIP_REASON})


def _skip_breakdown(skip_reasons: Mapping[str, int]) -> tuple[int, int, int]:
    non_lottery = int(skip_reasons.get("非抽奖活动", 0) or 0)
    failed = sum(int(skip_reasons.get(reason, 0) or 0) for reason in _FAILED_SKIP_REASONS)
    total = sum(int(count or 0) for count in skip_reasons.values())
    other = max(0, total - non_lottery - failed)
    return non_lottery, other, failed


def is_expired_before_scan(activity: Mapping[str, Any], scan_started_at: int | None) -> bool:
    """判断活动是否「可靠地」在本次扫描开始前已经结束。

    保守策略（宁可多保留，不可误过滤）：
    - 只有官方互动/预约抽奖且 business_type > 0（即有官方 lottery_notice 支撑）才可能被过滤；
    - 开奖时间必须来自官方结构化 notice，而不是 inferred / LLM / 文本推算；
    - 只有严格满足 reliable_end_time < scan_started_at 才返回 True；
    - reliable_end_time == scan_started_at 或无法证明时返回 False（保留）。
    """
    if not scan_started_at:
        return False
    try:
        started_at = int(scan_started_at)
    except (TypeError, ValueError):
        return False
    if started_at <= 0:
        return False
    lottery_type = str(activity.get("lottery_type") or "")
    if lottery_type not in ("互动抽奖", "预约抽奖"):
        return False
    try:
        business_type = int(activity.get("business_type") or 0)
    except (TypeError, ValueError):
        business_type = 0
    if business_type <= 0:
        return False
    conditions = activity.get("conditions") or {}
    if conditions.get("lottery_time_inferred") is True:
        return False
    try:
        reliable_end_time = int(activity.get("lottery_time") or 0)
    except (TypeError, ValueError):
        reliable_end_time = 0
    if reliable_end_time <= 0:
        return False
    return reliable_end_time < started_at


def _dedupe_new_dynamic_ids(raw_urls: list[str]) -> _DedupeResult:
    """一次本地遍历完成标准化/批内去重/数据库已有过滤，并保留严格计数。"""
    known = known_activity_ids()
    seen: set[str] = set()
    ordered: list[str] = []
    invalid_link_count = 0
    duplicate_link_count = 0
    existing_count = 0
    for raw_url in raw_urls:
        dynamic_id = normalize_activity_id(str(raw_url))
        if not dynamic_id:
            invalid_link_count += 1
            continue
        if dynamic_id in seen:
            duplicate_link_count += 1
            continue
        seen.add(dynamic_id)
        if dynamic_id in known:
            existing_count += 1
            continue
        ordered.append(dynamic_id)
    return _DedupeResult(
        dynamic_ids=ordered,
        invalid_link_count=invalid_link_count,
        duplicate_link_count=duplicate_link_count,
        existing_count=existing_count,
    )


def _collect_updated_links(ds_results: list[CheckResult]) -> list[str]:
    links: list[str] = []
    for result in ds_results:
        if not result.updated:
            continue
        links.extend(result.activity_links or [])
    return links


def run_new_links_pipeline(
    raw_urls: list[str],
    *,
    workers: int = 4,
    on_progress: ProgressCallback | None = None,
    scan_started_at: int | None = None,
    identity_by_raw: Mapping[str, CanonicalIdentity] | None = None,
) -> PipelineResult:
    """Step 2～5：仅处理新链接（内存流转，末步落库）。

    scan_started_at 不为 None 时，会在入库前过滤「可可靠证明在扫描开始前已结束」的官方活动。
    0 额外 Bilibili 请求：只复用本流水线已抓取的 official lottery_notice 结构化时间。
    """
    dedupe = _dedupe_new_dynamic_ids(raw_urls)
    dynamic_ids = dedupe.dynamic_ids
    invalid_ids = invalid_activity_ids()
    skipped_invalid = sum(dynamic_id in invalid_ids for dynamic_id in dynamic_ids)
    if invalid_ids:
        dynamic_ids = [dynamic_id for dynamic_id in dynamic_ids if dynamic_id not in invalid_ids]
    skip_reasons: dict[str, int] = {"invalid_activity": skipped_invalid} if skipped_invalid else {}

    if not dynamic_ids:
        return PipelineResult(
            ok=True,
            pipeline_skipped=True,
            raw_link_count=len(raw_urls),
            new_link_count=0,
            classified_count=0,
            skipped_count=sum(skip_reasons.values()),
            enriched_count=0,
            persisted_count=0,
            invalid_link_count=dedupe.invalid_link_count,
            duplicate_link_count=dedupe.duplicate_link_count,
            existing_count=dedupe.existing_count,
            skip_reasons=skip_reasons,
            message="无新链接，流水线结束",
        )

    participations = load_participations()
    # Keep old raw wrapper evidence fail-closed while new canonical rows are
    # introduced.  All of these stores are Profile-local.
    protected_ids: set[str] = {
        dynamic_id
        for dynamic_id, record in participations.items()
        if getattr(record, "user_status", "") == "已参加"
    }
    try:
        from src.db.uids import participation_uid
        from src.participation_guard import list_confirmed_guards, load_blocked_guard_ids

        current_participation_uid = participation_uid()
        protected_ids.update(load_blocked_guard_ids(current_participation_uid))
        protected_ids.update(
            record.dynamic_id
            for record in list_confirmed_guards(current_participation_uid)
        )
    except Exception:
        # A missing legacy guard table is handled by schema bootstrap; a read
        # failure must not turn a source refresh into an unsafe auto-entry.
        logger.debug("读取参与保护状态失败，继续使用可用的本地保护证据", exc_info=True)
    tasks: list[tuple[str, LotteryType]] = []
    classify_outcome_by_id: dict[str, ClassifyOutcome] = {}
    total = len(dynamic_ids)

    # V21：单候选 LLM 异常隔离 + provider breaker。作用域=当前 pipeline execution，
    # 不写全局状态、不触发 Bilibili 风控/冷却；下一次 Job 重新创建 breaker。
    breaker = LlmProviderBreaker()
    provider_failed_count = 0
    completed_count = 0
    partial_reason = ""

    if on_progress:
        on_progress(0, total, f"正在分类 {total} 条新链接…")

    classify_token = bind_llm_provider_breaker(breaker)
    try:
        with BilibiliClient() as shared_client:
            done = 0

            for dynamic_id in dynamic_ids:
                if breaker.opened:
                    break
                try:
                    outcome = classify_new_link(shared_client, dynamic_id)

                except Exception as exc:
                    # 明确的 404 / Not Found：
                    # 当前动态已经删除或不可访问，只跳过这一条。
                    if is_http_not_found(exc):
                        reason = "链接失效"

                    # 动态没有被 API 明确判定删除，
                    # 但所有正文抓取路径都失败。
                    elif (
                        isinstance(exc, RuntimeError)
                        and str(exc).startswith("无法获取动态正文:")
                    ):
                        reason = "正文不可读取"

                    # Config/Auth：第一次明确失败即停止当前源（不等连续 3 次），
                    # 不保存业务 final、不写 Bilibili 冷却。
                    elif is_config_failure(exc):
                        breaker.note_config_failure(exc)
                        partial_reason = breaker.stop_reason
                        done += 1
                        logger.warning(
                            "LLM 配置不可用，停止当前源 candidate=%s category=%s",
                            dynamic_id,
                            type(exc).__name__,
                        )
                        if on_progress:
                            on_progress(done, total, "LLM 配置不可用，已停止当前源")
                        break

                    # 单候选 temporary/protocol 失败：记录可恢复失败并继续下一个；
                    # 连续达到阈值则打开 breaker，当前源停止后续 LLM。
                    elif is_provider_failure(exc):
                        breaker.note_provider_failure(exc)
                        provider_failed_count += 1
                        done += 1
                        logger.warning(
                            "LLM provider 候选异常 candidate=%s category=%s streak=%s/%s",
                            dynamic_id,
                            type(exc).__name__,
                            breaker.consecutive_failures,
                            breaker.threshold,
                        )
                        if on_progress:
                            on_progress(done, total, f"LLM 服务异常，已跳过 {dynamic_id}")
                        if breaker.opened:
                            partial_reason = breaker.stop_reason
                            logger.warning(
                                "LLM provider breaker 打开 streak=%s success=%s "
                                "completed=%s provider_failed=%s remaining=%s",
                                breaker.consecutive_failures,
                                breaker.success_count,
                                completed_count,
                                provider_failed_count,
                                max(0, total - completed_count - provider_failed_count),
                            )
                            break
                        continue

                    # 其他异常不能吞掉（含 Bilibili 正式风控 -352/-509/429）
                    else:
                        raise

                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    completed_count += 1
                    done += 1

                    if on_progress:
                        on_progress(done, total, f"跳过 {dynamic_id}：{reason}")

                    continue

                completed_count += 1
                done += 1

                # 只有真正调用 provider 并成功返回的候选才重置连续失败计数。
                if outcome.llm_called:
                    breaker.note_success()

                if outcome.skipped:
                    reason = outcome.skip_reason or "skipped"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

                elif outcome.lottery_type in ("互动抽奖", "预约抽奖", "转发抽奖"):
                    tasks.append((outcome.dynamic_id, outcome.lottery_type))
                    classify_outcome_by_id[outcome.dynamic_id] = outcome

                if on_progress:
                    on_progress(done, total, "分类进度")
    finally:
        reset_llm_provider_breaker(classify_token)

    unprocessed_count = max(0, total - completed_count - provider_failed_count)
    partial = bool(breaker.opened or provider_failed_count)

    def _partial_fields() -> dict[str, Any]:
        return {
            "partial": partial,
            "provider_unavailable": bool(breaker.opened),
            "config_unavailable": bool(breaker.config_unavailable),
            "provider_failed_count": provider_failed_count,
            "unprocessed_count": unprocessed_count,
            "partial_reason": partial_reason or (breaker.stop_reason if partial else ""),
        }

    # Canonicalize only after classification has exposed the first-party
    # detail item.  This keeps source-specific Following policy above the
    # shared pipeline while making every source converge on one DB identity.
    canonical_tasks: list[tuple[str, LotteryType]] = []
    canonical_outcomes: dict[str, ClassifyOutcome] = {}
    canonical_identity_by_id: dict[str, CanonicalIdentity] = {}
    canonical_seen: set[str] = set()
    canonical_duplicate_count = 0
    known_after_classification = known_activity_ids()
    legacy_alias_ids: set[str] = set()
    try:
        from src.activity_store import known_activity_aliases

        legacy_alias_ids = known_activity_aliases()
    except Exception:
        logger.debug("读取活动历史 wrapper 别名失败，继续使用 canonical ID", exc_info=True)

    for raw_dynamic_id, lottery_type in tasks:
        outcome = classify_outcome_by_id[raw_dynamic_id]
        identity = (identity_by_raw or {}).get(raw_dynamic_id)
        if identity is None:
            identity = resolve_dynamic_identity(outcome.detail_item, raw_dynamic_id)
        if identity.is_forward and identity.unresolved_reason:
            skip_reasons[identity.unresolved_reason] = (
                skip_reasons.get(identity.unresolved_reason, 0) + 1
            )
            continue
        canonical_id = identity.canonical_dynamic_id or raw_dynamic_id
        if not canonical_id:
            skip_reasons["forward_identity_unresolved"] = (
                skip_reasons.get("forward_identity_unresolved", 0) + 1
            )
            continue
        if (
            canonical_id in known_after_classification
            or canonical_id in canonical_seen
            or raw_dynamic_id in legacy_alias_ids
            or raw_dynamic_id in invalid_ids
            or canonical_id in invalid_ids
            or raw_dynamic_id in protected_ids
            or canonical_id in protected_ids
        ):
            canonical_duplicate_count += 1
            continue
        canonical_seen.add(canonical_id)
        outcome.dynamic_id = canonical_id
        if identity.canonical_item is not None:
            outcome.detail_item = identity.canonical_item
        if canonical_id != raw_dynamic_id:
            # A notice keyed by the wrapper cannot safely be reused for the
            # original.  The enrichment stage will resolve the canonical ID.
            outcome.lottery_notice = None
            outcome.notice_business_type = None
            outcome.notice_business_id = None
        canonical_tasks.append((canonical_id, lottery_type))
        canonical_outcomes[canonical_id] = outcome
        canonical_identity_by_id[canonical_id] = identity

    tasks = canonical_tasks
    classify_outcome_by_id = canonical_outcomes
    worker_count = max(1, min(workers, len(tasks) if tasks else 1))

    if not tasks:
        non_lottery_count, other_skipped_count, failed_count = _skip_breakdown(
            skip_reasons
        )
        return PipelineResult(
            ok=True,
            pipeline_skipped=False,
            raw_link_count=len(raw_urls),
            new_link_count=len(dynamic_ids),
            classified_count=0,
            skipped_count=sum(skip_reasons.values()),
            enriched_count=0,
            persisted_count=0,
            skip_reasons=skip_reasons,
            invalid_link_count=dedupe.invalid_link_count,
            duplicate_link_count=dedupe.duplicate_link_count + canonical_duplicate_count,
            existing_count=dedupe.existing_count,
            non_lottery_count=non_lottery_count,
            other_skipped_count=other_skipped_count,
            failed_count=failed_count,
            message=(
                "部分完成：LLM 服务异常，剩余候选将在后续刷新继续"
                if partial
                else "新链接均已跳过（非抽奖/充电/失效）"
            ),
            **_partial_fields(),
        )

    if on_progress:
        on_progress(0, len(tasks), f"正在拉取 {len(tasks)} 条活动详情…")

    enriched_rows: list[dict] = []
    enrich_total = len(tasks)

    def _enrich_one(
        client: BilibiliClient,
        dynamic_id: str,
        lottery_type: LotteryType,
        outcome: ClassifyOutcome,
    ) -> EnrichedActivity:
        # 线程池 worker 不继承主线程 contextvars：在 worker 内绑定同一 breaker，
        # 使 forward_parser 的 check() 能真正阻止 breaker 打开后的额外 LLM 请求。
        # 注意：这里不无条件 check()——官方/API 候选的 enrich 不需要 LLM，
        # 已成功分类的结果必须继续保留；需要 LLM 的路径由 forward_parser 自行拦截。
        worker_token = bind_llm_provider_breaker(breaker)
        try:
            return enrich_activity(
                client,
                dynamic_id=dynamic_id,
                lottery_type=lottery_type,
                participation=participations.get(dynamic_id),
                classify_content=outcome.classify_content or None,
                classify_detail=outcome.detail_item,
                classify_notice=outcome.lottery_notice,
                classify_notice_business_type=outcome.notice_business_type,
                classify_notice_business_id=outcome.notice_business_id,
            )
        finally:
            reset_llm_provider_breaker(worker_token)

    enrich_token = bind_llm_provider_breaker(breaker)
    try:
        with BilibiliClient() as enrich_client:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(
                        _enrich_one,
                        enrich_client,
                        did,
                        lt,
                        classify_outcome_by_id[did],
                    ): did
                    for did, lt in tasks
                }

                done = 0

                for future in as_completed(futures):
                    try:
                        activity = future.result()

                    except EnrichSkippedError:
                        skip_reasons[ENRICH_SKIP_REASON] = (
                            skip_reasons.get(ENRICH_SKIP_REASON, 0) + 1
                        )
                        done += 1

                        if on_progress:
                            on_progress(done, enrich_total, "详情进度")

                        continue

                    except Exception as exc:
                        candidate_id = futures.get(future, "")
                        # Config/Auth：第一次明确失败即停止后续 LLM（其余 future 快速失败）。
                        if is_config_failure(exc):
                            breaker.note_config_failure(exc)
                            partial_reason = partial_reason or breaker.stop_reason
                            provider_failed_count += 1
                            done += 1
                            logger.warning(
                                "LLM 配置不可用（详情阶段），停止当前源 candidate=%s category=%s",
                                candidate_id,
                                type(exc).__name__,
                            )

                            if on_progress:
                                on_progress(done, enrich_total, "LLM 配置不可用，已停止当前源")

                            continue

                        # 单候选 temporary/protocol 失败：记录可恢复失败，不杀死整个 DS。
                        if is_provider_failure(exc) or isinstance(exc, LlmProviderUnavailable):
                            if not isinstance(exc, LlmProviderUnavailable):
                                breaker.note_provider_failure(exc)
                            partial_reason = partial_reason or (
                                breaker.stop_reason if breaker.opened else ""
                            )
                            provider_failed_count += 1
                            done += 1
                            logger.warning(
                                "LLM provider 详情异常 candidate=%s category=%s streak=%s/%s",
                                candidate_id,
                                type(exc).__name__,
                                breaker.consecutive_failures,
                                breaker.threshold,
                            )

                            if on_progress:
                                on_progress(done, enrich_total, "LLM 服务异常，已跳过当前候选")

                            continue

                        if is_enrich_detail_skip_error(exc):
                            skip_reasons[ENRICH_SKIP_REASON] = (
                                skip_reasons.get(ENRICH_SKIP_REASON, 0) + 1
                            )
                            done += 1

                            if on_progress:
                                on_progress(done, enrich_total, "详情进度")

                            continue

                        raise

                    if activity.skipped:
                        raise RuntimeError(
                            f"活动 {activity.dynamic_id} 不应为 skipped"
                        )

                    row = apply_initial_status(activity.to_dict())
                    identity = canonical_identity_by_id.get(activity.dynamic_id)
                    if identity is not None and (
                        identity.raw_dynamic_id != identity.canonical_dynamic_id
                        or identity.canonical_author_uid is not None
                    ):
                        row["canonical_author_uid"] = identity.canonical_author_uid
                        row["source_kind"] = "canonicalized_dynamic"
                        if identity.raw_dynamic_id != identity.canonical_dynamic_id:
                            row["observed_wrapper_dynamic_id"] = (
                                identity.observed_wrapper_dynamic_id
                            )
                            row["observed_wrapper_author_uid"] = identity.wrapper_author_uid
                    enriched_rows.append(row)
                    done += 1

                    if on_progress:
                        on_progress(done, enrich_total, "详情进度")
    finally:
        reset_llm_provider_breaker(enrich_token)

    unprocessed_count = max(0, total - completed_count - provider_failed_count)
    partial = bool(breaker.opened or provider_failed_count)

    if on_progress and enriched_rows:
        on_progress(0, len(enriched_rows), "正在写入活动库…")

    expired_skipped_count = 0
    rows_to_persist: list[dict] = []
    for row in enriched_rows:
        if is_expired_before_scan(row, scan_started_at):
            expired_skipped_count += 1
            continue
        rows_to_persist.append(row)

    persisted = append_activities(rows_to_persist)
    persist_skipped_count = max(0, len(rows_to_persist) - persisted)

    if on_progress and enriched_rows:
        on_progress(
            persisted,
            max(1, len(enriched_rows)),
            f"入库完成，新增 {persisted} 条活动",
        )

    message = f"新入库 {persisted} 条活动"
    if expired_skipped_count:
        message += f"，已跳过 {expired_skipped_count} 条在扫描开始前已结束的抽奖"
    if partial:
        message += "；部分完成：LLM 服务异常，剩余候选将在后续刷新继续"
    non_lottery_count, other_skipped_count, failed_count = _skip_breakdown(skip_reasons)

    return PipelineResult(
        ok=True,
        pipeline_skipped=False,
        raw_link_count=len(raw_urls),
        new_link_count=len(dynamic_ids),
        classified_count=len(tasks),
        skipped_count=sum(skip_reasons.values()),
        enriched_count=len(enriched_rows),
        persisted_count=persisted,
        skip_reasons=skip_reasons,
        invalid_link_count=dedupe.invalid_link_count,
        duplicate_link_count=dedupe.duplicate_link_count + canonical_duplicate_count,
        existing_count=dedupe.existing_count,
        non_lottery_count=non_lottery_count,
        other_skipped_count=other_skipped_count,
        failed_count=failed_count,
        persist_skipped_count=persist_skipped_count,
        expired_skipped_count=expired_skipped_count,
        message=message,
        **_partial_fields(),
    )


def run_refresh_all_pipeline(
    ds_results: list[CheckResult],
    *,
    workers: int = 4,
    on_progress: ProgressCallback | None = None,
    scan_started_at: int | None = None,
) -> PipelineResult:
    """完整 refresh_all：Step 1 结果 → Step 2～5。

    scan_started_at 为整轮扫描的固定基准时间；透传给新链接流水线做保守过滤。
    """
    if not any(result.updated for result in ds_results):
        return PipelineResult(
            ok=True,
            pipeline_skipped=True,
            raw_link_count=0,
            new_link_count=0,
            classified_count=0,
            skipped_count=0,
            enriched_count=0,
            persisted_count=0,
            message="所有数据源容器均未变更，流水线结束",
        )

    raw_links = _collect_updated_links(ds_results)
    return run_new_links_pipeline(
        raw_links,
        workers=workers,
        on_progress=on_progress,
        scan_started_at=scan_started_at,
    )
