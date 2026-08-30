// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchJSONMock } = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(),
}));

vi.mock("../api/client", () => ({
  fetchJSON: fetchJSONMock,
}));

import { syncProfilesAfterLoginJob } from "./index";

const payload = {
  runtime_profile_id: "account-1",
  active_profile_id: "account-1",
  restart_required: false,
  data_root: "D:\\BinggoData",
  profiles: [
    {
      profile_id: "account-1",
      mid: "123456",
      nickname: "测试账号",
    },
  ],
};

describe("syncProfilesAfterLoginJob", () => {
  beforeEach(() => {
    fetchJSONMock.mockReset();
    fetchJSONMock.mockResolvedValue(payload);
  });

  it("reloads profiles after a successful login job", async () => {
    await expect(
      syncProfilesAfterLoginJob({ action: "login", state: "success" }),
    ).resolves.toBe(true);

    expect(fetchJSONMock).toHaveBeenCalledOnce();
    expect(fetchJSONMock).toHaveBeenCalledWith("/api/profiles");
  });

  it("does not reload profiles for other job outcomes", async () => {
    await expect(
      syncProfilesAfterLoginJob({ action: "login", state: "error" }),
    ).resolves.toBe(false);
    await expect(
      syncProfilesAfterLoginJob({ action: "refresh_all", state: "success" }),
    ).resolves.toBe(false);

    expect(fetchJSONMock).not.toHaveBeenCalled();
  });
});
