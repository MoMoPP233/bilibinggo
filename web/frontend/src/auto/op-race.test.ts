// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";

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
    <pre id="auto-log"></pre>
    <p id="auto-next-task"></p>
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
    vi.restoreAllMocks();
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

  it("ships the original compact monitor structure without per-task cards", () => {
    const html = readFileSync("index.html", "utf-8");
    const monitor = html.slice(html.indexOf('id="auto-dock"'), html.indexOf('id="log-dock"'));

    expect(monitor).toContain('id="auto-dock-countdown"');
    expect(monitor).toContain('id="auto-dock-pipeline"');
    expect(monitor).toContain('id="auto-next-task"');
    expect(monitor.match(/auto-dock-schedule-note/g)).toHaveLength(4);
    expect(monitor).not.toContain('id="auto-task-status-list"');
    expect(html).toContain('id="auto-log"');
  });

  it("uses a read-only REST fallback after expiry and enters the next countdown without reload", async () => {
    const mod = await import("./index");
    const freshState = await import("../state");
    freshState.state.sseHealthy = true;
    freshState.state.eventSource = {} as EventSource;
    const now = Math.floor(Date.now() / 1000);
    const refreshed = {
      ...runningStatus(),
      server_now_unix: now,
      next_task: { label: "自动参与", minutes: 2, at_unix: now + 120 },
      tasks: {
        cleanup: {
          label: "清理维护",
          status: "completed",
          last_result: "清理维护完成",
          next_run_at: now + 21600,
        },
      },
      logs: [],
    };
    fetchJSONMock.mockImplementation((url, options) => {
      if (url === "/api/auto/status") return Promise.resolve(refreshed);
      return Promise.resolve({});
    });

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
    await vi.waitFor(() => expect(fetchJSONMock).toHaveBeenCalledWith("/api/auto/status"));
    await vi.waitFor(() => expect(document.getElementById("auto-dock-countdown")?.textContent).not.toBe("正在确认状态…"));

    expect(document.getElementById("auto-dock-countdown")?.textContent).not.toBe("00:00");
    expect(document.getElementById("auto-next-task")?.textContent).toContain("自动参与");
    expect(fetchJSONMock.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
  });

  it("deduplicates expiry refreshes and shows running from backend state", async () => {
    let clock = Date.now();
    vi.spyOn(Date, "now").mockImplementation(() => clock);
    const pending = deferred();
    fetchJSONMock.mockReturnValue(pending.promise);
    const mod = await import("./index");
    const now = Math.floor(Date.now() / 1000);
    mod.renderAutoDock({
      ...runningStatus(),
      server_now_unix: now,
      next_task: { label: "清理维护", minutes: 0, at_unix: now - 1 },
      tasks: { cleanup: { status: "waiting", next_run_at: now - 1 } },
      logs: [],
    });
    mod.tickAutoCountdown();
    mod.tickAutoCountdown();
    expect(fetchJSONMock).toHaveBeenCalledTimes(1);

    pending.resolve({
      ...runningStatus(),
      server_now_unix: now,
      next_task: { label: "自动参与", minutes: 5, at_unix: now + 300 },
      tasks: { cleanup: { status: "running", next_run_at: now + 21600 } },
      logs: [],
    });
    await pending.promise;
    await vi.waitFor(() => expect(document.getElementById("auto-dock-countdown")?.textContent).toBe("正在执行"));
    clock += 1600;
    mod.tickAutoCountdown();
    await vi.waitFor(() => expect(fetchJSONMock).toHaveBeenCalledTimes(2));
    expect(fetchJSONMock.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
  });

  it("uses risk cooldown and hides countdown when the scheduler is stopped", async () => {
    const mod = await import("./index");
    const now = Math.floor(Date.now() / 1000);
    mod.renderAutoDock({
      ...runningStatus(),
      server_now_unix: now,
      next_task: { label: "自动参与", minutes: 30, at_unix: now + 1800 },
      tasks: { cleanup: { status: "risk_paused", resume_at: now + 60 } },
      logs: [],
    });
    expect(document.getElementById("auto-dock-countdown")?.textContent).toBe("01:00");

    mod.renderAutoDock({
      ...stoppedStatus(),
      server_now_unix: now,
      next_task: { label: "自动参与", minutes: 30, at_unix: now + 1800 },
    });
    expect(document.getElementById("auto-dock-countdown")?.textContent).toBe("—");
  });

  it("moves a skipped_busy task directly to the backend-provided next countdown", async () => {
    const mod = await import("./index");
    const now = Math.floor(Date.now() / 1000);
    mod.renderAutoDock({
      ...runningStatus(),
      server_now_unix: now,
      current_phase: "本轮跳过（任务占用）",
      next_task: { label: "关注补漏", minutes: 5, at_unix: now + 300 },
      tasks: {
        cleanup: {
          status: "skipped_busy",
          last_result: "本轮跳过（任务占用）",
          next_run_at: now + 21600,
        },
      },
      logs: [],
    });

    expect(document.getElementById("auto-dock-countdown")?.textContent).toBe("05:00");
    expect(document.getElementById("auto-dock-phase")?.textContent).toBe("本轮跳过（任务占用）");
    expect(document.getElementById("auto-next-task")?.textContent).toContain("关注补漏");
    expect(fetchJSONMock.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
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
