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
    expect(stateText).toContain("自动调度：完成");
    expect(stateText).toContain("2026-09-08 17:04:00");
    expect(stateText).toContain("本轮发现 0 条疑似中奖");

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

    expect(document.getElementById("winner-scheduler-state")?.textContent).toContain("风控暂停");
    expect(document.getElementById("winner-scheduler-state")?.textContent).toContain("平台风控冷却中");
    expect(fetchJSONMock).toHaveBeenCalledTimes(before);
  });
});
