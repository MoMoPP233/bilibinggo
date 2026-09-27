// @ts-nocheck
/* eslint-disable */
/** 中奖通知页（M2：通知渠道 / 事件设置 / 测试通知 / 最近发送记录）。 */

import { fetchJSON } from "../api/client";
import { startJob } from "../jobs/index";
import { state as appState } from "../state";
import { showToast } from "../shell/toast";
import { formatAutoCountdown } from "../utils/format";
import { escapeHtml } from "../utils/text";

const ROOT_ID = "notifications-root";
let bound = false;

const CHANNEL_FIELDS = {
  serverchan: [
    { key: "sendkey", label: "SendKey", secret: true },
  ],
  dingtalk: [
    { key: "token", label: "Webhook Access Token", secret: true },
    { key: "secret", label: "加签 Secret（可选）", secret: true },
  ],
  wecom: [
    { key: "key", label: "机器人 Key", secret: true },
  ],
  feishu: [
    { key: "webhook", label: "Webhook URL", secret: true },
    { key: "secret", label: "签名 Secret（可选）", secret: true },
  ],
};

const STATUS_LABELS = {
  sent: "已发送",
  failed: "失败",
  delivery_unknown: "结果未知", // 崩溃遗留/超时等无法确认 → 绝不自动重发
  dispatching: "发送中", // 当前进程真实在途发送（短暂）
};
const EVENT_LABELS = {
  winning_candidate: "疑似中奖候选",
  winning_confirmed: "中奖确认",
  platform_risk: "平台风控",
  auto_task_error: "自动任务失败",
  auto_task_completed: "自动任务完成",
  test: "测试",
};
const EVENT_PRODUCER_PENDING = new Set(["platform_risk", "auto_task_error", "auto_task_completed"]);

