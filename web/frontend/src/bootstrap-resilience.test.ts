// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  syncProjectState: vi.fn(async () => ({ logged_in: true })),
  loadProfiles: vi.fn(async () => ({ profiles: [] })),
  loadSummary: vi.fn(),
  fetchAutoStatus: vi.fn(async () => ({ state: "idle" })),
  loadWatchUsers: vi.fn(async () => ({})),
  loadActivities: vi.fn(),
  startRealtime: vi.fn(),
  startPolling: vi.fn(),
  loadRuntimeInfo: vi.fn(async () => ({})),
  showToast: vi.fn(),
  fetchJSON: vi.fn(async () => ({ state: "idle", action: "", message: "" })),
}));

vi.mock("./account/index", () => ({
  bindOnboardingPanel: () => {},
  loadAccount: vi.fn(),
  loadAccountExtras: vi.fn(),
  logoutAccount: vi.fn(),
  requestLogoutConfirm: vi.fn(),
  syncProjectState: mocks.syncProjectState,
}));
vi.mock("./activities/index", () => ({
  bindFilterPills: () => {},
  loadActivities: mocks.loadActivities,
  loadSummary: mocks.loadSummary,
}));
vi.mock("./auto/index", () => ({
  bindAutoDock: () => {},
  fetchAutoStatus: mocks.fetchAutoStatus,
  setAutoDockOpen: () => {},
}));
vi.mock("./settings/index", () => ({
  bindLlmApiKeyToggle: () => {},
  bindParticipateSettings: () => {},
  bindSettingsDirtyTracking: () => {},
  loadSettings: vi.fn(),
  refreshLlmSettings: vi.fn(),
  resetParticipateText: vi.fn(),
  saveLlmSettings: vi.fn(),
  saveParticipateText: vi.fn(),
  testLlmSettings: vi.fn(),
}));
vi.mock("./shell/nav", () => ({ bindNavigation: () => {} }));
vi.mock("./shell/theme", () => ({ initSystemPreferences: () => {} }));
vi.mock("./shell/toast", () => ({ showToast: mocks.showToast }));
vi.mock("./utils/motion", () => ({
  playOverviewEnter: () => {},
  setButtonLoading: vi.fn(),
}));
vi.mock("./diagnostics/index", () => ({ bindDiagnosticsExport: () => {} }));
vi.mock("./runtime/index", () => ({
  bindCheckUpdates: () => {},
  loadRuntimeInfo: mocks.loadRuntimeInfo,
}));
vi.mock("./watch/index", () => ({
  bindWatchUsers: () => {},
  loadWatchUsers: mocks.loadWatchUsers,
}));
vi.mock("./profiles/index", () => ({
  bindProfiles: () => {},
  loadProfiles: mocks.loadProfiles,
}));
vi.mock("./repost-cleanup/index", () => ({ bindRepostCleanup: () => {} }));
vi.mock("./sources/update-all", () => ({ bindUpdateAllDatasources: () => {} }));
vi.mock("./realtime/sse", () => ({ startRealtime: mocks.startRealtime }));
vi.mock("./api/client", () => ({ fetchJSON: mocks.fetchJSON }));
vi.mock("./jobs/index", () => ({
  bindActionButtons: () => {},
  setLogDockOpen: () => {},
  startPolling: mocks.startPolling,
  acceptJobUpdate: () => true,
  updateJobUI: () => {},
}));

function failure(message = "boom") {
  return Promise.reject(new Error(message));
}

async function loadBootstrap() {
  return import("./bootstrap");
}

