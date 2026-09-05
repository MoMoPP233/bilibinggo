// @ts-nocheck
/* eslint-disable */
/** Migrated from web/static/app.js — logic preserved. */

import { state } from "../state";
import { ensureAutoCountdown, ensureAutoPolling, mergeAutoLogs, renderAutoDock } from "../auto/index";
import { acceptJobUpdate, appendJobLogChunk, applyRunningJobView, finishJobOnce, isJobTerminalAccepted, isJobTerminalState, mergeJobProgress, resetJobStreamFreshness, startPolling, stopJobPolling, updateJobUI } from "../jobs/index";

export const SSE_WATCHDOG_MS = 45000;

export const SSE_RECONNECT_MS = 3000;

export function markSseActive() {
  state.sseLastActive = Date.now();
}

export function stopSseWatchdog() {
  if (state.sseWatchdog) {
    window.clearInterval(state.sseWatchdog);
    state.sseWatchdog = null;
  }
}

export function startSseWatchdog() {
  stopSseWatchdog();
  state.sseWatchdog = window.setInterval(() => {
    if (!state.sseHealthy) return;
    if (Date.now() - state.sseLastActive > SSE_WATCHDOG_MS) {
      console.warn("SSE heartbeat timeout, fallback to polling");
      fallbackToPolling("heartbeat-timeout");
    }
  }, 5000);
}

export function closeEventSource() {
  if (state.eventSource) {
    try {
      state.eventSource.close();
    } catch {
      /* ignore */
    }
    state.eventSource = null;
  }
  stopSseWatchdog();
  state.sseHealthy = false;
}

export function fallbackToPolling(reason) {
  closeEventSource();
  // 浏览器不支持 EventSource 时不要循环重连
  if (reason !== "no-eventsource") {
    if (state.sseReconnectTimer) {
      window.clearTimeout(state.sseReconnectTimer);
    }
    state.sseReconnectTimer = window.setTimeout(() => {
      state.sseReconnectTimer = null;
      startRealtime();
    }, SSE_RECONNECT_MS);
  }
  const job = state.currentJob;
  if (job?.state === "running") startPolling();
  if (state.autoDockOpen || state.autoScheduler?.state === "running") {
    ensureAutoPolling();
  }
}

function sameJobAsCurrent(payload) {
  const current = state.currentJob;
  if (!current || current.id === undefined || current.id === null || current.id === "") {
    // 尚无权威身份：允许（随后 snapshot/job.created 会建立身份）。
    return true;
  }
  if (payload?.id === undefined || payload?.id === null || payload?.id === "") {
    return true;
  }
  return Number(current.id) === Number(payload.id);
}

/**
 * terminal snapshot 是否应当补一次完整 completion。
 *
 * 只有“页面此前已经知道该 Job 正在运行”才算断线期间完成（reconnect recovery）；
 * 首次页面基线里出现的历史终态只做展示，绝不重放 toast/结果/后台刷新。
 */
export function terminalSnapshotShouldComplete(job, currentJob) {
  if (!isJobTerminalState(job?.state)) return false;
  if (!currentJob || currentJob?.id == null || job?.id == null) return false;
  if (Number(currentJob.id) !== Number(job.id)) return false;
  return currentJob.state === "running";
}

