/* eslint-disable */
/** Migrated from web/static/app.js — logic preserved. */

import { INLINE_FEEDBACK_MS, inlineFeedbackTimers, toastStack } from "../dom";
import { prefersReducedMotion } from "../utils/motion";
import { escapeHtml } from "../utils/text";

export type ToastType = "success" | "error" | "info" | "running";

export interface ToastAction {
  label: string;
  onClick?: () => void;
}

export const TOAST_META: Record<ToastType, { title: string; icon: string }> = {
  success: { title: "执行成功", icon: "✓" },
  error: { title: "执行失败", icon: "!" },
  info: { title: "提示", icon: "i" },
  running: { title: "执行中", icon: "…" },
};

export function showToast(
  message: unknown,
  type: ToastType = "info",
  detail: unknown = "",
  actions: ToastAction[] = [],
) {
  if (!toastStack || !message) return;
  const meta = TOAST_META[type] || TOAST_META.info;
  const toast = document.createElement("div") as HTMLElement;
  toast.className = `toast toast-${type}`;
  const actionHtml = actions.length
    ? `<div class="toast-actions">${actions
        .map(
          (action: ToastAction, index: number) =>
            `<button type="button" class="btn btn-secondary btn-compact btn-pill toast-action-btn" data-toast-action="${index}">${escapeHtml(action.label)}</button>`
        )
        .join("")}</div>`
    : "";
  toast.innerHTML = `
    <div class="toast-icon" aria-hidden="true">${meta.icon}</div>
    <div class="toast-body">
      <p class="toast-title">${escapeHtml(meta.title)}</p>
      <p class="toast-message">${escapeHtml(message)}</p>
      ${detail ? `<p class="toast-detail">${escapeHtml(detail)}</p>` : ""}
      ${actionHtml}
    </div>
    <button type="button" class="toast-close" aria-label="关闭">×</button>
    <div class="toast-progress" aria-hidden="true"></div>`;
  const duration =
    actions.length > 0
      ? Math.max(type === "error" ? 7000 : 3200, 10000)
      : type === "error"
        ? 7000
        : type === "running"
          ? 1400
          : 3200;
  const progress = toast.querySelector(".toast-progress") as HTMLElement | null;
  if (progress) progress.style.animationDuration = `${duration}ms`;
  toast.querySelector(".toast-close")?.addEventListener("click", () => toast.remove());
  toast.querySelectorAll("[data-toast-action]").forEach((item) => {
    const button = item as HTMLElement;
    button.addEventListener("click", () => {
      const action = actions[Number(button.dataset.toastAction)];
      toast.remove();
      action?.onClick?.();
    });
  });
  toastStack.appendChild(toast);
  window.setTimeout(() => {
    toast.classList.add("toast-hide");
    window.setTimeout(() => toast.remove(), 320);
  }, duration);
}

export function dismissRunningToasts() {
  if (!toastStack) return;
  toastStack.querySelectorAll(".toast-running").forEach((toast) => toast.remove());
}

export function setInlineFeedback(
  element: HTMLElement | null,
  message: unknown,
  type: "success" | "error" | "info" = "info",
  { autoHide = true }: { autoHide?: boolean } = {},
) {
  if (!element) return;
  const existing = inlineFeedbackTimers.get(element);
  if (existing) window.clearTimeout(existing);

  if (!message) {
    element.hidden = true;
    element.textContent = "";
    element.className = "inline-feedback";
    return;
  }

  element.hidden = false;
  element.textContent = String(message);
  element.className = `inline-feedback inline-feedback--${type}`;
  if (!prefersReducedMotion()) {
    element.classList.remove("is-feedback-pop");
    void element.offsetWidth;
    element.classList.add("is-feedback-pop");
  }

  if (autoHide && type !== "info") {
    const timer = window.setTimeout(() => setInlineFeedback(element, "", "info"), INLINE_FEEDBACK_MS);
    inlineFeedbackTimers.set(element, timer);
  }
}
