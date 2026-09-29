// @ts-nocheck
/* eslint-disable */
/** DS9 话题发现面板（数据源页内嵌区块，非一级页面）。 */

import { fetchJSON } from "../api/client";
import { startJob } from "../jobs/index";
import { showToast } from "../shell/toast";
import { escapeHtml } from "../utils/text";

const PANEL_ID = "ds9-topic-panel";
let bound = false;

function timeText(value) {
  if (!value) return "—";
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function topicScanText(value) {
  if (!value) return "—";
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function statusText(topics) {
  const enabled = topics.filter((item) => item?.enabled);
  if (enabled.some((item) => item?.last_error)) return "partial";
  if (enabled.length === 0) return "paused";
  if (enabled.every((item) => !item?.last_success_at && !item?.last_error)) return "idle";
  return "ok";
}

const STATUS_LABELS = {
  ok: "正常",
  partial: "部分失败",
  paused: "已暂停",
  idle: "未运行",
};

function renderStatusPill(status) {
  return `<span class="ds9-status-pill is-${escapeHtml(status)}">${STATUS_LABELS[status] || status}</span>`;
}

function topicCardHtml(topic) {
  const kind = topic?.kind === "built-in" ? "内置" : "自定义";
  const enabled = Boolean(topic?.enabled);
  const processed = topic?.fp ? "已推送数据库" : "未处理";
  return `
    <li class="ds9-topic-card" data-topic-id="${escapeHtml(String(topic?.topic_id || ""))}">
      <div class="ds9-topic-card-head">
        <strong class="ds9-topic-name">话题：${escapeHtml(String(topic?.name || topic?.topic_id || ""))}</strong>
        <button type="button" class="btn btn-ghost btn-compact btn-pill" data-ds9-toggle data-ds9-enabled="${enabled ? "true" : "false"}">${enabled ? "暂停" : "恢复"}</button>
      </div>
      <div class="ds9-topic-card-id">${escapeHtml(String(topic?.topic_id || ""))} · ${kind}</div>
      <div class="ds9-topic-card-meta">
        <span class="caption">最近扫描：${topicScanText(topic?.last_scan_at)}</span>
        <span class="caption">发现 ${Number(topic?.last_found) || 0} · ${processed}</span>
        ${Number(topic?.incomplete_count) > 0 ? `<span class="caption" title="等待下轮重新解析的条目数">待解析：${Number(topic?.incomplete_count)} 条</span>` : ""}
        ${topic?.last_error ? `<span class="caption is-error" title="${escapeHtml(String(topic.last_error))}">最近错误</span>` : ""}
      </div>
      <div class="ds9-topic-actions">
        ${topic?.kind === "custom" ? `<button type="button" class="btn btn-ghost btn-compact btn-pill" data-ds9-delete>删除</button>` : ""}
      </div>
    </li>`;
}

function auditSampleHtml(sample) {
  const topic = `${String(sample?.source_topic_name || sample?.source_topic_id || "未知话题")} · ${String(sample?.source_topic_id || "")}`;
  const url = String(sample?.canonical_url || "");
  return `
    <li class="ds9-audit-item">
      <div class="ds9-audit-head">
        <strong>${escapeHtml(topic)}</strong>
        <span class="caption">${escapeHtml(timeText(sample?.last_seen_at))}</span>
      </div>
      <div class="ds9-audit-meta">动态 ID：${escapeHtml(String(sample?.dynamic_id || ""))}</div>
      ${url ? `<a class="ds9-audit-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">打开原动态</a>` : ""}
      <div class="ds9-audit-label">分类文本</div>
      <pre class="ds9-audit-text">${escapeHtml(String(sample?.classifier_text || "(无正文)"))}</pre>
    </li>`;
}

function renderAudit(samples, topicId = "") {
  const panel = document.getElementById(PANEL_ID);
  if (!panel) return;
  const rows = Array.isArray(samples) ? samples : [];
  const visible = topicId ? rows.filter((item) => String(item?.source_topic_id || "") === topicId) : rows;
  panel.querySelector("[data-ds9-audit-count]").textContent = String(visible.length);
  panel.querySelector("[data-ds9-audit-rows]").innerHTML = visible.length
    ? visible.map(auditSampleHtml).join("")
    : '<li class="ds9-audit-empty">暂无非抽奖审计样本</li>';
}

function render(state) {
  const topics = Array.isArray(state?.topics) ? state.topics : [];
  const summary = state?.summary || {};
  const builtin = Number(summary.builtin_count) || topics.filter((t) => t?.kind === "built-in").length;
  const custom = Number(summary.custom_count) || topics.filter((t) => t?.kind === "custom").length;
  const latest = topics
    .map((item) => item?.last_scan_at || 0)
    .reduce((a, b) => Math.max(a, b), 0);
  const panel = document.getElementById(PANEL_ID);
  if (!panel) return;
  panel.querySelector("[data-ds9-status]").outerHTML = renderStatusPill(statusText(topics));
  panel.querySelector("[data-ds9-builtin]").textContent = String(builtin);
  panel.querySelector("[data-ds9-custom]").textContent = String(custom);
  panel.querySelector("[data-ds9-last-scan]").textContent = timeText(latest || null);
  panel.querySelector("[data-ds9-rows]").innerHTML = topics.map(topicCardHtml).join("");
}

async function refreshView() {
  try {
    const [state, audit] = await Promise.all([
      fetchJSON("/api/ds9"),
      fetchJSON("/api/ds9/rejection-audit"),
    ]);
    render(state);
    const panel = document.getElementById(PANEL_ID);
    if (panel) panel._ds9AuditSamples = Array.isArray(audit?.samples) ? audit.samples : [];
    renderAudit(audit?.samples);
  } catch (error) {
    showToast(String(error?.message || error), "error", "DS9 状态读取失败");
  }
}

function handleTopicAction(event) {
  const button = event.target.closest("[data-ds9-toggle], [data-ds9-delete]");
  if (!button) return;
  const row = button.closest("[data-topic-id]");
  if (!row) return;
  const topicId = row.dataset.topicId;
  if (button.hasAttribute("data-ds9-delete")) {
    if (!window.confirm(`删除自定义话题 ${topicId}？`)) return;
    fetchJSON(`/api/ds9/topics/${encodeURIComponent(topicId)}`, { method: "DELETE" })
      .then(() => refreshView())
      .catch((error) => showToast(String(error?.message || error), "error"));
    return;
  }
  const enabling = button.dataset.ds9Enabled !== "true";
  fetchJSON(`/api/ds9/topics/${encodeURIComponent(topicId)}/enabled`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: enabling }),
  })
    .then(() => refreshView())
    .catch((error) => showToast(String(error?.message || error), "error"));
}

