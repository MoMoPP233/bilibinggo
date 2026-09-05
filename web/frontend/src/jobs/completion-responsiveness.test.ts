// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchJSON: vi.fn(),
  loadSummary: vi.fn(),
  loadActivities: vi.fn(),
  renderTripleParticipateBar: vi.fn(),
  buildActivityFilterJobParams: vi.fn(() => ({})),
  syncProjectState: vi.fn(),
  syncProfilesAfterLoginJob: vi.fn(async () => false),
  loadWatchUsers: vi.fn(),
  startRealtime: vi.fn(),
  showToast: vi.fn(),
  dismissRunningToasts: vi.fn(),
  switchSection: vi.fn(),
  confirmRefreshAll: vi.fn(async () => true),
  isLlmConfigured: vi.fn(() => true),
  requireSetup: vi.fn(() => true),
  scrollToLlmSettings: vi.fn(),
  clearActionButtonLoading: vi.fn(),
  flashActivityRows: vi.fn(),
  flashSourceRow: vi.fn(),
  pulseWatchSyncCard: vi.fn(),
  setButtonLoading: vi.fn(),
  setSourceRowUpdating: vi.fn(),
  prefersReducedMotion: vi.fn(() => false),
}));

vi.mock("../api/client", () => ({ fetchJSON: mocks.fetchJSON }));
vi.mock("../account/index", () => ({
  isLlmConfigured: mocks.isLlmConfigured,
  requireSetup: mocks.requireSetup,
  scrollToLlmSettings: mocks.scrollToLlmSettings,
  syncProjectState: mocks.syncProjectState,
}));
vi.mock("../activities/index", () => ({
  buildActivityFilterJobParams: mocks.buildActivityFilterJobParams,
  loadActivities: mocks.loadActivities,
  loadSummary: mocks.loadSummary,
  renderTripleParticipateBar: mocks.renderTripleParticipateBar,
}));
vi.mock("../profiles/index", () => ({
  syncProfilesAfterLoginJob: mocks.syncProfilesAfterLoginJob,
}));
vi.mock("../shell/toast", () => ({
  dismissRunningToasts: mocks.dismissRunningToasts,
  showToast: mocks.showToast,
}));
vi.mock("../shell/nav", () => ({ switchSection: mocks.switchSection }));
vi.mock("../shell/confirm", () => ({ confirmRefreshAll: mocks.confirmRefreshAll }));
vi.mock("../realtime/sse", () => ({ startRealtime: mocks.startRealtime }));
vi.mock("../watch/index", () => ({ loadWatchUsers: mocks.loadWatchUsers }));
vi.mock("../utils/motion", () => ({
  clearActionButtonLoading: mocks.clearActionButtonLoading,
  flashActivityRows: mocks.flashActivityRows,
  flashSourceRow: mocks.flashSourceRow,
  pulseWatchSyncCard: mocks.pulseWatchSyncCard,
  setButtonLoading: mocks.setButtonLoading,
  setSourceRowUpdating: mocks.setSourceRowUpdating,
  prefersReducedMotion: mocks.prefersReducedMotion,
}));

