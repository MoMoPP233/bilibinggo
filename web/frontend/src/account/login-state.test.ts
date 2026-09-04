// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const {
  fetchJSONMock,
  showToastMock,
  loadSettingsMock,
  openAppConfirmMock,
  closeAppConfirmMock,
  switchSectionMock,
  renderWatchUsersPanelMock,
  updateWatchUserFormStateMock,
} = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(),
  showToastMock: vi.fn(),
  loadSettingsMock: vi.fn(),
  openAppConfirmMock: vi.fn(),
  closeAppConfirmMock: vi.fn(),
  switchSectionMock: vi.fn(),
  renderWatchUsersPanelMock: vi.fn(),
  updateWatchUserFormStateMock: vi.fn(),
}));

vi.mock("../api/client", () => ({ fetchJSON: fetchJSONMock }));
vi.mock("../settings/index", () => ({ loadSettings: loadSettingsMock }));
vi.mock("../shell/confirm", () => ({
  openAppConfirm: openAppConfirmMock,
  closeAppConfirm: closeAppConfirmMock,
}));
vi.mock("../shell/nav", () => ({ switchSection: switchSectionMock }));
vi.mock("../shell/toast", () => ({ showToast: showToastMock }));
vi.mock("../watch/index", () => ({
  renderWatchUsersPanel: renderWatchUsersPanelMock,
  updateWatchUserFormState: updateWatchUserFormStateMock,
}));

function setupDom() {
  document.body.innerHTML = `
    <div id="account-hero"></div>
    <div id="sidebar-account-card"></div>
    <button id="sidebar-login"></button>
    <button id="sidebar-logout"></button>`;
}

function loggedInAccount() {
  return {
    logged_in: true,
    expired: false,
    message: "已登录",
    uname: "测试账号",
    face: "",
    mid: "12345",
    following: 10,
    dynamic_count: 2,
    unread_messages: 0,
    unread_at: 0,
    extras_loading: false,
  };
}

function loggedOutAccount() {
  return {
    logged_in: false,
    expired: true,
    message: "Cookie 已过期，请重新扫码登录",
    uname: "",
    face: "",
    mid: null,
    following: null,
    dynamic_count: null,
    unread_messages: null,
    unread_at: null,
    extras_loading: false,
  };
}

function deferred() {
  let resolve: (value: unknown) => void = () => {};
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

async function loadModule() {
  return import("./index");
}

describe("login state stability", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset();
    showToastMock.mockReset();
    loadSettingsMock.mockReset();
    openAppConfirmMock.mockReset();
    closeAppConfirmMock.mockReset();
    switchSectionMock.mockReset();
    renderWatchUsersPanelMock.mockReset();
    updateWatchUserFormStateMock.mockReset();
    setupDom();
  });

  it("success shows the account and keeps it on transient fetch failure", async () => {
    fetchJSONMock.mockResolvedValueOnce(loggedInAccount());
    fetchJSONMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const mod = await loadModule();

    await mod.loadAccount();
    const hero = document.getElementById("account-hero")?.textContent || "";
    expect(hero).toContain("测试账号");

    await mod.loadAccount();
    const after = document.getElementById("account-hero")?.textContent || "";
    expect(after).toContain("测试账号");
    expect(after).not.toContain("未登录");
    expect(showToastMock).toHaveBeenCalledWith(
      "暂时无法确认登录状态，已保留上次状态",
      "info",
    );
  });

  it("explicit logout still clears the snapshot", async () => {
    fetchJSONMock.mockResolvedValueOnce(loggedInAccount());
    fetchJSONMock.mockResolvedValueOnce(loggedOutAccount());
    const mod = await loadModule();
    await mod.loadAccount();
    await mod.loadAccount();

    const after = document.getElementById("account-hero")?.textContent || "";
    expect(after).not.toContain("测试账号");
    expect(after).toContain("未登录");
  });

  it("no snapshot + fetch failure shows unconfirmed instead of logged-out", async () => {
    fetchJSONMock.mockRejectedValueOnce(new TypeError("network down"));
    const mod = await loadModule();
    await mod.loadAccount();

    const hero = document.getElementById("account-hero")?.textContent || "";
    expect(hero).toContain("登录状态暂时无法确认");
    expect(hero).not.toContain("Cookie 已过期");
  });

  it("no snapshot + transient offline body shows unconfirmed", async () => {
    fetchJSONMock.mockResolvedValueOnce({
      logged_in: false,
      expired: true,
      network_error: true,
      cookie_saved: true,
      message: "无法连接 B 站",
      uname: "",
      face: "",
      mid: "12345",
      following: null,
      dynamic_count: null,
      unread_messages: null,
      unread_at: null,
      extras_loading: false,
    });
    const mod = await loadModule();
    await mod.loadAccount();
    const hero = document.getElementById("account-hero")?.textContent || "";
    expect(hero).toContain("登录状态暂时无法确认");
  });

  it("old late success cannot override a newer explicit logout", async () => {
    const oldLogin = deferred();
    const calls: Array<{ url: string; promise?: ReturnType<typeof deferred>["promise"] }> = [];
    fetchJSONMock.mockImplementation(() => {
      if (!calls.length) {
        calls.push({ url: "first" });
        return oldLogin.promise;
      }
      return Promise.resolve(loggedOutAccount());
    });

    const mod = await loadModule();
    const first = mod.loadAccount();
    const second = await mod.loadAccount(); // newer logout 先完成
    expect((document.getElementById("account-hero")?.textContent || "")).toContain("未登录");

    oldLogin.resolve(loggedInAccount());
    await first;
    const after = document.getElementById("account-hero")?.textContent || "";
    expect(after).not.toContain("测试账号");
    expect(after).toContain("未登录");
    void second;
  });
});
