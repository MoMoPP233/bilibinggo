import { requireSetup } from "../account/index";
import { fetchJSON } from "../api/client";
import {
  repostCandidatePagination,
  repostCandidateSummary,
  repostCandidatesBody,
  repostSafeTotal,
  repostManualTotal,
  repostDeferredTotal,
  repostBlockedTotal,
  repostPendingTotal,
  repostCheckpointStatus,
  repostClearSelectionBtn,
  repostDeleteSelectedBtn,
  repostHistoryBody,
  repostHistoryPagination,
  repostHistorySummary,
  repostHistoryTotal,
  repostDeletedTotal,
  repostWorkflowStatus,
  repostEvalStatus,
  repostStopBtn,
  repostSearchInput,
  repostSortSelect,
  repostDeleteProgress,
  repostSelectedCount,
  repostSelectPageBtn,
} from "../dom";
import { notifyJobStartError, startJob, trackCurrentJob } from "../jobs/index";
import { openAppConfirm } from "../shell/confirm";
import { showToast } from "../shell/toast";
import type { JobStatus } from "../types";
import { escapeHtml, sanitizeUserText } from "../utils/text";

export interface RepostHistoryItem {
  uid: string | number;
  repost_dynamic_id: string;
  original_dynamic_id: string;
  reposted_at: string | number;
  original_author_uid?: string | number | null;
  original_author_name?: string | null;
  source: string;
  delete_status: string;
  delete_requested_at?: string | number | null;
  deleted_at?: string | number | null;
  last_seen_at?: string | number | null;
  last_error?: string | null;
  updated_at?: string | number | null;
}

export interface CleanupCandidate {
  uid?: string | number;
  repost_dynamic_id: string;
  original_dynamic_id: string;
  reposted_at: string | number;
  original_author_uid?: string | number | null;
  original_author_name?: string | null;
  lottery_type?: string | null;
  lottery_time?: string | number | null;
  eligible_after?: string | number | null;
  days_since_lottery?: number | null;
  level?: "safe" | "manual_review" | "blocked" | "excluded" | string | null;
  defer_reason?: string | null;
  deferred_at?: string | number | null;
  deferred_until?: string | number | null;
  lottery_time_reliable?: boolean | null;
  assessment_status?: string | null;
  identity_source?: string | null;
  identity_checked_at?: string | number | null;
  delete_requested_at?: string | number | null;
  deleted_at?: string | number | null;
  last_error?: string | null;
  reason_code?: string | null;
  summary?: string | null;
  classification_source?: string | null;
  evaluated_at?: string | number | null;
  reason?: string | null;
  delete_status?: string | null;
  delete_message?: string | null;
}

interface HistoryCheckpoint {
  head_dynamic_id?: string | null;
  head_published_at?: string | number | null;
  full_scan_completed?: boolean;
  last_synced_at?: string | number | null;
}

interface RepostHistoryResponse {
  ok?: boolean;
  uid?: string | number;
  items?: RepostHistoryItem[];
  total?: number;
  page?: number;
  page_size?: number;
  checkpoint?: HistoryCheckpoint | null;
  full_scan_completed?: boolean;
}

interface RepostCandidatesResponse {
  ok?: boolean;
  uid?: string | number;
  candidates?: unknown;
  history_total?: number;
  assessed_total?: number;
  safe?: number;
  manual_review?: number;
  deferred?: number;
  blocked?: number;
  excluded?: number;
  pending_evaluation?: number;
  deleted?: number;
  deleted_candidates?: unknown;
  last_synced_at?: string | number | null;
  full_scan_completed?: boolean;
  last_evaluated_at?: string | number | null;
}

interface DeleteResultItem {
  repost_dynamic_id?: string;
  original_dynamic_id?: string;
  status?: string;
  message?: string;
}

interface DeleteJobResult {
  uid?: string | number;
  requested_count?: number;
  deleted_count?: number;
  failed_count?: number;
  unknown_count?: number;
  skipped_count?: number;
  items?: DeleteResultItem[];
}

const CANDIDATE_PAGE_SIZE = 20;
const HISTORY_PAGE_SIZE = 20;
const DIGITS_ONLY = /^\d+$/;

let candidates: CleanupCandidate[] = [];
let selectedRepostIds = new Set<string>();
let pendingDeleteIds = new Set<string>();
let candidatePage = 1;
let historyPage = 1;
let historyPages = 1;
let deleteSubmitting = false;
let deletePromptOpen = false;
let bound = false;
let filter: "all" | "safe" | "manual_review" | "deferred" | "blocked" | "deleted" = "all";
let pendingEvaluation = 0;
let assessedTotal = 0;
let deletedTotal = 0;
let deletedCandidates: CleanupCandidate[] = [];
let searchQuery = "";
let sortKey = "reposted_desc";

function updateEvalStatus(): void {
  if (!repostEvalStatus) return;
  repostEvalStatus.textContent = pendingEvaluation > 0
    ? `累计已评估 ${assessedTotal} 条 · 仍待评估 ${pendingEvaluation} 条`
    : assessedTotal > 0
      ? `累计已评估 ${assessedTotal} 条 · 全部完成`
      : "尚未开始智能评估";
}

function validDynamicId(value: unknown): string {
  const text = String(value ?? "").trim();
  return DIGITS_ONLY.test(text) ? text : "";
}

function opusUrl(dynamicId: string): string {
  return `https://www.bilibili.com/opus/${encodeURIComponent(dynamicId)}`;
}

