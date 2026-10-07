// @vitest-environment jsdom
/* eslint-disable */
// M7 前端 onboarding 主流程：不先建空 Profile、不调用旧 login、无 Cookie 进出。
// V28：mock 必须使用真实后端 envelope {ok, result}，防止 envelope bug 再次被 mock 掩盖。
// V30：mock 必须精确匹配真实 Backend path；错误 complete URL 不得被命中。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";

const mocks = vi.hoisted(() => ({
  fetchJSON: vi.fn(),
  showToast: vi.fn(),
}));

vi.mock("../api/client", () => ({ fetchJSON: mocks.fetchJSON }));
vi.mock("../shell/toast", () => ({ showToast: mocks.showToast }));

type Envelope<T> = { ok: true; result: T };

function envelope<T>(result: T): Envelope<T> {
  return { ok: true, result };
}

const PROFILES_PAYLOAD = {
  runtime_profile_id: "account-2",
  active_profile_id: "account-2",
  restart_required: true,
  data_root: "D:\\BinggoData",
  profiles: [],
};

const COMPLETE_PATH = "/api/accounts/onboarding/complete";
const START_PATH = "/api/accounts/onboarding/start";

function setupDom(): void {
  document.body.innerHTML = `
    <button id="profile-onboarding-start">扫码登录新账号</button>
    <div id="profile-current"></div>
    <div id="profile-list"></div>
    <div id="profile-restart-notice"></div>`;
}

async function load(): Promise<typeof import("./index")> {
  vi.resetModules();
  setupDom();
  mocks.fetchJSON.mockReset();
  mocks.showToast.mockReset();
  const mod = await import("./index");
  mod.bindProfiles();
  return mod;
}

async function openModal(): Promise<HTMLElement> {
  (document.getElementById("profile-onboarding-start") as HTMLButtonElement).click();
  await vi.waitFor(() => {
    expect(document.querySelector(".onboarding-qr-modal")).not.toBeNull();
  });
  return document.querySelector(".onboarding-qr-modal") as HTMLElement;
}

function loadQr(modal: HTMLElement): void {
  const img = modal.querySelector("#onboarding-qr-img") as HTMLImageElement;
  img.dispatchEvent(new Event("load"));
}

function requestPaths(): string[] {
  return mocks.fetchJSON.mock.calls.map((call) => String(call[0]));
}

function pollCalls(): string[] {
  return requestPaths().filter((url) => /^\/api\/accounts\/onboarding\/[^/]+\/poll$/.test(url));
}

function completeCalls(): string[] {
  return requestPaths().filter((url) => url === COMPLETE_PATH);
}

function wrongCompleteCalls(): string[] {
  return requestPaths().filter((url) =>
    /^\/api\/accounts\/onboarding\/[^/]+\/complete$/.test(url),
  );
}

function switchCalls(): string[] {
  return requestPaths().filter((url) => url.includes("/switch"));
}

function startCalls(): string[] {
  return requestPaths().filter((url) => url === START_PATH);
}

function errorToasts(): unknown[][] {
  return mocks.showToast.mock.calls.filter((call) => call[1] === "error");
}