function setupDom(): void {
  document.body.innerHTML = `
    <div class="log-dock is-idle" id="log-dock" data-tone="idle">
      <button type="button" class="log-dock-toggle" id="log-dock-toggle" aria-expanded="false" aria-label="打开任务日志">
        <span class="log-dock-icon" aria-hidden="true"></span>
        <span class="log-dock-label">任务日志</span>
        <span class="log-dock-badge" id="log-dock-badge" hidden>运行中</span>
      </button>
      <div class="log-dock-panel" id="log-dock-panel" aria-hidden="true">
        <div class="log-dock-head">
          <p class="log-dock-title">任务日志</p>
          <button type="button" class="log-dock-chevron-button" id="log-dock-panel-toggle" aria-expanded="false" aria-label="展开任务日志">
            <span class="log-dock-chevron" aria-hidden="true"></span>
          </button>
          <span class="log-dock-status is-idle" id="log-dock-status">空闲</span>
          <p id="job-message">暂无任务</p>
        </div>
        <div id="log-dock-body"><pre id="job-log"></pre><span id="log-dock-pin-hint" hidden></span></div>
      </div>
    </div>
    <div id="progress-banner" hidden data-percent="0"></div>
    <div id="progress-fill"></div>
    <div id="progress-fill-glow"></div>
    <div id="progress-label"></div>
    <div id="progress-detail"></div>
    <div id="progress-percent"></div>
    <span class="progress-percent-suffix">%</span>
    <svg><circle id="progress-ring"></circle></svg>
    <div id="progress-track"></div>
    <div id="progress-chip"></div>
    <div id="progress-steps"></div>
    <div id="job-result-banner" class="job-result-banner" hidden>
      <span id="job-result-icon"></span>
      <span id="job-result-eyebrow"></span>
      <strong id="job-result-title"></strong>
      <p id="job-result-summary"></p>
      <p id="job-result-hint" hidden></p>
      <div id="job-result-actions" hidden></div>
      <div id="job-result-body"></div>
      <div id="job-result-progress"></div>
      <button id="job-result-close" type="button"></button>
    </div>
    <div id="toast-stack"></div>
    <button type="button" class="btn btn-primary" data-action="participate">参与</button>
    <button type="button" data-action="participate_triple" id="triple-participate-btn">三连参与</button>
    <button type="button" data-job-control="1">独立任务控件</button>`;
}

type JobShape = {
  id?: number | null;
  action?: string;
  state?: string;
  message?: string;
  log?: string;
  finished_at?: number | null;
  result?: Record<string, unknown>;
};

function job(overrides: JobShape): Record<string, unknown> {
  return {
    id: 1,
    action: "refresh_all",
    state: "success",
    message: "同步完成",
    log: "第一行\n第二行",
    result: {},
    ...overrides,
  };
}

function deferred() {
  let resolve: (value?: unknown) => void = () => {};
  let reject: (reason?: unknown) => void = () => {};
  const promise = new Promise((r, j) => {
    resolve = r;
    reject = j;
  });
  return { promise, resolve, reject };
}

function flush(): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, 0));
}

async function loadJobsModule() {
  return import("./index");
}

