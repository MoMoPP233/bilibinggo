import { fetchJSON } from "../api/client";
import { state } from "../state";
import { openAppConfirm } from "../shell/confirm";
import { showToast } from "../shell/toast";
import { setButtonLoading } from "../utils/motion";
import { escapeHtml, sanitizeUserText } from "../utils/text";

type Profile = {
  profile_id: string;
  mid: string;
  nickname: string;
};

type ProfilesPayload = {
  runtime_profile_id: string;
  active_profile_id: string;
  restart_required: boolean;
  data_root: string;
  profiles: Profile[];
};

type JobSnapshot = {
  action?: string;
  state?: string;
} | null | undefined;

type RestartWaitOptions = {
  timeoutMs?: number;
  intervalMs?: number;
  probe?: () => Promise<ProfilesPayload>;
  sleep?: (milliseconds: number) => Promise<void>;
  now?: () => number;
  reload?: () => void;
};

const PROFILE_RESTART_TIMEOUT_MS = 60000;
const PROFILE_RESTART_POLL_INTERVAL_MS = 650;
const PROFILE_RESTART_PROBE_TIMEOUT_MS = 2000;

const profileCurrent = document.getElementById("profile-current");
const profileList = document.getElementById("profile-list");
const profileRestartNotice = document.getElementById("profile-restart-notice");

let currentPayload: ProfilesPayload | null = null;
let mutating = false;

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function showRestartNotice(message: string): void {
  if (!profileRestartNotice) return;
  profileRestartNotice.textContent = message;
  profileRestartNotice.hidden = false;
}

export async function waitForProfileRestart(
  profileId: string,
  options: RestartWaitOptions = {},
): Promise<ProfilesPayload> {
  const timeoutMs = options.timeoutMs ?? PROFILE_RESTART_TIMEOUT_MS;
  const intervalMs = options.intervalMs ?? PROFILE_RESTART_POLL_INTERVAL_MS;
  const probe =
    options.probe ??
    (() =>
      fetchJSON<ProfilesPayload>("/api/profiles", {
        timeoutMs: PROFILE_RESTART_PROBE_TIMEOUT_MS,
      }));
  const wait = options.sleep ?? sleep;
  const now = options.now ?? Date.now;
  const reload = options.reload ?? (() => window.location.reload());
  const startedAt = now();

  while (now() - startedAt < timeoutMs) {
    try {
      const payload = await probe();
      if (
        payload.runtime_profile_id === profileId &&
        payload.active_profile_id === profileId
      ) {
        reload();
        return payload;
      }
    } catch {
      // 旧进程退出、新进程绑定端口期间的连接失败属于预期状态。
    }

    const remainingMs = timeoutMs - (now() - startedAt);
    if (remainingMs <= 0) break;
    await wait(Math.min(intervalMs, remainingMs));
  }

  throw new Error("Binggo 自动重新启动超时，请手动重新启动后刷新页面。");
}

function errorMessage(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  return sanitizeUserText(raw) || "Profile 操作失败";
}

function accountValue(key: "uname" | "mid"): string {
  const value = state.account?.[key];
  return value === undefined || value === null ? "" : String(value).trim();
}

function displayName(profile: Profile): string {
  return profile.nickname || `账号 ${profile.profile_id}`;
}

