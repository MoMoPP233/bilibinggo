/* eslint-disable */
/** Migrated from web/static/app.js — logic preserved. */

import { filterResultSummary } from "../dom";

export function prefersReducedMotion(): boolean {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function setButtonLoading(
  button: HTMLButtonElement | null,
  loading: boolean,
  options: { label?: unknown } = {},
): void {
  if (!button) return;
  const { label } = options;
  if (loading) {
    if (button.dataset.loadingActive !== "true") {
      button.dataset.loadingOriginalHtml = button.innerHTML;
      button.dataset.loadingActive = "true";
    }
    button.classList.add("is-loading");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    const labelEl = button.querySelector(".triple-participate-btn-label");
    if (labelEl && label !== undefined) {
      labelEl.textContent = String(label);
    } else if (label !== undefined) {
      button.textContent = String(label);
    }
  } else {
    button.classList.remove("is-loading");
    button.removeAttribute("aria-busy");
    if (button.dataset.loadingActive === "true") {
      button.innerHTML = button.dataset.loadingOriginalHtml || button.innerHTML;
      delete button.dataset.loadingOriginalHtml;
      delete button.dataset.loadingActive;
    }
    button.disabled = false;
  }
}

export function clearActionButtonLoading(): void {
  document.querySelectorAll("[data-action].is-loading, .triple-participate-btn.is-loading").forEach((btn) => {
    setButtonLoading(btn as HTMLButtonElement, false);
  });
  document.querySelectorAll(".source-row.is-updating").forEach((row) => {
    row.classList.remove("is-updating");
  });
}

export function setSourceRowUpdating(sourceId: unknown, updating: boolean): void {
  if (!sourceId) return;
  const row = document.querySelector(`.source-row[data-source-id="${CSS.escape(String(sourceId))}"]`) as HTMLElement | null;
  if (!row) return;
  row.classList.toggle("is-updating", Boolean(updating));
}

export function flashSourceRow(sourceId: unknown): void {
  if (!sourceId || prefersReducedMotion()) return;
  const row = document.querySelector(`.source-row[data-source-id="${CSS.escape(String(sourceId))}"]`) as HTMLElement | null;
  if (!row) return;
  row.classList.remove("is-flash");
  void row.offsetWidth;
  row.classList.add("is-flash");
  window.setTimeout(() => row.classList.remove("is-flash"), 1100);
}

export function pulseWatchSyncCard(): void {
  const card = document.querySelector(".watch-sync-card") as HTMLElement | null;
  if (!card || prefersReducedMotion()) return;
  card.classList.remove("is-sync-pulse");
  void card.offsetWidth;
  card.classList.add("is-sync-pulse");
  document.querySelectorAll(".watch-metric-value").forEach((el) => {
    const element = el as HTMLElement;
    element.classList.remove("is-value-pop");
    void element.offsetWidth;
    element.classList.add("is-value-pop");
  });
  window.setTimeout(() => {
    card.classList.remove("is-sync-pulse");
    document.querySelectorAll(".watch-metric-value").forEach((el) => el.classList.remove("is-value-pop"));
  }, 900);
}

export function playSourcesEnter(): void {
  const stack = document.querySelector("#section-sources .sources-stack") as HTMLElement | null;
  if (!stack || prefersReducedMotion()) return;
  stack.classList.remove("is-sources-entering");
  void stack.offsetWidth;
  stack.classList.add("is-sources-entering");
}

export function playActivitiesEnter(): void {
  const panel = document.querySelector("#section-activities .activities-panel") as HTMLElement | null;
  if (!panel || prefersReducedMotion()) return;
  panel.classList.remove("is-activities-entering");
  void panel.offsetWidth;
  panel.classList.add("is-activities-entering");
}

export function pulseFilterSummary(): void {
  if (!filterResultSummary || prefersReducedMotion()) return;
  filterResultSummary.classList.remove("is-updated");
  void filterResultSummary.offsetWidth;
  filterResultSummary.classList.add("is-updated");
}

export function flashFilterPill(button: Element | null): void {
  if (!button || prefersReducedMotion()) return;
  button.classList.remove("is-just-selected");
  void (button as HTMLElement).offsetWidth;
  button.classList.add("is-just-selected");
  window.setTimeout(() => button.classList.remove("is-just-selected"), 520);
}

export function playActivityListEnter(): void {
  if (prefersReducedMotion()) return;
  const rows = document.querySelectorAll("#activities-body tr[data-dynamic-id], #activities-cards .activity-card");
  rows.forEach((row, index) => {
    const el = row as HTMLElement;
    el.classList.remove("is-row-entering");
    el.style.setProperty("--row-delay", `${Math.min(index, 12) * 28}ms`);
    void el.offsetWidth;
    el.classList.add("is-row-entering");
  });
}

export function highlightWatchUserChip(mid: unknown): void {
  if (!mid) return;
  const chip = document.querySelector(`[data-watch-mid="${CSS.escape(String(mid))}"]`) as HTMLElement | null;
  if (!chip || prefersReducedMotion()) return;
  chip.classList.add("is-new");
  window.setTimeout(() => chip.classList.remove("is-new"), 700);
}

export function flashButtonSuccess(button: HTMLElement | null, label = "已保存"): void {
  if (!button || prefersReducedMotion()) return;
  const previousHtml = button.innerHTML;
  button.classList.add("is-save-success");
  button.textContent = label;
  window.setTimeout(() => {
    button.classList.remove("is-save-success");
    if (button.dataset.loadingActive === "true") return;
    button.innerHTML = previousHtml;
  }, 1100);
}

export function markSaveDirty(button: Element | null): void {
  button?.classList.add("is-dirty");
}

export function clearSaveDirty(button: Element | null): void {
  button?.classList.remove("is-dirty");
}

export function animateStatValue(el: HTMLElement | null, from: unknown, to: unknown): void {
  if (!el) return;
  const element = el;
  const startValue = Number(from) || 0;
  const endValue = Number(to) || 0;
  if (prefersReducedMotion() || startValue === endValue) {
    element.textContent = String(endValue);
    return;
  }
  const duration = 520;
  const startedAt = performance.now();
  element.classList.add("is-ticking");
  const tick = (now: number) => {
    const progress = Math.min(1, (now - startedAt) / duration);
    const eased = 1 - (1 - progress) ** 3;
    element.textContent = String(Math.round(startValue + (endValue - startValue) * eased));
    if (progress < 1) {
      requestAnimationFrame(tick);
      return;
    }
    element.textContent = String(endValue);
    element.classList.remove("is-ticking");
    element.classList.remove("is-value-pop");
    void element.offsetWidth;
    element.classList.add("is-value-pop");
    window.setTimeout(() => element.classList.remove("is-value-pop"), 420);
  };
  requestAnimationFrame(tick);
}

export function playOverviewEnter(): void {
  const stack = document.querySelector("#section-overview .overview-stack") as HTMLElement | null;
  if (!stack || prefersReducedMotion()) return;
  stack.classList.remove("is-overview-entering");
  void stack.offsetWidth;
  stack.classList.add("is-overview-entering");
}

export function flashActivityRows(dynamicIds: ReadonlyArray<unknown>): void {
  dynamicIds.forEach((dynamicId) => {
    if (!dynamicId) return;
    document.querySelectorAll(`[data-dynamic-id="${String(dynamicId)}"]`).forEach((el) => {
      el.classList.add("row-flash");
      el.querySelector(".badge")?.classList.add("is-badge-pop");
      window.setTimeout(() => {
        el.classList.remove("row-flash");
        el.querySelector(".badge")?.classList.remove("is-badge-pop");
      }, 1800);
    });
  });
}
