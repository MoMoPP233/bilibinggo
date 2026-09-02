import { describe, expect, it } from "vitest";
import {
  isTransientAccountOffline,
  shouldPreserveLoggedInSnapshot,
} from "./account-preserve";

describe("account UI preserve decision", () => {
  it("classifies transient network/risk offline profile without local cookie loss", () => {
    expect(
      isTransientAccountOffline({
        logged_in: false,
        expired: true,
        network_error: true,
        cookie_saved: true,
      }),
    ).toBe(true);
  });

  it("does not classify real expiry or missing cookie as transient", () => {
    expect(
      isTransientAccountOffline({
        logged_in: false,
        expired: true,
        network_error: false,
        cookie_saved: false,
      }),
    ).toBe(false);
    expect(isTransientAccountOffline({ logged_in: false })).toBe(false);
    expect(isTransientAccountOffline(null)).toBe(false);
  });

  it("preserves a logged-in snapshot on transient offline only", () => {
    const loggedIn = { logged_in: true, expired: false, mid: 1 };
    const offline = { logged_in: false, network_error: true, cookie_saved: true };
    expect(shouldPreserveLoggedInSnapshot(loggedIn, offline)).toBe(true);
    expect(shouldPreserveLoggedInSnapshot(loggedIn, { logged_in: false })).toBe(false);
    expect(shouldPreserveLoggedInSnapshot(null, offline)).toBe(false);
    expect(shouldPreserveLoggedInSnapshot({ logged_in: false, expired: true }, offline)).toBe(false);
  });
});