function renderProfiles(payload: ProfilesPayload): void {
  currentPayload = payload;
  const runtime = payload.profiles.find(
    (profile) => profile.profile_id === payload.runtime_profile_id,
  );
  const nickname = runtime?.nickname || accountValue("uname") || "尚未登录";
  const mid = runtime?.mid || accountValue("mid") || "—";

  if (profileRestartNotice) {
    profileRestartNotice.hidden = !payload.restart_required;
  }
  if (profileCurrent) {
    profileCurrent.innerHTML = `
      <p class="profile-current-label">当前运行 Profile</p>
      <div class="profile-current-grid">
        <div class="profile-field"><span>昵称</span><strong>${escapeHtml(nickname)}</strong></div>
        <div class="profile-field"><span>MID</span><strong>${escapeHtml(mid)}</strong></div>
        <div class="profile-field"><span>Profile ID</span><strong>${escapeHtml(payload.runtime_profile_id)}</strong></div>
      </div>
      <p class="caption profile-data-root" title="${escapeHtml(payload.data_root)}">数据目录：${escapeHtml(payload.data_root)}</p>`;
  }
  if (!profileList) return;
  profileList.innerHTML = payload.profiles
    .map((profile) => {
      const isRuntime = profile.profile_id === payload.runtime_profile_id;
      const isActive = profile.profile_id === payload.active_profile_id;
      const deleteDisabled = isRuntime || isActive;
      const switchDisabled = isRuntime && isActive;
      const switchLabel = switchDisabled
        ? "已选择"
        : isActive
          ? "重启生效"
          : "切换账号";
      const badges = [
        isRuntime ? '<span class="profile-badge is-runtime">当前运行</span>' : "",
        isActive && !isRuntime
          ? '<span class="profile-badge is-next">下次启动</span>'
          : "",
      ].join("");
      return `
        <div class="profile-row${isRuntime ? " is-runtime" : ""}">
          <div class="profile-row-copy">
            <div class="profile-row-title">
              <strong>${escapeHtml(displayName(profile))}</strong>
              <span class="profile-badges">${badges}</span>
            </div>
            <p>${escapeHtml(profile.profile_id)} · MID ${escapeHtml(profile.mid || "—")}</p>
          </div>
          <div class="profile-row-actions">
            <button type="button" class="btn btn-secondary btn-compact btn-pill" data-profile-activate="${escapeHtml(profile.profile_id)}" ${switchDisabled ? "disabled" : ""}>${switchLabel}</button>
            <button type="button" class="btn btn-ghost btn-compact btn-pill profile-delete" data-profile-delete="${escapeHtml(profile.profile_id)}" ${deleteDisabled ? "disabled" : ""}>删除</button>
          </div>
        </div>`;
    })
    .join("");
}

export async function loadProfiles(): Promise<ProfilesPayload> {
  const payload = await fetchJSON<ProfilesPayload>("/api/profiles");
  renderProfiles(payload);
  return payload;
}

export async function syncProfilesAfterLoginJob(job: JobSnapshot): Promise<boolean> {
  if (job?.action !== "login" || job?.state !== "success") return false;
  await loadProfiles();
  return true;
}

async function activateProfile(
  profileId: string,
  button: HTMLButtonElement | null,
): Promise<void> {
  const profile = currentPayload?.profiles.find((item) => item.profile_id === profileId);
  if (!profile || mutating) return;
  mutating = true;
  let restartRequested = false;
  let restartReady = false;
  try {
    const confirmed = await openAppConfirm({
      eyebrow: "账号 Profile",
      title: `确定切换到 ${displayName(profile)} 吗？`,
      desc: "Binggo 将自动重新启动。当前正在运行的账号将安全退出，重启后加载目标账号的数据与登录状态。",
      confirmLabel: "切换并重启",
      cancelLabel: "取消",
    });
    if (!confirmed) return;

    setButtonLoading(button, true, { label: "正在切换..." });
    await fetchJSON(`/api/profiles/${encodeURIComponent(profileId)}/switch`, {
      method: "POST",
    });
    restartRequested = true;
    showRestartNotice("正在切换账号并重新启动 Binggo...");
    await waitForProfileRestart(profileId);
    restartReady = true;
  } catch (error) {
    if (restartRequested) {
      showRestartNotice(
        "自动切换未完成。若 Binggo 未自动恢复，请手动重新启动后刷新页面。",
      );
    }
    showToast(errorMessage(error), "error");
  } finally {
    if (!restartReady) {
      mutating = false;
      setButtonLoading(button, false);
    }
  }
}

async function deleteProfile(profileId: string): Promise<void> {
  const profile = currentPayload?.profiles.find((item) => item.profile_id === profileId);
  if (!profile || mutating) return;
  const confirmed = await openAppConfirm({
    eyebrow: "删除账号 Profile",
    title: `永久删除 ${displayName(profile)}？`,
    desc: "该 Profile 的数据库、Cookie 和参与状态都会被删除，此操作无法撤销。",
    confirmLabel: "确认删除",
    cancelLabel: "取消",
    danger: true,
  });
  if (!confirmed) return;

  mutating = true;
  try {
    await fetchJSON(`/api/profiles/${encodeURIComponent(profileId)}`, {
      method: "DELETE",
    });
    await loadProfiles();
    showToast(`已删除 ${profileId}`, "success");
  } catch (error) {
    showToast(errorMessage(error), "error");
  } finally {
    mutating = false;
  }
}

