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

  it("renders local rejection samples and clears only the audit endpoint", async () => {
    fetchJSONMock
      .mockResolvedValueOnce({
        topics: [{ topic_id: "47801", name: "抽奖号的日常", kind: "built-in", enabled: true }],
        summary: { builtin_count: 1, custom_count: 0 },
      })
      .mockResolvedValueOnce({
        samples: [{
          dynamic_id: "100000000000000001",
          canonical_url: "https://www.bilibili.com/opus/100000000000000001",
          source_topic_id: "47801",
          source_topic_name: "抽奖号的日常",
          classifier_text: "分类器实际看到的最终文本",
          last_seen_at: 1_758_600_000,
        }],
      });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const module = await import("./ds9-panel");
    module.bindDs9Panel();
    await vi.waitFor(() => expect(document.querySelector(".ds9-audit-item")).not.toBeNull());
    const panel = document.getElementById("ds9-topic-panel")!;
    expect(panel.textContent).toContain("47801");
    expect(panel.textContent).toContain("分类器实际看到的最终文本");
    expect(panel.querySelector(".ds9-audit-link")?.getAttribute("href")).toContain("100000000000000001");
    (panel.querySelector("[data-ds9-audit-clear]") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(fetchJSONMock).toHaveBeenCalledWith(
      "/api/ds9/rejection-audit",
      expect.objectContaining({ method: "DELETE" }),
    ));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("仅清空本地"));
    confirm.mockRestore();
  });

  it("exports the current local topic filter as CSV", async () => {
    fetchJSONMock
      .mockResolvedValueOnce({
        topics: [{ topic_id: "47801", name: "抽奖号的日常", kind: "built-in", enabled: true }],
        summary: { builtin_count: 1, custom_count: 0 },
      })
      .mockResolvedValueOnce({ samples: [] });
    const clicked: string[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this.href);
    });
    const module = await import("./ds9-panel");
    module.bindDs9Panel();
    await vi.waitFor(() => expect(document.querySelector("[data-ds9-audit-export]")).not.toBeNull());
    const panel = document.getElementById("ds9-topic-panel")!;
    const select = panel.querySelector("[data-ds9-audit-filter]") as HTMLSelectElement;
    select.value = "47801";
    (panel.querySelector("[data-ds9-audit-export]") as HTMLButtonElement).click();
    expect(clicked).toEqual([expect.stringContaining("/api/ds9/rejection-audit.csv?topic_id=47801")]);
    click.mockRestore();
  });
});
