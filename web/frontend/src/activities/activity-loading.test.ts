// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const {
  fetchJSONMock,
  isSetupCompleteMock,
  showToastMock,
} = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(),
  isSetupCompleteMock: vi.fn(),
  showToastMock: vi.fn(),
}));

vi.mock("../api/client", () => ({ fetchJSON: fetchJSONMock }));
vi.mock("../account/index", () => ({ isSetupComplete: isSetupCompleteMock }));
vi.mock("../shell/toast", () => ({ showToast: showToastMock }));
vi.mock("../jobs/index", () => ({
  bindActionButtons: () => {},
  updateJobUI: () => {},
}));
vi.mock("../watch/index", () => ({ renderSources: () => {} }));

if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = (query) => ({
    matches: true,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}

function setupDom() {
  document.body.innerHTML = `
    <div id="stats-grid"></div>
    <div id="activities-body"></div>
    <div id="activities-cards"></div>
    <p id="filter-result-summary"></p>
    <p id="activities-status" hidden></p>
    <div id="pagination"></div>
    <div id="triple-participate-bar"></div>
    <p id="triple-participate-desc"></p>
    <div id="triple-participate-targets"></div>
    <button id="triple-participate-btn"></button>
    <span id="triple-participate-btn-label"></span>`;
}

function activity(dynamicId: string, title: string) {
  return {
    dynamic_id: dynamicId,
    activity_title: title,
    prize: title,
    source_url: "",
    lottery_type: "互动抽奖",
    lottery_time: 1_800_000_000,
    activity_status: "未参加",
    can_participate: false,
    heat: 1,
  };
}

function payload(rows: ReturnType<typeof activity>[]) {
  return { items: rows, page: 1, pages: 1, total: rows.length };
}

function deferred() {
  let resolve: (value: unknown) => void = () => {};
  let reject: (reason?: unknown) => void = () => {};
  const promise = new Promise((r, j) => { resolve = r; reject = j; });
  return { promise, resolve, reject };
}

async function loadModule() {
  return import("./index");
}

describe("activities list stability", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset();
    isSetupCompleteMock.mockReset();
    showToastMock.mockReset();
    isSetupCompleteMock.mockReturnValue(true);
    setupDom();
  });

  it("first load success renders activities", async () => {
    fetchJSONMock.mockResolvedValueOnce(payload([activity("d1", "活动甲")]));
    const mod = await loadModule();
    await mod.loadActivities();
    expect(document.getElementById("activities-body")?.textContent || "").toContain("活动甲");
  });

  it("first load failure shows failure state, not empty", async () => {
    fetchJSONMock.mockRejectedValueOnce(new TypeError("boom"));
    const mod = await loadModule();
    await mod.loadActivities();
    const text = document.getElementById("activities-body")?.textContent || "";
    expect(text).toContain("活动加载失败");
    expect(text).not.toContain("没有匹配的活动");
  });

  it("retry after first failure recovers without browser reload", async () => {
    fetchJSONMock.mockRejectedValueOnce(new TypeError("boom"));
    fetchJSONMock.mockResolvedValueOnce(payload([activity("d1", "活动乙")]));
    const mod = await loadModule();
    await mod.loadActivities();
    await mod.loadActivities();
    expect(document.getElementById("activities-body")?.textContent || "").toContain("活动乙");
  });

  it("refresh failure keeps old rows and shows warn note", async () => {
    fetchJSONMock.mockResolvedValueOnce(payload([activity("d1", "活动甲")]));
    fetchJSONMock.mockRejectedValueOnce(new TypeError("boom"));
    const mod = await loadModule();
    await mod.loadActivities();
    await mod.loadActivities();
    const text = document.getElementById("activities-body")?.textContent || "";
    expect(text).toContain("活动甲");
    const status = document.getElementById("activities-status");
    expect(status?.textContent).toContain("活动刷新失败，当前显示上次结果");
  });

  it("latest request wins when an older request returns later", async () => {
    const oldRequest = deferred();
    const calls: number[] = [];
    fetchJSONMock.mockImplementation(() => {
      calls.push(calls.length);
      if (calls.length === 1) return oldRequest.promise;
      return Promise.resolve(payload([activity("d2", "活动B")]));
    });

    const mod = await loadModule();
    const first = mod.loadActivities();
    await mod.loadActivities(); // B 先完成
    expect(document.getElementById("activities-body")?.textContent || "").toContain("活动B");

    oldRequest.resolve(payload([activity("d1", "活动A")]));
    await first;
    const finalText = document.getElementById("activities-body")?.textContent || "";
    expect(finalText).toContain("活动B");
    expect(finalText).not.toContain("活动A");
  });

  it("expired failure neither overwrites result nor shows failure", async () => {
    const oldFail = deferred();
    const calls: number[] = [];
    fetchJSONMock.mockImplementation(() => {
      calls.push(calls.length);
      if (calls.length === 1) return oldFail.promise;
      return Promise.resolve(payload([activity("d2", "活动B")]));
    });

    const mod = await loadModule();
    const first = mod.loadActivities();
    await mod.loadActivities(); // B 完成
    oldFail.reject(new TypeError("late fail"));
    await first;
    const text = document.getElementById("activities-body")?.textContent || "";
    expect(text).toContain("活动B");
    expect(text).not.toContain("活动加载失败");
  });
});
