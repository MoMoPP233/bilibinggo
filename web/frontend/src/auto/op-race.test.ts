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
    <div id="auto-task-status-list"></div>
    <pre id="auto-log"></pre>
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

  it("renders backend task state and never leaves an expired countdown at 00:00", async () => {
    const mod = await import("./index");
    const now = Math.floor(Date.now() / 1000);

    mod.renderAutoDock({
      ...runningStatus(),
      server_now_unix: now,
      next_task: { label: "清理维护", minutes: 0, at_unix: now - 1 },
      tasks: {
        cleanup: {
          label: "清理维护",
          status: "waiting",
          last_result: "尚未执行",
          next_run_at: now - 1,
        },
      },
      logs: [],
    });

    expect(document.getElementById("auto-dock-countdown")?.textContent).toBe("正在确认状态…");
    expect(document.getElementById("auto-task-status-list")?.textContent).toContain("正在确认状态…");

    mod.renderAutoDock({
      ...runningStatus(),
      server_now_unix: now,
      next_task: { label: "自动参与", minutes: 3, at_unix: now + 180 },
      tasks: {
        cleanup: {
          label: "清理维护",
          status: "running",
          last_result: "清理维护开始",
          next_run_at: now + 21600,
        },
      },
      logs: [],
    });

    expect(document.getElementById("auto-dock-countdown")?.textContent).toBe("正在执行");
    expect(document.getElementById("auto-task-status-list")?.textContent).toContain("运行中");
  });

  it("renders only the newest 100 sanitized scheduler log lines", async () => {
    const mod = await import("./index");
    const logs = Array.from({ length: 105 }, (_, index) => ({
      ts: `2026-09-08 12:00:${String(index % 60).padStart(2, "0")}`,
      level: "info",
      message: index === 104
        ? "GET https://api.example.test/feed?topic_id=123&csrf=secret SESSDATA=cookie-secret"
        : `日志 ${index}`,
    }));

    mod.renderAutoLogs(logs);

    const text = document.getElementById("auto-log")?.textContent || "";
    expect(text.split("\n")).toHaveLength(100);
    expect(text).not.toContain("日志 0");
    expect(text).not.toContain("topic_id=123");
    expect(text).not.toContain("cookie-secret");
    expect(text).toContain("[查询参数已隐藏]");
  });
});
