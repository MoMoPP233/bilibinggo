// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const {
  fetchJSONMock,
  openAppConfirmMock,
  showToastMock,
} = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(),
  openAppConfirmMock: vi.fn(),
  showToastMock: vi.fn(),
}));

vi.mock("../api/client", () => ({ fetchJSON: fetchJSONMock }));
vi.mock("../shell/confirm", () => ({ openAppConfirm: openAppConfirmMock }));
vi.mock("../shell/toast", () => ({ showToast: showToastMock }));

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
    <p id="auto-following-info"></p>
    <div id="auto-dock-panel"></div>
    <button id="auto-dock-start"></button>
    <button id="auto-dock-stop"></button>
    <button id="auto-dock-toggle"></button>`;
}

function runningStatus() {
  return {
    state: "running",
    state_label: "调度运行中",
    message: "调度器运行中",
    current_phase: "等待下一刻度",
    logs: [],
  };
}

function stoppedStatus() {
  return { state: "stopped", state_label: "已停止", message: "已停止", current_phase: "", logs: [] };
}

function deferred() {
  let resolve: (value: unknown) => void = () => {};
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

function installFetch(statusQueue: Array<{ promise: Promise<unknown>; resolve: (value: unknown) => void }>) {
  fetchJSONMock.mockImplementation((url, options) => {
    if (url === "/api/auto/start") return Promise.resolve(runningStatus());
    if (url === "/api/auto/stop") return Promise.resolve(stoppedStatus());
    if (url === "/api/auto/status") {
      const next = statusQueue.shift();
      return next ? next.promise : Promise.resolve({});
    }
    return Promise.resolve({});
  });
}

describe("scheduler start/stop async race", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset();
    openAppConfirmMock.mockReset();
    showToastMock.mockReset();
    openAppConfirmMock.mockResolvedValue(true);
    setupDom();
  });

  it("late old start-refresh cannot override a newer stop", async () => {
    const startStatus = deferred();
    const stopStatus = deferred();
    installFetch([startStatus, stopStatus]);

    const mod = await import("./index");
    await mod.startAutoScheduler(); // 后台 /api/auto/status 使用 startStatus（挂起）
    await mod.stopAutoScheduler(); // stop 的后台刷新使用 stopStatus

    stopStatus.resolve(stoppedStatus());
    await stopStatus.promise;
    startStatus.resolve(runningStatus()); // 旧 start 轮询迟到
    await startStatus.promise;

    expect(document.getElementById("auto-dock-status")?.textContent).toBe("已停止");
  });

  it("late old stop-refresh cannot override a newer start", async () => {
    const stopStatus = deferred();
    const startStatus = deferred();
    installFetch([stopStatus, startStatus]);

    const mod = await import("./index");
    await mod.stopAutoScheduler(); // 后台 /api/auto/status 使用 stopStatus（挂起）
    await mod.startAutoScheduler(); // start 的后台刷新使用 startStatus

    startStatus.resolve(runningStatus());
    await startStatus.promise;
    stopStatus.resolve(stoppedStatus()); // 旧 stop 轮询迟到
    await stopStatus.promise;

    expect(document.getElementById("auto-dock-status")?.textContent).toBe("调度运行中");
  });
});
