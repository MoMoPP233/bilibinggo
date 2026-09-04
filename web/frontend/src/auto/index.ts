// @ts-nocheck
/* eslint-disable */
/** Migrated from web/static/app.js — logic preserved. */

import { state } from "../state";
import { fetchJSON } from "../api/client";
import { autoDock, autoDockBadge, autoDockCountdown, autoDockFatal, autoDockFatalText, autoDockHint, autoDockJob, autoDockPanel, autoDockPhase, autoDockPipeline, autoDockScheduler, autoDockStartBtn, autoDockStatus, autoDockStopBtn, autoDockToggle, autoDockToggleMeta } from "../dom";
import { openAppConfirm } from "../shell/confirm";
import { showToast } from "../shell/toast";
import { formatAutoCountdown } from "../utils/format";
import { prefersReducedMotion } from "../utils/motion";
import { sanitizeUserText, escapeHtml } from "../utils/text";

let schedulerOpSeq = 0;

export function setAutoDockOpen(open) {
  state.autoDockOpen = open;
  if (!autoDockPanel || !autoDockToggle) return;
  autoDockPanel.classList.toggle("is-open", open);
  autoDockPanel.setAttribute("aria-hidden", String(!open));
  autoDockToggle.setAttribute("aria-expanded", String(open));
  autoDock?.classList.toggle("open", open);
  if (open) {
    if (!prefersReducedMotion()) {
      autoDockPanel.classList.remove("is-entering");
      void autoDockPanel.offsetWidth;
      autoDockPanel.classList.add("is-entering");
    }
    fetchAutoStatus().catch(() => {});
    ensureAutoPolling();
    ensureAutoCountdown();
    tickAutoCountdown();
  } else {
    autoDockPanel.classList.remove("is-entering");
    if (!(state.autoScheduler && state.autoScheduler.state === "running")) {
      stopAutoPolling();
    }
  }
}

export function getAutoCountdownSeconds(targetUnix) {
  if (!targetUnix) return null;
  const nowSec = Math.floor((Date.now() + state.autoServerSkewMs) / 1000);
  return Math.max(0, Number(targetUnix) - nowSec);
}

export function toggleAutoDock(forceOpen) {
  const next = typeof forceOpen === "boolean" ? forceOpen : !state.autoDockOpen;
  setAutoDockOpen(next);
}

export function resolveAutoSchedulerText(status) {
  const schedulerState = String(status?.state || "idle");
  if (schedulerState === "fatal") {
    return sanitizeUserText(status.fatal_error || status.message || "已停机");
  }
  if (schedulerState !== "running") {
    return sanitizeUserText(status.state_label || status.message || "尚未启动");
  }
  const phase = sanitizeUserText(status.current_phase || "");
  if (phase && phase !== "—") return phase;
  return sanitizeUserText(status.message || "调度运行中");
}

export function resolveAutoJobText(status) {
  const jobProbe = status?.job_probe || {};
  const jobState = String(jobProbe.job_state || "idle");
  const jobLabel = sanitizeUserText(jobProbe.job_label || jobProbe.job_action || "");
  if (jobState === "running") {
    return jobLabel ? `运行中 · ${jobLabel}` : "运行中";
  }
  if (jobState === "idle") return "空闲";
  if (jobState === "success") return jobLabel ? `刚完成 · ${jobLabel}` : "刚完成";
  if (jobState === "error") return jobLabel ? `出错 · ${jobLabel}` : "出错";
  return jobLabel || jobState || "—";
}

export function resolveAutoJobTone(status) {
  const jobState = String(status?.job_probe?.job_state || "idle");
  if (jobState === "running") return "running";
  if (jobState === "error") return "error";
  if (jobState === "success") return "success";
  return "idle";
}

export function renderAutoPipeline(pipeline) {
  if (!autoDockPipeline) return;
  const steps = Array.isArray(pipeline?.steps) ? pipeline.steps : [];
  if (!steps.length) {
    autoDockPipeline.innerHTML = "";
    return;
  }
  autoDockPipeline.innerHTML = steps
    .map((step, index) => {
      const label = escapeHtml(sanitizeUserText(step.label || step.action || "") || "");
      const rawStatus = String(step.status || "pending");
      const status = ["pending", "running", "success", "error", "skipped", "waiting"].includes(rawStatus)
        ? rawStatus
        : "pending";
      const stepIndex = Number.isFinite(step.index) ? Number(step.index) + 1 : index + 1;
      return `<div class="auto-dock-step" data-status="${escapeHtml(status)}" data-index="${stepIndex}"><span class="auto-dock-step-label">${label}</span></div>`;
    })
    .join("");
}

export function updateAutoCollapsedMeta(status) {
  if (!autoDockToggleMeta) return;
  const schedulerState = String(status?.state || "idle");
  if (schedulerState === "running") {
    const countdown = formatAutoCountdown(status?.next_slot?.at_unix);
    autoDockToggleMeta.hidden = false;
    autoDockToggleMeta.textContent = countdown === "—" ? "…" : countdown;
    return;
  }
  // fatal / idle：角标或默认文案已够，折叠态不再重复「已停机」
  autoDockToggleMeta.hidden = true;
  autoDockToggleMeta.textContent = "";
}