function timeText(value) {
  if (!value) return "—";
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function statusBadge(status) {
  return `<span class="ntf-status is-${escapeHtml(status)}">${STATUS_LABELS[status] || escapeHtml(status)}</span>`;
}

function channelCardHtml(channel) {
  const fields = (CHANNEL_FIELDS[channel.channel] || []).map(
    (field) => `
      <label class="ntf-field">
        <span class="caption">${escapeHtml(field.label)}</span>
        <input class="form-control" data-ntf-field="${escapeHtml(field.key)}" type="${field.secret ? "password" : "text"}" autocomplete="off" placeholder="${escapeHtml(channel.configured ? "已配置，留空保持不变" : `请输入 ${field.label.replace(/（可选）$/, "")}`)}" />
      </label>`
  ).join("");
  return `
    <article class="panel ntf-card is-${channel.configured ? "configured" : "missing"}" data-channel="${escapeHtml(channel.channel)}">
      <div class="ntf-card-head">
        <h3 class="section-title">${escapeHtml(channel.label)}</h3>
        <span class="caption ntf-config-state">${channel.configured ? (channel.enabled ? "已启用" : "已停用") : "未配置"}</span>
        <label class="ntf-toggle"><input type="checkbox" data-ntf-enabled ${channel.enabled ? "checked" : ""} /> ${channel.enabled ? "已启用" : "已停用"}</label>
      </div>
      <form class="ntf-config-form" data-ntf-config>
        ${fields}
        <div class="action-row">
          <button type="submit" class="btn btn-secondary btn-compact btn-pill">保存配置</button>
          <button type="button" class="btn btn-ghost btn-compact btn-pill" data-ntf-test ${channel.configured ? "" : "disabled"}>发送测试通知</button>
        </div>
      </form>
    </article>`;
}

function renderRoot(state) {
  const root = document.getElementById(ROOT_ID);
  if (!root) return;
  const channels = Array.isArray(state?.channels) ? state.channels : [];
  const events = state?.events || {};
  root.innerHTML = `
    <article class="panel">
      <div class="watch-panel-head">
        <div class="watch-panel-intro">
          <p class="eyebrow">通知</p>
          <h2 class="section-title">中奖通知 · 通知渠道</h2>
          <p class="section-desc">配置后，当符合条件的事件发生时 Binggo 会向已启用的渠道发送通知。</p>
        </div>
      </div>
      <div class="ntf-channel-grid">
        ${channels.map(channelCardHtml).join("")}
      </div>
    </article>
    <article class="panel">
      <h3 class="section-title">通知事件设置</h3>
      <p class="section-desc">M2 事件模型已预留中奖事件；M3 才接入中奖深检生产逻辑。</p>
      <div class="ntf-event-list">
        ${Object.entries(events)
          .map(
            ([event, enabled]) => `
          <label class="ntf-toggle-row">
            <input type="checkbox" data-ntf-event="${escapeHtml(event)}" ${enabled ? "checked" : ""} ${EVENT_PRODUCER_PENDING.has(event) ? "disabled" : ""} />
            <span>${escapeHtml(EVENT_LABELS[event] || event)}${EVENT_PRODUCER_PENDING.has(event) ? ' <span class="ntf-pending-badge">未接入</span>' : ""}</span>
          </label>`
          )
          .join("")}
      </div>
    </article>
    <article class="panel">
      <h3 class="section-title">最近发送记录</h3>
      <table class="ntf-records">
        <thead><tr><th>时间</th><th>渠道</th><th>事件</th><th>状态</th><th>摘要</th></tr></thead>
        <tbody>
          ${(state?.records || []).length
            ? (state?.records || [])
            .map(
              (record) => `
            <tr>
              <td>${timeText(record.sent_at)}</td>
              <td>${escapeHtml(record.channel)}</td>
              <td>${escapeHtml(String(record.event_id || "").slice(0, 40))}</td>
              <td>${statusBadge(record.status)}</td>
              <td>${escapeHtml(String(record.summary || ""))}</td>
            </tr>`
            )
            .join("")
            : '<tr class="empty-row"><td colspan="5" class="caption">暂无发送记录</td></tr>'}
        </tbody>
      </table>
    </article>`;
  bindEvents();
}

function bindEvents() {
  const root = document.getElementById(ROOT_ID);
  if (!root) return;
  root.querySelectorAll("[data-ntf-config]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const card = form.closest("[data-channel]");
      const channel = card.dataset.channel;
      const values = {};
      form.querySelectorAll("[data-ntf-field]").forEach((input) => {
        const raw = String(input.value || "").trim();
        if (raw) values[input.dataset.ntfField] = raw;
      });
      if (!Object.keys(values).length) {
        showToast("请输入要保存的凭据", "info");
        return;
      }
      fetchJSON(`/api/notifications/channels/${encodeURIComponent(channel)}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      })
        .then(() => {
          showToast("配置已保存", "success");
          refreshView();
        })
        .catch((error) => showToast(String(error?.message || error), "error"));
      form.querySelectorAll("input[type=password]").forEach((input) => {
        input.value = "";
      });
    });
  });
  root.querySelectorAll("[data-ntf-test]").forEach((button) => {
    button.addEventListener("click", () => {
      const card = button.closest("[data-channel]");
      fetchJSON(`/api/notifications/channels/${encodeURIComponent(card.dataset.channel)}/test`, {
        method: "POST",
      })
        .then((payload) => {
          const status = payload?.result?.status;
          if (status === "sent") showToast("测试通知已发送", "success");
          else if (status === "delivery_unknown") showToast("请求已发出但结果未知", "info");
          else showToast("测试通知发送失败", "error");
        })
        .catch((error) => showToast(String(error?.message || error), "error"));
    });
  });
  root.querySelectorAll("[data-ntf-enabled]").forEach((toggle) => {
    toggle.addEventListener("change", () => {
      const card = toggle.closest("[data-channel]");
      fetchJSON(`/api/notifications/channels/${encodeURIComponent(card.dataset.channel)}/enabled`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: toggle.checked }),
      })
        .then(() => refreshView())
        .catch((error) => showToast(String(error?.message || error), "error"));
    });
  });
  root.querySelectorAll("[data-ntf-event]").forEach((toggle) => {
    toggle.addEventListener("change", () => {
      fetchJSON(`/api/notifications/events/${encodeURIComponent(toggle.dataset.ntfEvent)}/enabled`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: toggle.checked }),
      }).catch((error) => showToast(String(error?.message || error), "error"));
    });
  });
}

async function refreshView() {
  try {
    const state = await fetchJSON("/api/notifications");
    renderRoot(state);
    let winner = null;
    try {
      winner = await fetchJSON("/api/winner-scan");
    } catch {
      winner = null;
    }
    let auto = null;
    try {
      auto = await fetchJSON("/api/auto/status");
    } catch {
      auto = null;
    }
    if (winner) renderWinner({ ...winner, scheduler_task: auto?.tasks?.winner || null, scheduler: auto });
  } catch (error) {
    showToast(String(error?.message || error), "error", "通知状态读取失败");
  }
}

const SOURCE_LABELS_M3 = { mention: "@我的", reply: "回复", dm: "私信" };

function winnerClueCount(task, sources) {
  const found = Object.values(sources || {}).reduce((total, source) => {
    const count = Number(source?.found);
    return Number.isFinite(count) && count >= 0 ? total + count : total;
  }, 0);
  if (found > 0 || Object.keys(sources || {}).length) return found;
  const match = String(task?.last_result || "").match(/发现\s*(\d+)\s*条/);
  return match ? Number(match[1]) : 0;
}

function winnerSchedulerHtml(task, scheduler, clueCount) {
  const statusLabels = {
    waiting: "等待",
    running: "运行中",
    completed: "完成",
    skipped_busy: "本轮跳过（任务占用）",
    risk_paused: "风控暂停",
    error: "错误",
  };
  const status = String(task?.status || "waiting");
  const next = Number(status === "risk_paused" ? task?.resume_at : task?.next_run_at) || 0;
  const expired = String(scheduler?.state || "") === "running"
    && next > 0
    && Math.floor((Date.now() + Number(appState.autoServerSkewMs || 0)) / 1000) >= next;
  const countdown = status === "running"
    ? "正在执行"
    : expired
      ? "正在确认状态…"
      : formatAutoCountdown(next || null);
  return `<div class="winner-scheduler-state" id="winner-scheduler-state" data-status="${escapeHtml(status)}">
    <strong>扫描状态：${escapeHtml(statusLabels[status] || status)}</strong>
    <span>上次扫描：${escapeHtml(String(task?.last_started_at || "—"))}</span>
    <span>下次扫描：${next ? escapeHtml(new Date(next * 1000).toLocaleString("zh-CN", { hour12: false })) : "—"}</span>
    <span>新增线索：${escapeHtml(String(Number.isFinite(Number(clueCount)) ? clueCount : winnerClueCount(task)))}</span>
    <span>倒计时：${escapeHtml(countdown)}</span>
  </div>`;
}

function renderWinnerSchedulerTask(task, scheduler) {
  const current = document.getElementById("winner-scheduler-state");
  if (!current) return;
  const holder = document.createElement("div");
  holder.innerHTML = winnerSchedulerHtml(task, scheduler, winnerClueCount(task));
  current.replaceWith(holder.firstElementChild!);
}

function renderWinner(state) {
  const root = document.getElementById(ROOT_ID);
  if (!root) return;
  const sources = state?.sources || {};
  const candidates = Array.isArray(state?.candidates) ? state.candidates : [];
  const scheduler = state?.scheduler || appState.autoScheduler || null;
  const schedulerTask = state?.scheduler_task || scheduler?.tasks?.winner || null;
  const panel = document.createElement("article");
  panel.className = "panel";
  panel.id = "winner-scan-panel";
  panel.innerHTML = `
    <div class="watch-panel-head">
      <div class="watch-panel-intro">
        <p class="eyebrow">中奖深检（M3）</p>
        <h2 class="section-title">疑似中奖记录</h2>
        <p class="section-desc">自动扫描私信、@提及和未读消息，发现疑似中奖线索。</p>
      </div>
      <button type="button" class="btn btn-primary btn-pill" id="winner-scan-run">立即执行深检</button>
    </div>
    ${winnerSchedulerHtml(schedulerTask, scheduler, winnerClueCount(schedulerTask, sources))}
    <div class="ntf-event-list">
    ${Object.entries(SOURCE_LABELS_M3)
      .map(([key, label]) => {
        const source = sources[key] || {};
        const skipped = Array.isArray(source.malformed_skipped) ? source.malformed_skipped.length : 0;
        const lastReason = Array.isArray(source.malformed_skipped) && source.malformed_skipped.length
          ? String(source.malformed_skipped[source.malformed_skipped.length - 1]?.reason || "")
          : "";
        return `<span class="caption"${skipped && lastReason ? ` title="${escapeHtml(lastReason.slice(0, 80))}"` : ""}>
          <b>${label}</b> · ${source.last_error ? "最近错误" : source.last_success_at ? "正常" : "未运行"}${skipped ? ` · 跳过 ${skipped} 条无法稳定识别消息` : ""}</span>`;
      })
      .join("")}
    </div>
    <table class="ntf-records">
      <thead><tr><th>时间</th><th>来源</th><th>发送者</th><th>摘要</th><th>状态</th></tr></thead>
      <tbody>
        ${candidates
          .map(
            (item) => `
          <tr>
            <td>${timeText(item.discovered_at)}</td>
            <td>${escapeHtml(String(item.source_type || ""))}</td>
            <td>${escapeHtml(String(item.sender?.name || item.sender?.id || ""))}</td>
            <td>${escapeHtml(truncateSummary(item.summary))}</td>
            <td>${escapeHtml(candidateStatusLabel(item.status))}</td>
          </tr>`
          )
          .join("") || '<tr><td colspan="5" class="caption">暂无疑似中奖线索</td></tr>'}
      </tbody>
    </table>`;
  const previous = document.getElementById("winner-scan-panel");
  if (previous) previous.remove();
  root.appendChild(panel);
  document.getElementById("winner-scan-run")?.addEventListener("click", () => {
    startJob("winner_deep_scan", {}).catch((error) => {
      showToast(String(error?.message || error), "error");
    });
  });
}

let lastWinnerRefreshAt = 0;

// winner_deep_scan terminal 后 / 页面恢复可见时触发一次轻量刷新（2s 节流防重复）。
// 复用一次 GET（/api/winner-scan + /api/auto/status），不新增任何 polling timer。
function refreshWinnerState() {
  const now = Date.now();
  if (now - lastWinnerRefreshAt < 2000) return;
  lastWinnerRefreshAt = now;
  refreshView().catch(() => {});
}

export function bindNotificationsPage() {
  if (!document.getElementById(ROOT_ID)) return;
  if (bound) return;
  bound = true;
  window.addEventListener("binggo:auto-status", ((event: CustomEvent<any>) => {
    renderWinnerSchedulerTask(event.detail?.tasks?.winner || null, event.detail);
  }) as EventListener);
  window.addEventListener("binggo:auto-tick", ((event: CustomEvent<any>) => {
    renderWinnerSchedulerTask(event.detail?.tasks?.winner || null, event.detail);
  }) as EventListener);
  // 手动 / Scheduler 的 winner job terminal → 刷新候选列表；页面恢复可见 → 一次轻量刷新。
  // 监听器只在 window 上注册一次（跨模块热载/重绑定不重复），避免同一事件触发多次 GET。
  const globalWindow = window as unknown as { __binggoWinnerJobBound?: boolean };
  if (!globalWindow.__binggoWinnerJobBound) {
    globalWindow.__binggoWinnerJobBound = true;
    window.addEventListener("binggo:job-completed", ((event: CustomEvent<any>) => {
      if (String((event.detail || {}).action || "") === "winner_deep_scan") {
        refreshWinnerState();
      }
    }) as EventListener);
    document.addEventListener("visibilitychange", () => {
      const panel = document.getElementById("winner-scan-panel");
      if (document.hidden || !panel || panel.offsetParent === null) return;
      refreshWinnerState();
    });
  }
  refreshView().catch(() => {});
}

export function truncateSummary(value, max = 50) {
  const text = String(value ?? "");
  return Array.from(text).length > max ? `${Array.from(text).slice(0, max).join("")}……` : text;
}

export function candidateStatusLabel(status) {
  const labels = { candidate: "疑似中奖", confirmed: "已确认中奖", dismissed: "已排除", ignored: "已忽略" };
  return labels[String(status)] || String(status ?? "");
}
