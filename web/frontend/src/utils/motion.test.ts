/* eslint-disable */
// @vitest-environment jsdom
// V14: motion.ts 行为锁定（fake timers / stub RAF；不真实等待动画）。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  setButtonLoading,
  clearActionButtonLoading,
  setSourceRowUpdating,
  flashSourceRow,
  pulseWatchSyncCard,
  playSourcesEnter,
  playActivitiesEnter,
  pulseFilterSummary,
  flashFilterPill,
  playActivityListEnter,
  highlightWatchUserChip,
  flashButtonSuccess,
  markSaveDirty,
  clearSaveDirty,
  animateStatValue,
  playOverviewEnter,
  flashActivityRows,
} from "./motion";

type FrameCb = (now: number) => void;
let rafQueue: FrameCb[] = [];
let rafClock = 0;

function installRafStub(): void {
  vi.stubGlobal("requestAnimationFrame", (cb: FrameCb) => {
    rafQueue.push(cb);
    return rafQueue.length;
  });
}

function runRafUntilIdle(maxSteps = 50): void {
  let guard = 0;
  while (rafQueue.length && guard < maxSteps) {
    const step = rafQueue.splice(0);
    rafClock += 30;
    step.forEach((cb) => cb(rafClock));
    guard += 1;
  }
}

interface MediaQueryListStub {
  matches: boolean;
  media: string;
  onchange: null;
  addListener: () => void;
  removeListener: () => void;
  addEventListener: () => void;
  removeEventListener: () => void;
  dispatchEvent: () => boolean;
}

function mediaQuery(matches: boolean): MediaQueryListStub {
  return { matches, media: "", onchange: null, addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false };
}

function setReducedMotion(reduced: boolean): void {
  vi.stubGlobal("matchMedia", () => mediaQuery(reduced));
}

let reduced = false;