function formatTimestamp(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  const raw = String(value).trim();
  const numeric = Number(raw);
  let date: Date;
  if (Number.isFinite(numeric) && numeric > 0) {
    date = new Date(numeric < 10_000_000_000 ? numeric * 1000 : numeric);
  } else {
    date = new Date(raw);
  }
  if (Number.isNaN(date.getTime())) return sanitizeUserText(raw) || "—";
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function historyStatus(status: unknown): { label: string; tone: string } {
  switch (String(status || "active")) {
    case "deleted":
      return { label: "已删除", tone: "deleted" };
    case "delete_pending":
      return { label: "删除确认中", tone: "pending" };
    case "delete_failed":
      return { label: "删除失败", tone: "failed" };
    case "unknown":
      return { label: "结果未知", tone: "unknown" };
    default:
      return { label: "仍存在", tone: "active" };
  }
}

function candidateSelectable(candidate: CleanupCandidate): boolean {
  const status = String(candidate.delete_status || "active");
  const level = String(candidate.level || "safe");
  const deletableLevel = level === "safe" || level === "manual_review";
  return deletableLevel && (status === "active" || status === "delete_failed");
}

function candidatePageCount(): number {
  return Math.max(1, Math.ceil(visibleCandidates().length / CANDIDATE_PAGE_SIZE));
}

function visibleCandidates(): CleanupCandidate[] {
  if (filter === "deleted") return deletedCandidates;
  const levelFiltered = filter === "all"
    ? candidates
    : candidates.filter((candidate) => String(candidate.level || "safe") === filter);
  const query = searchQuery.trim().toLowerCase();
  const searched = query
    ? levelFiltered.filter((candidate) => {
        const author = String(candidate.original_author_name || "").toLowerCase();
        const summary = String(candidate.summary || "").toLowerCase();
        const original = String(candidate.original_dynamic_id || "");
        const repost = String(candidate.repost_dynamic_id || "");
        return author.includes(query) || summary.includes(query) || original.includes(query) || repost.includes(query);
      })
    : levelFiltered;
  return sortCandidates(searched);
}

function sortCandidates(items: CleanupCandidate[]): CleanupCandidate[] {
  const priorityOrder: Record<string, number> = {
    safe: 0,
    manual_review: 1,
    deferred: 2,
    blocked: 3,
  };
  const sorted = [...items];
  switch (sortKey) {
    case "reposted_asc":
      return sorted.sort((a, b) => Number(a.reposted_at) - Number(b.reposted_at));
    case "lottery_desc":
      return sorted.sort((a, b) => (Number(b.lottery_time) || 0) - (Number(a.lottery_time) || 0));
    case "lottery_asc":
      return sorted.sort((a, b) => (Number(a.lottery_time) || Infinity) - (Number(b.lottery_time) || Infinity));
    case "priority":
      return sorted.sort((a, b) => {
        const pa = priorityOrder[String(a.level || "safe")] ?? 9;
        const pb = priorityOrder[String(b.level || "safe")] ?? 9;
        return pa - pb || Number(b.reposted_at) - Number(a.reposted_at);
      });
    case "evaluated_desc":
      return sorted.sort((a, b) => (Number(b.evaluated_at) || 0) - (Number(a.evaluated_at) || 0));
    default:
      return sorted.sort((a, b) => Number(b.reposted_at) - Number(a.reposted_at));
  }
}

function currentCandidatePageItems(): CleanupCandidate[] {
  const start = (candidatePage - 1) * CANDIDATE_PAGE_SIZE;
  return visibleCandidates().slice(start, start + CANDIDATE_PAGE_SIZE);
}

function levelLabel(level: unknown): { label: string; tone: string } {
  switch (String(level || "safe")) {
    case "safe":
      return { label: "🟢 安全可删", tone: "safe" };
    case "manual_review":
      return { label: "🟡 人工确认", tone: "manual_review" };
    case "blocked":
      return { label: "🔴 不可判断", tone: "blocked" };
    case "deferred":
      return { label: "🕒 暂缓处理", tone: "deferred" };
    case "deleted":
      return { label: "已删除", tone: "deleted" };
    case "excluded":
      return { label: "非抽奖", tone: "excluded" };
    default:
      return { label: "待评估", tone: "pending" };
  }
}

function humanReasonCode(code: unknown): string {
  const map: Record<string, string> = {
    safe_official_notice: "官方已开奖、中奖名单完整、当前账号未中奖",
    forward_lottery_manual: "公共抽奖分类器判断为普通转发抽奖，但无法获取可靠中奖结果",
    notice_winners_incomplete: "官方中奖结果不完整，无法确认当前账号是否中奖",
    notice_status_unclear: "官方开奖状态不明确",
    notice_not_ended: "官方抽奖尚未明确结束",
    notice_time_missing: "官方开奖时间缺失",
    notice_business_missing: "缺少可靠 lottery_notice 业务标识",
    notice_business_mismatch: "lottery_notice 业务标识不一致",
    notice_unreadable: "抽奖结果暂时无法读取",
    recent_reliable_lottery: "官方可靠开奖时间距今未满 30 天，暂缓人工清理",
    user_deferred: "用户选择暂不删除",
    current_account_won: "当前账号在中奖名单中，请先领奖，禁止删除",
    original_unreadable: "原动态无法读取，无法判断是否为抽奖",
    classify_failed: "公共分类无法可靠判断原动态是否为抽奖",
    identity_unverified: "无法确认转发归属",
    remote_unknown: "关键状态无法确认",
    participation_conflict: "本地参与状态与转发历史冲突",
    guard_conflict: "转发保护状态异常",
  };
  return map[String(code || "")] || "";
}

function lotteryTimeLabel(candidate: CleanupCandidate): string {
  const value = candidate.lottery_time;
  if (!value) return "未知";
  if (!candidate.lottery_time_reliable) return "时间不可靠";
  return formatTimestamp(value);
}

function renderPager(
  element: HTMLElement | null,
  page: number,
  pages: number,
  total: number,
  kind: "candidate" | "history",
): void {
  if (!element) return;
  if (total <= 0) {
    element.innerHTML = "";
    return;
  }
  element.innerHTML = `
    <p class="caption pagination-summary">共 ${total} 条 · 第 ${page}/${pages} 页</p>
    <div class="action-row pagination-actions">
      <button type="button" class="btn btn-secondary btn-compact btn-pill" data-repost-page-kind="${kind}" data-repost-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
      <button type="button" class="btn btn-secondary btn-compact btn-pill" data-repost-page-kind="${kind}" data-repost-page="${page + 1}" ${page >= pages ? "disabled" : ""}>下一页</button>
    </div>`;
}

function renderSelectionState(): void {
  const count = selectedRepostIds.size;
  if (repostSelectedCount) repostSelectedCount.textContent = `已选择 ${count} 条`;
  if (repostDeleteSelectedBtn instanceof HTMLButtonElement) {
    repostDeleteSelectedBtn.disabled = deleteSubmitting || count === 0;
    repostDeleteSelectedBtn.textContent = deleteSubmitting ? "正在提交…" : `删除选中的 ${count} 条`;
  }
  if (repostClearSelectionBtn instanceof HTMLButtonElement) {
    repostClearSelectionBtn.disabled = count === 0 || deleteSubmitting;
  }
  if (repostSelectPageBtn instanceof HTMLButtonElement) {
    const selectable = currentCandidatePageItems().filter(candidateSelectable);
    repostSelectPageBtn.disabled = selectable.length === 0 || deleteSubmitting;
  }
}

export function normalizeCandidates(value: unknown): CleanupCandidate[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const normalized: CleanupCandidate[] = [];
  for (const raw of value) {
    if (!raw || typeof raw !== "object") continue;
    const item = raw as Record<string, unknown>;
    const repostId = validDynamicId(item.repost_dynamic_id);
    const originalId = validDynamicId(item.original_dynamic_id);
    if (!repostId || !originalId || repostId === originalId || seen.has(repostId)) continue;
    seen.add(repostId);
    normalized.push({
      ...(item as unknown as CleanupCandidate),
      repost_dynamic_id: repostId,
      original_dynamic_id: originalId,
    });
  }
  return normalized;
}

export function extractJobCandidates(job: JobStatus | null | undefined): CleanupCandidate[] {
  const result = job?.result as Record<string, unknown> | null | undefined;
  const nested = result?.result && typeof result.result === "object"
    ? (result.result as Record<string, unknown>)
    : null;
  return normalizeCandidates(result?.candidates ?? nested?.candidates);
}

export function renderCandidates(): void {
  const pages = candidatePageCount();
  candidatePage = Math.min(Math.max(1, candidatePage), pages);
  const pageItems = currentCandidatePageItems();
  const validIds = new Set(
    (filter === "deleted" ? deletedCandidates : candidates)
      .filter(candidateSelectable)
      .map((item) => item.repost_dynamic_id),
  );
  selectedRepostIds = new Set([...selectedRepostIds].filter((id) => validIds.has(id)));

  const safeCount = candidates.filter((c) => String(c.level) === "safe").length;
  const manualCount = candidates.filter((c) => String(c.level) === "manual_review").length;
  const deferredCount = candidates.filter((c) => String(c.level) === "deferred").length;
  const blockedCount = candidates.filter((c) => String(c.level) === "blocked").length;
  if (repostDeletedTotal) repostDeletedTotal.textContent = String(deletedTotal);
  if (repostSafeTotal) repostSafeTotal.textContent = String(safeCount);
  if (repostManualTotal) repostManualTotal.textContent = String(manualCount);
  if (repostDeferredTotal) repostDeferredTotal.textContent = String(deferredCount);
  if (repostBlockedTotal) repostBlockedTotal.textContent = String(blockedCount);
  if (repostCandidateSummary) {
    if (filter === "deleted") {
      repostCandidateSummary.textContent = `已删除 tombstone ${deletedCandidates.length} 条；仅供查看，不提供再次删除。`;
    } else {
      repostCandidateSummary.textContent = pendingEvaluation > 0
        ? `还有 ${pendingEvaluation} 条原动态待评估，可继续点击“一键智能评估”。`
        : candidates.length
          ? `当前待处理 ${candidates.length} 条；候选默认不勾选，删除前服务端还会再次校验。`
          : "当前没有可展示的清理候选；请先同步并评估历史抽奖。";
    }
  }
  if (repostCandidatesBody) {
    if (!pageItems.length) {
      repostCandidatesBody.innerHTML = '<tr class="empty-row"><td colspan="6">当前筛选下没有记录</td></tr>';
    } else {
      repostCandidatesBody.innerHTML = pageItems.map((candidate) => {
        const repostId = candidate.repost_dynamic_id;
        const originalId = candidate.original_dynamic_id;
        const selectable = candidateSelectable(candidate);
        const status = historyStatus(candidate.delete_status);
        const level = levelLabel(candidate.level);
        const author = sanitizeUserText(candidate.original_author_name || "") || "原作者未知";
        const reason = sanitizeUserText(candidate.reason || "") || "暂无判断依据";
        const summary = sanitizeUserText(candidate.summary || "");
        const lotteryType = sanitizeUserText(candidate.lottery_type || "") || "官方抽奖";
        const lotteryTime = lotteryTimeLabel(candidate);
        const message = sanitizeUserText(candidate.delete_message || "");
        const isDeferred = String(candidate.level) === "deferred";
        const isUserDeferred = String(candidate.defer_reason) === "user";
        const isDeleted = String(candidate.level) === "deleted";
        const isRetryableBlocked = String(candidate.level) === "blocked"
          && String(candidate.assessment_status) === "retryable_unknown";
        const detailReason = humanReasonCode(candidate.reason_code) || reason;
        const deferLine = isDeferred
          ? isUserDeferred
            ? `用户于 ${formatTimestamp(candidate.deferred_at)} 选择暂不删除`
            : `官方开奖时间距今未满 30 天 · 预计可重新进入人工确认：${formatTimestamp(candidate.deferred_until)}`
          : "";
        return `
          <tr class="repost-candidate-row" data-repost-row="${escapeHtml(repostId)}">
            <td class="repost-check-cell">
              ${!selectable
                ? `<span class="repost-blocked-mark" title="该候选禁止勾选删除">🔒</span>`
                : `<input type="checkbox" class="repost-checkbox" data-repost-select="${escapeHtml(repostId)}" aria-label="选择本人转发 ${escapeHtml(repostId)}" ${selectedRepostIds.has(repostId) ? "checked" : ""} ${selectable ? "" : "disabled"} />`}
            </td>
            <td>
              <span class="repost-level repost-level--${level.tone}">${level.label}</span>
              <p class="repost-author">${escapeHtml(author)}</p>
              ${isDeleted ? `<p class="caption repost-summary">删除于 ${formatTimestamp(candidate.deleted_at)}</p>` : ""}
            </td>
            <td>
              <p class="repost-reason">${escapeHtml(isDeleted ? "已删除 tombstone" : reason)}</p>
              ${deferLine ? `<p class="caption repost-summary">${escapeHtml(deferLine)}</p>` : ""}
              ${summary ? `<p class="caption repost-summary" title="${escapeHtml(summary)}">${escapeHtml(summary)}</p>` : ""}
              ${message ? `<p class="caption repost-error">${escapeHtml(message)}</p>` : ""}
            </td>
            <td>
              <p class="caption">我的转发：${escapeHtml(formatTimestamp(candidate.reposted_at))}</p>
              <p class="caption repost-lottery-time">开奖：${escapeHtml(lotteryTime)}（${escapeHtml(lotteryType)}）</p>
              ${isDeleted ? "" : `<p class="caption">评估：${escapeHtml(formatTimestamp(candidate.evaluated_at))}</p>`}
            </td>
            <td>
              <p class="repost-id">原动态：<span class="repost-id-value" title="${escapeHtml(originalId)}">${escapeHtml(originalId)}</span>
                <button type="button" class="btn btn-ghost btn-compact btn-mini" data-repost-copy="${escapeHtml(originalId)}">复制</button>
              </p>
              <p class="repost-id">我的转发：<span class="repost-id-value" title="${escapeHtml(repostId)}">${escapeHtml(repostId)}</span>
                <button type="button" class="btn btn-ghost btn-compact btn-mini" data-repost-copy="${escapeHtml(repostId)}">复制</button>
              </p>
              <a class="activity-link" href="${opusUrl(originalId)}" target="_blank" rel="noopener">查看原动态</a>
            </td>
            <td>
              ${isDeleted ? "" : `
                <div class="action-row repost-row-actions">
                  ${isUserDeferred
                    ? `<button type="button" class="btn btn-ghost btn-compact btn-pill" data-repost-restore="${escapeHtml(repostId)}">恢复</button>`
                    : ""}
                  ${selectable
                    ? `<button type="button" class="btn btn-ghost btn-compact btn-pill" data-repost-defer="${escapeHtml(repostId)}">暂不删除</button>`
                    : ""}
                  ${isRetryableBlocked
                    ? `<button type="button" class="btn btn-ghost btn-compact btn-pill" data-repost-reeval="${escapeHtml(originalId)}">重新评估此条</button>`
                    : ""}
                </div>
                <details class="repost-details">
                  <summary>详情 / 判断依据</summary>
                  <p class="caption">判断：${escapeHtml(detailReason)}</p>
                  <p class="caption">评估来源：${escapeHtml(String(candidate.classification_source || "—"))}</p>
                  <p class="caption">可靠开奖时间：${candidate.lottery_time_reliable ? "是" : "否"}</p>
                  <p class="caption">身份来源：${escapeHtml(String(candidate.identity_source || "—"))}</p>
                  <p class="caption">删除状态：${escapeHtml(status.label)}</p>
                  <p class="caption">暂缓：${isDeferred ? escapeHtml(isUserDeferred ? "用户暂不删除" : "系统 30 天暂缓") : "无"}</p>
                </details>
              `}
            </td>
          </tr>`;
      }).join("");
    }
  }
  renderPager(repostCandidatePagination, candidatePage, pages, visibleCandidates().length, "candidate");
  renderSelectionState();
}

export function setCandidates(items: unknown): CleanupCandidate[] {
  candidates = normalizeCandidates(items);
  selectedRepostIds.clear();
  pendingDeleteIds.clear();
  candidatePage = 1;
  renderCandidates();
  return candidates;
}

function renderHistory(data: RepostHistoryResponse): void {
  const items = Array.isArray(data.items) ? data.items : [];
  const total = Math.max(0, Number(data.total) || 0);
  historyPage = Math.max(1, Number(data.page) || historyPage);
  const pageSize = Math.max(1, Number(data.page_size) || HISTORY_PAGE_SIZE);
  historyPages = Math.max(1, Math.ceil(total / pageSize));
  if (repostHistoryTotal) repostHistoryTotal.textContent = String(total);
  if (repostHistorySummary) {
    repostHistorySummary.textContent = total
      ? `当前运行账号已索引 ${total} 条本人转发。不可删除的记录也会保留在这里。`
      : "尚未索引到本人转发；请手动同步转发历史。";
  }

  const checkpoint = data.checkpoint;
  const complete = Boolean(checkpoint?.full_scan_completed ?? data.full_scan_completed);
  if (repostCheckpointStatus) {
    if (!checkpoint) {
      repostCheckpointStatus.textContent = "尚未完成首次同步";
    } else if (complete) {
      repostCheckpointStatus.textContent = `完整历史已同步 · ${formatTimestamp(checkpoint.last_synced_at || checkpoint.head_published_at)}`;
    } else {
      repostCheckpointStatus.textContent = `历史索引尚未完整 · ${formatTimestamp(checkpoint.last_synced_at || checkpoint.head_published_at)}`;
    }
  }

  if (repostHistoryBody) {
    if (!items.length) {
      repostHistoryBody.innerHTML = '<tr class="empty-row"><td colspan="5">暂无转发历史</td></tr>';
    } else {
      repostHistoryBody.innerHTML = items.map((item) => {
        const repostId = validDynamicId(item.repost_dynamic_id);
        const originalId = validDynamicId(item.original_dynamic_id);
        const status = historyStatus(item.delete_status);
        const author = sanitizeUserText(item.original_author_name || "") || "—";
        const error = sanitizeUserText(item.last_error || "");
        return `
          <tr>
            <td>${repostId ? `<a class="activity-link" href="${opusUrl(repostId)}" target="_blank" rel="noopener">${escapeHtml(repostId)}</a>` : "—"}</td>
            <td>${originalId ? `<a class="activity-link" href="${opusUrl(originalId)}" target="_blank" rel="noopener">${escapeHtml(originalId)}</a>` : "—"}</td>
            <td>${escapeHtml(author)}</td>
            <td class="time-cell">${escapeHtml(formatTimestamp(item.reposted_at))}</td>
            <td>
              <span class="repost-status repost-status--${status.tone}">${status.label}</span>
              ${error ? `<p class="caption repost-error" title="${escapeHtml(error)}">${escapeHtml(error)}</p>` : ""}
            </td>
          </tr>`;
      }).join("");
    }
  }
  renderPager(repostHistoryPagination, historyPage, historyPages, total, "history");
}

export async function loadRepostHistory(page = historyPage): Promise<RepostHistoryResponse> {
  historyPage = Math.max(1, page);
  try {
    const data = await fetchJSON<RepostHistoryResponse>(
      `/api/repost-cleanup/history?page=${historyPage}&page_size=${HISTORY_PAGE_SIZE}`,
    );
    renderHistory(data || {});
    return data || {};
  } catch (error) {
    if (repostHistoryBody) {
      const message = sanitizeUserText(error instanceof Error ? error.message : String(error));
      repostHistoryBody.innerHTML = `<tr class="empty-row"><td colspan="5">读取失败：${escapeHtml(message || "未知错误")}</td></tr>`;
    }
    throw error;
  }
}

export async function loadPersistedCandidates(): Promise<void> {
  try {
    const data = await fetchJSON<RepostCandidatesResponse>("/api/repost-cleanup/candidates?show_deleted=1");
    pendingEvaluation = Math.max(0, Number(data?.pending_evaluation) || 0);
    assessedTotal = Math.max(0, Number(data?.assessed_total) || 0);
    deletedTotal = Math.max(0, Number(data?.deleted) || 0);
    deletedCandidates = normalizeCandidates(data?.deleted_candidates).map((item) => ({
      ...item,
      level: "deleted",
      delete_status: "deleted",
    }));
    if (repostPendingTotal) repostPendingTotal.textContent = String(pendingEvaluation);
    if (repostHistoryTotal && Number(data?.history_total) >= 0) {
      repostHistoryTotal.textContent = String(Number(data?.history_total) || 0);
    }
    updateWorkflowStatus(data);
    updateEvalStatus();
    setCandidates(normalizeCandidates(data?.candidates));
  } catch {
    // 登录/网络异常时保留当前已渲染候选，避免误清空。
  }
}

function updateWorkflowStatus(data: RepostCandidatesResponse): void {
  if (!repostWorkflowStatus) return;
  const syncState = data.full_scan_completed ? "已完成" : "未完成";
  const syncedAt = formatTimestamp(data.last_synced_at);
  const evaluatedAt = formatTimestamp(data.last_evaluated_at);
  repostWorkflowStatus.textContent =
    `历史同步：${syncState} · 最近同步：${syncedAt} · 已评估：${assessedTotal} · 待评估：${pendingEvaluation} · 最近评估：${evaluatedAt}`;
}

export async function deferCandidate(repostId: string): Promise<void> {
  try {
    const data = await fetchJSON<RepostCandidatesResponse>("/api/repost-cleanup/defer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repost_dynamic_ids: [repostId] }),
    });
    pendingEvaluation = Math.max(0, Number(data?.pending_evaluation) || 0);
    setCandidates(normalizeCandidates(data?.candidates));
    updateEvalStatus();
  } catch (error) {
    showToast(
      sanitizeUserText(error instanceof Error ? error.message : String(error)) || "暂不删除失败",
      "error",
    );
  }
}

