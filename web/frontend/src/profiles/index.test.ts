// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchJSONMock, openAppConfirmMock, showToastMock } = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(),
  openAppConfirmMock: vi.fn(),
  showToastMock: vi.fn(),
}));

vi.mock("../api/client", () => ({
  fetchJSON: fetchJSONMock,
}));

vi.mock("../shell/confirm", () => ({
  openAppConfirm: openAppConfirmMock,
}));

vi.mock("../shell/toast", () => ({
  showToast: showToastMock,
}));

const payload = {
  runtime_profile_id: "account-1",
  active_profile_id: "account-1",
  restart_required: false,
  data_root: "D:\\BinggoData",
  profiles: [
    {
      profile_id: "account-1",
      mid: "123456",
      nickname: "摸摸Pp",
    },
    {
      profile_id: "account-2",
      mid: "654321",
      nickname: "H牵手的痛",
    },
  ],
};

const targetPayload = {
  ...payload,
  runtime_profile_id: "account-2",
  active_profile_id: "account-2",
};

function setupProfileDom(): void {
  document.body.innerHTML = `
    <button type="button" id="profile-create">新建账号</button>
    <div id="profile-restart-notice" hidden></div>
    <div id="profile-current"></div>
    <div id="profile-list"></div>`;
}

describe("profiles", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset();
    openAppConfirmMock.mockReset();
    showToastMock.mockReset();
    fetchJSONMock.mockResolvedValue(payload);
    openAppConfirmMock.mockResolvedValue(true);
    setupProfileDom();
  });

  it("reloads profiles after a successful login job", async () => {
    const { syncProfilesAfterLoginJob } = await import("./index");

    await expect(
      syncProfilesAfterLoginJob({ action: "login", state: "success" }),
    ).resolves.toBe(true);

    expect(fetchJSONMock).toHaveBeenCalledOnce();
    expect(fetchJSONMock).toHaveBeenCalledWith("/api/profiles");
  });

  it("does not reload profiles for other job outcomes", async () => {
    const { syncProfilesAfterLoginJob } = await import("./index");

    await expect(
      syncProfilesAfterLoginJob({ action: "login", state: "error" }),
    ).resolves.toBe(false);
    await expect(
      syncProfilesAfterLoginJob({ action: "refresh_all", state: "success" }),
    ).resolves.toBe(false);

    expect(fetchJSONMock).not.toHaveBeenCalled();
  });

  it("keeps a previously selected pending Profile switchable so restart can resume", async () => {
    fetchJSONMock.mockResolvedValue({
      ...payload,
      active_profile_id: "account-2",
      restart_required: true,
    });
    const { loadProfiles } = await import("./index");

    await loadProfiles();

    const pendingButton = document.querySelector(
      '[data-profile-activate="account-2"]',
    ) as HTMLButtonElement;
    expect(pendingButton.disabled).toBe(false);
    expect(pendingButton.textContent).toBe("重启生效");
  });

  it("ignores transient disconnects and reloads only after the target runtime is ready", async () => {
    const { waitForProfileRestart } = await import("./index");
    const probe = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce({
        ...payload,
        active_profile_id: "account-2",
        restart_required: true,
      })
      .mockResolvedValueOnce(targetPayload);
    const reload = vi.fn();
    let clock = 0;
    const fakeSleep = vi.fn(async (milliseconds: number) => {
      clock += milliseconds;
    });

    await expect(
      waitForProfileRestart("account-2", {
        timeoutMs: 5000,
        intervalMs: 100,
        probe,
        sleep: fakeSleep,
        now: () => clock,
        reload,
      }),
    ).resolves.toEqual(targetPayload);

    expect(probe).toHaveBeenCalledTimes(3);
    expect(fakeSleep).toHaveBeenCalledTimes(2);
    expect(reload).toHaveBeenCalledOnce();
  });

  it("times out without reloading when the service does not recover", async () => {
    const { waitForProfileRestart } = await import("./index");
    const probe = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const reload = vi.fn();
    let clock = 0;

    await expect(
      waitForProfileRestart("account-2", {
        timeoutMs: 250,
        intervalMs: 100,
        probe,
        sleep: async (milliseconds: number) => {
          clock += milliseconds;
        },
        now: () => clock,
        reload,
      }),
    ).rejects.toThrow("自动重新启动超时");

    expect(probe).toHaveBeenCalledTimes(3);
    expect(reload).not.toHaveBeenCalled();
  });

  it("blocks duplicate switch submissions and shows the persistent restart state", async () => {
    let resolveConfirm: ((confirmed: boolean) => void) | undefined;
    openAppConfirmMock.mockImplementation(
      () =>
        new Promise<boolean>((resolve) => {
          resolveConfirm = resolve;
        }),
    );
    fetchJSONMock.mockImplementation(
      (url: string, options?: { method?: string; timeoutMs?: number }) => {
        if (url.endsWith("/switch")) return Promise.resolve({ ok: true });
        if (url === "/api/profiles" && options?.timeoutMs) {
          return new Promise(() => undefined);
        }
        return Promise.resolve(payload);
      },
    );

    const { bindProfiles, loadProfiles } = await import("./index");
    bindProfiles();
    await loadProfiles();
    const button = document.querySelector(
      '[data-profile-activate="account-2"]',
    ) as HTMLButtonElement;

    button.click();
    button.click();
    expect(openAppConfirmMock).toHaveBeenCalledOnce();

    resolveConfirm?.(true);
    await vi.waitFor(() => {
      expect(
        fetchJSONMock.mock.calls.filter(
          ([url]) => url === "/api/profiles/account-2/switch",
        ),
      ).toHaveLength(1);
    });

    expect(button.disabled).toBe(true);
    expect(button.textContent).toBe("正在切换...");
    const notice = document.getElementById("profile-restart-notice") as HTMLElement;
    expect(notice.hidden).toBe(false);
    expect(notice.textContent).toBe("正在切换账号并重新启动 Binggo...");

    button.click();
    expect(
      fetchJSONMock.mock.calls.filter(
        ([url]) => url === "/api/profiles/account-2/switch",
      ),
    ).toHaveLength(1);
  });
});
