// @vitest-environment jsdom
/* eslint-disable */
// M7 前端 onboarding 主流程：不先建空 Profile、不调用旧 login、无 Cookie 进出。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";

const mocks = vi.hoisted(() => ({
  fetchJSON: vi.fn(),
  showToast: vi.fn(),
}));

vi.mock("../api/client", () => ({ fetchJSON: mocks.fetchJSON }));
vi.mock("../shell/toast", () => ({ showToast: mocks.showToast }));

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
    const mod = await load();
    mocks.fetchJSON
      .mockResolvedValueOnce({ onboarding_id: "o1", qr_url: "https://qr.x" })
      .mockResolvedValueOnce({ status: "waiting" })
      .mockResolvedValueOnce({ status: "scanned" })
      .mockResolvedValueOnce({
        profile_id: "account-2",
        created_new: true,
        reused_existing: false,
        display_name: "n",
        uid: "9",
        restart_required: true,
      })
      .mockResolvedValueOnce({ ok: true });
    // switch + profiles reload after complete
    mocks.fetchJSON.mockResolvedValueOnce({
      runtime_profile_id: "account-1",
      active_profile_id: "account-2",
      restart_required: true,
      profiles: [],
    });
    (document.getElementById("profile-onboarding-start") as HTMLButtonElement).click();
    await vi.advanceTimersByTimeAsync(2100); // poll round 1 -> scanned
    await vi.advanceTimersByTimeAsync(2100);
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
});
