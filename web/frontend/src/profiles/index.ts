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

const profileCreate = document.getElementById("profile-create") as HTMLButtonElement | null;
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

async function createProfile(): Promise<void> {
  if (mutating) return;
  mutating = true;
  if (profileCreate) profileCreate.disabled = true;
  try {
    const result = await fetchJSON<{ profile: Profile }>("/api/profiles", {
      method: "POST",
    });
    await loadProfiles();
    showToast(`已创建 ${result.profile.profile_id}`, "success", "切换后重启 Binggo 即可登录新账号");
  } catch (error) {
    showToast(errorMessage(error), "error");
  } finally {
    mutating = false;
    if (profileCreate) profileCreate.disabled = false;
  }
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
  profileCreate?.addEventListener("click", () => {
    createProfile().catch(() => undefined);
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