export async function restoreCandidate(repostId: string): Promise<void> {
  try {
    const data = await fetchJSON<RepostCandidatesResponse>("/api/repost-cleanup/restore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repost_dynamic_ids: [repostId] }),
    });
    pendingEvaluation = Math.max(0, Number(data?.pending_evaluation) || 0);
    setCandidates(normalizeCandidates(data?.candidates));
    updateEvalStatus();
  } catch (error) {
    showToast(
      sanitizeUserText(error instanceof Error ? error.message : String(error)) || "恢复失败",
      "error",
    );
  }
}

function deleteJobResult(job: JobStatus): DeleteJobResult {
  const result = (job.result || {}) as Record<string, unknown>;
  if (result.result && typeof result.result === "object") {
    return result.result as DeleteJobResult;
  }
  return result as DeleteJobResult;
}

function applyDeleteCompletion(job: JobStatus): void {
  const result = deleteJobResult(job);
  const outcomes = new Map<string, DeleteResultItem>();
  for (const item of Array.isArray(result.items) ? result.items : []) {
    const repostId = validDynamicId(item?.repost_dynamic_id);
    if (repostId) outcomes.set(repostId, item);
  }

  candidates = candidates.flatMap((candidate) => {
    const wasPending = pendingDeleteIds.has(candidate.repost_dynamic_id);
    const outcome = outcomes.get(candidate.repost_dynamic_id);
    if (!wasPending && !outcome) return [candidate];
    const rawStatus = String(outcome?.status || (job.state === "success" ? "unknown" : "unknown"));
    if (rawStatus === "deleted") return [];
    if (rawStatus === "delete_failed" || rawStatus === "failed") {
      return [{ ...candidate, delete_status: "delete_failed", delete_message: outcome?.message || "删除失败" }];
    }
    if (rawStatus === "skipped") return [];
    return [{ ...candidate, delete_status: "unknown", delete_message: outcome?.message || "删除结果无法确认，请先重新同步核实" }];
  });
  selectedRepostIds.clear();
  pendingDeleteIds.clear();
  deleteSubmitting = false;
  renderCandidates();

  const deleted = Number(result.deleted_count) || 0;
  const failed = Number(result.failed_count) || 0;
  const unknown = Number(result.unknown_count) || 0;
  const skipped = Number(result.skipped_count) || 0;
  const detail = `已删除 ${deleted} · 失败 ${failed} · 结果未知 ${unknown} · 跳过 ${skipped}`;
  if (unknown > 0) {
    showToast("部分删除结果无法确认", "info", `${detail}；不会自动重试，请重新同步后核实。`);
  } else if (failed > 0 || skipped > 0) {
    showToast("删除任务已完成（含未删除项）", "info", detail);
  } else {
    showToast("删除任务已完成", "success", detail);
  }
}

