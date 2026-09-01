// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const {
  fetchJSONMock,
  notifyJobStartErrorMock,
  openAppConfirmMock,
  requireSetupMock,
  showToastMock,
  startJobMock,
  trackCurrentJobMock,
} = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(),
  notifyJobStartErrorMock: vi.fn(),
  openAppConfirmMock: vi.fn(),
  requireSetupMock: vi.fn(),
  showToastMock: vi.fn(),
  startJobMock: vi.fn(),
  trackCurrentJobMock: vi.fn(),
}));

vi.mock("../account/index", () => ({ requireSetup: requireSetupMock }));
vi.mock("../api/client", () => ({ fetchJSON: fetchJSONMock }));
vi.mock("../jobs/index", () => ({
  notifyJobStartError: notifyJobStartErrorMock,
  startJob: startJobMock,
  trackCurrentJob: trackCurrentJobMock,
}));
vi.mock("../shell/confirm", () => ({ openAppConfirm: openAppConfirmMock }));
vi.mock("../shell/toast", () => ({ showToast: showToastMock }));

const candidate = {
  repost_dynamic_id: "90001",
  original_dynamic_id: "80001",
  reposted_at: 1_750_000_000,
  original_author_name: "抽奖作者",
  lottery_type: "互动抽奖",
  lottery_time: 1_750_100_000,
  level: "safe",
  delete_status: "active",
};

function setupDom(): void {
  document.body.innerHTML = `
    <button id="repost-scan-btn" data-action="scan_expired_reposts">扫描</button>
    <button id="repost-stop-btn" data-job-control disabled>停止评估</button>
    <p id="repost-eval-status">尚未开始智能评估</p>
    <span id="repost-history-total">—</span>
    <span id="repost-safe-total">0</span>
    <span id="repost-manual-total">0</span>
    <span id="repost-deferred-total">0</span>
    <span id="repost-blocked-total">0</span>
    <span id="repost-pending-total">0</span>
    <span id="repost-checkpoint-status">尚未读取</span>
    <p id="repost-candidate-summary"></p>
    <span id="repost-selected-count"></span>
    <button id="repost-select-page">全选当前页</button>
    <button id="repost-clear-selection">取消选择</button>
    <button id="repost-delete-selected" data-job-control disabled>删除</button>
    <div class="repost-filter-bar">
      <button type="button" class="repost-filter-btn is-active" data-repost-filter="all">全部</button>
      <button type="button" class="repost-filter-btn" data-repost-filter="safe">安全可删</button>
      <button type="button" class="repost-filter-btn" data-repost-filter="manual_review">人工确认</button>
      <button type="button" class="repost-filter-btn" data-repost-filter="blocked">不可判断</button>
    </div>
    <table><tbody id="repost-candidates-body"></tbody></table>
    <div id="repost-candidate-pagination"></div>
    <p id="repost-history-summary"></p>
    <table><tbody id="repost-history-body"></tbody></table>
    <div id="repost-history-pagination"></div>`;
}

async function loadModule() {
  return import("./index");
}

async function selectOnlyCandidate(module: Awaited<ReturnType<typeof loadModule>>): Promise<void> {
  module.setCandidates([candidate]);
  const checkbox = document.querySelector<HTMLInputElement>("[data-repost-select='90001']");
  expect(checkbox).not.toBeNull();
  checkbox!.checked = true;
  checkbox!.dispatchEvent(new Event("change", { bubbles: true }));
}

