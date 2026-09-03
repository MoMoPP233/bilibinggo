// @ts-nocheck
/* eslint-disable */
/** 一键更新全部数据源（严格串行编排）——前端即时反馈 + 逻辑锁 + 进度面板。 */

import { state } from "../state";
import { fetchJSON } from "../api/client";
import { requireSetup } from "../account/index";
import { startJob, updateJobUI } from "../jobs/index";
import { showToast } from "../shell/toast";
import { escapeHtml, sanitizeUserText } from "../utils/text";

const ACTION = "update_all_datasources";
let starting = false;
let stopBusy = false;
let bound = false;

function getElements() {
  return {
    panel: document.getElementById("update-all-panel"),
    summary: document.getElementById("update-all-summary"),
    lanes: document.getElementById("update-all-lanes"),
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
  const { panel, summary, lanes } = getElements();
  if (panel) panel.hidden = false;
  if (summary) summary.textContent = "正在启动全部数据源更新…";
  if (lanes) lanes.innerHTML = '<p class="caption">等待任务开始…</p>';
}

function statusIcon(status) {
  switch (String(status || "waiting")) {
    case "success":
      return "✓";
    case "failed":
      return "✕";
    case "risk":
      return "!";
    case "running":
      return "●";
    case "not_run":
    case "stopped":
      return "—";
    default:
      return "○";
  }
}

function scanMeta(entry) {
  const status = String(entry?.status || "waiting");
  if (status !== "success" && status !== "risk") return [];
  const discovered = Number(entry?.discovered_count) || 0;
  if (entry?.updated === false && entry?.pipeline_skipped) {
    return [`本轮源返回 ${discovered}`, "未进入候选处理"];
  }
  const duplicateOrInvalid = (Number(entry?.duplicate_link_count) || 0)
    + (Number(entry?.invalid_link_count) || 0);
  const safeSkipped = (Number(entry?.non_lottery_count) || 0)
    + (Number(entry?.other_skipped_count) || 0);
  const meta = [
    `发现 ${discovered}`,
    `已有 ${Number(entry?.existing_count) || 0}`,
  ];
  if (duplicateOrInvalid > 0) meta.push(`重复/无效 ${duplicateOrInvalid}`);
  meta.push(
    `新候选 ${Number(entry?.candidate_count ?? entry?.new_link_count) || 0}`,
    `过期 ${Number(entry?.expired_skipped_count) || 0}`,
    `非抽奖/其他 ${safeSkipped}`,
    `失败 ${Number(entry?.processing_failed_count) || 0}`,
  );
  if (Number(entry?.persist_skipped_count) > 0) {
    meta.push(`写入时已有 ${Number(entry.persist_skipped_count)}`);
  }
  meta.push(`新增 ${Number(entry?.persisted_count) || 0}`);
  return meta;
}

function renderLanes(payload) {
  const { lanes } = getElements();
  if (!lanes) return;
  const sources = Array.isArray(payload?.sources) ? payload.sources : [];
  if (!sources.length) return;
  lanes.innerHTML = sources
    .map((entry) => {
      const status = String(entry?.status || "waiting");
      const name = sanitizeUserText(entry?.name) || String(entry?.source_id || "");
      const key = escapeHtml(String(entry?.source_id || ""));
      const message = sanitizeUserText(entry?.message || "") || "";
      const meta = scanMeta(entry);
      const metaText = meta.length ? ` · ${meta.join(" · ")}` : "";
      return `
        <div class="update-all-lane" data-status="${escapeHtml(status)}">
          <span class="update-all-lane-key"><span class="update-all-lane-dot" aria-hidden="true"></span>${key}</span>
          <span class="update-all-lane-name">${escapeHtml(name)}</span>
          <span class="update-all-lane-message">${escapeHtml(message || status)}${escapeHtml(metaText)}</span>
        </div>`;
    })
    .join("");
}

function renderPanel(payload) {
  const { summary } = getElements();
  const runningCurrent = Number(payload?.current_index ?? 0) + 1;
  const total = Number(payload?.total) || 0;
  const phase = String(payload?.phase || "running");
  let text = "";
  if (phase === "done") {
    text = String(payload?.summary || "全部数据源更新完成");
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
  renderLanes(payload);
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
    if (!current || current.state === "idle") {
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