export async function deleteSelectedReposts(): Promise<void> {
  if (deleteSubmitting || deletePromptOpen) return;
  const known = new Map(candidates.map((candidate) => [candidate.repost_dynamic_id, candidate]));
  const ids = [...selectedRepostIds].filter((id) => {
    const candidate = known.get(id);
    return Boolean(candidate && candidateSelectable(candidate));
  });
  if (!ids.length) return;
  if (ids.length > 20) {
    showToast("单次最多删除 20 条", "error");
    return;
  }
  if (!requireSetup("delete_expired_reposts")) return;
  const hasManualReview = ids.some((id) => {
    const candidate = known.get(id);
    return String(candidate?.level) === "manual_review";
  });

  deletePromptOpen = true;
  let confirmed = false;
  let manualConfirmed = false;
  try {
    confirmed = await openAppConfirm({
      eyebrow: "危险操作",
      title: hasManualReview
        ? `删除选中的 ${ids.length} 条（含人工确认项）`
        : `删除选中的 ${ids.length} 条已确认安全的转发`,
      desc: hasManualReview
        ? `即将删除你自己空间中的 ${ids.length} 条历史抽奖转发，其中 ${ids.filter((id) => String(known.get(id)?.level) === "manual_review").length} 条属于人工确认项目。程序无法确认你是否中奖或仍需领奖。请确认你已经检查原动态，并确定不再需要保留这些转发。`
        : `即将删除你自己空间中的 ${ids.length} 条已确认安全的历史抽奖转发。`,
      bullets: [
        `本次只提交 ${ids.length} 个“本人转发动态 ID”，绝不会提交原抽奖动态 ID`,
        "服务端会在每条删除前重新确认运行 Profile、UID、动态归属与转发关系",
        "网络超时或响应不确定时会标记为 unknown，不会自动再次发送删除请求",
      ],
      confirmLabel: `确认删除 ${ids.length} 条`,
      cancelLabel: "取消",
      danger: true,
    });
    if (confirmed && hasManualReview) {
      manualConfirmed = await openAppConfirm({
        eyebrow: "人工确认项",
        title: "包含人工确认候选，请再次确认",
        desc: "程序只能确认这是你的历史抽奖转发，无法确认你是否中奖或是否仍需领奖。请确认你已经人工检查原动态，并确定不再需要保留这条转发。",
        bullets: ["人工确认项删除前仍会重新验证 Profile、UID、动态归属与 orig 关系"],
        confirmLabel: "我已人工检查并确认删除",
        cancelLabel: "取消",
        danger: true,
      });
      if (!manualConfirmed) confirmed = false;
    }
  } finally {
    deletePromptOpen = false;
  }
  if (!confirmed) return;

  deleteSubmitting = true;
  pendingDeleteIds = new Set(ids);
  if (repostDeleteProgress) {
    repostDeleteProgress.textContent = `正在删除 0 / ${ids.length} …（串行执行）`;
  }
  renderSelectionState();
  try {
    await fetchJSON("/api/repost-cleanup/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        repost_dynamic_ids: ids,
        confirmed: true,
        manual_review_confirmed: Boolean(manualConfirmed),
      }),
    });
    selectedRepostIds.clear();
    candidates = candidates.map((candidate) => pendingDeleteIds.has(candidate.repost_dynamic_id)
      ? { ...candidate, delete_status: "delete_pending", delete_message: "删除任务已提交，等待服务端确认" }
      : candidate);
    renderCandidates();
    await trackCurrentJob();
  } catch (error) {
    // 请求可能已经到达服务端。保持 pending，禁止用户在本页面直接重复发送。
    candidates = candidates.map((candidate) => pendingDeleteIds.has(candidate.repost_dynamic_id)
      ? { ...candidate, delete_status: "unknown", delete_message: "无法确认删除任务是否已接收，请先重新同步核实" }
      : candidate);
    pendingDeleteIds.clear();
    selectedRepostIds.clear();
    showToast("无法确认删除任务状态", "error", "不会自动重试，请等待当前任务结束并重新同步历史。"
    );
    renderCandidates();
  } finally {
    deleteSubmitting = false;
    renderSelectionState();
  }
}

