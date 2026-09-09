// @vitest-environment jsdom
// idle countdown（V11）：stopped/无目标 → 0 个 1s timer；有目标/运行中 → 恢复唯一 timer。

import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("../api/client", () => ({ fetchJSON: fetchJSONMock }));
vi.mock("../shell/toast", () => ({ showToast: vi.fn() }));

function setupDom() {
  document.body.innerHTML = `
    <p id="auto-dock-status"></p>
    <p id="auto-dock-phase"></p>
    <p id="auto-dock-hint"></p>
    <p id="auto-dock-message"></p>
    <div id="auto-dock-pipeline"></div>
    <span id="auto-dock-countdown"></span>
    <span id="auto-dock-scheduler"></span>
    <span id="auto-dock-job"></span>
    <pre id="auto-log"></pre>
    <p id="auto-next-task"></p>
    <p id="auto-following-info"></p>
    <div id="auto-dock-panel"></div>
    <button id="auto-dock-start"></button>
    <button id="auto-dock-stop"></button>
    <button id="auto-dock-toggle"></button>
    <p id="auto-dock-collapse"></p>`;
}

function status(overrides: Record<string, any> = {}) {
  const now = Math.floor(Date.now() / 1000);
  return {
    state: "running",
    state_label: "运行中",
    message: "状态正常",
    current_phase: "等待下一轮",
    server_now_unix: now,
    logs: [],
    tasks: {},
    next_task: null,
    ...overrides,
  };
}

async function loadModule() {
  vi.resetModules();
  fetchJSONMock.mockReset();
  fetchJSONMock.mockResolvedValue({ ok: true });
  const { state } = await import("../state");
  state.autoDockOpen = false;
  state.sseHealthy = true;
  state.autoScheduler = null;
  state.autoPollTimer = null;
  state.autoCountdownTimer = null;
  const mod = await import("./index");
  return { state, mod };
}

describe("idle countdown timer lifecycle", () => {
  beforeEach(() => {
    setupDom();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("A stopped without target keeps 0 active countdown intervals", async () => {
    const { mod } = await loadModule();
    let ticks = 0;
    const onTick = () => { ticks += 1; };
    window.addEventListener("binggo:auto-tick", onTick);
    mod.renderAutoDock(status({ state: "stopped", next_task: null, tasks: {} }));
    ticks = 0;
    await vi.advanceTimersByTimeAsync(2500);
    expect(ticks).toBe(0);
    window.removeEventListener("binggo:auto-tick", onTick);
  });

  it("B waiting/running with future next_task keeps 1 countdown interval", async () => {
    const { mod } = await loadModule();
    let ticks = 0;
    const onTick = () => { ticks += 1; };
    window.addEventListener("binggo:auto-tick", onTick);
    mod.renderAutoDock(status({ next_task: { label: "清理维护", minutes: 5, at_unix: Math.floor(Date.now() / 1000) + 300 } }));
    ticks = 0;
    await vi.advanceTimersByTimeAsync(1500);
    expect(ticks).toBeGreaterThan(0);
    window.removeEventListener("binggo:auto-tick", onTick);
  });

  it("C stopped -> started restarts the countdown", async () => {
    const { mod } = await loadModule();
    let ticks = 0;
    const onTick = () => { ticks += 1; };
    window.addEventListener("binggo:auto-tick", onTick);
    mod.renderAutoDock(status({ state: "stopped" }));
    ticks = 0;
    await vi.advanceTimersByTimeAsync(1500);
    expect(ticks).toBe(0);
    mod.renderAutoDock(status({ next_task: { label: "刷新", minutes: 1, at_unix: Math.floor(Date.now() / 1000) + 60 } }));
    ticks = 0;
    await vi.advanceTimersByTimeAsync(1500);
    expect(ticks).toBeGreaterThan(0);
    window.removeEventListener("binggo:auto-tick", onTick);
  });

  it("D started -> stopped clears the countdown", async () => {
    const { mod } = await loadModule();
    let ticks = 0;
    const onTick = () => { ticks += 1; };
    window.addEventListener("binggo:auto-tick", onTick);
    mod.renderAutoDock(status({ next_task: { label: "刷新", minutes: 5, at_unix: Math.floor(Date.now() / 1000) + 300 } }));
    ticks = 0;
    await vi.advanceTimersByTimeAsync(1100);
    expect(ticks).toBeGreaterThan(0);
    mod.renderAutoDock(status({ state: "stopped", tasks: {} }));
    ticks = 0;
    await vi.advanceTimersByTimeAsync(2500);
    expect(ticks).toBe(0);
    window.removeEventListener("binggo:auto-tick", onTick);
  });

  it("E repeated status updates never create two timers", async () => {
    const { mod } = await loadModule();
    let ticks = 0;
    const onTick = () => { ticks += 1; };
    window.addEventListener("binggo:auto-tick", onTick);
    const future = status({ next_task: { label: "刷新", minutes: 5, at_unix: Math.floor(Date.now() / 1000) + 300 } });
    for (let i = 0; i < 5; i += 1) {
      mod.renderAutoDock(future);
    }
    ticks = 0;
    await vi.advanceTimersByTimeAsync(2600);
    // 单一 1s interval 在 2.6s 内产生 2~3 次 tick；若存在两个 timer 则约 5~6 次
    expect(ticks).toBeGreaterThan(0);
    expect(ticks).toBeLessThanOrEqual(3);
    window.removeEventListener("binggo:auto-tick", onTick);
  });

  it("G risk cooldown resume_at keeps counting down", async () => {
    const { mod } = await loadModule();
    let ticks = 0;
    const onTick = () => { ticks += 1; };
    window.addEventListener("binggo:auto-tick", onTick);
    const now = Math.floor(Date.now() / 1000);
    mod.renderAutoDock(status({
      state: "running",
      tasks: { winner: { label: "中奖深探", status: "risk_paused", resume_at: now + 600 } },
    }));
    ticks = 0;
    await vi.advanceTimersByTimeAsync(1500);
    expect(ticks).toBeGreaterThan(0);
    window.removeEventListener("binggo:auto-tick", onTick);
  });

  it("F 00:00 expiry confirmation still performs exactly one bounded status refresh", async () => {
    const { mod } = await loadModule();
    const now = Math.floor(Date.now() / 1000);
    mod.renderAutoDock(status({
      state: "running",
      next_task: { label: "刷新", minutes: -1, at_unix: now - 60 },
    }));
    const statusCalls = fetchJSONMock.mock.calls.filter(([url]) => url === "/api/auto/status");
    await vi.advanceTimersByTimeAsync(3500);
    const laterCalls = fetchJSONMock.mock.calls.filter(([url]) => url === "/api/auto/status");
    expect(statusCalls.length).toBeGreaterThan(0);
    expect(laterCalls.length).toBeLessThanOrEqual(statusCalls.length + 1);
  });
});
