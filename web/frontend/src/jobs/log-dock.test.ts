// @vitest-environment jsdom

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

function setupDom(): void {
  document.body.innerHTML = `
    <div class="log-dock is-idle" id="log-dock" data-tone="idle">
      <button type="button" class="log-dock-toggle" id="log-dock-toggle" aria-expanded="false" aria-label="打开任务日志">
        <span class="log-dock-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/></svg>
        </span>
        <span class="log-dock-label">任务日志</span>
        <span class="log-dock-badge" id="log-dock-badge" hidden>运行中</span>
      </button>
      <div class="log-dock-panel" id="log-dock-panel" aria-hidden="true">
        <div class="log-dock-head">
          <p class="log-dock-title">任务日志</p>
          <button type="button" class="log-dock-chevron-button" id="log-dock-panel-toggle" aria-expanded="false" aria-label="展开任务日志">
            <span class="log-dock-chevron" aria-hidden="true"></span>
          </button>
          <span class="log-dock-status is-idle" id="log-dock-status">空闲</span>
          <p id="job-message">暂无任务</p>
        </div>
        <div id="log-dock-body"><pre id="job-log"></pre><span id="log-dock-pin-hint" hidden></span></div>
      </div>
    </div>
    <div id="progress-banner" hidden data-percent="0"></div>
    <div id="progress-fill"></div>
    <div id="progress-fill-glow"></div>
    <div id="progress-label"></div>
    <div id="progress-detail"></div>
    <div id="progress-percent"></div>
    <span class="progress-percent-suffix">%</span>
    <svg><circle id="progress-ring"></circle></svg>
    <div id="progress-track"></div>
    <div id="progress-chip"></div>
    <div id="progress-steps"></div>`;
}

function firstBlock(css: string, selector: string): string {
  const start = css.indexOf(`${selector} {`);
  if (start < 0) return "";
  const end = css.indexOf("}", start);
  return end >= 0 ? css.slice(start, end + 1) : "";
}

