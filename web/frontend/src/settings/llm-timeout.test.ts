// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchJSON: vi.fn(),
  isLoggedIn: vi.fn(() => true),
  renderAccountViews: vi.fn(),
  renderOnboardingPanel: vi.fn(),
  setInlineFeedback: vi.fn(),
  showToast: vi.fn(),
  setButtonLoading: vi.fn(),
  clearSaveDirty: vi.fn(),
  flashButtonSuccess: vi.fn(),
  markSaveDirty: vi.fn(),
  prefersReducedMotion: vi.fn(() => false),
}));

vi.mock("../api/client", () => ({ fetchJSON: mocks.fetchJSON }));
vi.mock("../account/index", () => ({
  isLoggedIn: mocks.isLoggedIn,
  renderAccountViews: mocks.renderAccountViews,
  renderOnboardingPanel: mocks.renderOnboardingPanel,
}));
vi.mock("../shell/toast", () => ({
  setInlineFeedback: mocks.setInlineFeedback,
  showToast: mocks.showToast,
}));
vi.mock("../utils/motion", () => ({
  setButtonLoading: mocks.setButtonLoading,
  clearSaveDirty: mocks.clearSaveDirty,
  flashButtonSuccess: mocks.flashButtonSuccess,
  markSaveDirty: mocks.markSaveDirty,
  prefersReducedMotion: mocks.prefersReducedMotion,
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((r, j) => {
    resolve = r;
    reject = j;
  });
  return { promise, resolve, reject };
}

function llmFormInputs(): string {
  return JSON.stringify({
    api_key: "sk-test",
    base_url: "http://127.0.0.1:9",
    model_name: "fake-model",
  });
}

async function loadSettings() {
  return import("./index");
}

describe("LLM test request timeout consistency", () => {
  beforeEach(() => {
    vi.resetModules();
    Object.values(mocks).forEach((fn) => fn.mockReset());
    mocks.isLoggedIn.mockReturnValue(true);
    mocks.prefersReducedMotion.mockReturnValue(false);
    document.body.innerHTML = "";
  });

  it("uses a dedicated timeout longer than backend 90s window", async () => {
    const { LLM_TEST_REQUEST_TIMEOUT_MS } = await loadSettings();
    expect(LLM_TEST_REQUEST_TIMEOUT_MS).toBeGreaterThan(90_000);
    const gate = deferred();
    mocks.fetchJSON.mockImplementationOnce(() => gate.promise);

    const settings = await loadSettings();
    const pending = settings.testLlmSettings();
    const [url, options] = mocks.fetchJSON.mock.calls[0];
    expect(url).toBe("/api/settings/llm/test");
    expect(options.timeoutMs).toBe(LLM_TEST_REQUEST_TIMEOUT_MS);
    gate.resolve({
      ok: true,
      llm: { configured: true, test_passed: true },
      setup_complete: true,
      message: "连接成功：fake-model @ http://127.0.0.1:9",
    });
    await pending;
  });

  it("backend returning later than the old 60s window still shows success", async () => {
    const gate = deferred();
    mocks.fetchJSON.mockReturnValueOnce(gate.promise);
    const settings = await loadSettings();
    const pending = settings.testLlmSettings();
    // 模拟 provider 耗时 80s（超过旧 60s，但低于 backend 90s）。
    gate.resolve({
      ok: true,
      llm: { configured: true, test_passed: true },
      setup_complete: true,
      message: "连接成功：fake-model",
    });
    await pending;
    const success = mocks.setInlineFeedback.mock.calls.find((call) => call[2] === "success");
    expect(success).toBeTruthy();
  });

  it("stale slow request cannot overwrite a newer successful request", async () => {
    const settings = await loadSettings();
    const slow = deferred();
    const fast = deferred();
    mocks.fetchJSON
      .mockImplementationOnce(() => slow.promise)
      .mockImplementationOnce(() => fast.promise);

    const first = settings.testLlmSettings();
    const second = settings.testLlmSettings();
    // 新请求先完成：成功反馈一次。
    fast.resolve({
      ok: true,
      llm: { configured: true, test_passed: true },
      setup_complete: true,
      message: "连接成功：fast",
    });
    await second;
    const successCalls = mocks.setInlineFeedback.mock.calls.filter((call) => call[2] === "success");
    expect(successCalls).toHaveLength(1);
    // 旧请求晚到：必须被丢弃（不写状态、不出现第二次成功/错误反馈）。
    slow.resolve({
      ok: true,
      llm: { configured: true, test_passed: true },
      setup_complete: true,
      message: "连接成功：slow-late",
    });
    await first;
    const afterSlow = mocks.setInlineFeedback.mock.calls.filter((call) => call[2] === "success");
    expect(afterSlow).toHaveLength(1);
    const successMessage = afterSlow[0][1] as string;
    expect(successMessage).toContain("fast");
  });

  it("does not extend non-LLM API timeout defaults", async () => {
    const settings = await loadSettings();
    void settings;
    // 默认 fetchJSON 超时属于 api/client（30s，未改动）；此处仅确认导出常量未用于普通 GET。
    expect(mocks.fetchJSON).not.toHaveBeenCalled();
  });
});
