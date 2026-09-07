// @ts-nocheck
/* eslint-disable */
/** 中奖通知页（M2：通知渠道 / 事件设置 / 测试通知 / 最近发送记录）。 */

import { fetchJSON } from "../api/client";
import { startJob } from "../jobs/index";
import { showToast } from "../shell/toast";
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

const STATUS_LABELS = { sent: "已发送", failed: "失败", delivery_unknown: "结果未知" };
const EVENT_LABELS = {
  winning_candidate: "疑似中奖候选（M3）",
  winning_confirmed: "中奖确认（M3）",
  platform_risk: "平台风控",
  auto_task_error: "自动任务失败",
  auto_task_completed: "自动任务完成",
  test: "测试",
};

function timeText(value) {
  if (!value) return "—";
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function statusBadge(status) {
  return `<span class="ntf-status is-${escapeHtml(status)}">${STATUS_LABELS[status] || escapeHtml(status)}</span>`;
}

function channelCardHtml(channel) {
  const fields = (CHANNEL_FIELDS[channel.channel] || []).map(
    (field) => `
      <label class="ntf-field">
        <span class="caption">${escapeHtml(field.label)}</span>
        <input class="form-control" data-ntf-field="${escapeHtml(field.key)}" type="${field.secret ? "password" : "text"}" autocomplete="off" placeholder="已配置则留空以保持不变" />
      </label>`
  ).join("");
  return `
    <article class="panel ntf-card is-${channel.configured ? "configured" : "missing"}" data-channel="${escapeHtml(channel.channel)}">
      <div class="ntf-card-head">
        <h3 class="section-title">${escapeHtml(channel.label)}</h3>
        <span class="caption ntf-config-state">${channel.configured ? "已配置" : "未配置"} · ${channel.masked ? `凭据 ${escapeHtml(channel.masked)}` : "无凭据"}</span>
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
            <input type="checkbox" data-ntf-event="${escapeHtml(event)}" ${enabled ? "checked" : ""} />
            <span>${escapeHtml(EVENT_LABELS[event] || event)}</span>
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
          ${(state?.records || [])
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
            .join("")}
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
    if (winner) renderWinner(winner);
  } catch (error) {
    showToast(String(error?.message || error), "error", "通知状态读取失败");
  }
}

const SOURCE_LABELS_M3 = { mention: "@我的", reply: "回复我的", dm: "未读私信" };

function renderWinner(state) {
  const root = document.getElementById(ROOT_ID);
  if (!root) return;
  const sources = state?.sources || {};
  const candidates = Array.isArray(state?.candidates) ? state.candidates : [];
  const panel = document.createElement("article");
  panel.className = "panel";
  panel.id = "winner-scan-panel";
  panel.innerHTML = `
    <div class="watch-panel-head">
      <div class="watch-panel-intro">
        <p class="eyebrow">中奖深检（M3）</p>
        <h2 class="section-title">疑似中奖记录</h2>
        <p class="section-desc">只扫描 @我的 / 回复我的 / 未读私信；命中关键词仅标记「疑似中奖」，请人工确认。</p>
      </div>
      <button type="button" class="btn btn-primary btn-pill" id="winner-scan-run">立即执行深检</button>
    </div>
    <div class="ntf-event-list">
      ${Object.entries(SOURCE_LABELS_M3)
        .map(([key, label]) => {
          const source = sources[key] || {};
          const cursor = key === "dm" ? "—" : `${source.cursor_id || 0}`;
          return `<span class="caption"><b>${label}</b> · ${source.last_error ? "最近错误" : source.last_success_at ? "正常" : "未运行"} · cursor ${cursor}</span>`;
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
            <td>${escapeHtml(String(item.summary || ""))}</td>
            <td>${escapeHtml(String(item.status || ""))}</td>
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

export function bindNotificationsPage() {
  if (!document.getElementById(ROOT_ID)) return;
  if (bound) return;
  bound = true;
  refreshView().catch(() => {});
}