describe("bootstrap resilience", () => {
  beforeEach(() => {
    vi.resetModules();
    Object.values(mocks).forEach((fn) => fn.mockReset());
    mocks.syncProjectState.mockImplementation(async () => ({ logged_in: true }));
    mocks.loadProfiles.mockImplementation(async () => ({ profiles: [] }));
    mocks.fetchAutoStatus.mockImplementation(async () => ({ state: "idle" }));
    mocks.loadWatchUsers.mockImplementation(async () => ({}));
    mocks.fetchJSON.mockImplementation(async () => ({ state: "idle", action: "", message: "" }));
    mocks.loadRuntimeInfo.mockImplementation(async () => ({}));
    mocks.startPolling.mockReset();
  });

  it("summary success: later modules all initialize", async () => {
    mocks.loadSummary.mockResolvedValue({ job: null, sources: [], user_status_counts: {} });
    mocks.loadActivities.mockResolvedValue({ items: [], page: 1, pages: 1, total: 0 });
    const bootstrap = await loadBootstrap();
    await bootstrap.init();

    expect(mocks.loadActivities).toHaveBeenCalledTimes(1);
    expect(mocks.fetchAutoStatus).toHaveBeenCalledTimes(1);
    expect(mocks.loadWatchUsers).toHaveBeenCalledTimes(1);
    expect(mocks.startRealtime).toHaveBeenCalledTimes(1);
    expect(mocks.showToast).not.toHaveBeenCalled();
  });

  it("summary failure does not skip activities / auto status / realtime", async () => {
    mocks.loadSummary.mockImplementation(() => failure("summary down"));
    mocks.loadActivities.mockResolvedValue({ items: [], page: 1, pages: 1, total: 0 });
    const bootstrap = await loadBootstrap();
    await bootstrap.init();

    expect(mocks.loadActivities).toHaveBeenCalledTimes(1);
    expect(mocks.fetchAutoStatus).toHaveBeenCalledTimes(1);
    expect(mocks.loadWatchUsers).toHaveBeenCalledTimes(1);
    expect(mocks.startRealtime).toHaveBeenCalledTimes(1);
    // 失败路径只做一次 /api/jobs/current 种子（不是 loadSummary 重复）。
    expect(mocks.fetchJSON).toHaveBeenCalledTimes(1);
    expect(mocks.fetchJSON).toHaveBeenCalledWith("/api/jobs/current");
    expect(mocks.showToast).not.toHaveBeenCalled();
  });

  it("auto status failure keeps activities + realtime", async () => {
    mocks.loadSummary.mockResolvedValue({ job: null, sources: [], user_status_counts: {} });
    mocks.fetchAutoStatus.mockImplementation(() => failure("auto down"));
    mocks.loadActivities.mockResolvedValue({ items: [], page: 1, pages: 1, total: 0 });
    const bootstrap = await loadBootstrap();
    await bootstrap.init();

    expect(mocks.loadActivities).toHaveBeenCalledTimes(1);
    expect(mocks.startRealtime).toHaveBeenCalledTimes(1);
    expect(mocks.showToast).not.toHaveBeenCalled();
  });

  it("settings/account step failure does not abort bootstrap", async () => {
    mocks.syncProjectState.mockImplementation(() => failure("settings down"));
    mocks.loadSummary.mockResolvedValue({ job: null, sources: [], user_status_counts: {} });
    mocks.loadActivities.mockResolvedValue({ items: [], page: 1, pages: 1, total: 0 });
    const bootstrap = await loadBootstrap();
    await bootstrap.init();

    expect(mocks.loadProfiles).toHaveBeenCalledTimes(1);
    expect(mocks.loadSummary).toHaveBeenCalledTimes(1);
    expect(mocks.loadActivities).toHaveBeenCalledTimes(1);
    expect(mocks.startRealtime).toHaveBeenCalledTimes(1);
    expect(mocks.showToast).not.toHaveBeenCalled();
  });

  it("activities failure does not affect realtime or others", async () => {
    mocks.loadSummary.mockResolvedValue({ job: null, sources: [], user_status_counts: {} });
    mocks.loadActivities.mockImplementation(() => failure("activities down"));
    const bootstrap = await loadBootstrap();
    await bootstrap.init();

    expect(mocks.fetchAutoStatus).toHaveBeenCalledTimes(1);
    expect(mocks.startRealtime).toHaveBeenCalledTimes(1);
    expect(mocks.showToast).not.toHaveBeenCalled();
  });

  it("multiple module failures still allow remaining modules", async () => {
    mocks.loadSummary.mockImplementation(() => failure("summary down"));
    mocks.loadActivities.mockImplementation(() => failure("activities down"));
    mocks.fetchAutoStatus.mockImplementation(() => failure("auto down"));
    mocks.loadWatchUsers.mockImplementation(() => failure("watch down"));
    const bootstrap = await loadBootstrap();
    await bootstrap.init();

    expect(mocks.startRealtime).toHaveBeenCalledTimes(1);
    expect(mocks.startPolling).not.toHaveBeenCalled();
    expect(mocks.showToast).not.toHaveBeenCalled();
  });

  it("no duplicate loader calls on failure fallback paths", async () => {
    mocks.loadSummary.mockImplementation(() => failure("summary down"));
    mocks.loadActivities.mockResolvedValue({ items: [], page: 1, pages: 1, total: 0 });
    const bootstrap = await loadBootstrap();
    await bootstrap.init();

    expect(mocks.loadSummary).toHaveBeenCalledTimes(1);
    expect(mocks.loadActivities).toHaveBeenCalledTimes(1);
    expect(mocks.fetchAutoStatus).toHaveBeenCalledTimes(1);
    expect(mocks.loadWatchUsers).toHaveBeenCalledTimes(1);
    expect(mocks.fetchJSON).toHaveBeenCalledTimes(1);
  });

  it("success path never issues extra current-job seed request", async () => {
    mocks.loadSummary.mockResolvedValue({ job: null, sources: [], user_status_counts: {} });
    mocks.loadActivities.mockResolvedValue({ items: [], page: 1, pages: 1, total: 0 });
    const bootstrap = await loadBootstrap();
    await bootstrap.init();
    expect(mocks.fetchJSON).not.toHaveBeenCalled();
  });

  it("running summary job seeds polling once (no summary duplicate)", async () => {
    mocks.loadSummary.mockResolvedValue({
      id: 9,
      state: "running",
      action: "refresh_all",
      message: "run",
    });
    mocks.loadActivities.mockResolvedValue({ items: [], page: 1, pages: 1, total: 0 });
    const bootstrap = await loadBootstrap();
    await bootstrap.init();
    expect(mocks.startPolling).toHaveBeenCalledTimes(1);
    expect(mocks.loadSummary).toHaveBeenCalledTimes(1);
    expect(mocks.startRealtime).toHaveBeenCalledTimes(1);
  });
});
