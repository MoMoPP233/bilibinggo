// @vitest-environment jsdom
/* eslint-disable */
// M7 前端 onboarding 主流程：不先建空 Profile、不调用旧 login、无 Cookie 进出。
// V28：mock 必须使用真实后端 envelope {ok, result}，防止 envelope bug 再次被 mock 掩盖。

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

function pollCalls(): string[] {
  return mocks.fetchJSON.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.includes("/poll"));
}

function completeCalls(): string[] {
  return mocks.fetchJSON.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.includes("/complete"));
}

function switchCalls(): string[] {
  return mocks.fetchJSON.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.includes("/switch"));
}

function errorToasts(): unknown[][] {
  return mocks.showToast.mock.calls.filter((call) => call[1] === "error");
}

function installBackend(options: {
  onboardingId?: string;
  polls: unknown[];
  complete?: unknown;
  profiles?: unknown;
}): void {
  let pollIndex = 0;
  mocks.fetchJSON.mockImplementation((url: string) => {
    const text = String(url);
    if (text.endsWith("/api/accounts/onboarding/start")) {
      return Promise.resolve(
        envelope({ onboarding_id: options.onboardingId ?? "o1", qr_url: "https://qr.x" }),
      );
    }
    if (text.includes("/poll")) {
      const next = options.polls[Math.min(pollIndex, options.polls.length - 1)];
      pollIndex += 1;
      return typeof next === "function" ? (next as () => Promise<unknown>)() : Promise.resolve(next);
    }
    if (text.includes("/complete")) {
      return Promise.resolve(
        options.complete ?? envelope({ profile_id: "account-2", created_new: true }),
      );
    }
    if (text.includes("/switch")) {
      return Promise.resolve({ ok: true });
    }
    return Promise.resolve(options.profiles ?? PROFILES_PAYLOAD);
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
    const urls = mocks.fetchJSON.mock.calls.map((call) => String(call[0]));
    expect(urls[0]).toContain("onboarding/start");
    const postCreates = mocks.fetchJSON.mock.calls.filter(
      (call) => call[1] && (call[1] as { method?: string }).method === "POST" && String(call[0]) === "/api/profiles",
    );
    expect(postCreates).toEqual([]); // 没有先 POST /api/profiles 建空 Profile
    expect(urls.some((u) => u.includes("/api/profiles/account-2/switch"))).toBe(true);
    expect(urls.some((u) => u.includes("onboarding/o1/poll"))).toBe(true);
    expect(urls.some((u) => u.includes("onboarding/o1/complete"))).toBe(true);
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

  it("CASE 21+22: success calls complete and switches with the real profile_id", async () => {
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
    const completeCall = mocks.fetchJSON.mock.calls.find((call) =>
      String(call[0]).includes("/complete"),
    );
    expect((completeCall?.[1] as { method?: string } | undefined)?.method).toBe("POST");
    expect(JSON.stringify(completeCall?.[1])).toContain("o-ok");
    expect(switchCalls()).toEqual(["/api/profiles/account-x/switch"]);
    expect(
      mocks.fetchJSON.mock.calls.every((call) => !String(call[0]).includes("undefined")),
    ).toBe(true);
    expect(mocks.showToast).toHaveBeenCalledWith("账号已就绪，正在切换并重启…", "success");
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
      const text = String(url);
      if (text.endsWith("/start")) {
        return Promise.resolve(envelope({ onboarding_id: "o-close", qr_url: "https://qr.x" }));
      }
      if (text.includes("/poll")) {
        return new Promise((resolve) => {
          pollResolvers.push(resolve);
        });
      }
      if (text.includes("/complete")) {
        completeCount += 1;
        return Promise.resolve(envelope({ profile_id: "account-2", created_new: true }));
      }
      return Promise.resolve(PROFILES_PAYLOAD);
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
    let startCalls = 0;
    mocks.fetchJSON.mockImplementation((url: string) => {
      const text = String(url);
      if (text.endsWith("/start")) {
        startCalls += 1;
        return Promise.resolve(
          envelope({ onboarding_id: `o-${startCalls}`, qr_url: "https://qr.x" }),
        );
      }
      if (text.includes("/poll")) {
        return new Promise((resolve) => {
          pollResolvers.push(resolve);
        });
      }
      return Promise.resolve(PROFILES_PAYLOAD);
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
    expect(statusEl?.textContent).toContain("已经存在");

    await vi.advanceTimersByTimeAsync(2200);
    expect(pollCount).toBe(1);
    expect(statusEl?.textContent).toContain("已经存在");
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
});