describe("job completion responsiveness", () => {
  beforeEach(() => {
    vi.resetModules();
    setupDom();
    Object.values(mocks).forEach((fn) => fn.mockReset());
    mocks.requireSetup.mockReturnValue(true);
    mocks.isLlmConfigured.mockReturnValue(true);
    mocks.syncProfilesAfterLoginJob.mockResolvedValue(false);
    mocks.prefersReducedMotion.mockReturnValue(false);
  });

  it("shows success toast and restores buttons before background refreshes finish", async () => {
    const summaryWait = deferred();
    const accountWait = deferred();
    const activitiesWait = deferred();
    mocks.loadSummary.mockReturnValue(summaryWait.promise);
    mocks.syncProjectState.mockReturnValue(accountWait.promise);
    mocks.loadActivities.mockReturnValue(activitiesWait.promise);

    const jobs = await loadJobsModule();
    await jobs.finishJobOnce(job({ action: "refresh_all", state: "success" }));

    expect(mocks.showToast).toHaveBeenCalledWith(
      "同步完成",
      "success",
      expect.anything(),
    );
    expect(mocks.clearActionButtonLoading).toHaveBeenCalledTimes(1);
    expect(mocks.loadSummary).toHaveBeenCalledTimes(1);
    expect(mocks.syncProjectState).toHaveBeenCalledTimes(1);
    expect(mocks.loadActivities).toHaveBeenCalledTimes(1);

    summaryWait.resolve();
    accountWait.resolve();
    activitiesWait.resolve();
    await flush();
  });

  it("shows failure immediately for a failed job without waiting background refresh", async () => {
    const accountWait = deferred();
    mocks.syncProjectState.mockReturnValue(accountWait.promise);
    mocks.loadSummary.mockResolvedValue({ job: { state: "error" } });
    mocks.loadActivities.mockResolvedValue({ items: [], page: 1, pages: 1, total: 0 });

    const jobs = await loadJobsModule();
    await jobs.finishJobOnce(
      job({ id: 2, action: "refresh_source", state: "error", message: "网络异常" }),
    );

    const errorCalls = mocks.showToast.mock.calls.filter((call) => call[1] === "error");
    expect(errorCalls.length).toBeGreaterThan(0);
    expect(String(errorCalls[0][0])).toContain("网络异常");
    expect(mocks.clearActionButtonLoading).toHaveBeenCalledTimes(1);

    accountWait.resolve();
    await flush();
  });

  it("shows participation failure result immediately while background refresh still pending", async () => {
    const accountWait = deferred();
    mocks.syncProjectState.mockReturnValue(accountWait.promise);
    mocks.loadSummary.mockResolvedValue({ job: { state: "error" } });
    mocks.loadActivities.mockResolvedValue({ items: [], page: 1, pages: 1, total: 0 });

    const jobs = await loadJobsModule();
    await jobs.finishJobOnce(
      job({ id: 3, action: "participate", state: "error", message: "网络异常" }),
    );

    const banner = document.getElementById("job-result-banner");
    expect(banner?.hidden).toBe(false);
    expect(document.getElementById("job-result-title")?.textContent).not.toBe("");
    expect(mocks.loadActivities).toHaveBeenCalledTimes(1);

    accountWait.resolve();
    await flush();
    jobs.hideParticipationResult(true);
  });

  it("slow background account refresh does not block success UI", async () => {
    const accountWait = deferred();
    mocks.syncProjectState.mockReturnValue(accountWait.promise);
    mocks.loadSummary.mockResolvedValue({ job: { state: "success" } });
    mocks.loadActivities.mockResolvedValue({ items: [], page: 1, pages: 1, total: 0 });

    const jobs = await loadJobsModule();
    await jobs.finishJobOnce(job({ action: "refresh_source", state: "success" }));
    expect(mocks.showToast).toHaveBeenCalledWith(
      "同步完成",
      "success",
      expect.anything(),
    );

    accountWait.resolve();
    await flush();
  });

  it("slow background activities refresh does not block terminal feedback", async () => {
    const activitiesWait = deferred();
    mocks.loadActivities.mockReturnValue(activitiesWait.promise);
    mocks.loadSummary.mockResolvedValue({ job: { state: "success" } });
    mocks.syncProjectState.mockResolvedValue({});

    const jobs = await loadJobsModule();
    await jobs.finishJobOnce(job({ action: "refresh_all", state: "success" }));
    expect(mocks.clearActionButtonLoading).toHaveBeenCalledTimes(1);
    expect(mocks.loadActivities).toHaveBeenCalledTimes(1);

    activitiesWait.resolve();
    await flush();
  });

  it("background account failure does not stop other background refreshes", async () => {
    mocks.syncProjectState.mockRejectedValue(new Error("account down"));
    mocks.loadSummary.mockResolvedValue({ job: { state: "success" } });
    mocks.loadActivities.mockResolvedValue({ items: [], page: 1, pages: 1, total: 0 });

    const jobs = await loadJobsModule();
    await expect(
      jobs.finishJobOnce(job({ action: "refresh_all", state: "success" })),
    ).resolves.toBeUndefined();
    expect(mocks.loadSummary).toHaveBeenCalledTimes(1);
    expect(mocks.loadActivities).toHaveBeenCalledTimes(1);
    await flush();
  });

  it("background activities failure does not stop profiles/summary refreshes", async () => {
    mocks.loadActivities.mockRejectedValue(new Error("activities down"));
    mocks.loadSummary.mockResolvedValue({ job: { state: "success" } });
    mocks.syncProjectState.mockResolvedValue({});

    const jobs = await loadJobsModule();
    await expect(
      jobs.finishJobOnce(job({ action: "refresh_all", state: "success" })),
    ).resolves.toBeUndefined();
    expect(mocks.loadSummary).toHaveBeenCalledTimes(1);
    expect(mocks.syncProfilesAfterLoginJob).toHaveBeenCalledTimes(1);
    await flush();
  });

  it("background refresh failure never turns a successful job into failure", async () => {
    mocks.loadSummary.mockRejectedValue(new Error("summary down"));
    mocks.syncProjectState.mockRejectedValue(new Error("account down"));
    mocks.loadActivities.mockRejectedValue(new Error("activities down"));
    mocks.syncProfilesAfterLoginJob.mockRejectedValue(new Error("profiles down"));

    const jobs = await loadJobsModule();
    await jobs.finishJobOnce(job({ action: "refresh_all", state: "success" }));
    const errorToasts = mocks.showToast.mock.calls.filter((call) => call[1] === "error");
    expect(errorToasts.length).toBe(0);
    const successToasts = mocks.showToast.mock.calls.filter((call) => call[1] === "success");
    expect(successToasts.length).toBe(1);
    await flush();
  });

  it("starts all independent background refreshes concurrently", async () => {
    const gate = deferred();
    mocks.loadSummary.mockReturnValue(gate.promise);
    mocks.syncProjectState.mockReturnValue(gate.promise);
    mocks.loadActivities.mockReturnValue(gate.promise);

    const jobs = await loadJobsModule();
    await jobs.finishJobOnce(job({ action: "refresh_all", state: "success" }));

    expect(mocks.loadSummary).toHaveBeenCalledTimes(1);
    expect(mocks.syncProjectState).toHaveBeenCalledTimes(1);
    expect(mocks.syncProfilesAfterLoginJob).toHaveBeenCalledTimes(1);
    expect(mocks.loadActivities).toHaveBeenCalledTimes(1);

    gate.resolve();
    await flush();
  });

  it("keeps dependent order for refresh_watch: pulse only after users load", async () => {
    const usersWait = deferred();
    mocks.loadWatchUsers.mockReturnValue(usersWait.promise);
    mocks.loadSummary.mockResolvedValue({ job: { state: "success" } });
    mocks.syncProjectState.mockResolvedValue({});
    mocks.loadActivities.mockResolvedValue({ items: [], page: 1, pages: 1, total: 0 });

    const jobs = await loadJobsModule();
    await jobs.finishJobOnce(job({ action: "refresh_watch", state: "success" }));
    expect(mocks.pulseWatchSyncCard).not.toHaveBeenCalled();

    usersWait.resolve();
    await flush();
    expect(mocks.pulseWatchSyncCard).toHaveBeenCalledTimes(1);
    await flush();
  });

  it("does not run completion refresh twice when SSE terminal and polling both arrive", async () => {
    mocks.loadSummary.mockResolvedValue({ job: { state: "success" } });
    mocks.syncProjectState.mockResolvedValue({});
    mocks.loadActivities.mockResolvedValue({ items: [], page: 1, pages: 1, total: 0 });

    const jobs = await loadJobsModule();
    const terminalJob = job({ id: 7, action: "participate", state: "success" });
    const fromSse = { ...terminalJob, finished_at: null };
    const fromPolling = { ...terminalJob, finished_at: 1_700_000_000 };

    await jobs.finishJobOnce(fromSse);
    await jobs.finishJobOnce(fromPolling);

    expect(mocks.loadSummary).toHaveBeenCalledTimes(1);
    expect(mocks.syncProjectState).toHaveBeenCalledTimes(1);
    expect(mocks.loadActivities).toHaveBeenCalledTimes(1);

    await jobs.finishJobOnce(job({ id: 8, action: "participate", state: "success" }));
    expect(mocks.loadSummary).toHaveBeenCalledTimes(2);
    expect(mocks.loadActivities).toHaveBeenCalledTimes(2);
    await flush();
  });

  it("re-enables action buttons right after terminal while background still pending", async () => {
    const gate = deferred();
    mocks.loadSummary.mockReturnValue(gate.promise);
    mocks.syncProjectState.mockReturnValue(gate.promise);
    mocks.loadActivities.mockReturnValue(gate.promise);

    const jobs = await loadJobsModule();
    jobs.updateJobUI({
      id: 1,
      action: "participate",
      state: "running",
      message: "参与中",
      log: "",
    });
    const participateBtn = document.querySelector<HTMLButtonElement>(
      '[data-action="participate"]',
    );
    const controlBtn = document.querySelector<HTMLButtonElement>("[data-job-control]");
    expect(participateBtn?.disabled).toBe(true);
    expect(controlBtn?.disabled).toBe(true);

    await jobs.finishJobOnce(job({ id: 1, action: "participate", state: "success" }));

    expect(participateBtn?.disabled).toBe(false);
    expect(mocks.clearActionButtonLoading).toHaveBeenCalledTimes(1);

    gate.resolve();
    await flush();
  });
});