beforeEach(() => {
  vi.stubGlobal("CSS", { escape: (v: string) => v });
  document.body.innerHTML = "";
  rafQueue = [];
  rafClock = 0;
  reduced = false;
  vi.useFakeTimers();
  installRafStub();
  vi.stubGlobal("matchMedia", () => mediaQuery(reduced));
  vi.spyOn(performance, "now").mockReturnValue(0);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("setButtonLoading / clearActionButtonLoading", () => {
  it("captures original html, toggles classes/disabled, restores html", () => {
    const btn = document.createElement("button");
    btn.innerHTML = "<span class=\"triple-participate-btn-label\">原标签</span>";
    document.body.appendChild(btn);
    setButtonLoading(btn, true, { label: "参与中…" });
    expect(btn.dataset.loadingActive).toBe("true");
    expect(btn.dataset.loadingOriginalHtml).toContain("triple-participate-btn-label");
    expect(btn.classList.contains("is-loading")).toBe(true);
    expect(btn.disabled).toBe(true);
    expect(btn.getAttribute("aria-busy")).toBe("true");
    expect(btn.querySelector(".triple-participate-btn-label")?.textContent).toBe("参与中…");
    setButtonLoading(btn, true, { label: "第二次" });
    expect(btn.querySelector(".triple-participate-btn-label")?.textContent).toBe("第二次");
    setButtonLoading(btn, false);
    expect(btn.classList.contains("is-loading")).toBe(false);
    expect(btn.disabled).toBe(false);
    expect(btn.getAttribute("aria-busy")).toBeNull();
    expect(btn.innerHTML).toContain("triple-participate-btn-label");
    expect(btn.dataset.loadingActive).toBeUndefined();
  });

  it("falls back to button text when label element missing", () => {
    const btn = document.createElement("button");
    btn.textContent = "原文字";
    document.body.appendChild(btn);
    setButtonLoading(btn, true, { label: "执行中" });
    expect(btn.textContent).toBe("执行中");
    setButtonLoading(btn, false);
    expect(btn.textContent).toBe("原文字");
  });

  it("does nothing for null button", () => {
    expect(() => setButtonLoading(null, true)).not.toThrow();
  });

  it("clearActionButtonLoading resets loading buttons and source rows", () => {
    const a = document.createElement("button");
    a.setAttribute("data-action", "refresh");
    a.classList.add("is-loading");
    const b = document.createElement("button");
    b.classList.add("triple-participate-btn", "is-loading");
    const row = document.createElement("div");
    row.classList.add("source-row", "is-updating");
    document.body.append(a, b, row);
    clearActionButtonLoading();
    expect(a.classList.contains("is-loading")).toBe(false);
    expect(b.classList.contains("is-loading")).toBe(false);
    expect(row.classList.contains("is-updating")).toBe(false);
  });
});

describe("source row helpers", () => {
  it("setSourceRowUpdating toggles is-updating", () => {
    const row = document.createElement("div");
    row.className = "source-row";
    row.setAttribute("data-source-id", "42");
    document.body.appendChild(row);
    setSourceRowUpdating(42, true);
    expect(row.classList.contains("is-updating")).toBe(true);
    setSourceRowUpdating("42", false);
    expect(row.classList.contains("is-updating")).toBe(false);
  });

  it("flashSourceRow reflows then removes class at 1100ms", () => {
    const row = document.createElement("div");
    row.className = "source-row";
    row.setAttribute("data-source-id", "7");
    document.body.appendChild(row);
    flashSourceRow(7);
    expect(row.classList.contains("is-flash")).toBe(true);
    vi.advanceTimersByTime(500);
    expect(row.classList.contains("is-flash")).toBe(true);
    vi.advanceTimersByTime(600);
    expect(row.classList.contains("is-flash")).toBe(false);
  });

  it("skips flash under reduced motion", () => {
    reduced = true;
    const row = document.createElement("div");
    row.className = "source-row";
    row.setAttribute("data-source-id", "7");
    document.body.appendChild(row);
    flashSourceRow(7);
    expect(row.classList.contains("is-flash")).toBe(false);
  });
});

describe("card / panel entrances", () => {
  it("pulseWatchSyncCard adds classes and clears them at 900ms", () => {
    const card = document.createElement("div");
    card.className = "watch-sync-card";
    const metric = document.createElement("span");
    metric.className = "watch-metric-value";
    document.body.append(card, metric);
    pulseWatchSyncCard();
    expect(card.classList.contains("is-sync-pulse")).toBe(true);
    expect(metric.classList.contains("is-value-pop")).toBe(true);
    vi.advanceTimersByTime(450);
    expect(card.classList.contains("is-sync-pulse")).toBe(true);
    vi.advanceTimersByTime(450);
    expect(card.classList.contains("is-sync-pulse")).toBe(false);
    expect(metric.classList.contains("is-value-pop")).toBe(false);
  });

  it("entrance helpers apply their entering classes once", () => {
    const sectionSources = document.createElement("section");
    sectionSources.id = "section-sources";
    const sectionActivities = document.createElement("section");
    sectionActivities.id = "section-activities";
    const sectionOverview = document.createElement("section");
    sectionOverview.id = "section-overview";
    const sources = document.createElement("div");
    sources.className = "sources-stack";
    const activities = document.createElement("div");
    activities.className = "activities-panel";
    const overview = document.createElement("div");
    overview.className = "overview-stack";
    sectionSources.appendChild(sources);
    sectionActivities.appendChild(activities);
    sectionOverview.appendChild(overview);
    document.body.append(sectionSources, sectionActivities, sectionOverview);
    playSourcesEnter();
    playActivitiesEnter();
    playOverviewEnter();
    expect(sources.classList.contains("is-sources-entering")).toBe(true);
    expect(activities.classList.contains("is-activities-entering")).toBe(true);
    expect(overview.classList.contains("is-overview-entering")).toBe(true);
  });

  it("skips entrances under reduced motion", () => {
    reduced = true;
    const section = document.createElement("section");
    section.id = "section-sources";
    const stack = document.createElement("div");
    stack.className = "sources-stack";
    section.appendChild(stack);
    document.body.appendChild(section);
    playSourcesEnter();
    expect(stack.classList.contains("is-sources-entering")).toBe(false);
  });
});

describe("filter pills / list enter", () => {
  it("flashFilterPill toggles is-just-selected at 520ms", () => {
    const pill = document.createElement("button");
    document.body.appendChild(pill);
    flashFilterPill(pill);
    expect(pill.classList.contains("is-just-selected")).toBe(true);
    vi.advanceTimersByTime(520);
    expect(pill.classList.contains("is-just-selected")).toBe(false);
  });

  it("pulseFilterSummary animates summary box", async () => {
    const summary = document.createElement("div");
    summary.id = "filter-result-summary";
    document.body.appendChild(summary);
    vi.resetModules();
    const pulseModulePath = "./motion.ts?pulseSummary";
    const fresh = await import(pulseModulePath);
    const summaryEl = document.getElementById("filter-result-summary");
    expect(summaryEl).not.toBeNull();
    fresh.pulseFilterSummary();
    expect(summaryEl?.classList.contains("is-updated")).toBe(true);
  });

  it("playActivityListEnter staggers row delays capped at index 12", () => {
    const body = document.createElement("tbody");
    body.id = "activities-body";
    document.body.appendChild(body);
    for (let i = 0; i < 15; i += 1) {
      const row = document.createElement("tr");
      row.setAttribute("data-dynamic-id", `a${i}`);
      body.appendChild(row);
    }
    playActivityListEnter();
    const rows = body.querySelectorAll("tr[data-dynamic-id]");
    rows.forEach((row, idx) => {
      const el = row as HTMLElement;
      expect(el.classList.contains("is-row-entering")).toBe(true);
      const delay = el.style.getPropertyValue("--row-delay");
      expect(delay).toBe(`${Math.min(idx, 12) * 28}ms`);
    });
  });
});

describe("watch chip / flash rows", () => {
  it("highlightWatchUserChip clears is-new at 700ms", () => {
    const chip = document.createElement("span");
    chip.setAttribute("data-watch-mid", "10086");
    document.body.appendChild(chip);
    highlightWatchUserChip(10086);
    expect(chip.classList.contains("is-new")).toBe(true);
    vi.advanceTimersByTime(700);
    expect(chip.classList.contains("is-new")).toBe(false);
  });

  it("flashActivityRows flashes each row and badge, clearing at 1800ms", () => {
    const rowA = document.createElement("tr");
    rowA.setAttribute("data-dynamic-id", "111");
    const badgeA = document.createElement("span");
    badgeA.className = "badge";
    rowA.appendChild(badgeA);
    const rowB = document.createElement("tr");
    rowB.setAttribute("data-dynamic-id", "222");
    document.body.append(rowA, rowB);
    flashActivityRows(["111", "222"]);
    expect(rowA.classList.contains("row-flash")).toBe(true);
    expect(badgeA.classList.contains("is-badge-pop")).toBe(true);
    expect(rowB.classList.contains("row-flash")).toBe(true);
    vi.advanceTimersByTime(1800);
    expect(rowA.classList.contains("row-flash")).toBe(false);
    expect(badgeA.classList.contains("is-badge-pop")).toBe(false);
    expect(rowB.classList.contains("row-flash")).toBe(false);
  });
});

describe("save button feedback", () => {
  it("flashButtonSuccess swaps text and restores html at 1100ms unless loading", () => {
    const btn = document.createElement("button");
    btn.innerHTML = "<b>保存</b>";
    document.body.appendChild(btn);
    flashButtonSuccess(btn, "已保存");
    expect(btn.classList.contains("is-save-success")).toBe(true);
    expect(btn.textContent).toBe("已保存");
    btn.dataset.loadingActive = "true";
    vi.advanceTimersByTime(1100);
    expect(btn.innerHTML).toBe("已保存");

    const btn2 = document.createElement("button");
    btn2.innerHTML = "<b>保存</b>";
    document.body.appendChild(btn2);
    flashButtonSuccess(btn2);
    expect(btn2.textContent).toBe("已保存");
    vi.advanceTimersByTime(1100);
    expect(btn2.innerHTML).toBe("<b>保存</b>");
    expect(btn2.classList.contains("is-save-success")).toBe(false);
  });

  it("markSaveDirty / clearSaveDirty", () => {
    const btn = document.createElement("button");
    markSaveDirty(btn);
    expect(btn.classList.contains("is-dirty")).toBe(true);
    clearSaveDirty(btn);
    expect(btn.classList.contains("is-dirty")).toBe(false);
  });
});

describe("animateStatValue", () => {
  it("writes final value immediately under reduced motion or equal endpoints", () => {
    reduced = true;
    const el = document.createElement("span");
    animateStatValue(el, 10, 20);
    expect(el.textContent).toBe("20");
    expect(el.classList.contains("is-ticking")).toBe(false);
    reduced = false;
    const same = document.createElement("span");
    animateStatValue(same, 5, 5);
    expect(same.textContent).toBe("5");
  });

  it("ticks through RAF to final value then pops at 420ms", () => {
    const el = document.createElement("span");
    animateStatValue(el, 0, 100);
    expect(el.classList.contains("is-ticking")).toBe(true);
    runRafUntilIdle();
    expect(el.textContent).toBe("100");
    expect(el.classList.contains("is-ticking")).toBe(false);
    expect(el.classList.contains("is-value-pop")).toBe(true);
    vi.advanceTimersByTime(420);
    expect(el.classList.contains("is-value-pop")).toBe(false);
  });

  it("repeated call only leaves one trailing pop timer", () => {
    const el = document.createElement("span");
    animateStatValue(el, 0, 10);
    runRafUntilIdle();
    animateStatValue(el, 10, 20);
    runRafUntilIdle();
    expect(el.textContent).toBe("20");
  });

  it("ignores null element", () => {
    expect(() => animateStatValue(null, 1, 2)).not.toThrow();
  });
});
