// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchJSONMock, requireSetupMock, showToastMock, startJobMock } = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(), requireSetupMock: vi.fn(), showToastMock: vi.fn(), startJobMock: vi.fn(),
}));

vi.mock("../account/index", () => ({ requireSetup: requireSetupMock }));
vi.mock("../api/client", () => ({ fetchJSON: fetchJSONMock }));
vi.mock("../jobs/index", () => ({ startJob: startJobMock, updateJobUI: () => {} }));
vi.mock("../shell/toast", () => ({ showToast: showToastMock }));

function setupDom(): void {
  document.body.innerHTML = `
    <h2 id="source-count">共 7 个数据源</h2>
    <p id="update-all-summary"></p>
    <button id="update-all-sources-btn">一键更新全部数据源（串行）</button>
    <button id="update-all-stop-btn" disabled>停止更新</button>`;
}

describe("update all datasources UI", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset(); requireSetupMock.mockReset(); showToastMock.mockReset(); startJobMock.mockReset();
    requireSetupMock.mockReturnValue(true); startJobMock.mockResolvedValue(undefined); fetchJSONMock.mockResolvedValue({ ok: true });
    setupDom();
  });

  it("keeps the update button and start behavior", async () => {
    const module = await import("./update-all");
    module.bindUpdateAllDatasources();
    const startBtn = document.getElementById("update-all-sources-btn") as HTMLButtonElement;
    startBtn.click();
    expect(startBtn.disabled).toBe(true);
    await vi.waitFor(() => expect(startJobMock).toHaveBeenCalledWith("update_all_datasources", {}));
  });

  it("renders concise summary without standalone detail UI", async () => {
    const module = await import("./update-all");
    module.bindUpdateAllDatasources();
    window.dispatchEvent(new CustomEvent("binggo:job-completed", { detail: {
      action: "update_all_datasources",
      result: { update_all: {
        phase: "done", total: 8, success_count: 6, failed_count: 1,
        totals: { discovered_count: 120, persisted_count: 12 },
        sources: [
          { source_id: "DS-2", status: "failed", discovered_count: 8, persisted_count: 1 },
          { source_id: "DS1", status: "success", discovered_count: 100, persisted_count: 11 },
          { source_id: "DS-9", status: "not_run" },
        ],
      } },
    } }));
    expect(document.getElementById("update-all-summary")?.textContent).toContain("成功 6");
    expect(document.getElementById("update-all-summary")?.textContent).toContain("失败 1");
    expect(document.getElementById("update-all-summary")?.textContent).toContain("本轮发现 120");
    expect(document.getElementById("update-all-summary")?.textContent).toContain("实际新增 12");
    expect(document.getElementById("update-all-panel")).toBeNull();
    expect(document.getElementById("update-all-lanes")).toBeNull();
  });
});