describe("repost cleanup UI", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset();
    notifyJobStartErrorMock.mockReset();
    openAppConfirmMock.mockReset();
    requireSetupMock.mockReset();
    showToastMock.mockReset();
    startJobMock.mockReset();
    trackCurrentJobMock.mockReset();
    requireSetupMock.mockReturnValue(true);
    openAppConfirmMock.mockResolvedValue(true);
    fetchJSONMock.mockResolvedValue({ ok: true });
    trackCurrentJobMock.mockResolvedValue({ state: "running" });
    setupDom();
  });

  it("starts with no selection and keeps delete disabled", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();

    expect(document.getElementById("repost-selected-count")?.textContent).toBe("已选择 0 条");
    expect((document.getElementById("repost-delete-selected") as HTMLButtonElement).disabled).toBe(true);
  });

  it("does not send a delete request when confirmation is cancelled", async () => {
    openAppConfirmMock.mockResolvedValue(false);
    const module = await loadModule();
    module.bindRepostCleanup();
    await selectOnlyCandidate(module);

    await module.deleteSelectedReposts();

    expect(openAppConfirmMock).toHaveBeenCalledOnce();
    expect(fetchJSONMock).not.toHaveBeenCalled();
    expect((document.getElementById("repost-delete-selected") as HTMLButtonElement).disabled).toBe(false);
  });

  it("submits only selected repost IDs after explicit confirmation", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    await selectOnlyCandidate(module);

    await module.deleteSelectedReposts();

    expect(fetchJSONMock).toHaveBeenCalledOnce();
    const [url, options] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/repost-cleanup/delete");
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({
      repost_dynamic_ids: ["90001"],
      confirmed: true,
      manual_review_confirmed: false,
    });
    expect(options.body).not.toContain("80001");
    expect(trackCurrentJobMock).toHaveBeenCalledOnce();
  });

  it("blocks duplicate submission while the confirmation dialog is open", async () => {
    let resolveConfirm: ((confirmed: boolean) => void) | undefined;
    openAppConfirmMock.mockImplementation(() => new Promise<boolean>((resolve) => {
      resolveConfirm = resolve;
    }));
    const module = await loadModule();
    module.bindRepostCleanup();
    await selectOnlyCandidate(module);

    const first = module.deleteSelectedReposts();
    const second = module.deleteSelectedReposts();
    expect(openAppConfirmMock).toHaveBeenCalledOnce();
    resolveConfirm?.(false);
    await Promise.all([first, second]);
    expect(fetchJSONMock).not.toHaveBeenCalled();
  });

  it("loads successful scan candidates with duplicate IDs removed and none selected", async () => {
    fetchJSONMock.mockResolvedValue({
      ok: true,
      candidates: [candidate, { ...candidate, original_author_name: "重复项" }],
    });
    const module = await loadModule();
    module.bindRepostCleanup();

    await module.handleRepostCleanupJobCompletion({
      action: "scan_expired_reposts",
      state: "success",
      result: {},
    });

    expect(document.getElementById("repost-safe-total")?.textContent).toBe("1");
    expect((document.getElementById("repost-delete-selected") as HTMLButtonElement).disabled).toBe(true);
    expect(document.querySelectorAll("[data-repost-select]")).toHaveLength(1);
  });

  it("does not retry when the delete-job request result is unknown", async () => {
    fetchJSONMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const module = await loadModule();
    module.bindRepostCleanup();
    await selectOnlyCandidate(module);

    await module.deleteSelectedReposts();

    expect(fetchJSONMock).toHaveBeenCalledOnce();
    expect(document.getElementById("repost-candidates-body")?.textContent).toContain("结果未知");
    expect(document.querySelector<HTMLInputElement>("[data-repost-select='90001']")?.disabled).toBe(true);
  });

  it("restores the delete control from selection state after any terminal job", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    await selectOnlyCandidate(module);
    const deleteButton = document.getElementById("repost-delete-selected") as HTMLButtonElement;
    deleteButton.disabled = true;

    await module.handleRepostCleanupJobCompletion({ action: "refresh_all", state: "success" });

    expect(deleteButton.disabled).toBe(false);
  });

  it("loads persisted candidates without re-scanning", async () => {
    fetchJSONMock.mockResolvedValue({ ok: true, candidates: [candidate] });
    const module = await loadModule();
    module.bindRepostCleanup();

    await module.loadPersistedCandidates();

    expect(fetchJSONMock).toHaveBeenCalledWith("/api/repost-cleanup/candidates");
    expect(document.getElementById("repost-safe-total")?.textContent).toBe("1");
    expect(document.querySelector<HTMLInputElement>("[data-repost-select='90001']")).not.toBeNull();
  });

  it("restores candidates when the cleanup section is re-activated", async () => {
    fetchJSONMock.mockImplementation((url: string) => {
      if (url === "/api/repost-cleanup/candidates") {
        return Promise.resolve({ ok: true, candidates: [candidate] });
      }
      return Promise.resolve({ ok: true });
    });
    const module = await loadModule();
    module.bindRepostCleanup();

    window.dispatchEvent(
      new CustomEvent("binggo:section-activated", { detail: { sectionId: "repost-cleanup" } }),
    );

    await vi.waitFor(() => {
      expect(document.querySelector("[data-repost-select='90001']")).not.toBeNull();
    });
    expect(document.getElementById("repost-safe-total")?.textContent).toBe("1");
  });

  it("renders three levels and blocks have no delete checkbox", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    module.setCandidates([
      { ...candidate, repost_dynamic_id: "90001", level: "safe" },
      {
        repost_dynamic_id: "90002",
        original_dynamic_id: "80002",
        reposted_at: 1_750_000_001,
        level: "manual_review",
        delete_status: "active",
      },
      {
        repost_dynamic_id: "90003",
        original_dynamic_id: "80003",
        reposted_at: 1_750_000_002,
        level: "blocked",
        reason: "身份关系未验证",
        delete_status: "active",
      },
    ]);

    expect(document.getElementById("repost-safe-total")?.textContent).toBe("1");
    expect(document.getElementById("repost-manual-total")?.textContent).toBe("1");
    expect(document.getElementById("repost-blocked-total")?.textContent).toBe("1");
    expect(document.querySelectorAll("[data-repost-select]")).toHaveLength(2);
    expect(document.querySelector(".repost-blocked-mark")).not.toBeNull();
  });

  it("filters candidates by level", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    module.setCandidates([
      { ...candidate, repost_dynamic_id: "90001", level: "safe" },
      {
        repost_dynamic_id: "90002",
        original_dynamic_id: "80002",
        reposted_at: 1_750_000_001,
        level: "manual_review",
        delete_status: "active",
      },
    ]);

    document.querySelector<HTMLButtonElement>("[data-repost-filter='manual_review']")!.click();

    expect(document.querySelectorAll("[data-repost-select]")).toHaveLength(1);
    expect(document.querySelector<HTMLInputElement>("[data-repost-select='90002']")).not.toBeNull();
    expect(document.querySelector<HTMLInputElement>("[data-repost-select='90001']")).toBeNull();
  });

  it("manual_review delete requires an extra confirmation", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    module.setCandidates([
      {
        repost_dynamic_id: "90002",
        original_dynamic_id: "80002",
        reposted_at: 1_750_000_001,
        level: "manual_review",
        delete_status: "active",
      },
    ]);
    const checkbox = document.querySelector<HTMLInputElement>("[data-repost-select='90002']");
    expect(checkbox).not.toBeNull();
    checkbox!.checked = true;
    checkbox!.dispatchEvent(new Event("change", { bubbles: true }));

    await module.deleteSelectedReposts();

    expect(openAppConfirmMock).toHaveBeenCalledTimes(2);
    const [, options] = fetchJSONMock.mock.calls[0];
    expect(JSON.parse(options.body)).toEqual({
      repost_dynamic_ids: ["90002"],
      confirmed: true,
      manual_review_confirmed: true,
    });
  });

  it("stop button cancels the running assessment job", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    (document.getElementById("repost-stop-btn") as HTMLButtonElement).disabled = false;

    document.getElementById("repost-stop-btn")!.click();

    expect(fetchJSONMock).toHaveBeenCalledWith("/api/jobs/cancel", { method: "POST" });
  });

  it("shows cumulative and pending counts from persisted summary", async () => {
    fetchJSONMock.mockResolvedValue({
      ok: true,
      history_total: 10,
      assessed_total: 7,
      pending_evaluation: 3,
      candidates: [candidate],
    });
    const module = await loadModule();
    module.bindRepostCleanup();

    await module.loadPersistedCandidates();

    expect(document.getElementById("repost-eval-status")?.textContent).toBe(
      "累计已评估 7 条 · 仍待评估 3 条",
    );
    expect(document.getElementById("repost-pending-total")?.textContent).toBe("3");
  });

  it("cancelled scan job reloads persisted results locally", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    fetchJSONMock.mockResolvedValue({ ok: true, candidates: [candidate] });

    await module.handleRepostCleanupJobCompletion({
      action: "scan_expired_reposts",
      state: "cancelled",
    });

    expect(fetchJSONMock).toHaveBeenCalledWith("/api/repost-cleanup/candidates");
    expect(document.getElementById("repost-safe-total")?.textContent).toBe("1");
  });

  it("defer sends only the repost id to the local defer endpoint", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    fetchJSONMock.mockResolvedValue({ ok: true, candidates: [] });

    await module.deferCandidate("90001");

    const [url, options] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/repost-cleanup/defer");
    expect(JSON.parse(options.body)).toEqual({ repost_dynamic_ids: ["90001"] });
  });

  it("restore sends only the repost id to the local restore endpoint", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    fetchJSONMock.mockResolvedValue({ ok: true, candidates: [] });

    await module.restoreCandidate("90001");

    const [url, options] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/repost-cleanup/restore");
    expect(JSON.parse(options.body)).toEqual({ repost_dynamic_ids: ["90001"] });
  });
});
