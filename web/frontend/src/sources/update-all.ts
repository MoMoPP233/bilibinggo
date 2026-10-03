// @ts-nocheck
/* eslint-disable */
/** 一键更新全部数据源（严格串行编排）——前端即时反馈 + 逻辑锁 + 进度面板。 */

import { state } from "../state";
import { fetchJSON } from "../api/client";
import { requireSetup } from "../account/index";
import { startJob, updateJobUI } from "../jobs/index";
import { showToast } from "../shell/toast";
import { sanitizeUserText } from "../utils/text";

const ACTION = "update_all_datasources";
let starting = false;
let stopBusy = false;
let bound = false;

function getElements() {
  return {
    summary: document.getElementById("update-all-summary"),
    startBtn: document.getElementById("update-all-sources-btn"),
    stopBtn: document.getElementById("update-all-stop-btn"),
  };
}

function runningJob() {
  const job = state.currentJob || null;
  return job && job.state === "running" ? job : null;
}

function waitForNextFrame() {
  return new Promise((resolve) => {
    if (typeof window.requestAnimationFrame === "function") {
      window.requestAnimationFrame(() => resolve());
    } else {
      window.setTimeout(resolve, 0);
    }
  });
}

function refreshControls() {
  const { startBtn, stopBtn } = getElements();
  const running = runningJob() !== null;
  if (startBtn) startBtn.disabled = running || starting;
  if (stopBtn) stopBtn.disabled = !running || stopBusy;
}

function showPanel() {
  const { summary } = getElements();
  if (summary) summary.textContent = "正在启动全部数据源更新…";
}

function sourceCounts(payload) {
  const sources = Array.isArray(payload?.sources) ? payload.sources : [];
  return sources.reduce((counts, item) => {
    const status = String(item?.status || "");
    if (status === "success") counts.success += 1;
    if (["failed", "error", "risk"].includes(status)) counts.failed += 1;
    return counts;
  }, { success: 0, failed: 0 });
}

function compactSummary(payload) {
  const phase = String(payload?.phase || "running");
  const counts = sourceCounts(payload);
  const success = Number(payload?.success_count);
  const failed = Number(payload?.failed_count);
  const successCount = Number.isFinite(success) ? success : counts.success;
  const failedCount = Number.isFinite(failed) ? failed : counts.failed;
  const totals = payload?.totals || payload || {};
  const discovered = Number(totals?.discovered_count) || 0;
  const persisted = Number(totals?.persisted_count) || 0;
  const status = phase === "done"
    ? (failedCount > 0 ? "更新完成（有失败）" : "更新完成")
    : phase === "stopped"
      ? "已停止更新"
      : "正在更新";
  return `${status}（成功 ${successCount}，失败 ${failedCount}） · 本轮发现 ${discovered} · 实际新增 ${persisted}`;
}

function renderPanel(payload) {
  const { summary } = getElements();
  const runningCurrent = Number(payload?.current_index ?? 0) + 1;
  const total = Number(payload?.total) || 0;
  const phase = String(payload?.phase || "running");
  let text = "";
  if (phase === "done" || phase === "stopped") {
    text = compactSummary(payload);
  } else if (total > 0) {
    const active = Array.isArray(payload?.sources)
      ? payload.sources.find((item) => String(item?.status) === "running")
      : null;
    if (active) {
      text = `正在串行更新 ${active?.source_id || ""}（${active?.name || ""}） ${runningCurrent}/${total}`;
    } else {
      text = `正在串行更新数据源 ${runningCurrent}/${total}`;
    }
    const totals = payload?.totals || null;
    if (totals && Number(totals?.finished_sources) > 0) {
      text += `；累计发现 ${Number(totals?.discovered_count) || 0}，过期 ${Number(totals?.expired_skipped_count) || 0}，新增 ${Number(totals?.persisted_count) || 0}`;
    }
  } else {
    text = "正在启动全部数据源更新…";
  }
  if (summary) summary.textContent = text;
  window.dispatchEvent(new CustomEvent("binggo:update-all-result", { detail: payload }));
}

function applyJobPayload(job) {
  const payload = job?.result?.update_all || job?.result?.sources_update || null;
  if (!payload) return;
  renderPanel(payload);
}

export function renderUpdateAllStarting() {
  showPanel();
}

export async function startUpdateAllDatasources() {
  if (starting || stopBusy) return;
  if (runningJob()) {
    showToast("已有任务正在运行", "info", "请等待当前任务结束后再试。");
    refreshControls();
    return;
  }
  if (!requireSetup(ACTION)) return;

  starting = true;
  refreshControls();
  updateJobUI({
    id: null,
    state: "running",
    action: ACTION,
    label: "一键更新全部数据源",
    source: "ui",
    message: "正在启动全部数据源更新…",
    progress_message: "正在启动全部数据源更新…",
    log: "",
    progress_step: 0,
    progress_total: 7,
  });
  showPanel();
  await waitForNextFrame();
  try {
    await startJob(ACTION, {});
  } catch (error) {
    showToast(
      sanitizeUserText(error?.message || error) || "启动全部数据源更新失败",
      "error",
    );
  } finally {
    starting = false;
    const current = state.currentJob || null;
    if (!current || (current.id == null && current.action === ACTION)) {
      const { summary } = getElements();
      if (summary) summary.textContent = "全部数据源更新任务已结束";
    }
    refreshControls();
  }
}

export async function stopUpdateAllDatasources() {
  if (stopBusy) return;
  const job = runningJob();
  if (!job || job.action !== ACTION) {
    showToast("当前没有可停止的全部数据源更新任务", "info");
    refreshControls();
    return;
  }
  stopBusy = true;
  refreshControls();
  try {
    await fetchJSON("/api/jobs/cancel", { method: "POST" });
    showToast("停止请求已发送", "info", "当前数据源结束后会停止，已完成结果保留。");
  } catch (error) {
    showToast(
      sanitizeUserText(error?.message || error) || "停止更新失败",
      "error",
    );
  } finally {
    stopBusy = false;
    refreshControls();
  }
}

function handleJobProgress(event) {
  const job = event.detail;
  if (!job || job.action !== ACTION) return;
  renderUpdateAllFromJob(job);
  refreshControls();
}

function handleJobCompleted(event) {
  const job = event.detail;
  if (!job || job.action !== ACTION) return;
  renderUpdateAllFromJob(job);
  refreshControls();
  const { summary } = getElements();
  if (summary && !summary.textContent) {
    summary.textContent = "全部数据源更新完成";
  }
}

function renderUpdateAllFromJob(job) {
  applyJobPayload(job);
}

export function bindUpdateAllDatasources() {
  if (bound) return;
  bound = true;
  const { startBtn, stopBtn } = getElements();
  startBtn?.addEventListener("click", () => {
    startUpdateAllDatasources().catch(() => {});
  });
  stopBtn?.addEventListener("click", () => {
    stopUpdateAllDatasources().catch(() => {});
  });
  window.addEventListener("binggo:job-progress", handleJobProgress);
  window.addEventListener("binggo:job-completed", handleJobCompleted);
  refreshControls();
}
