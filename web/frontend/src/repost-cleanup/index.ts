import { requireSetup } from "../account/index";
import { fetchJSON } from "../api/client";
import {
  repostCandidatePagination,
  repostCandidateSummary,
  repostCandidateTotal,
  repostCandidatesBody,
  repostCheckpointStatus,
  repostClearSelectionBtn,
  repostDeleteSelectedBtn,
  repostHistoryBody,
  repostHistoryPagination,
  repostHistorySummary,
  repostHistoryTotal,
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
  return status === "active" || status === "delete_failed";
}

function candidatePageCount(): number {
  return Math.max(1, Math.ceil(candidates.length / CANDIDATE_PAGE_SIZE));
}

function currentCandidatePageItems(): CleanupCandidate[] {
  const start = (candidatePage - 1) * CANDIDATE_PAGE_SIZE;
  return candidates.slice(start, start + CANDIDATE_PAGE_SIZE);
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
  const validIds = new Set(candidates.filter(candidateSelectable).map((item) => item.repost_dynamic_id));
  selectedRepostIds = new Set([...selectedRepostIds].filter((id) => validIds.has(id)));

  if (repostCandidateTotal) repostCandidateTotal.textContent = String(candidates.length);
  if (repostCandidateSummary) {
    repostCandidateSummary.textContent = candidates.length
      ? `严格校验得到 ${candidates.length} 条候选；候选默认不勾选，删除前服务端还会再次校验。`
      : "当前没有能够通过官方信息严格确认安全的删除候选。";
  }
  if (repostCandidatesBody) {
    if (!pageItems.length) {
      repostCandidatesBody.innerHTML = '<tr class="empty-row"><td colspan="5">暂无可安全删除候选</td></tr>';
    } else {
      repostCandidatesBody.innerHTML = pageItems.map((candidate) => {
        const repostId = candidate.repost_dynamic_id;
        const originalId = candidate.original_dynamic_id;
        const selectable = candidateSelectable(candidate);
        const status = historyStatus(candidate.delete_status);
        const author = sanitizeUserText(candidate.original_author_name || "") || "原作者未知";
        const reason = sanitizeUserText(candidate.reason || "") || "官方抽奖已结束，且当前账号不在完整中奖名单中";
        const lotteryType = sanitizeUserText(candidate.lottery_type || "") || "官方抽奖";
        const lotteryTime = formatTimestamp(candidate.lottery_time);
        const message = sanitizeUserText(candidate.delete_message || "");
        return `
          <tr class="repost-candidate-row" data-repost-row="${escapeHtml(repostId)}">
            <td class="repost-check-cell">
              <input type="checkbox" class="repost-checkbox" data-repost-select="${escapeHtml(repostId)}" aria-label="选择本人转发 ${escapeHtml(repostId)}" ${selectedRepostIds.has(repostId) ? "checked" : ""} ${selectable ? "" : "disabled"} />
            </td>
            <td>
              <a class="activity-link" href="${opusUrl(repostId)}" target="_blank" rel="noopener">查看本人转发</a>
              <p class="repost-id">${escapeHtml(repostId)}</p>
              <p class="caption">${escapeHtml(formatTimestamp(candidate.reposted_at))}</p>
            </td>
            <td>
              <a class="activity-link" href="${opusUrl(originalId)}" target="_blank" rel="noopener">查看原动态</a>
              <p class="repost-author">${escapeHtml(author)}</p>
            </td>
            <td>
              <span class="type-chip type-chip--interact">${escapeHtml(lotteryType)}</span>
              <p class="caption repost-lottery-time">开奖：${escapeHtml(lotteryTime)}</p>
            </td>
            <td>
              ${selectable ? `<p class="repost-reason">${escapeHtml(reason)}</p>` : `<span class="repost-status repost-status--${status.tone}">${status.label}</span>`}
              ${message ? `<p class="caption repost-error">${escapeHtml(message)}</p>` : ""}
            </td>
          </tr>`;
      }).join("");
    }
  }
  renderPager(repostCandidatePagination, candidatePage, pages, candidates.length, "candidate");
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
    const data = await fetchJSON<RepostCandidatesResponse>("/api/repost-cleanup/candidates");
    setCandidates(normalizeCandidates(data?.candidates));
  } catch {
    // 登录/网络异常时保留当前已渲染候选，避免误清空。
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
  if (!requireSetup("delete_expired_reposts")) return;

  deletePromptOpen = true;
  let confirmed = false;
  try {
    confirmed = await openAppConfirm({
      eyebrow: "危险操作",
      title: `确定删除选中的 ${ids.length} 条个人转发吗？`,
      desc: "将从当前登录的 Bilibili 账号中删除这些转发动态。此操作无法撤销。",
      bullets: [
        `本次只提交 ${ids.length} 个“本人转发动态 ID”，绝不会提交原抽奖动态 ID`,
        "服务端会在每条删除前重新确认运行 Profile、UID、动态归属与官方开奖状态",
        "网络超时或响应不确定时会标记为 unknown，不会自动再次发送删除请求",
      ],
      confirmLabel: `确认删除 ${ids.length} 条`,
      cancelLabel: "取消",
      danger: true,
    });
  } finally {
    deletePromptOpen = false;
  }
  if (!confirmed) return;

  deleteSubmitting = true;
  pendingDeleteIds = new Set(ids);
  renderSelectionState();
  try {
    await fetchJSON("/api/repost-cleanup/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repost_dynamic_ids: ids, confirmed: true }),
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
  // 全局任务管理器不会盲目重新启用 data-job-control；按本页选择状态恢复。
  renderSelectionState();
  if (job.action === "sync_repost_history") {
    if (job.state === "success") {
      const result = (job.result || {}) as Record<string, unknown>;
      showToast("转发历史同步完成", "success", `本次导入 ${Number(result.imported_count) || 0} 条`);
      await loadRepostHistory(1).catch(() => {});
    }
    return;
  }
  if (job.action === "scan_expired_reposts") {
    if (job.state === "success") {
      const found = extractJobCandidates(job);
      setCandidates(found);
      showToast("安全候选扫描完成", "success", `严格校验得到 ${found.length} 条候选，当前均未勾选`);
    }
    return;
  }
  if (job.action === "delete_expired_reposts") {
    applyDeleteCompletion(job);
    await loadRepostHistory(historyPage).catch(() => {});
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

function selectCurrentCandidatePage(): void {
  for (const candidate of currentCandidatePageItems()) {
    if (candidateSelectable(candidate)) selectedRepostIds.add(candidate.repost_dynamic_id);
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
  repostSelectPageBtn?.addEventListener("click", selectCurrentCandidatePage);
  repostClearSelectionBtn?.addEventListener("click", clearCandidateSelection);
  repostDeleteSelectedBtn?.addEventListener("click", () => {
    deleteSelectedReposts().catch((error) => {
      notifyJobStartError(error, "delete_expired_reposts", {});
    });
  });
  repostCandidatePagination?.addEventListener("click", handlePaginationClick);
  repostHistoryPagination?.addEventListener("click", handlePaginationClick);
  document.getElementById("repost-scan-btn")?.addEventListener("click", () => {
    // 旧候选可能已过期；每次开始重新扫描即清空，只有本轮成功结果可重新出现。
    setCandidates([]);
    if (repostCandidateSummary) repostCandidateSummary.textContent = "正在重新校验安全候选…";
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
  renderCandidates();
}

// Exported for focused UI tests; production actions still flow through the normal Job API.
export async function startCleanupJob(action: "sync_repost_history" | "scan_expired_reposts"): Promise<void> {
  try {
    await startJob(action);
  } catch (error) {
    notifyJobStartError(error, action, {});
  }
}