function fmtNextTaskWhen(minutes) {
  const m = Math.max(0, Number(minutes) || 0);
  if (m < 1) return "不到1分钟后执行";
  if (m < 60) return `${m}分钟后执行`;
  const hours = Math.floor(m / 60);
  const rest = m % 60;
  return rest ? `${hours}小时${rest}分钟后执行` : `${hours}小时后执行`;
}

function renderNextAutoTask(status) {
  const el = document.getElementById("auto-next-task");
  if (!el) return;
  const task = status?.next_task;
  if (!task || !Number.isFinite(Number(task.minutes))) {
    el.textContent = "—";
    return;
  }
  const label = sanitizeUserText(task.label || task.action || "自动任务");
  el.textContent = `${fmtNextTaskWhen(task.minutes)}：${label}`;
}

export function renderAutoDock(status) {
  if (!status) return;
  state.autoScheduler = status;
  if (status.server_now_unix) {
    state.autoServerSkewMs = status.server_now_unix * 1000 - Date.now();
  }

  const schedulerState = String(status.state || "idle");
  const stateLabel = sanitizeUserText(status.state_label || status.message || "尚未启动");
  const message = sanitizeUserText(status.message || "");
  const phase = sanitizeUserText(status.current_phase || "—");
  const hint = sanitizeUserText(status.next_hint || status.next_slot?.hint || "—");

  if (autoDockStatus) {
    // 头部只保留短状态，长进度细节交给「调度 / 抽奖任务」行
    autoDockStatus.textContent =
      schedulerState === "running"
        ? sanitizeUserText(status.state_label || "调度器运行中")
        : message || stateLabel;
  }
  if (autoDockPhase) {
    autoDockPhase.textContent = phase;
    autoDockPhase.classList.toggle("is-live", schedulerState === "running");
  }
  if (autoDockHint) {
    autoDockHint.textContent = hint;
  }
  const hero = document.querySelector(".auto-dock-hero");
  const pipelineBlock = document.querySelector(".auto-dock-pipeline-block");
  const countdownSec = getAutoCountdownSeconds(status.next_slot?.at_unix);
  const urgent = schedulerState === "running" && countdownSec !== null && countdownSec > 0 && countdownSec < 60;
  if (hero) {
    hero.classList.toggle("is-live", schedulerState === "running");
    hero.classList.toggle("is-urgent", urgent);
  }
  if (pipelineBlock) {
    pipelineBlock.classList.toggle("is-active", Boolean(status.refresh_pipeline?.active));
  }
  if (autoDockScheduler) {
    autoDockScheduler.textContent = resolveAutoSchedulerText(status);
    autoDockScheduler.dataset.tone = schedulerState === "fatal" ? "error" : schedulerState === "running" ? "running" : "idle";
  }
  if (autoDockJob) {
    autoDockJob.textContent = resolveAutoJobText(status);
    autoDockJob.dataset.tone = resolveAutoJobTone(status);
  }
  tickAutoCountdown();
  renderAutoPipeline(status.refresh_pipeline);
  updateAutoCollapsedMeta(status);
  renderNextAutoTask(status);

  const running = schedulerState === "running";
  const fatal = schedulerState === "fatal";

  autoDock?.classList.toggle("fatal", fatal);
  autoDock?.classList.toggle("is-running", running);
  if (autoDockBadge) {
    autoDockBadge.hidden = !running && !fatal;
    autoDockBadge.textContent = fatal ? "已停机" : "运行中";
    autoDockBadge.classList.toggle("fatal", fatal);
  }
  if (autoDockFatal) {
    autoDockFatal.hidden = !fatal;
  }
  if (autoDockFatalText) {
    autoDockFatalText.textContent = sanitizeUserText(status.fatal_error || "");
  }
  if (autoDockStartBtn) {
    autoDockStartBtn.hidden = running;
  }
  if (autoDockStopBtn) {
    autoDockStopBtn.hidden = !running;
  }

  if (running || state.autoDockOpen) {
    if (state.sseHealthy) {
      ensureAutoCountdown();
      // SSE 健康时停 2s 轮询，倒计时仍本地跑
      if (state.autoPollTimer) {
        window.clearInterval(state.autoPollTimer);
        state.autoPollTimer = null;
      }
    } else {
      ensureAutoPolling();
      ensureAutoCountdown();
    }
  } else if (!state.sseHealthy) {
    stopAutoPolling();
  } else {
    ensureAutoCountdown();
  }
}

export function tickAutoCountdown() {
  const status = state.autoScheduler;
  if (!autoDockCountdown) return;
  const targetUnix = status?.next_slot?.at_unix;
  autoDockCountdown.textContent = formatAutoCountdown(targetUnix);
  const countdownSec = getAutoCountdownSeconds(targetUnix);
  const running = String(status?.state || "") === "running";
  const urgent = running && countdownSec !== null && countdownSec > 0 && countdownSec < 60;
  autoDockCountdown.classList.toggle("is-urgent", urgent);
  document.querySelector(".auto-dock-hero")?.classList.toggle("is-urgent", urgent);
  updateAutoCollapsedMeta(status);
}

