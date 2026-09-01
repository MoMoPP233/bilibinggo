// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../utils/motion", () => ({ prefersReducedMotion: () => false }));
vi.mock("../jobs/index", () => ({
  hideParticipationResult: vi.fn(),
  scheduleParticipationResultDismiss: vi.fn(),
}));
vi.mock("../state", () => ({ state: {} }));

function setupThemeDom(): void {
  document.documentElement.dataset.theme = "light";
  document.body.innerHTML = `
    <button type="button" class="system-btn" id="theme-toggle">
      <span class="system-btn-icon">
        <svg class="icon-moon"></svg>
        <svg class="icon-sun" hidden></svg>
      </span>
      <span class="system-btn-text">夜间模式</span>
    </button>`;
}

function visibleIcons(): string[] {
  const button = document.getElementById("theme-toggle") as HTMLElement;
  const visible: string[] = [];
  for (const cls of ["icon-moon", "icon-sun"]) {
    const element = button.querySelector(`.${cls}`) as HTMLElement;
    if (element && !element.hasAttribute("hidden")) visible.push(cls);
  }
  return visible;
}

describe("theme toggle icons", () => {
  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
    setupThemeDom();
  });

  it("light mode shows only the moon icon", async () => {
    const { applyTheme } = await import("./theme");
    applyTheme("light", { animate: false });
    expect(visibleIcons()).toEqual(["icon-moon"]);
  });

  it("dark mode shows only the sun icon", async () => {
    const { applyTheme } = await import("./theme");
    applyTheme("dark", { animate: false });
    expect(visibleIcons()).toEqual(["icon-sun"]);
  });

  it("toggling never leaves two icons visible", async () => {
    const { applyTheme } = await import("./theme");
    applyTheme("dark", { animate: false });
    applyTheme("light", { animate: false });
    expect(visibleIcons().length).toBe(1);
    applyTheme("dark", { animate: false });
    expect(visibleIcons().length).toBe(1);
  });
});
