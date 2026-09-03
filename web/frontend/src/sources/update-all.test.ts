// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const {
  fetchJSONMock,
  requireSetupMock,
  showToastMock,
  startJobMock,
} = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(),
  requireSetupMock: vi.fn(),
  showToastMock: vi.fn(),
  startJobMock: vi.fn(),
}));

vi.mock("../account/index", () => ({ requireSetup: requireSetupMock }));
vi.mock("../api/client", () => ({ fetchJSON: fetchJSONMock }));
vi.mock("../jobs/index", () => ({
  startJob: startJobMock,
  updateJobUI: () => {},
  trackCurrentJob: () => {},
}));
vi.mock("../shell/toast", () => ({ showToast: showToastMock }));

function setupDom(): void {
  document.body.innerHTML = `
    <div id="update-all-panel" hidden></div>
    <p id="update-all-summary"></p>
    <div id="update-all-lanes"></div>
    <button id="update-all-sources-btn">一键更新全部数据源（串行）</button>
    <button id="update-all-stop-btn" disabled>停止更新</button>`;
}

async function loadModule() {
  return import("./update-all");
}

describe("update all datasources UI", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset();
    requireSetupMock.mockReset();
    showToastMock.mockReset();
    startJobMock.mockReset();
    requireSetupMock.mockReturnValue(true);
    startJobMock.mockResolvedValue(undefined);
    fetchJSONMock.mockResolvedValue({ ok: true });
    setupDom();
  });

  it("start locks the button instantly and ignores an ultra-fast second click", async () => {
    const module = await loadModule();
    module.bindUpdateAllDatasources();
    const startBtn = document.getElementById("update-all-sources-btn") as HTMLButtonElement;

    expect(startBtn.disabled).toBe(false);
    startBtn.click();
    expect(startBtn.disabled).toBe(true);
    expect(document.getElementById("update-all-panel")?.hasAttribute("hidden")).toBe(false);

    startBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await vi.waitFor(() => expect(startJobMock).toHaveBeenCalledTimes(1));
    expect(startJobMock).toHaveBeenCalledWith("update_all_datasources", {});
  });

  it("recovers the start button when the job start API fails", async () => {
    startJobMock.mockRejectedValueOnce(new Error("网络异常"));
    const module = await loadModule();
    module.bindUpdateAllDatasources();
    const startBtn = document.getElementById("update-all-sources-btn") as HTMLButtonElement;

    startBtn.click();
    await vi.waitFor(() => expect(showToastMock).toHaveBeenCalled());
    expect(startBtn.disabled).toBe(false);
    expect(startJobMock).toHaveBeenCalledTimes(1);
  });

  it("keeps start disabled while a real running job exists and re-enables on completion", async () => {
    const freshState = await import("../state");
    (freshState.state as unknown as { currentJob: unknown }).currentJob = {
      id: 1,
      state: "running",
      action: "update_all_datasources",
      source: "ui",
    };
    const module = await loadModule();
    module.bindUpdateAllDatasources();
    const startBtn = document.getElementById("update-all-sources-btn") as HTMLButtonElement;
    const stopBtn = document.getElementById("update-all-stop-btn") as HTMLButtonElement;
    expect(startBtn.disabled).toBe(true);
    expect(stopBtn.disabled).toBe(false);

    stopBtn.click();
    expect(stopBtn.disabled).toBe(true);
    stopBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await vi.waitFor(() =>
      expect(fetchJSONMock).toHaveBeenCalledWith("/api/jobs/cancel", { method: "POST" }),
    );
    expect(fetchJSONMock.mock.calls.filter(([url]) => url === "/api/jobs/cancel")).toHaveLength(1);
  });

  it("renders per-source lanes from job progress and a final summary on completion", async () => {
    const module = await loadModule();
    module.bindUpdateAllDatasources();
    const running = {
      action: "update_all_datasources",
      result: {
        update_all: {
          phase: "running",
          current_index: 0,
          total: 7,
          totals: { finished_sources: 1, discovered_count: 120, expired_skipped_count: 25, persisted_count: 10 },
          sources: [
            { source_id: "DS-1", name: "哔哩抽奖小助理", status: "running", phase: "importing", message: "正在导入", updated: false, new_link_count: 0, persisted_count: 0 },
            { source_id: "DS-2", name: "番茄薯条喵", status: "waiting", phase: "waiting", message: "等待", updated: false, new_link_count: 0, persisted_count: 0 },
          ],
        },
      },
    };
    window.dispatchEvent(new CustomEvent("binggo:job-progress", { detail: running }));
    const lanes = document.getElementById("update-all-lanes")?.innerHTML || "";
    expect(lanes).toContain("DS-1");
    expect(lanes).toContain("哔哩抽奖小助理");
    expect(lanes).toContain("DS-2");
    expect(document.getElementById("update-all-summary")?.textContent).toContain("累计发现 120");

    const done = {
      action: "update_all_datasources",
      result: {
        update_all: {
          phase: "done",
          total: 7,
          success_count: 7,
          failed_count: 0,
          persisted_count: 12,
          summary: "全部数据源更新完成：成功 7 个，失败 0 个",
          sources: [
            {
              source_id: "DS-1",
              name: "哔哩抽奖小助理",
              status: "success",
              phase: "success",
              message: "完成",
              updated: true,
              discovered_count: 120,
              existing_count: 80,
              duplicate_link_count: 2,
              invalid_link_count: 1,
              candidate_count: 37,
              non_lottery_count: 4,
              other_skipped_count: 1,
              processing_failed_count: 2,
              expired_skipped_count: 18,
              persisted_count: 12,
            },
            {
              source_id: "DS-2",
              name: "番茄薯条喵",
              status: "not_run",
              phase: "not_run",
              message: "未执行",
              discovered_count: 0,
              persisted_count: 0,
            },
          ],
        },
      },
    };
    window.dispatchEvent(new CustomEvent("binggo:job-completed", { detail: done }));
    expect(document.getElementById("update-all-summary")?.textContent).toContain("全部数据源更新完成");
    const doneLanes = document.getElementById("update-all-lanes")?.innerHTML || "";
    expect(doneLanes).toContain("发现 120");
    expect(doneLanes).toContain("已有 80");
    expect(doneLanes).toContain("重复/无效 3");
    expect(doneLanes).toContain("新候选 37");
    expect(doneLanes).toContain("过期 18");
    expect(doneLanes).toContain("非抽奖/其他 5");
    expect(doneLanes).toContain("失败 2");
    expect(doneLanes).toContain("新增 12");
    const notRun = document.querySelector('[data-status="not_run"]')?.textContent || "";
    expect(notRun).toContain("未执行");
    expect(notRun).not.toContain("发现 0");
  });
});