function installBackend(options: {
  onboardingId?: string;
  polls: unknown[];
  complete?: unknown;
  completeError?: Error;
  switchError?: Error;
  profilesError?: Error;
  profiles?: unknown;
}): void {
  const onboardingId = options.onboardingId ?? "o1";
  let pollIndex = 0;
  mocks.fetchJSON.mockImplementation((url: string, init?: RequestInit) => {
    const path = String(url);
    if (path === START_PATH) {
      return Promise.resolve(envelope({ onboarding_id: onboardingId, qr_url: "https://qr.x" }));
    }
    if (path === `/api/accounts/onboarding/${onboardingId}/poll`) {
      if ((init as { method?: string } | undefined)?.method !== "POST") {
        return Promise.reject(new Error(`unexpected method for ${path}`));
      }
      const next = options.polls[Math.min(pollIndex, options.polls.length - 1)];
      pollIndex += 1;
      return typeof next === "function" ? (next as () => Promise<unknown>)() : Promise.resolve(next);
    }
    if (path === COMPLETE_PATH) {
      if ((init as { method?: string } | undefined)?.method !== "POST") {
        return Promise.reject(new Error(`unexpected method for ${path}`));
      }
      if (options.completeError) return Promise.reject(options.completeError);
      return Promise.resolve(
        options.complete ?? envelope({ profile_id: "account-2", created_new: true }),
      );
    }
    if (/^\/api\/profiles\/[^/]+\/switch$/.test(path)) {
      if (options.switchError) return Promise.reject(options.switchError);
      return Promise.resolve({ ok: true });
    }
    if (path === "/api/profiles") {
      if (options.profilesError) return Promise.reject(options.profilesError);
      return Promise.resolve(options.profiles ?? PROFILES_PAYLOAD);
    }
    return Promise.reject(new Error(`unexpected url: ${path}`));
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("profiles onboarding main flow", () => {
  it("keeps QR onboarding as the only account creation button in the shipped HTML", () => {
    const html = readFileSync("index.html", "utf-8");
    expect(html).toContain('id="profile-onboarding-start"');
    expect(html).not.toContain('id="profile-create"');
  });

  it("starts onboarding without create_profile and shows QR", async () => {
    await load();
    installBackend({
      onboardingId: "o1",
      polls: [
        envelope({ onboarding_id: "o1", status: "waiting" }),
        envelope({ onboarding_id: "o1", status: "scanned" }),
        envelope({ onboarding_id: "o1", status: "success" }),
      ],
      complete: envelope({
        profile_id: "account-2",
        created_new: true,
        reused_existing: false,
        display_name: "n",
        uid: "9",
        restart_required: true,
      }),
    });
    const modal = await openModal();
    const qrImage = modal.querySelector("#onboarding-qr-img") as HTMLImageElement;
    expect(qrImage.src).toContain("/api/accounts/onboarding/o1/qr");
    expect(qrImage.src).not.toContain("undefined");
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100); // round 1 -> waiting
    await vi.advanceTimersByTimeAsync(2100); // round 2 -> scanned
    expect(modal.querySelector("#onboarding-status")?.textContent).toContain(
      "已扫码，请在手机端确认登录",
    );
    await vi.advanceTimersByTimeAsync(2100); // round 3 -> success -> complete -> switch
    await vi.advanceTimersByTimeAsync(0);
    const urls = requestPaths();
    expect(urls[0]).toBe(START_PATH);
    const postCreates = mocks.fetchJSON.mock.calls.filter(
      (call) => call[1] && (call[1] as { method?: string }).method === "POST" && String(call[0]) === "/api/profiles",
    );
    expect(postCreates).toEqual([]); // 没有先 POST /api/profiles 建空 Profile
    expect(urls.some((u) => u === "/api/profiles/account-2/switch")).toBe(false);
    expect(urls.some((u) => u === "/api/accounts/onboarding/o1/poll")).toBe(true);
    expect(urls.some((u) => u === COMPLETE_PATH)).toBe(true);
    expect(wrongCompleteCalls()).toEqual([]);
    // 前端 state 不包含任何完整 Cookie
    expect(mocks.fetchJSON.mock.calls.every((call) => !JSON.stringify(call).includes("SESSDATA"))).toBe(true);
    expect(mocks.fetchJSON.mock.calls.every((call) => !JSON.stringify(call).includes("cookie"))).toBe(true);
    // 未调用旧 current-profile login job
    expect(urls.some((u) => u.includes("/api/jobs"))).toBe(false);
  });

  it("shows an explicit error and does not poll when the QR image fails", async () => {
    await load();
    mocks.fetchJSON.mockResolvedValueOnce(
      envelope({ onboarding_id: "o-error", qr_url: "https://qr.x" }),
    );

    (document.getElementById("profile-onboarding-start") as HTMLButtonElement).click();
    await vi.waitFor(() => {
      expect(document.getElementById("onboarding-qr-img")).not.toBeNull();
    });
    const qrImage = document.getElementById("onboarding-qr-img") as HTMLImageElement;

    await vi.advanceTimersByTimeAsync(2100);
    expect(mocks.fetchJSON).toHaveBeenCalledTimes(1);

    qrImage.dispatchEvent(new Event("error"));
    await Promise.resolve();

    expect(document.getElementById("onboarding-status")?.textContent).toContain(
      "二维码加载失败",
    );
    expect(mocks.showToast).toHaveBeenCalledWith(
      "二维码加载失败，请关闭后重试。",
      "error",
    );
    expect(mocks.fetchJSON).toHaveBeenCalledTimes(1);
  });

  // ---------- V28 CASE 19-29：真实 envelope 消费 / 状态语义 / 竞态 ----------

  it("CASE 19: reads waiting from the real {ok,result} envelope without error toasts", async () => {
    await load();
    installBackend({
      onboardingId: "o-wait",
      polls: [envelope({ onboarding_id: "o-wait", status: "waiting" })],
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);
    await vi.advanceTimersByTimeAsync(2100);
    expect(pollCalls().length).toBe(2);
    expect(modal.querySelector("#onboarding-status")?.textContent).toContain("哔哩哔哩 App");
    expect(errorToasts()).toEqual([]);
    expect(completeCalls()).toEqual([]);
  });

  it("CASE 20: scanned keeps polling and shows the phone-confirm status", async () => {
    await load();
    installBackend({
      onboardingId: "o-scan",
      polls: [envelope({ onboarding_id: "o-scan", status: "scanned" })],
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);
    expect(modal.querySelector("#onboarding-status")?.textContent).toBe(
      "已扫码，请在手机端确认登录",
    );
    await vi.advanceTimersByTimeAsync(2100);
    expect(pollCalls().length).toBe(2);
    expect(completeCalls()).toEqual([]);
    expect(errorToasts()).toEqual([]);
  });

  it("CASE 21+22: success calls complete and only refreshes the Profile list", async () => {
    await load();
    installBackend({
      onboardingId: "o-ok",
      polls: [envelope({ onboarding_id: "o-ok", status: "success" })],
      complete: envelope({
        profile_id: "account-x",
        created_new: true,
        reused_existing: false,
        display_name: "n",
        uid: "9",
        restart_required: true,
      }),
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);
    await vi.advanceTimersByTimeAsync(0);

    expect(completeCalls()).toHaveLength(1);
    const completeCall = mocks.fetchJSON.mock.calls.find((call) => String(call[0]) === COMPLETE_PATH);
    expect((completeCall?.[1] as { method?: string } | undefined)?.method).toBe("POST");
    expect(JSON.stringify(completeCall?.[1])).toContain("o-ok");
    expect(switchCalls()).toEqual([]);
    expect(
      mocks.fetchJSON.mock.calls.every((call) => !String(call[0]).includes("undefined")),
    ).toBe(true);
    expect(mocks.showToast).toHaveBeenCalledWith("账号已就绪，可点击“新窗口运行”。", "success");
  });

  it("CASE 23: missing result fails explicitly instead of running on undefined", async () => {
    await load();
    installBackend({ onboardingId: "o-bad", polls: [{ ok: true }] });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);

    expect(mocks.showToast).toHaveBeenCalledWith(
      expect.stringContaining("响应缺少 result"),
      "error",
    );
    expect(completeCalls()).toEqual([]);
    expect(switchCalls()).toEqual([]);
  });

  it("CASE 24: expired stops with an explicit message", async () => {
    await load();
    installBackend({
      onboardingId: "o-exp",
      polls: [envelope({ onboarding_id: "o-exp", status: "expired" })],
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);
    await vi.advanceTimersByTimeAsync(2100);

    expect(modal.querySelector("#onboarding-status")?.textContent).toBe("二维码已过期，请重新获取");
    expect(pollCalls().length).toBe(1);
    expect(completeCalls()).toEqual([]);
  });

  it("CASE 25: network error shows the retryable upstream message", async () => {
    await load();
    installBackend({
      onboardingId: "o-net",
      polls: [() => Promise.reject(new Error("暂时无法查询扫码状态，请重新获取二维码"))],
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);

    expect(mocks.showToast).toHaveBeenCalledWith(
      "暂时无法查询扫码状态，请重新获取二维码",
      "error",
    );
    expect(completeCalls()).toEqual([]);
  });

  it("CASE 26: an in-flight poll has no authority after the modal is closed", async () => {
    await load();
    const pollResolvers: Array<(value: unknown) => void> = [];
    let completeCount = 0;
    mocks.fetchJSON.mockImplementation((url: string) => {
      const path = String(url);
      if (path === START_PATH) {
        return Promise.resolve(envelope({ onboarding_id: "o-close", qr_url: "https://qr.x" }));
      }
      if (path === "/api/accounts/onboarding/o-close/poll") {
        return new Promise((resolve) => {
          pollResolvers.push(resolve);
        });
      }
      if (path === COMPLETE_PATH) {
        completeCount += 1;
        return Promise.resolve(envelope({ profile_id: "account-2", created_new: true }));
      }
      return Promise.reject(new Error(`unexpected url: ${path}`));
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);
    expect(pollResolvers).toHaveLength(1);

    (modal.querySelector("#onboarding-close") as HTMLButtonElement).click();
    expect(document.querySelector(".onboarding-qr-modal")).toBeNull();

    pollResolvers[0](envelope({ status: "success" }));
    await vi.advanceTimersByTimeAsync(6000);

    expect(completeCount).toBe(0);
    expect(switchCalls()).toEqual([]);
    expect(errorToasts()).toEqual([]);
  });

  it("CASE 27: a stale session response cannot override the new session", async () => {
    await load();
    const pollResolvers: Array<(value: unknown) => void> = [];
    let startCallsCount = 0;
    mocks.fetchJSON.mockImplementation((url: string) => {
      const path = String(url);
      if (path === START_PATH) {
        startCallsCount += 1;
        return Promise.resolve(
          envelope({ onboarding_id: `o-${startCallsCount}`, qr_url: "https://qr.x" }),
        );
      }
      if (/^\/api\/accounts\/onboarding\/[^/]+\/poll$/.test(path)) {
        return new Promise((resolve) => {
          pollResolvers.push(resolve);
        });
      }
      return Promise.reject(new Error(`unexpected url: ${path}`));
    });

    const modalA = await openModal();
    loadQr(modalA);
    await vi.advanceTimersByTimeAsync(2100);
    expect(pollResolvers).toHaveLength(1);
    (modalA.querySelector("#onboarding-close") as HTMLButtonElement).click();
    await vi.advanceTimersByTimeAsync(0);

    const modalB = await openModal();
    loadQr(modalB);
    await vi.advanceTimersByTimeAsync(2100);
    expect(pollResolvers).toHaveLength(2);

    pollResolvers[0](envelope({ status: "expired" }));
    await vi.advanceTimersByTimeAsync(500);
    expect(modalB.querySelector("#onboarding-status")?.textContent).toContain("哔哩哔哩 App");
    expect(modalB.querySelector("#onboarding-status")?.textContent).not.toContain("过期");

    pollResolvers[1](envelope({ status: "waiting" }));
    await vi.advanceTimersByTimeAsync(500);
    expect(modalB.querySelector("#onboarding-status")?.textContent).toContain("哔哩哔哩 App");
    expect(errorToasts()).toEqual([]);
  });

  it("CASE 28: success invalidates the loop so no later poll can override it", async () => {
    await load();
    let pollCount = 0;
    installBackend({
      onboardingId: "o-success",
      polls: [
        () => {
          pollCount += 1;
          return Promise.resolve(envelope({ status: "success" }));
        },
      ],
      complete: envelope({ profile_id: "account-7", created_new: false }),
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);
    await vi.advanceTimersByTimeAsync(0);

    const statusEl = modal.querySelector("#onboarding-status");
    expect(pollCount).toBe(1);
    expect(statusEl?.textContent).toContain("已存在");

    await vi.advanceTimersByTimeAsync(2200);
    expect(pollCount).toBe(1);
    expect(statusEl?.textContent).toContain("已存在");
    expect(statusEl?.textContent).not.toContain("过期");
  });

  it("CASE 29: UI copy names the Bilibili App and phone confirmation", async () => {
    await load();
    installBackend({ onboardingId: "o-copy", polls: [envelope({ status: "waiting" })] });
    const modal = await openModal();
    expect(modal.textContent).toContain("哔哩哔哩 App");
    expect(modal.textContent).toContain("手机端确认登录");
    expect(mocks.showToast).toHaveBeenCalledWith(
      expect.stringContaining("哔哩哔哩 App"),
      "info",
    );
    expect(mocks.showToast).not.toHaveBeenCalledWith(
      expect.stringContaining("浏览器中完成扫码"),
      expect.anything(),
    );
  });

  // ---------- V30：complete 契约 / 失败终态 / switch 失败恢复 ----------

  it("V30 CASE 1-3: complete uses the exact backend path and never the legacy id path", async () => {
    await load();
    installBackend({
      onboardingId: "o1",
      polls: [envelope({ onboarding_id: "o1", status: "success" })],
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);
    await vi.advanceTimersByTimeAsync(0);

    expect(completeCalls()).toEqual([COMPLETE_PATH]);
    expect(wrongCompleteCalls()).toEqual([]);
    const completeCall = mocks.fetchJSON.mock.calls.find((call) => String(call[0]) === COMPLETE_PATH);
    const body = JSON.parse(String((completeCall?.[1] as { body?: string } | undefined)?.body));
    expect(body).toEqual({ onboarding_id: "o1" });
  });

  it("V30 CASE 14: complete 404 enters a failed terminal state with zero switch", async () => {
    await load();
    installBackend({
      onboardingId: "o-404",
      polls: [envelope({ onboarding_id: "o-404", status: "success" })],
      completeError: new Error("Not Found"),
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);

    expect(modal.querySelector("#onboarding-status")?.textContent).toContain("账号创建失败");
    expect(completeCalls()).toHaveLength(1);
    expect(switchCalls()).toEqual([]);
    expect(errorToasts().length).toBeGreaterThan(0);

    await vi.advanceTimersByTimeAsync(3000);
    expect(pollCalls().length).toBe(1); // 失败终态后不再轮询
    expect(completeCalls()).toHaveLength(1); // 不重复 complete
  });

  it("V30 CASE 15: complete 500 enters a failed terminal state with zero switch", async () => {
    await load();
    installBackend({
      onboardingId: "o-500",
      polls: [envelope({ onboarding_id: "o-500", status: "success" })],
      completeError: new Error("服务器错误"),
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);

    expect(modal.querySelector("#onboarding-status")?.textContent).toContain("账号创建失败");
    expect(switchCalls()).toEqual([]);
    expect(completeCalls()).toHaveLength(1);
  });

  it("V30 CASE 16: complete envelope without result is a protocol failure with zero switch", async () => {
    await load();
    installBackend({
      onboardingId: "o-proto",
      polls: [envelope({ onboarding_id: "o-proto", status: "success" })],
      complete: { ok: true },
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);

    expect(modal.querySelector("#onboarding-status")?.textContent).toContain("账号创建失败");
    expect(switchCalls()).toEqual([]);
    expect(mocks.showToast).toHaveBeenCalledWith(
      expect.stringContaining("响应缺少 result"),
      "error",
    );
  });

  it("V109: onboarding never invokes switch even if that endpoint would fail", async () => {
    await load();
    installBackend({
      onboardingId: "o-switchfail",
      polls: [envelope({ onboarding_id: "o-switchfail", status: "success" })],
      complete: envelope({ profile_id: "account-9", created_new: true }),
      switchError: new Error("switch boom"),
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);
    await vi.advanceTimersByTimeAsync(0);

    expect(mocks.showToast).toHaveBeenCalledWith("账号已就绪，可点击“新窗口运行”。", "success");
    expect(switchCalls()).toEqual([]);
    expect(completeCalls()).toHaveLength(1); // 不重复 complete
    expect(startCalls()).toHaveLength(1); // 不重新扫码
    expect(pollCalls()).toHaveLength(1);
    expect(modal.querySelector("#onboarding-status")?.textContent).toContain("账号创建成功");
  });

  it("V30 CASE 21: loadProfiles failure does not mark the account creation as failed", async () => {
    await load();
    installBackend({
      onboardingId: "o-listfail",
      polls: [envelope({ onboarding_id: "o-listfail", status: "success" })],
      complete: envelope({ profile_id: "account-8", created_new: true }),
      profilesError: new Error("list boom"),
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);
    await vi.advanceTimersByTimeAsync(0);

    expect(mocks.showToast).toHaveBeenCalledWith("账号已就绪，可点击“新窗口运行”。", "success");
    expect(
      mocks.showToast.mock.calls.some((call) => String(call[0]).includes("账号创建失败")),
    ).toBe(false);
    expect(modal.querySelector("#onboarding-status")?.textContent).toContain("账号创建成功");
  });

  it("V30 CASE 22: success path removes the modal", async () => {
    await load();
    installBackend({
      onboardingId: "o-modal",
      polls: [envelope({ onboarding_id: "o-modal", status: "success" })],
      complete: envelope({ profile_id: "account-5", created_new: true }),
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);
    await vi.advanceTimersByTimeAsync(2600);

    expect(document.querySelector(".onboarding-qr-modal")).toBeNull();
  });

  it("V30 CASE 23-24: after a failure a new flow does not stack modals and the old flow stays dead", async () => {
    await load();
    installBackend({
      onboardingId: "o-first",
      polls: [envelope({ onboarding_id: "o-first", status: "success" })],
      completeError: new Error("Not Found"),
    });
    const modal = await openModal();
    loadQr(modal);
    await vi.advanceTimersByTimeAsync(2100);
    expect(modal.querySelector("#onboarding-status")?.textContent).toContain("账号创建失败");

    // 立即开启新 flow（旧 modal 尚未到 2500ms 清理时间）
    installBackend({
      onboardingId: "o-second",
      polls: [envelope({ onboarding_id: "o-second", status: "waiting" })],
    });
    const second = await openModal();
    await vi.advanceTimersByTimeAsync(0);
    expect(document.querySelectorAll(".onboarding-qr-modal")).toHaveLength(1);
    expect(second.querySelector("#onboarding-status")?.textContent).toContain("哔哩哔哩 App");

    await vi.advanceTimersByTimeAsync(2100);
    expect(completeCalls()).toHaveLength(1); // 旧 flow 不会再次 complete
    expect(pollCalls().filter((u) => u.includes("o-first"))).toHaveLength(1);
  });
});