export function bindProfiles(): void {
  onboardingStartBtn?.addEventListener("click", () => {
    runOnboardingFlow().catch(() => undefined);
  });
  profileList?.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest("button") : null;
    if (!(target instanceof HTMLButtonElement) || target.disabled) return;
    const activateId = target.dataset.profileActivate;
    const deleteId = target.dataset.profileDelete;
    if (activateId) activateProfile(activateId, target).catch(() => undefined);
    if (deleteId) deleteProfile(deleteId).catch(() => undefined);
  });
}


// ---------- M7：扫码登录新账号（server-side onboarding，不再要求先建空 Profile） ----------

const onboardingStartBtn = document.getElementById("profile-onboarding-start") as HTMLButtonElement | null;

function errorMessageOnboarding(error: unknown): string {
  return String(error && typeof error === "object" && "message" in error ? (error as { message: string }).message : error);
}

async function sleepOnboarding(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export async function runOnboardingFlow(): Promise<void> {
  if (mutating) return;
  mutating = true;
  if (onboardingStartBtn) onboardingStartBtn.disabled = true;
  let onboardingId = "";
  try {
    const started = await fetchJSON<{ onboarding_id: string; qr_url: string }>(
      "/api/accounts/onboarding/start",
      { method: "POST" },
    );
    onboardingId = started.onboarding_id;
    showToast("请在浏览器中完成扫码", "info");
    const modal = document.createElement("div");
    modal.className = "onboarding-qr-modal";
    modal.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:99";
    modal.innerHTML =
      `<div class="panel" style="min-width:320px;text-align:center;padding:18px">
         <h3 class="section-title">扫码登录新账号</h3>
         <img id="onboarding-qr-img" alt="登录二维码" style="width:220px;height:220px;image-rendering:pixelated" />
         <p class="caption" id="onboarding-status">等待扫码…</p>
         <button type="button" class="btn btn-ghost btn-compact btn-pill" id="onboarding-close">取消</button>
       </div>`;
    document.body.appendChild(modal);
    const img = modal.querySelector("#onboarding-qr-img") as HTMLImageElement | null;
    if (img) img.src = `/api/accounts/onboarding/${encodeURIComponent(onboardingId)}/qr`;
    const statusEl = modal.querySelector("#onboarding-status");
    modal.querySelector("#onboarding-close")?.addEventListener("click", () => {
      modal.remove();
      if (onboardingStartBtn) onboardingStartBtn.disabled = false;
      mutating = false;
    });

    // 轮询直到 scanned，再 complete；只使用 onboarding_id，绝不接触 Cookie/UID。
    let result: { profile_id: string; created_new: boolean; reused_existing: boolean } | null = null;
    let terminal = false;
    for (let round = 0; round < 120 && !terminal; round += 1) {
      await sleepOnboarding(2000);
      if (!document.body.contains(modal)) break;
      const poll = await fetchJSON<{ status: string; need_confirm?: boolean }>(
        `/api/accounts/onboarding/${encodeURIComponent(onboardingId)}/poll`,
        { method: "POST" },
      );
      if (poll.status === "scanned") {
        if (statusEl) statusEl.textContent = "扫码成功，正在确认…";
        const completed = await fetchJSON<{
          profile_id: string;
          created_new: boolean;
          reused_existing: boolean;
          display_name: string;
          uid: string;
          restart_required: boolean;
        }>(`/api/accounts/onboarding/${encodeURIComponent(onboardingId)}/complete`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ onboarding_id: onboardingId }),
        });
        result = completed;
        terminal = true;
        if (statusEl) {
          statusEl.textContent = completed.created_new
            ? "账号登录成功，正在创建独立账号数据…正在切换账号并重启…"
            : "该 B站账号已经存在，正在更新登录状态并切换…";
        }
      } else if (poll.status === "failed" || poll.status === "expired") {
        terminal = true;
        if (statusEl) statusEl.textContent = "登录失败或二维码已过期，请重试。";
        modal.querySelector("#onboarding-qr-img")?.remove();
      }
    }
    window.setTimeout(() => modal.remove(), 2500);
    if (result) {
      // 触发现有安全 restart（不热切）；重启后新 runtime 绑定目标 Profile。
      await fetchJSON(`/api/profiles/${encodeURIComponent(result.profile_id)}/switch`, {
        method: "POST",
      }).catch(() => undefined);
      showToast("账号已就绪，正在切换并重启…", "success");
      await loadProfiles();
      window.setTimeout(() => window.location.reload(), 3000);
    }
  } catch (error) {
    showToast(errorMessageOnboarding(error), "error");
  } finally {
    mutating = false;
    if (onboardingStartBtn) onboardingStartBtn.disabled = false;
  }
}
