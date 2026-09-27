// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchJSONMock, showToastMock, startJobMock } = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(),
  showToastMock: vi.fn(),
  startJobMock: vi.fn(),
}));

vi.mock("../api/client", () => ({ fetchJSON: fetchJSONMock }));
vi.mock("../jobs/index", () => ({ startJob: startJobMock }));
vi.mock("../shell/toast", () => ({ showToast: showToastMock }));

describe("winner scheduler state", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset();
    showToastMock.mockReset();
    startJobMock.mockReset();
    startJobMock.mockResolvedValue(undefined);
    document.body.innerHTML = '<div id="notifications-root"></div>';
  });

  it("renders backend status/times/result and keeps the manual scan action", async () => {
    const now = Math.floor(Date.now() / 1000);
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url === "/api/notifications") return { channels: [], events: {}, records: [] };
      if (url === "/api/winner-scan") return { sources: {}, candidates: [] };
      if (url === "/api/auto/status") {
        return {
          state: "running",
          server_now_unix: now,
          tasks: {
            winner: {
              label: "中奖深探",
              status: "completed",
              last_started_at: "2026-09-08 17:04:00",
              last_finished_at: "2026-09-08 17:04:10",
              last_result: "本轮发现 0 条疑似中奖",
              next_run_at: now + 3600,
            },
          },
        };
      }
      return {};
    });

    const module = await import("./index");
    module.bindNotificationsPage();

    await vi.waitFor(() => expect(document.getElementById("winner-scheduler-state")).not.toBeNull());
    const stateText = document.getElementById("winner-scheduler-state")?.textContent || "";
    expect(stateText).toContain("扫描状态：完成");
    expect(stateText).toContain("上次扫描：2026-09-08 17:04:00");
    expect(stateText).toContain("新增线索：0");

    document.getElementById("winner-scan-run")?.click();
    expect(startJobMock).toHaveBeenCalledWith("winner_deep_scan", {});
  });

  it("updates risk state from existing scheduler events without a remote request", async () => {
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url === "/api/notifications") return { channels: [], events: {}, records: [] };
      if (url === "/api/winner-scan") return { sources: {}, candidates: [] };
      if (url === "/api/auto/status") return { state: "running", tasks: { winner: { status: "waiting" } } };
      return {};
    });
    const module = await import("./index");
    module.bindNotificationsPage();
    await vi.waitFor(() => expect(document.getElementById("winner-scheduler-state")).not.toBeNull());
    const before = fetchJSONMock.mock.calls.length;

    window.dispatchEvent(new CustomEvent("binggo:auto-status", {
      detail: {
        state: "running",
        tasks: {
          winner: {
            status: "risk_paused",
            resume_at: Math.floor(Date.now() / 1000) + 600,
            last_result: "平台风控冷却中",
          },
        },
      },
    }));

    expect(document.getElementById("winner-scheduler-state")?.textContent).toContain("扫描状态：风控暂停");
    expect(fetchJSONMock).toHaveBeenCalledTimes(before);
  });
});

describe("winner scan auto refresh on job terminal", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset();
    showToastMock.mockReset();
    startJobMock.mockReset();
    startJobMock.mockResolvedValue(undefined);
    document.body.innerHTML = '<div id="notifications-root"></div>';
  });

  function winnerScanCalls() {
    return fetchJSONMock.mock.calls.filter(([url]) => url === "/api/winner-scan").length;
  }

  async function setupPage() {
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url === "/api/notifications") return { channels: [], events: {}, records: [] };
      if (url === "/api/winner-scan") return { sources: {}, candidates: [] };
      if (url === "/api/auto/status") return { state: "running", tasks: { winner: { status: "completed" } } };
      return {};
    });
    const module = await import("./index");
    module.bindNotificationsPage();
    await vi.waitFor(() => expect(document.getElementById("winner-scheduler-state")).not.toBeNull());
    return module;
  }

  it("manual winner job terminal triggers a candidate refresh (one GET batch)", async () => {
    await setupPage();
    const before = winnerScanCalls();
    window.dispatchEvent(new CustomEvent("binggo:job-completed", {
      detail: { action: "winner_deep_scan", state: "completed" },
    }));
    await vi.waitFor(() => expect(winnerScanCalls()).toBe(before + 1));
  });

  it("same terminal event repeated does not cause unbounded refreshes", async () => {
    await setupPage();
    const before = winnerScanCalls();
    for (let i = 0; i < 5; i += 1) {
      window.dispatchEvent(new CustomEvent("binggo:job-completed", {
        detail: { action: "winner_deep_scan", state: "completed" },
      }));
    }
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(winnerScanCalls()).toBeLessThanOrEqual(before + 1);
  });

  it("terminal of another job does not refresh winner candidates", async () => {
    await setupPage();
    const before = winnerScanCalls();
    window.dispatchEvent(new CustomEvent("binggo:job-completed", {
      detail: { action: "refresh_all", state: "completed" },
    }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(winnerScanCalls()).toBe(before);
  });

  it("no periodic polling timer is created by the refresh listener", async () => {
    await setupPage();
    window.dispatchEvent(new CustomEvent("binggo:job-completed", {
      detail: { action: "winner_deep_scan", state: "completed" },
    }));
    await vi.waitFor(() => expect(winnerScanCalls()).toBeGreaterThan(0));
    const before = winnerScanCalls();
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(winnerScanCalls()).toBe(before); // 只触发一次 GET，无 interval 持续刷新
  });

  it("shows bounded malformed skip count per source without message bodies", async () => {
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url === "/api/notifications") return { channels: [], events: {}, records: [] };
      if (url === "/api/winner-scan") {
        return {
          sources: {
            mention: {
              cursor_id: 5,
              last_error: null,
              last_success_at: 1_750_000_000,
              malformed_skipped: [
                { ts: 1, source_type: "mention", reason: "条目缺少稳定平台 ID，已跳过" },
                { ts: 2, source_type: "mention", reason: "条目缺少稳定平台 ID，已跳过" },
              ],
            },
            reply: {},
            dm: {},
          },
          candidates: [{ id: "c1", discovered_at: 1_750_000_000, source_type: "mention", sender: {}, summary: "ok", status: "candidate" }],
        };
      }
      if (url === "/api/auto/status") return { state: "running", tasks: { winner: { status: "waiting" } } };
      return {};
    });
    const module = await import("./index");
    module.bindNotificationsPage();
    await vi.waitFor(() => expect(document.getElementById("winner-scan-panel")).not.toBeNull());
    const text = document.getElementById("winner-scan-panel")?.textContent || "";
    expect(text).toContain("跳过 2 条无法稳定识别消息");
    // 正常 candidate 不受影响（候选行仍渲染其摘要）
    expect(text).toContain("ok");
  });

  it("hidden tab does not trigger refresh while hidden", async () => {
    await setupPage();
    const before = winnerScanCalls();
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    document.dispatchEvent(new Event("visibilitychange"));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(winnerScanCalls()).toBe(before);
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
  });
});
