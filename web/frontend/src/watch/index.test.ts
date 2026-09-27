// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({ fetchJSON: vi.fn() }));
vi.mock("../account/index", () => ({ isLoggedIn: () => true }));
vi.mock("../jobs/index", () => ({ bindActionButtons: vi.fn() }));
vi.mock("../shell/toast", () => ({ showToast: vi.fn() }));
vi.mock("../utils/motion", () => ({
  highlightWatchUserChip: vi.fn(),
  setButtonLoading: vi.fn(),
}));

describe("data source collection UI", () => {
  beforeEach(() => {
    vi.resetModules();
    document.body.innerHTML = `
      <p id="source-count"></p>
      <div id="source-grid"></div>`;
  });

  it("hides the DS9 duplicate and counts only visible collection sources", async () => {
    const { renderSources } = await import("./index");
    renderSources([
      { id: "DS-1", name: "源一", link_count: 2 },
      { id: "DS-9", name: "话题发现", link_count: 3 },
      { id: "DS-2", name: "源二", link_count: 4 },
    ]);

    expect(document.getElementById("source-count")?.textContent).toBe("共 2 个数据源");
    expect(document.querySelectorAll(".source-row")).toHaveLength(2);
    expect(document.querySelector('[data-source-id="DS-9"]')).toBeNull();
    expect(document.querySelector('[data-source-id="DS-1"] [data-action="refresh_source"]')).not.toBeNull();
    expect(document.querySelector('[data-source-id="DS-2"] [data-action="refresh_source"]')).not.toBeNull();
  });

  it("maps update-all results by source id and preserves source actions", async () => {
    const { renderSources } = await import("./index");
    const sources = [
      { id: "DS-1", name: "源一", link_count: 2 },
      { id: "DS-2", name: "源二", link_count: 4 },
      { id: "DS-9", name: "话题发现", link_count: 8 },
    ];
    renderSources(sources);
    window.dispatchEvent(new CustomEvent("binggo:update-all-result", { detail: {
      sources: [
        { source_id: "DS-2", status: "failed", discovered_count: 7, persisted_count: 2 },
        { source_id: "DS1", status: "success", discovered_count: 11, persisted_count: 3 },
      ],
    } }));
    expect(document.querySelector('[data-update-all-source-id="DS-1"]')?.textContent).toContain("本次完成");
    expect(document.querySelector('[data-update-all-source-id="DS-1"]')?.textContent).toContain("本轮发现 11");
    expect(document.querySelector('[data-update-all-source-id="DS-2"]')?.textContent).toContain("更新失败");
    expect(document.querySelector('[data-update-all-source-id="DS-2"]')?.textContent).toContain("新增 2");
    expect(document.querySelector('[data-source-id="DS-9"]')).toBeNull();
    expect(document.querySelector('[data-source-id="DS-1"] [data-action="refresh_source"]')).not.toBeNull();
  });
});