export function handleSseMessage(eventName, payload) {
  markSseActive();
  if (eventName === "heartbeat") return;

  if (eventName === "job.snapshot") {
    // 快照参与同一套 freshness：snapshot 携带读取时 watermark（payload.seq）。
    const job = { ...payload };
    if (!acceptJobUpdate(job, { seq: payload?.seq })) {
      // 旧快照（已被新事件覆盖/该 job 已 terminal）：不得回退 UI。
      return;
    }
    if (isJobTerminalState(job.state)) {
      // 历史终态基线 vs 断线期间完成：
      // - 页面从不知道它在 running → 只展示（绝不重放 completion/toast）；
      // - 页面正 tracking 该 running Job → reconnect snapshot terminal 必须完整 finishJobOnce。
      if (terminalSnapshotShouldComplete(job, state.currentJob)) {
        void finishJobOnce(job);
      } else {
        state.currentJob = job;
        updateJobUI(job);
      }
      return;
    }
    applyRunningJobView(job);
    return;
  }

  if (eventName === "job.created") {
    const job = {
      ...(state.currentJob || {}),
      ...payload,
      state: "running",
    };
    // 新任务不得沿用上一任务的 log/result/进度
    job.log = "";
    job.result = payload.result && typeof payload.result === "object" ? payload.result : {};
    job.progress_step = payload.progress_step ?? 0;
    job.progress_total = payload.progress_total ?? 0;
    job.finished_at = null;
    job.message = payload.message || "任务已启动";
    state.lastFinishedJobKey = "";
    if (!acceptJobUpdate(job, { seq: payload?.seq })) return;
    applyRunningJobView(job);
    return;
  }

  if (eventName === "job.progress") {
    if (!sameJobAsCurrent(payload)) return; // 旧 Job 的 progress 不写当前 Job
    if (!acceptJobUpdate({ id: payload?.id, state: "running" }, { seq: payload?.seq })) {
      return;
    }
    applyRunningJobView(mergeJobProgress(payload));
    return;
  }

  if (eventName === "job.log") {
    if (!payload?.chunk) return;
    if (!sameJobAsCurrent(payload)) return; // 旧 Job 的 log 不写当前 Job
    if (!acceptJobUpdate({ id: payload?.id, state: "running" }, { seq: payload?.seq })) {
      return;
    }
    appendJobLogChunk(String(payload.chunk));
    return;
  }

  if (eventName === "job.terminal") {
    const job = { ...payload, state: payload.state };
    if (!acceptJobUpdate(job, { seq: payload?.seq })) {
      // 该 job 已 terminal（重复路径）或属于旧身份：不再重复 completion。
      return;
    }
    void finishJobOnce(job);
    return;
  }

  if (eventName === "auto.snapshot") {
    if (Array.isArray(payload.logs)) {
      state.autoLogs = mergeAutoLogs(state.autoLogs, payload.logs);
    }
    renderAutoDock({ ...payload, logs: state.autoLogs });
    return;
  }

  if (eventName === "auto.log") {
    const row = {
      ts: payload.log_ts || "",
      level: payload.level || "info",
      message: payload.message || "",
    };
    state.autoLogs = mergeAutoLogs(state.autoLogs, [row]);
    if (state.autoScheduler) {
      renderAutoDock({ ...state.autoScheduler, logs: state.autoLogs });
    }
  }
}

export function startRealtime(options = {}) {
  void options;
  if (typeof EventSource === "undefined") {
    fallbackToPolling("no-eventsource");
    return;
  }
  if (state.eventSource && state.sseHealthy) return;
  // 已有连接正在建立时不要重复创建
  if (state.eventSource && state.eventSource.readyState === EventSource.CONNECTING) return;

  closeEventSource();
  // 新连接 = 新流：stream seq 窗口归零（backend 重启后 seq 从 1 重新开始，
  // 旧窗口不保留，否则会永久拒绝所有新事件）。REST 路径不消耗该窗口。
  resetJobStreamFreshness();
  try {
    const es = new EventSource("/api/events");
    state.eventSource = es;
    const bind = (name) => {
      es.addEventListener(name, (ev) => {
        try {
          const payload = JSON.parse(ev.data || "{}");
          state.sseHealthy = true;
          handleSseMessage(name, payload);
          // SSE 恢复后停掉 Job REST 轮询；Auto 倒计时保留
          if (state.polling) stopJobPolling();
          if (state.autoPollTimer) {
            window.clearInterval(state.autoPollTimer);
            state.autoPollTimer = null;
          }
        } catch (error) {
          console.error("SSE parse failed", name, error);
        }
      });
    };
    [
      "heartbeat",
      "job.snapshot",
      "job.created",
      "job.progress",
      "job.log",
      "job.terminal",
      "auto.snapshot",
      "auto.log",
    ].forEach(bind);
    es.onopen = () => {
      state.sseHealthy = true;
      markSseActive();
      startSseWatchdog();
      stopJobPolling();
      // 保留倒计时，仅停 REST 轮询
      if (state.autoPollTimer) {
        window.clearInterval(state.autoPollTimer);
        state.autoPollTimer = null;
      }
      ensureAutoCountdown();
    };
    es.onerror = () => {
      if (state.sseHealthy) {
        fallbackToPolling("eventsource-error");
        return;
      }
      // 尚未建连成功：关闭后稍后重试，并立刻用轮询兜底
      closeEventSource();
      const job = state.currentJob;
      if (job?.state === "running") startPolling();
      if (state.autoDockOpen || state.autoScheduler?.state === "running") {
        ensureAutoPolling();
      }
      if (!state.sseReconnectTimer) {
        state.sseReconnectTimer = window.setTimeout(() => {
          state.sseReconnectTimer = null;
          startRealtime();
        }, SSE_RECONNECT_MS);
      }
    };
    markSseActive();
    startSseWatchdog();
  } catch (error) {
    console.error(error);
    fallbackToPolling("eventsource-throw");
  }
}
