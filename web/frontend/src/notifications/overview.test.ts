// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchJSONMock, startJobMock, showToastMock, openAppConfirmMock } = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(),
  startJobMock: vi.fn(),
  showToastMock: vi.fn(),
  openAppConfirmMock: vi.fn(),
}));

vi.mock("../api/client", () => ({ fetchJSON: fetchJSONMock }));
vi.mock("../jobs/index", () => ({ startJob: startJobMock }));
vi.mock("../shell/toast", () => ({ showToast: showToastMock }));
vi.mock("../shell/confirm", () => ({ openAppConfirm: openAppConfirmMock }));

describe("winner notification overview", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset();
    startJobMock.mockReset();
    showToastMock.mockReset();
    openAppConfirmMock.mockReset();
    openAppConfirmMock.mockResolvedValue(true);
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
          records: [
            {
              channel: "serverchan",
              event_type: "winning_candidate",
              event_id: "winning_candidate:dm:39945369:2596947714",
              status: "sent",
              sent_at: 1790486165,
              summary: "来源：dm\n发送者：39945369\n时间：1790486145\n摘要：39945369: 中奖中奖",
            },
          ],
        };
      }
      if (url === "/api/winner-scan") {
        return {
          sources: {
            mention: { cursor_id: 12345, found: 1, last_success_at: 1 },
            reply: { found: 0, last_success_at: 1 },
            dm: { found: 0, last_success_at: 1 },
          },
          candidates: [{ ...rawCandidate, key: "mention:1", discovered_at: 1, source_type: "mention", source_url: "https://t.bilibili.com/123", sender: { id: 7, name: "A" } }],
        };
      }
      if (url === "/api/auto/status") return { tasks: { winner: { status: "completed" } } };
      return {};
    });

    const module = await import("./index");
    module.bindNotificationsPage();
    await vi.waitFor(() => expect(document.getElementById("winner-scan-panel")).not.toBeNull());

    const rootText = document.getElementById("notifications-root")?.textContent || "";
    expect(rootText).toContain("凭据：已配置");
    expect(rootText).toContain("凭据：未配置");
    expect(rootText).toContain("渠道：✅ 已启用");
    expect(rootText).toContain("渠道：未启用");
    expect(rootText).toContain("@提及");
    expect(rootText).toContain("发送测试通知");
    expect(rootText).toContain("保存配置");
    expect(rootText).toContain("未接入");
    expect(rootText).toContain("Server酱");
    expect(rootText).toContain("中奖提醒 · 私信");
    expect(rootText).toContain("39945369：中奖中奖");
    expect(rootText).not.toContain("winning_candidate:dm:39945369:2596947714");
    expect(rootText).not.toContain("1790486145");
    expect(rootText).toContain("跳转链接");
    expect(rootText).toContain("删除");
    expect(rootText).toContain("A（UID 7）");
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

    const recordDelete = document.querySelector(".ntf-record-delete") as HTMLButtonElement;
    expect(recordDelete).not.toBeNull();
    recordDelete.click();
    await vi.waitFor(() => expect(fetchJSONMock).toHaveBeenCalledWith(
      "/api/notifications/records/winning_candidate%3Adm%3A39945369%3A2596947714/serverchan",
      { method: "DELETE" },
    ));
    expect(document.querySelectorAll("table.ntf-records")[0].querySelectorAll("tbody tr")).toHaveLength(0);
  });

  it("confirms and suppresses one candidate without deleting other rows", async () => {
    fetchJSONMock.mockImplementation(async (url: string, options: RequestInit = {}) => {
      if (url === "/api/notifications") return { channels: [], events: {}, records: [] };
      if (url === "/api/winner-scan") {
        return {
          sources: {},
          candidates: [
            { key: "mention:one", source_type: "mention", source_url: "https://t.bilibili.com/1", sender: { name: "A" }, summary: "A: 一" },
            { key: "reply:two", source_type: "reply", source_url: "", sender: { name: "B" }, summary: "B: 二" },
          ],
        };
      }
      if (url === "/api/auto/status") return { tasks: { winner: {} } };
      if (url === "/api/winner-scan/candidates/mention%3Aone/suppress") {
        expect(options.method).toBe("POST");
        return { ok: true };
      }
      return {};
    });
    const module = await import("./index");
    module.bindNotificationsPage();
    await vi.waitFor(() => expect(document.querySelector("[data-winner-delete]")).not.toBeNull());
    const rows = document.querySelectorAll("#winner-scan-panel tbody tr");
    expect(rows).toHaveLength(2);
    expect(document.querySelector("#winner-scan-panel")?.textContent).toContain("无链接");
    expect(document.querySelectorAll(".winner-row-actions")).toHaveLength(2);
    expect(document.querySelectorAll(".winner-jump-action")).toHaveLength(2);
    expect(document.querySelectorAll(".winner-delete-action")).toHaveLength(2);
    expect(document.querySelector('[data-winner-delete="mention:one"]')?.parentElement?.className).toBe("winner-row-actions");
    expect(document.querySelector('[data-winner-delete="reply:two"]')?.parentElement?.className).toBe("winner-row-actions");
    expect(document.querySelector('[data-winner-delete="reply:two"]')?.previousElementSibling?.textContent).toBe("无链接");
    (document.querySelector('[data-winner-delete="mention:one"]') as HTMLButtonElement)?.click();
    await vi.waitFor(() => expect(openAppConfirmMock).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(fetchJSONMock).toHaveBeenCalledWith(
      "/api/winner-scan/candidates/mention%3Aone/suppress",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(document.querySelectorAll("#winner-scan-panel tbody tr")).toHaveLength(1);
  });

  it("uses the message center for DM fallback and rejects unsafe links", async () => {
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url === "/api/notifications") return { channels: [], events: {}, records: [] };
      if (url === "/api/winner-scan") {
        return {
          sources: {},
          candidates: [
            { key: "dm:1:1", source_type: "dm", source_url: "javascript:alert(1)", sender: { id: 1 }, summary: "1: 私信" },
            { key: "mention:unsafe", source_type: "mention", source_url: "data:text/html,unsafe", sender: { id: 2 }, summary: "2: 不安全" },
          ],
        };
      }
      if (url === "/api/auto/status") return { tasks: { winner: {} } };
      return {};
    });
    const module = await import("./index");
    module.bindNotificationsPage();
    await vi.waitFor(() => expect(document.querySelectorAll(".winner-jump-action")).toHaveLength(2));
    const links = [...document.querySelectorAll("a.winner-jump-action")] as HTMLAnchorElement[];
    expect(links).toHaveLength(1);
    expect(links[0].href).toBe("https://message.bilibili.com/");
    expect(document.querySelectorAll("button.winner-jump-action")).toHaveLength(1);
    expect(document.querySelector("button.winner-jump-action")?.textContent).toBe("无链接");
  });
});