describe("task log dock: original circular launcher + expanded blue card", () => {
  beforeEach(() => {
    vi.resetModules();
    setupDom();
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockReturnValue({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    });
  });

  it("starts collapsed showing the original circular icon launcher, blue card hidden", () => {
    const toggle = document.getElementById("log-dock-toggle") as HTMLElement;
    const panel = document.getElementById("log-dock-panel") as HTMLElement;
    const badge = document.getElementById("log-dock-badge") as HTMLElement;

    expect(toggle.hidden).toBe(false);
    expect(panel.classList.contains("is-open")).toBe(false);
    expect(toggle.querySelector(".log-dock-icon svg")).not.toBeNull();
    expect(toggle.querySelector(".log-dock-chevron")).toBeNull();
    expect(badge.hidden).toBe(true);
  });

  it("restores the original three-line icon DOM and keeps CSS classes from before the change", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    const css = readFileSync(resolve(process.cwd(), "src/styles/styles.css"), "utf8");

    // 原始圆形入口：三条横杠 svg 图标。
    expect(html).toContain("M8 6h13M8 12h13M8 18h13");
    expect(html).toContain('class="log-dock-icon"');
    expect(html).toContain('class="log-dock-label"');
    expect(html).toContain('class="log-dock-badge"');
    expect(html).toContain('id="log-dock-panel-toggle"');
    // 收起态不再是 “任务日志 + ▲ + 状态” 的横向 compact bar。
    expect(html).not.toContain("log-dock-compact-status");
    expect(html).not.toContain('<span class="log-dock-label">任务日志</span><span class="log-dock-chevron"');

    const iconBlock = firstBlock(css, ".log-dock-icon");
    expect(iconBlock).toContain("border-radius: 50%");
    const chevronButtonBlock = firstBlock(css, ".log-dock-chevron-button");
    expect(chevronButtonBlock).not.toContain("border-radius: 50%");
    expect(chevronButtonBlock).not.toContain("width: 34px");
    expect(chevronButtonBlock).not.toContain("background: color-mix");
    // 深浅主题原始样式仍在；三角无专属暗色背景。
    expect(css).toContain('[data-theme="dark"] .log-dock-panel');
    expect(css).not.toContain('[data-theme="dark"] .log-dock-chevron-button');
    expect(css).toContain("@media (max-width: 520px)");
  });

  it("expanded title shows a bare ▼ and collapsed launcher hides the chevron", async () => {
    const jobs = await import("./index");
    const toggle = document.getElementById("log-dock-toggle") as HTMLElement;
    const panel = document.getElementById("log-dock-panel") as HTMLElement;
    const triangle = document.getElementById("log-dock-panel-toggle") as HTMLElement;
    const triangleGlyph = triangle.querySelector(".log-dock-chevron") as HTMLElement;

    jobs.setLogDockOpen(false);
    expect(triangleGlyph.textContent).toBe("");
    expect(toggle.hidden).toBe(false);

    jobs.setLogDockOpen(true);
    expect(panel.classList.contains("is-open")).toBe(true);
    expect(triangleGlyph.textContent).toBe("▼");
    expect(toggle.hidden).toBe(true);
  });

  it("clicking the circular launcher expands; clicking the blue card / bare chevron collapses it", async () => {
    const jobs = await import("./index");
    const navigation = await import("../shell/nav");
    navigation.bindNavigation();

    const toggle = document.getElementById("log-dock-toggle") as HTMLButtonElement;
    const panel = document.getElementById("log-dock-panel") as HTMLElement;
    const triangle = document.getElementById("log-dock-panel-toggle") as HTMLButtonElement;
    const triangleGlyph = triangle.querySelector(".log-dock-chevron") as HTMLElement;

    toggle.click();
    expect(panel.classList.contains("is-open")).toBe(true);
    expect(triangleGlyph.textContent).toBe("▼");
    expect(toggle.hidden).toBe(true);

    // 点击整张蓝色卡片空白处收起 → 恢复圆形入口。
    panel.click();
    expect(panel.classList.contains("is-open")).toBe(false);
    expect(toggle.hidden).toBe(false);
    expect(triangleGlyph.textContent).toBe("");

    // 再次展开后点击裸 ▼ 收起。
    toggle.click();
    triangle.click();
    expect(panel.classList.contains("is-open")).toBe(false);
    expect(toggle.hidden).toBe(false);
  });

  it("does not collapse when clicking inside the interactive log body", async () => {
    const jobs = await import("./index");
    const navigation = await import("../shell/nav");
    navigation.bindNavigation();

    jobs.setLogDockOpen(true);
    const panel = document.getElementById("log-dock-panel") as HTMLElement;
    const logBox = document.getElementById("job-log") as HTMLElement;
    logBox.textContent = "第一行";
    logBox.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(panel.classList.contains("is-open")).toBe(true);
  });

  it("auto-collapses to the circular launcher on terminal states and preserves logs", async () => {
    const jobs = await import("./index");
    const running = {
      state: "running",
      action: "refresh_source",
      message: "正在扫描",
      log: "第一行\n第二行",
      progress_step: 1,
      progress_total: 2,
    };
    jobs.updateJobUI(running);
    const panel = document.getElementById("log-dock-panel") as HTMLElement;
    const toggle = document.getElementById("log-dock-toggle") as HTMLElement;
    expect(panel.classList.contains("is-open")).toBe(true);

    const completed = {
      ...running,
      state: "success",
      message: "扫描完成",
      log: "第一行\n第二行\n完成",
      progress_step: 2,
    };
    jobs.updateJobUI(completed);
    expect(panel.classList.contains("is-open")).toBe(false);
    expect(toggle.hidden).toBe(false);
    // 日志未被清空。
    expect(document.getElementById("job-log")?.textContent).toContain("第二行\n完成");
    // 展开卡片的右侧状态徽标仍为已完成；收起时圆形入口不展示整段状态文字。
    expect(document.getElementById("log-dock-status")?.textContent).toBe("已完成");
    expect(document.getElementById("log-dock-badge")?.hidden).toBe(true);
  });

  it("supports re-expanding after completion and ignores repeat terminal polls", async () => {
    const jobs = await import("./index");
    const navigation = await import("../shell/nav");
    navigation.bindNavigation();
    const running = {
      state: "running",
      action: "refresh_source",
      message: "正在扫描",
      log: "第一行",
      progress_step: 1,
      progress_total: 2,
    };
    jobs.updateJobUI(running);
    const panel = document.getElementById("log-dock-panel") as HTMLElement;
    const toggle = document.getElementById("log-dock-toggle") as HTMLElement;
    const triangle = document.getElementById("log-dock-panel-toggle") as HTMLElement;

    const completed = { ...running, state: "error", message: "失败", log: "第一行\n错误", progress_step: 2 };
    jobs.updateJobUI(completed);
    expect(panel.classList.contains("is-open")).toBe(false);

    // 用户手动重新展开后，重复收到相同终态轮询不再次自动收起。
    toggle.click();
    expect(panel.classList.contains("is-open")).toBe(true);
    expect(triangle.textContent).toContain("▼");
    jobs.updateJobUI(completed);
    jobs.updateJobUI(completed);
    expect(panel.classList.contains("is-open")).toBe(true);
    expect(document.getElementById("job-log")?.textContent).toContain("错误");
  });
});