export async function handleRepostCleanupJobCompletion(job: JobStatus): Promise<void> {
  if (!job || (job.state !== "success" && job.state !== "error" && job.state !== "cancelled")) return;
  if (repostStopBtn instanceof HTMLButtonElement) repostStopBtn.disabled = true;
  // 全局任务管理器不会盲目重新启用 data-job-control；按本页选择状态恢复。
  renderSelectionState();
  if (job.action === "sync_repost_history") {
    if (job.state === "success") {
      const result = (job.result || {}) as Record<string, unknown>;
      const reconciliation = (result.guard_reconciliation || {}) as Record<string, unknown>;
      const resolved = Number(reconciliation.resolved) || 0;
      const resolvedText = resolved > 0
        ? `，已根据本地转发历史自动确认 ${resolved} 条历史参与记录`
        : "";
      showToast("转发历史同步完成", "success", `本次导入 ${Number(result.imported_count) || 0} 条${resolvedText}`);
      await loadRepostHistory(1).catch(() => {});
    }
    return;
  }
  if (job.action === "scan_expired_reposts") {
    // 无论成功 / 失败 / 被用户停止，都从本地库重建最终状态，避免 UI 滞后。
    await loadPersistedCandidates().catch(() => {});
    if (job.state === "success") {
      const result = (job.result || {}) as Record<string, unknown>;
      const message = String(result.message || "历史抽奖评估完成");
      if (result.rate_limited) {
        showToast("智能评估提前停止", "info", "本次智能评估因平台限制提前停止，已完成结果已经保存，请稍后再继续。");
      } else {
        showToast("智能评估完成", "success", message);
      }
    } else if (job.state === "cancelled") {
      showToast("智能评估已停止", "info", "已保存本次已完成结果，未完成部分将在下次继续。");
    }
    return;
  }
  if (job.action === "delete_expired_reposts") {
    applyDeleteCompletion(job);
    if (repostDeleteProgress) {
      const result = deleteJobResult(job);
      const deleted = Number(result.deleted_count) || 0;
      const failed = Number(result.failed_count) || 0;
      const unknown = Number(result.unknown_count) || 0;
      const skipped = Number(result.skipped_count) || 0;
      const riskStopped = Boolean((job.result as Record<string, unknown> | undefined)?.rate_limited);
      repostDeleteProgress.textContent = riskStopped
        ? `平台限制，本批次已提前停止。已删除 ${deleted} · 失败 ${failed} · 结果未知 ${unknown} · 剩余未执行 ${skipped}。已经完成的结果已保存，剩余项目未执行。`
        : `本次删除：已删除 ${deleted} · 明确失败 ${failed} · 结果未知 ${unknown} · 未执行 ${skipped}`;
    }
    await loadRepostHistory(historyPage).catch(() => {});
    await loadPersistedCandidates().catch(() => {});
  }
}

