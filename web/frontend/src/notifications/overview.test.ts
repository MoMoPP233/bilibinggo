// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchJSONMock, startJobMock, showToastMock } = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(),
  startJobMock: vi.fn(),
  showToastMock: vi.fn(),
}));

vi.mock("../api/client", () => ({ fetchJSON: fetchJSONMock }));
vi.mock("../jobs/index", () => ({ startJob: startJobMock }));
vi.mock("../shell/toast", () => ({ showToast: showToastMock }));

describe("winner notification overview", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset();
    startJobMock.mockReset();
    showToastMock.mockReset();
    document.body.innerHTML = '<div id="notifications-root"></div>';
  });

  it("keeps channel controls and simplifies empty states and winner clues", async () => {
    const longSummary = "这是一条用于验证展示层截断的中奖线索摘要".repeat(8);
    const rawCandidate = { summary: longSummary, status: "candidate" };
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url === "/api/notifications") {
        return {
          channels: [
            { channel: "serverchan", label: "Server酱", configured: true, enabled: true },
            { channel: "dingtalk", label: "钉钉", configured: false, enabled: false },
            { channel: "wecom", label: "企业微信", configured: false, enabled: false },
            { channel: "feishu", label: "飞书", configured: true, enabled: false },
          ],
          events: { winning_candidate: true, platform_risk: false, auto_task_error: false },
          records: [],
        };
      }
      if (url === "/api/winner-scan") {
        return {
          sources: {
            mention: { cursor_id: 12345, found: 1, last_success_at: 1 },
            reply: { found: 0, last_success_at: 1 },
            dm: { found: 0, last_success_at: 1 },
          },
          candidates: [{ ...rawCandidate, discovered_at: 1, source_type: "mention", sender: { name: "A" } }],
        };
      }
      if (url === "/api/auto/status") return { tasks: { winner: { status: "completed" } } };
      return {};
    });

    const module = await import("./index");
    module.bindNotificationsPage();
    await vi.waitFor(() => expect(document.getElementById("winner-scan-panel")).not.toBeNull());

    const rootText = document.getElementById("notifications-root")?.textContent || "";
    expect(rootText).toContain("已启用");
    expect(rootText).toContain("未配置");
    expect(rootText).toContain("已停用");
    expect(rootText).toContain("发送测试通知");
    expect(rootText).toContain("保存配置");
    expect(rootText).toContain("未接入");
    expect(rootText).toContain("暂无发送记录");
    expect(rootText).toContain("自动扫描私信、@提及和未读消息");
    expect(rootText).toContain("上次扫描：");
    expect(rootText).toContain("下次扫描：");
    expect(rootText).toContain("新增线索：1");
    expect(rootText).not.toContain("cursor 12345");
    expect(rootText).toContain("疑似中奖");
    expect(rootText).toContain("……");
    expect(document.querySelectorAll("[data-ntf-field]")).toHaveLength(6);
    expect(document.querySelectorAll("[data-ntf-event]")).toHaveLength(3);
    expect(rawCandidate.summary).toBe(longSummary);
  });
});
