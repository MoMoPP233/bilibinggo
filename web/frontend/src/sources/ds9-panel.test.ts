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

describe("DS9 topic cards", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset();
    startJobMock.mockReset();
    showToastMock.mockReset();
    startJobMock.mockResolvedValue(undefined);
    document.body.innerHTML = '<article id="watch-panel"></article>';
    fetchJSONMock.mockResolvedValue({
      topics: [
        { topic_id: "1261901", name: "抽奖！！！", kind: "built-in", enabled: true, fp: "fp-a", last_scan_at: 1_758_600_000, last_found: 59 },
        { topic_id: "1055656", name: "转发动态抽奖！", kind: "built-in", enabled: false, fp: "fp-b", last_scan_at: 1_758_600_000, last_found: 57 },
        { topic_id: "47801", name: "抽奖号的日常", kind: "built-in", enabled: true, fp: null, last_scan_at: null, last_found: 0 },
      ],
      summary: { builtin_count: 3, custom_count: 0 },
    });
  });

  it("renders compact horizontal-ready cards without checkpoint terminology", async () => {
    const module = await import("./ds9-panel");
    module.bindDs9Panel();
    await vi.waitFor(() => expect(document.querySelectorAll(".ds9-topic-card")).toHaveLength(3));
    const panel = document.getElementById("ds9-topic-panel")!;
    const text = panel.textContent || "";
    expect(text).toContain("话题：抽奖！！！");
    expect(text).toContain("1261901 · 内置");
    expect(text).toContain("话题：抽奖号的日常");
    expect(text).toContain("47801 · 内置");
    expect(panel.querySelector("[data-ds9-builtin]")?.textContent).toBe("3");
    expect(text).toContain("最近扫描：");
    expect(text).toContain("发现 59 · 已推送数据库");
    expect(text).toContain("暂停");
    expect(text).toContain("恢复");
    expect(text).not.toContain("checkpoint");
    expect(document.querySelector(".ds9-topic-rows")?.classList.contains("ds9-topic-rows")).toBe(true);
  });

  it("uses the same card template for custom topics and keeps delete action", async () => {
    fetchJSONMock.mockResolvedValueOnce({
      topics: [{ topic_id: "12345678", name: "自定义抽奖", kind: "custom", enabled: true, fp: null, last_scan_at: null, last_found: 0 }],
      summary: { builtin_count: 0, custom_count: 1 },
    });
    const module = await import("./ds9-panel");
    module.bindDs9Panel();
    await vi.waitFor(() => expect(document.querySelector(".ds9-topic-card")).not.toBeNull());
    expect(document.querySelector("[data-ds9-delete]")).not.toBeNull();
    expect(document.querySelector(".ds9-topic-card-id")?.textContent).toContain("自定义");
  });

  it("keeps pause/resume and refresh actions wired to existing APIs", async () => {
    const module = await import("./ds9-panel");
    module.bindDs9Panel();
    await vi.waitFor(() => expect(document.querySelector("[data-ds9-toggle]")).not.toBeNull());
    (document.querySelector("[data-ds9-toggle]") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(fetchJSONMock).toHaveBeenCalledWith(
      "/api/ds9/topics/1261901/enabled",
      expect.objectContaining({ method: "PUT", body: JSON.stringify({ enabled: false }) }),
    ));
    (document.querySelector("[data-ds9-refresh]") as HTMLButtonElement).click();
    expect(startJobMock).toHaveBeenCalledWith("refresh_source", { source_id: "DS-9" });
  });

  it("shows per-topic unique totals and stable-ID delta without standalone audit sections", async () => {
    fetchJSONMock
      .mockResolvedValueOnce({
        topics: [
          { topic_id: "47801", name: "抽奖号的日常", kind: "built-in", enabled: true, last_unique_added: 2 },
          { topic_id: "1261901", name: "抽奖！！！", kind: "built-in", enabled: true, last_unique_added: 0 },
        ],
        summary: { builtin_count: 2, custom_count: 0 },
      })
      .mockResolvedValueOnce({
        records: [
          { topic_id: "47801", dynamic_id: "100000000000000001" },
          { topic_id: "47801", dynamic_id: "100000000000000002" },
          { topic_id: "47801", dynamic_id: "100000000000000001" },
        ],
      });
    const module = await import("./ds9-panel");
    module.bindDs9Panel();
    await vi.waitFor(() => expect(document.querySelector("[data-topic-id='47801'] .ds9-topic-unique-summary")?.textContent).toContain("独有抽奖 2 条"));
    const panel = document.getElementById("ds9-topic-panel")!;
    expect(panel.querySelector("[data-topic-id='47801'] .ds9-topic-unique-summary")?.textContent).toContain("较上次扫描新增 2 条");
    expect(panel.querySelector("[data-topic-id='1261901'] .ds9-topic-unique-summary")?.textContent).toContain("独有抽奖 0 条");
    expect(panel.querySelector("[data-topic-id='1261901'] .ds9-topic-unique-summary")?.textContent).toContain("较上次扫描新增 0 条");
    expect(panel.querySelectorAll("[data-topic-id='47801'] .ds9-unique-item")).toHaveLength(2);
    expect(panel.querySelector("[data-ds9-unique]")).toBeNull();
    expect(panel.querySelector("[data-ds9-audit]")).toBeNull();
    expect(panel.textContent).not.toContain("非抽奖审计样本");
    expect(fetchJSONMock).not.toHaveBeenCalledWith("/api/ds9/rejection-audit");
  });

  it("shows first-scan state with old topic data and keeps an empty topic usable", async () => {
    fetchJSONMock
      .mockResolvedValueOnce({
        topics: [{ topic_id: "47801", name: "抽奖号的日常", kind: "built-in", enabled: true }],
        summary: { builtin_count: 1, custom_count: 0 },
      })
      .mockResolvedValueOnce({ records: [] });
    const module = await import("./ds9-panel");
    module.bindDs9Panel();
    await vi.waitFor(() => expect(document.querySelector(".ds9-topic-unique-summary")?.textContent).toContain("首次扫描"));
    expect(document.querySelector(".ds9-topic-unique-summary")?.textContent).toContain("独有抽奖 0 条");
    expect(document.querySelector(".ds9-topic-unique-details")).toBeNull();
  });

  it("renders an empty topic list without a standalone unique or rejection section", async () => {
    fetchJSONMock
      .mockResolvedValueOnce({ topics: [], summary: { builtin_count: 0, custom_count: 0 } })
      .mockResolvedValueOnce({ records: [] });
    const module = await import("./ds9-panel");
    module.bindDs9Panel();
    await vi.waitFor(() => expect(document.querySelector("#ds9-topic-panel .ds9-status-pill")?.textContent).toBe("已暂停"));
    expect(document.querySelectorAll(".ds9-topic-card")).toHaveLength(0);
    expect(document.querySelector("[data-ds9-unique]")).toBeNull();
    expect(document.querySelector("[data-ds9-audit]")).toBeNull();
  });

  it("keeps unique details inside their topic, collapsed by default, and preserves CSV export", async () => {
    fetchJSONMock
      .mockResolvedValueOnce({
        topics: [{ topic_id: "47801", name: "抽奖号的日常", kind: "built-in", enabled: true }],
        summary: { builtin_count: 1, custom_count: 0 },
      })
      .mockResolvedValueOnce({ records: [{ topic_id: "47801", topic_name: "抽奖号的日常", dynamic_id: "123" }] });
    const clicked: string[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this.href);
    });
    const module = await import("./ds9-panel");
    module.bindDs9Panel();
    await vi.waitFor(() => expect(document.querySelector(".ds9-topic-unique-details")).not.toBeNull());
    const details = document.querySelector("[data-topic-id='47801'] .ds9-topic-unique-details") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details.textContent).toContain("DS9 独有");
    expect(details.querySelector(".ds9-audit-link")?.getAttribute("href")).toContain("123");
    (document.querySelector("[data-ds9-contribution-export]") as HTMLButtonElement).click();
    expect(clicked).toEqual([expect.stringContaining("/api/ds9/contribution.csv")]);
    click.mockRestore();
  });
});