function handleCandidateChange(event: Event): void {
  const input = (event.target as Element | null)?.closest<HTMLInputElement>("[data-repost-select]");
  if (!input || input.disabled) return;
  const repostId = validDynamicId(input.dataset.repostSelect);
  if (!repostId) return;
  if (input.checked) selectedRepostIds.add(repostId);
  else selectedRepostIds.delete(repostId);
  renderSelectionState();
}

function selectFilteredResults(): void {
  const selectable = visibleCandidates().filter(candidateSelectable);
  if (selectable.length > 20) {
    showToast("单次最多处理 20 条", "info", "已选择前 20 条。");
  }
  for (const candidate of selectable.slice(0, 20)) {
    selectedRepostIds.add(candidate.repost_dynamic_id);
  }
  renderCandidates();
}

function clearCandidateSelection(): void {
  selectedRepostIds.clear();
  renderCandidates();
}

async function handlePaginationClick(event: Event): Promise<void> {
  const button = (event.target as Element | null)?.closest<HTMLButtonElement>("[data-repost-page]");
  if (!button || button.disabled) return;
  const page = Number(button.dataset.repostPage) || 1;
  if (button.dataset.repostPageKind === "candidate") {
    candidatePage = Math.min(Math.max(1, page), candidatePageCount());
    renderCandidates();
    return;
  }
  await loadRepostHistory(Math.min(Math.max(1, page), historyPages)).catch((error) => {
    showToast(sanitizeUserText(error instanceof Error ? error.message : String(error)) || "读取转发历史失败", "error");
  });
}

