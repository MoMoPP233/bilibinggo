// 账号 UI 刷新决策：仅依赖本地 runtime 状态，避免瞬时 NAV 失败把已登录显示切走。

export interface AccountLike {
  logged_in?: boolean;
  expired?: boolean;
  network_error?: boolean;
  cookie_saved?: boolean;
}

export function isTransientAccountOffline(profile: AccountLike | null | undefined): boolean {
  if (!profile) return false;
  return Boolean(profile.network_error === true && profile.cookie_saved === true && !profile.logged_in);
}

export function shouldPreserveLoggedInSnapshot(
  currentAccount: AccountLike | null | undefined,
  incomingProfile: AccountLike | null | undefined,
): boolean {
  if (!isTransientAccountOffline(incomingProfile)) return false;
  return Boolean(currentAccount && currentAccount.logged_in && !currentAccount.expired);
}