function handleAddTopic(event) {
  event.preventDefault();
  const panel = document.getElementById(PANEL_ID);
  const nameInput = panel?.querySelector("[data-ds9-name]");
  const idInput = panel?.querySelector("[data-ds9-id]");
  if (!idInput) return;
  const name = String(nameInput?.value || "").trim();
  const topicId = String(idInput.value || "").trim();
  if (!topicId) {
    showToast("请输入 topic_id 或话题链接", "info");
    return;
  }
  fetchJSON("/api/ds9/topics", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, topic_id: topicId }),
  })
    .then(() => {
      if (nameInput) nameInput.value = "";
      idInput.value = "";
      refreshView();
    })
    .catch((error) => showToast(String(error?.message || error), "error"));
}

function handleAuditAction(event) {
  const select = event.target.closest("[data-ds9-audit-filter]");
  if (select) {
    const panel = document.getElementById(PANEL_ID);
    const samples = panel?._ds9AuditSamples || [];
    renderAudit(samples, String(select.value || ""));
    return;
  }
  const button = event.target.closest("[data-ds9-audit-clear]");
  if (!button) return;
  if (!window.confirm("仅清空本地 DS9 非抽奖审计记录，不会删除活动，也不会操作 B 站内容。")) return;
  fetchJSON("/api/ds9/rejection-audit", { method: "DELETE" })
    .then(() => refreshView())
    .catch((error) => showToast(String(error?.message || error), "error"));
}

function ensurePanel() {
  const anchor = document.getElementById("watch-panel");
  if (!anchor || document.getElementById(PANEL_ID)) return;
  const panel = document.createElement("article");
  panel.className = "panel ds9-panel";
  panel.id = PANEL_ID;
  panel.innerHTML = `
    <div class="watch-panel-head">
      <div class="watch-panel-intro">
        <p class="eyebrow">数据源</p>
        <h2 class="section-title">DS9 话题发现</h2>
        <p class="section-desc">扫描 B 站话题，发现新的抽奖动态。</p>
      </div>
      <div class="action-row">
        <span data-ds9-status class="ds9-status-pill is-idle">未运行</span>
        <button type="button" class="btn btn-primary btn-pill" data-ds9-refresh>刷新 DS9</button>
      </div>
    </div>
    <div class="ds9-summary-row">
      <span class="caption">默认话题：<b data-ds9-builtin>3</b></span>
      <span class="caption">自定义话题：<b data-ds9-custom>0</b></span>
      <span class="caption">最近扫描：<span data-ds9-last-scan>—</span></span>
    </div>
    <form class="ds9-add-form action-row" data-ds9-add>
      <input class="form-control" data-ds9-name placeholder="话题名称（可选）" />
      <input class="form-control" data-ds9-id placeholder="topic_id 或话题链接" required />
      <button type="submit" class="btn btn-secondary btn-compact btn-pill">添加话题</button>
    </form>
    <ul class="ds9-topic-rows" data-ds9-rows></ul>
    <section class="ds9-audit-section" data-ds9-audit>
      <div class="ds9-audit-section-head">
        <div>
          <h3>非抽奖审计样本</h3>
          <p class="section-desc">仅保存本地最近 100 条分类为非抽奖活动的样本。</p>
        </div>
        <div class="action-row">
          <select class="form-control" data-ds9-audit-filter aria-label="按话题筛选">
            <option value="">全部话题</option>
            <option value="1261901">抽奖！！！ · 1261901</option>
            <option value="1055656">转发动态抽奖！ · 1055656</option>
            <option value="47801">抽奖号的日常 · 47801</option>
          </select>
          <span class="caption">共 <b data-ds9-audit-count>0</b> 条</span>
          <button type="button" class="btn btn-ghost btn-compact btn-pill" data-ds9-audit-clear>清空审计样本</button>
        </div>
      </div>
      <ul class="ds9-audit-rows" data-ds9-audit-rows></ul>
    </section>
  `;
  anchor.insertAdjacentElement("afterend", panel);
}

export function bindDs9Panel() {
  ensurePanel();
  const panel = document.getElementById(PANEL_ID);
  if (!panel || bound) return;
  bound = true;
  panel.querySelector("[data-ds9-refresh]")?.addEventListener("click", () => {
    startJob("refresh_source", { source_id: "DS-9" }).catch((error) => {
      showToast(String(error?.message || error), "error");
    });
  });
  panel.querySelector("[data-ds9-add]")?.addEventListener("submit", handleAddTopic);
  panel.querySelector("[data-ds9-rows]")?.addEventListener("click", handleTopicAction);
  panel.querySelector("[data-ds9-audit]")?.addEventListener("click", handleAuditAction);
  panel.querySelector("[data-ds9-audit]")?.addEventListener("change", handleAuditAction);
  refreshView().catch(() => {});
}
