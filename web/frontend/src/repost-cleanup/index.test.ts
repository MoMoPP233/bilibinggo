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
  restoreAccountViewsFromSnapshotMock,
  loadAccountMock,
} = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(),
  notifyJobStartErrorMock: vi.fn(),
  openAppConfirmMock: vi.fn(),
  requireSetupMock: vi.fn(),
  showToastMock: vi.fn(),
  startJobMock: vi.fn(),
  trackCurrentJobMock: vi.fn(),
  restoreAccountViewsFromSnapshotMock: vi.fn(),
  loadAccountMock: vi.fn(),
}));

vi.mock("../account/index", () => ({
  requireSetup: requireSetupMock,
  restoreAccountViewsFromSnapshot: restoreAccountViewsFromSnapshotMock,
  loadAccount: loadAccountMock,
}));
vi.mock("../api/client", () => ({ fetchJSON: fetchJSONMock }));
vi.mock("../jobs/index", () => ({
  notifyJobStartError: notifyJobStartErrorMock,
  startJob: startJobMock,
  trackCurrentJob: trackCurrentJobMock,
  updateJobUI: () => {},
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
    <button id="repost-sync-btn" data-action="sync_repost_history">同步历史</button>
    <button id="repost-scan-btn" data-action="scan_expired_reposts">扫描</button>
    <button id="repost-stop-btn" data-job-control disabled>停止评估</button>
    <p id="repost-eval-status">尚未开始智能评估</p>
    <span id="repost-history-total">—</span>
    <span id="repost-deleted-total">0</span>
    <span id="repost-workflow-status">尚未读取</span>
    <span id="repost-safe-total">0</span>
    <span id="repost-manual-total">0</span>
    <span id="repost-deferred-total">0</span>
    <span id="repost-blocked-total">0</span>
    <span id="repost-pending-total">0</span>
    <span id="repost-checkpoint-status">尚未读取</span>
    <p id="repost-candidate-summary"></p>
    <input type="search" id="repost-search-input" />
    <select id="repost-sort-select">
      <option value="reposted_desc">最新优先</option>
      <option value="reposted_asc">最早优先</option>
      <option value="lottery_desc">最近开奖</option>
      <option value="lottery_asc">最早开奖</option>
      <option value="priority">清理优先级</option>
      <option value="evaluated_desc">最近评估</option>
    </select>
    <p id="repost-delete-progress"></p>
    <span id="repost-selected-count"></span>
    <button id="repost-select-page">全选当前页</button>
    <button id="repost-clear-selection">取消选择</button>
    <button id="repost-delete-selected" data-job-control disabled>删除</button>
    <div class="repost-filter-bar">
      <button type="button" class="repost-filter-btn is-active" data-repost-filter="all">全部</button>
      <button type="button" class="repost-filter-btn" data-repost-filter="safe">安全可删</button>
      <button type="button" class="repost-filter-btn" data-repost-filter="manual_review">人工确认</button>
      <button type="button" class="repost-filter-btn" data-repost-filter="deferred">暂缓处理</button>
      <button type="button" class="repost-filter-btn" data-repost-filter="blocked">不可判断</button>
      <button type="button" class="repost-filter-btn" data-repost-filter="deleted">已删除</button>
    </div>
    <table><tbody id="repost-candidates-body"></tbody></table>
    <div id="repost-candidate-pagination"></div>
    <p id="repost-history-summary"></p>
    <table><tbody id="repost-history-body"></tbody></table>
    <div id="repost-history-pagination"></div>
    <span id="repost-health-pill">—</span>
    <strong id="repost-health-all">—</strong>
    <strong id="repost-health-existing">—</strong>
    <strong id="repost-health-deleted">—</strong>
    <strong id="repost-health-pending">—</strong>
    <strong id="repost-health-failed">—</strong>
    <strong id="repost-health-unknown">—</strong>
    <div id="repost-health-lines">读取中…</div>
    <div id="repost-health-issues"></div>`;
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
    restoreAccountViewsFromSnapshotMock.mockReset();
    loadAccountMock.mockReset();
    requireSetupMock.mockReturnValue(true);
    openAppConfirmMock.mockResolvedValue(true);
    fetchJSONMock.mockResolvedValue({ ok: true });
    trackCurrentJobMock.mockResolvedValue({ state: "running" });
    restoreAccountViewsFromSnapshotMock.mockReturnValue(true);
    loadAccountMock.mockResolvedValue({ logged_in: true, expired: false });
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

  it("reports guards resolved from local history after a successful sync", async () => {
    const module = await loadModule();

    await module.handleRepostCleanupJobCompletion({
      action: "sync_repost_history",
      state: "success",
      result: {
        imported_count: 12,
        guard_reconciliation: { checked: 300, resolved: 287, remaining: 13 },
      },
    });

    expect(showToastMock).toHaveBeenCalledWith(
      "转发历史同步完成",
      "success",
      "本次导入 12 条，已根据本地转发历史自动确认 287 条历史参与记录",
    );
  });

  it("does not retry when the delete-job request result is unknown", async () => {
    fetchJSONMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const module = await loadModule();
    module.bindRepostCleanup();
    await selectOnlyCandidate(module);

    await module.deleteSelectedReposts();

    expect(fetchJSONMock).toHaveBeenCalledOnce();
    expect(document.getElementById("repost-candidates-body")?.textContent).toContain("结果未知");
    expect(document.querySelector<HTMLInputElement>("[data-repost-select='90001']")).toBeNull();
    expect(document.querySelector(".repost-blocked-mark")).not.toBeNull();
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

    expect(fetchJSONMock).toHaveBeenCalledWith("/api/repost-cleanup/candidates?show_deleted=1");
    expect(document.getElementById("repost-safe-total")?.textContent).toBe("1");
    expect(document.querySelector<HTMLInputElement>("[data-repost-select='90001']")).not.toBeNull();
  });

  it("restores candidates when the cleanup section is re-activated", async () => {
    fetchJSONMock.mockImplementation((url: string) => {
      if (url.includes("/api/repost-cleanup/candidates")) {
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
    const freshState = await import("../state");
    (freshState.state as unknown as { currentJob: unknown }).currentJob = {
      id: 55,
      state: "running",
      action: "scan_expired_reposts",
      source: "ui",
      message: "评估中",
    };
    const module = await loadModule();
    module.bindRepostCleanup();
    const stopBtn = document.getElementById("repost-stop-btn") as HTMLButtonElement;
    expect(stopBtn.disabled).toBe(false);
    stopBtn.click();
    await vi.waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledWith("/api/jobs/cancel", { method: "POST" });
    });
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

    expect(fetchJSONMock).toHaveBeenCalledWith("/api/repost-cleanup/candidates?show_deleted=1");
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

  it("searches locally by author, original id, repost id and summary", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    module.setCandidates([
      { ...candidate, repost_dynamic_id: "90001", original_dynamic_id: "80001", original_author_name: "张三", summary: "转发抽奖" },
      { ...candidate, repost_dynamic_id: "90002", original_dynamic_id: "80002", original_author_name: "李四", summary: "预约抽奖" },
    ]);
    const input = document.getElementById("repost-search-input") as HTMLInputElement;
    input.value = "张三";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    expect(document.querySelectorAll("[data-repost-row]")).toHaveLength(1);
    expect(document.querySelector("[data-repost-row='90001']")).not.toBeNull();
    expect(document.querySelector("[data-repost-row='90002']")).toBeNull();
  });

  it("sorts by reposted time ascending", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    module.setCandidates([
      { ...candidate, repost_dynamic_id: "90001", reposted_at: 200 },
      { ...candidate, repost_dynamic_id: "90002", reposted_at: 100 },
    ]);
    const select = document.getElementById("repost-sort-select") as HTMLSelectElement;
    select.value = "reposted_asc";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    const rows = [...document.querySelectorAll("[data-repost-row]")];
    expect(rows[0]?.getAttribute("data-repost-row")).toBe("90002");
    expect(rows[1]?.getAttribute("data-repost-row")).toBe("90001");
  });

  it("select filtered results caps at 20", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    const many = Array.from({ length: 25 }, (_, index) => ({
      ...candidate,
      repost_dynamic_id: `900${String(index + 1).padStart(2, "0")}`,
    }));
    module.setCandidates(many);
    document.getElementById("repost-select-page")!.click();
    expect(document.getElementById("repost-selected-count")?.textContent).toBe("已选择 20 条");
    expect(showToastMock).toHaveBeenCalledWith("单次最多处理 20 条", "info", "已选择前 20 条。");
  });

  it("deleted filter shows tombstones after local load", async () => {
    fetchJSONMock.mockResolvedValue({
      ok: true,
      deleted: 1,
      deleted_candidates: [{ repost_dynamic_id: "90099", original_dynamic_id: "80099", deleted_at: 1_750_000_000 }],
      candidates: [],
    });
    const module = await loadModule();
    module.bindRepostCleanup();
    await module.loadPersistedCandidates();
    document.querySelector<HTMLButtonElement>("[data-repost-filter='deleted']")!.click();
    expect(document.querySelector("[data-repost-row='90099']")).not.toBeNull();
    expect(document.querySelector("[data-repost-select='90099']")).toBeNull();
  });

  it("retryable blocked shows re-evaluate, permanent blocked does not", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    module.setCandidates([
      { ...candidate, repost_dynamic_id: "90001", level: "blocked", assessment_status: "retryable_unknown", reason: "临时失败" },
      { ...candidate, repost_dynamic_id: "90002", level: "blocked", assessment_status: "final", reason: "永久冲突" },
    ]);
    expect(document.querySelector("[data-repost-reeval='80001']")).not.toBeNull();
    expect(document.querySelector("[data-repost-reeval='80002']")).toBeNull();
  });

  it("copy button uses browser clipboard without remote", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const module = await loadModule();
    module.bindRepostCleanup();
    module.setCandidates([candidate]);
    document.querySelector<HTMLButtonElement>("[data-repost-copy='80001']")!.click();
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith("80001");
    expect(fetchJSONMock).not.toHaveBeenCalled();
  });

  it("sync start locks buttons instantly and an ultra-fast second click is ignored", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    const syncBtn = document.getElementById("repost-sync-btn") as HTMLButtonElement;
    const scanBtn = document.getElementById("repost-scan-btn") as HTMLButtonElement;
    expect(syncBtn.disabled).toBe(false);

    syncBtn.click();
    expect(syncBtn.disabled).toBe(true);
    expect(scanBtn.disabled).toBe(true);

    syncBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await vi.waitFor(() => expect(startJobMock).toHaveBeenCalledTimes(1));
    expect(startJobMock).toHaveBeenCalledWith("sync_repost_history", {});
  });

  it("evaluate start locks instantly and ignores ultra-fast double click", async () => {
    const module = await loadModule();
    module.bindRepostCleanup();
    const scanBtn = document.getElementById("repost-scan-btn") as HTMLButtonElement;
    const syncBtn = document.getElementById("repost-sync-btn") as HTMLButtonElement;

    scanBtn.click();
    expect(scanBtn.disabled).toBe(true);
    expect(syncBtn.disabled).toBe(true);
    expect(document.getElementById("repost-eval-status")?.textContent).toBe("智能评估进行中…");

    scanBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await vi.waitFor(() => expect(startJobMock).toHaveBeenCalledTimes(1));
    expect(startJobMock).toHaveBeenCalledWith("scan_expired_reposts", {});
  });

  it("stop request sends exactly one cancel even under rapid double click", async () => {
    const freshState = await import("../state");
    (freshState.state as unknown as { currentJob: unknown }).currentJob = {
      id: 66,
      state: "running",
      action: "scan_expired_reposts",
      source: "ui",
    };
    const module = await loadModule();
    module.bindRepostCleanup();
    const stopBtn = document.getElementById("repost-stop-btn") as HTMLButtonElement;

    stopBtn.click();
    expect(stopBtn.disabled).toBe(true);
    stopBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await vi.waitFor(() =>
      expect(fetchJSONMock).toHaveBeenCalledWith("/api/jobs/cancel", { method: "POST" }),
    );
    expect(fetchJSONMock.mock.calls.filter(([url]) => url === "/api/jobs/cancel")).toHaveLength(1);
  });

  it("recovers buttons when the job start API fails", async () => {
    startJobMock.mockRejectedValueOnce(new Error("网络异常"));
    const module = await loadModule();
    module.bindRepostCleanup();
    const syncBtn = document.getElementById("repost-sync-btn") as HTMLButtonElement;
    const scanBtn = document.getElementById("repost-scan-btn") as HTMLButtonElement;

    syncBtn.click();
    await vi.waitFor(() => expect(notifyJobStartErrorMock).toHaveBeenCalled());

    expect(syncBtn.disabled).toBe(false);
    expect(scanBtn.disabled).toBe(false);
    expect(startJobMock).toHaveBeenCalledTimes(1);
  });

  it("keeps buttons locked when the job transitions to a real running state", async () => {
    const freshState = await import("../state");
    startJobMock.mockImplementation(async () => {
      (freshState.state as unknown as { currentJob: unknown }).currentJob = {
        id: 77,
        state: "running",
        action: "sync_repost_history",
        source: "ui",
      };
    });
    const module = await loadModule();
    module.bindRepostCleanup();
    const syncBtn = document.getElementById("repost-sync-btn") as HTMLButtonElement;
    const scanBtn = document.getElementById("repost-scan-btn") as HTMLButtonElement;
    const stopBtn = document.getElementById("repost-stop-btn") as HTMLButtonElement;

    syncBtn.click();
    await vi.waitFor(() => expect(startJobMock).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(syncBtn.disabled).toBe(true));
    expect(scanBtn.disabled).toBe(true);
    expect(stopBtn.disabled).toBe(false);
  });

  it("delete completion restores account UI from local snapshot without an extra account request", async () => {
    fetchJSONMock.mockResolvedValue({ ok: true, candidates: [] });
    const module = await loadModule();
    module.bindRepostCleanup();

    await module.handleRepostCleanupJobCompletion({
      action: "delete_expired_reposts",
      state: "success",
      result: { items: [], deleted_count: 1 },
    });

    expect(restoreAccountViewsFromSnapshotMock).toHaveBeenCalled();
    expect(fetchJSONMock).toHaveBeenCalledWith("/api/repost-cleanup/candidates?show_deleted=1");
    const accountCalls = fetchJSONMock.mock.calls.filter(([url]) =>
      String(url).startsWith("/api/account"),
    );
    expect(accountCalls).toHaveLength(0);
    expect(loadAccountMock).not.toHaveBeenCalled();
  });
});

describe("repost cleanup maintenance health", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset();
    showToastMock.mockReset();
    setupDom();
  });

  it("normal health renders pill, counts and human lines", async () => {
    const module = await loadModule();
    module.renderHealth({
      status: "normal",
      status_text: "维护状态正常",
      counts: { all_total: 6, history_total: 3, deleted: 1, delete_pending: 1, delete_failed: 1, unknown: 1 },
      maintenance: { enabled: true, interval_hours: 2, risk_paused: false, risk_reason: null },
      issues: [],
      issue_total: 0,
      checks: [{ key: "ok", tone: "ok", text: "没有需要人工检查的删除记录。" }],
    });
    expect(document.getElementById("repost-health-pill")?.textContent).toBe("维护状态正常");
    expect(document.getElementById("repost-health-pill")?.getAttribute("data-tone")).toBe("ok");
    expect(document.getElementById("repost-health-all")?.textContent).toBe("6");
    expect(document.getElementById("repost-health-existing")?.textContent).toBe("3");
    expect(document.getElementById("repost-health-deleted")?.textContent).toBe("1");
    expect(document.getElementById("repost-health-pending")?.textContent).toBe("1");
    expect(document.getElementById("repost-health-failed")?.textContent).toBe("1");
    expect(document.getElementById("repost-health-unknown")?.textContent).toBe("1");
    const lines = document.getElementById("repost-health-lines")?.textContent || "";
    expect(lines).toContain("自动维护：开启");
    expect(lines).toContain("没有需要人工检查");
    expect(document.getElementById("repost-health-issues")?.hasAttribute("hidden")).toBe(true);
  });

  it("maintenance off shows a clear disabled line", async () => {
    const module = await loadModule();
    module.renderHealth({
      status: "normal",
      status_text: "维护状态正常",
      counts: {},
      maintenance: { enabled: false, interval_hours: 2 },
      issues: [],
      issue_total: 0,
      checks: [],
    });
    const lines = document.getElementById("repost-health-lines")?.textContent || "";
    expect(lines).toContain("自动维护：关闭");
  });

  it("issue rows are visible with copy but no auto re-delete controls", async () => {
    const module = await loadModule();
    module.renderHealth({
      status: "review_needed",
      status_text: "需要人工检查",
      counts: { all_total: 3, delete_pending: 1, delete_failed: 1, unknown: 1 },
      maintenance: { enabled: true, interval_hours: 2, risk_paused: false },
      issue_total: 2,
      issues: [
        { repost_dynamic_id: "91001", original_dynamic_id: "81001", delete_status: "delete_pending", last_error: null, delete_requested_at: 1_750_000_000 },
        { repost_dynamic_id: "91002", original_dynamic_id: "81002", delete_status: "delete_failed", last_error: "Bilibili API error -101", delete_requested_at: 1_750_000_001 },
      ],
      checks: [{ key: "pending", tone: "warn", text: "未完成删除操作" }],
    });
    const issues = document.getElementById("repost-health-issues");
    expect(issues?.hasAttribute("hidden")).toBe(false);
    const html = issues?.innerHTML || "";
    expect(html).toContain("91001");
    expect(html).toContain("91002");
    expect(html).toContain("81002");
    expect(html).toContain("Bilibili API error -101");
    expect(html).toContain("未完成删除");
    expect(html).toContain("data-repost-copy=");
    expect(html).not.toContain("data-auto-");
    expect(html).not.toContain('data-repost-delete');
    expect(document.getElementById("repost-health-pill")?.getAttribute("data-tone")).toBe("warn");
  });
});