export function bindRepostCleanup(): void {
  if (bound) return;
  bound = true;
  repostCandidatesBody?.addEventListener("change", handleCandidateChange);
  repostCandidatesBody?.addEventListener("click", (event) => {
    const target = event.target as Element | null;
    const deferButton = target?.closest<HTMLButtonElement>("[data-repost-defer]");
    if (deferButton) {
      const repostId = validDynamicId(deferButton.dataset.repostDefer);
      if (repostId) deferCandidate(repostId);
      return;
    }
    const restoreButton = target?.closest<HTMLButtonElement>("[data-repost-restore]");
    if (restoreButton) {
      const repostId = validDynamicId(restoreButton.dataset.repostRestore);
      if (repostId) restoreCandidate(repostId);
      return;
    }
    const copyButton = target?.closest<HTMLButtonElement>("[data-repost-copy]");
    if (copyButton) {
      const value = String(copyButton.dataset.repostCopy || "").trim();
      if (value) {
        navigator.clipboard?.writeText(value).then(
          () => showToast("已复制", "success", value),
          () => showToast("复制失败", "error"),
        );
      }
      return;
    }
    const reevalButton = target?.closest<HTMLButtonElement>("[data-repost-reeval]");
    if (reevalButton) {
      const originalId = validDynamicId(reevalButton.dataset.repostReeval);
      if (originalId) {
        if (repostEvalStatus) repostEvalStatus.textContent = "定向重新评估中…";
        startCleanupJob("scan_expired_reposts", { force_original_ids: [originalId] });
      }
    }
  });
  repostSelectPageBtn?.addEventListener("click", selectFilteredResults);
  repostClearSelectionBtn?.addEventListener("click", clearCandidateSelection);
  repostSearchInput?.addEventListener("input", () => {
    searchQuery = String((repostSearchInput as HTMLInputElement).value || "");
    candidatePage = 1;
    renderCandidates();
  });
  repostSortSelect?.addEventListener("change", () => {
    sortKey = String((repostSortSelect as HTMLSelectElement).value || "reposted_desc");
    candidatePage = 1;
    renderCandidates();
  });
  repostDeleteSelectedBtn?.addEventListener("click", () => {
    deleteSelectedReposts().catch((error) => {
      notifyJobStartError(error, "delete_expired_reposts", {});
    });
  });
  repostStopBtn?.addEventListener("click", async () => {
    try {
      await fetchJSON("/api/jobs/cancel", { method: "POST" });
    } catch (error) {
      showToast(
        sanitizeUserText(error instanceof Error ? error.message : String(error)) || "停止评估失败",
        "error",
      );
    }
  });
  repostCandidatePagination?.addEventListener("click", handlePaginationClick);
  repostHistoryPagination?.addEventListener("click", handlePaginationClick);
  document.querySelectorAll<HTMLButtonElement>("[data-repost-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      const value = button.dataset.repostFilter;
      if (value === "all" || value === "safe" || value === "manual_review" || value === "deferred" || value === "blocked" || value === "deleted") {
        filter = value;
        candidatePage = 1;
      }
      document.querySelectorAll<HTMLButtonElement>("[data-repost-filter]").forEach((item) => {
        item.classList.toggle("is-active", item === button);
      });
      renderCandidates();
    });
  });
  document.getElementById("repost-scan-btn")?.addEventListener("click", () => {
    // 旧候选可能已过期；每次开始重新扫描即清空，只有本轮成功结果可重新出现。
    setCandidates([]);
    if (repostStopBtn instanceof HTMLButtonElement) repostStopBtn.disabled = false;
    if (repostEvalStatus) repostEvalStatus.textContent = "智能评估进行中：正在本地评估…";
    if (repostCandidateSummary) repostCandidateSummary.textContent = "正在串行分批评估历史抽奖…";
  });
  window.addEventListener("binggo:section-activated", ((event: CustomEvent<{ sectionId?: string }>) => {
    if (event.detail?.sectionId !== "repost-cleanup") return;
    loadRepostHistory(historyPage).catch(() => {});
    loadPersistedCandidates().catch(() => {});
  }) as EventListener);
  window.addEventListener("binggo:job-completed", ((event: CustomEvent<JobStatus>) => {
    handleRepostCleanupJobCompletion(event.detail).catch((error) => {
      showToast(sanitizeUserText(error instanceof Error ? error.message : String(error)) || "清理页面刷新失败", "error");
    });
  }) as EventListener);
  window.addEventListener("binggo:job-progress", ((event: CustomEvent<JobStatus>) => {
    const job = event.detail;
    if (job?.action !== "delete_expired_reposts" || !repostDeleteProgress) return;
    const step = Math.max(0, Number(job.progress_step) || 0);
    const total = Math.max(0, Number(job.progress_total) || 0);
    repostDeleteProgress.textContent = total > 0
      ? `正在删除 ${step} / ${total} …`
      : "删除任务已提交，正在等待结果…";
  }) as EventListener);
  renderCandidates();
}

// Exported for focused UI tests; production actions still flow through the normal Job API.
export async function startCleanupJob(
  action: "sync_repost_history" | "scan_expired_reposts",
  params: Record<string, unknown> = {},
): Promise<void> {
  try {
    await startJob(action, params);
  } catch (error) {
    notifyJobStartError(error, action, params);
  }
}