export function ensureAutoCountdown() {
  if (state.autoCountdownTimer) return;
  state.autoCountdownTimer = window.setInterval(tickAutoCountdown, 1000);
}

export function ensureAutoPolling() {
  if (state.sseHealthy && state.eventSource) {
    ensureAutoCountdown();
    return;
  }
  if (!state.autoPollTimer) {
    state.autoPollTimer = window.setInterval(() => {
      if (state.sseHealthy && state.eventSource) return;
      fetchAutoStatus().catch(() => {});
    }, 2000);
  }
  ensureAutoCountdown();
}

export function stopAutoPolling() {
  if (state.autoPollTimer) {
    window.clearInterval(state.autoPollTimer);
    state.autoPollTimer = null;
  }
  if (state.autoCountdownTimer) {
    window.clearInterval(state.autoCountdownTimer);
    state.autoCountdownTimer = null;
  }
}

export async function fetchAutoStatus(opToken) {
  const status = await fetchJSON("/api/auto/status");
  if (opToken !== undefined && opToken !== schedulerOpSeq) return status;
  renderAutoDock(status);
  if (opToken !== undefined) {
    // 带 token 的调用是 start/stop 后台刷新：过期则整体跳过。
    return status;
  }
  await refreshFollowingInfo();
  return status;
}

function fmtFollowingHM(value) {
  if (!value) return "";
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (part) => String(part).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function renderFollowingInfo(data) {
  const info = document.getElementById("auto-following-info");
  if (!info) return;
  const status = String(data?.last_status || "waiting");
  const parts = [];
  if (status === "failed") {
    parts.push("关注补漏：上次扫描失败");
    if (String(data?.last_error || "")) {
      parts.push(`原因：${sanitizeUserText(String(data.last_error))}`);
    }
  } else if (data?.last_scan_at) {
    parts.push(`关注补漏：上次 ${fmtFollowingHM(data.last_scan_at)}`);
  } else {
    parts.push("关注补漏：尚未扫描");
  }
  if (data?.next_scan_at) {
    parts.push(`下次 ${fmtFollowingHM(data.next_scan_at)}`);
  }
  if (parts.length) {
    info.hidden = false;
    info.textContent = parts.join(" · ");
  } else {
    info.hidden = true;
    info.textContent = "";
  }
}

export async function refreshFollowingInfo() {
  try {
    const data = await fetchJSON("/api/auto/following-feed");
    renderFollowingInfo(data);
  } catch {
    // 读取失败不阻塞自动面板。
  }
}

export async function startAutoScheduler() {
  const token = ++schedulerOpSeq;
  const status = await fetchJSON("/api/auto/start", { method: "POST" });
  if (token !== schedulerOpSeq) return;
  renderAutoDock(status);
  ensureAutoCountdown();
  fetchAutoStatus(token).catch(() => {});
  showToast("调度已启动", "success", "自动远程任务由程序统一按时间执行。");
}

export async function stopAutoScheduler() {
  const ok = await openAppConfirm({
    eyebrow: "定时调度",
    title: "确定停止调度？",
    desc: "停止调度只会停止安排新的自动任务；正在执行的任务会自然结束，不会被强制取消。",
    confirmLabel: "停止调度",
    cancelLabel: "继续运行",
  });
  if (!ok) return;
  const token = ++schedulerOpSeq;
  const status = await fetchJSON("/api/auto/stop", { method: "POST" });
  if (token !== schedulerOpSeq) return;
  renderAutoDock(status);
  fetchAutoStatus(token).catch(() => {});
  showToast("调度已停止", "info", "正在执行的任务会安全结束，之后不再安排新的自动任务。");
}

export function bindAutoDock() {
  autoDockToggle?.addEventListener("click", () => toggleAutoDock());
  document.getElementById("auto-dock-collapse")?.addEventListener("click", () => toggleAutoDock(false));
  autoDockStartBtn?.addEventListener("click", () => {
    autoDockStartBtn.disabled = true;
    startAutoScheduler()
      .catch((error) => showToast(sanitizeUserText(error.message || error) || "启动失败", "error"))
      .finally(() => {
        autoDockStartBtn.disabled = false;
      });
  });
  autoDockStopBtn?.addEventListener("click", () => {
    autoDockStopBtn.disabled = true;
    stopAutoScheduler()
      .catch((error) => showToast(sanitizeUserText(error.message || error) || "停止失败", "error"))
      .finally(() => {
        autoDockStopBtn.disabled = false;
      });
  });
}

export function autoLogKey(row) {
  return `${row?.ts || ""}|${row?.level || ""}|${row?.message || ""}`;
}

export function mergeAutoLogs(existing, incoming) {
  const map = new Map();
  for (const row of existing || []) {
    if (!row) continue;
    map.set(autoLogKey(row), row);
  }
  for (const row of incoming || []) {
    if (!row) continue;
    map.set(autoLogKey(row), row);
  }
  return Array.from(map.values()).slice(-80);
}
