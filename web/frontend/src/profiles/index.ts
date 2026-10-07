import { fetchJSON } from "../api/client";
import { openAppConfirm } from "../shell/confirm";
import { showToast } from "../shell/toast";
import { setButtonLoading } from "../utils/motion";
import { escapeHtml, sanitizeUserText } from "../utils/text";

type Profile = {
  profile_id: string;
  mid: string;
  nickname: string;
  running?: boolean;
  runtime_ready?: boolean;
  runtime_port?: number | null;
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

function displayName(profile: Profile): string {
  return profile.nickname || `账号 ${profile.profile_id}`;
}

function renderProfiles(payload: ProfilesPayload): void {
  currentPayload = payload;

  if (profileRestartNotice) {
    profileRestartNotice.hidden = !payload.restart_required;
  }
  if (!profileList) return;
  profileList.innerHTML = payload.profiles
    .map((profile) => {
      const isRuntime = profile.profile_id === payload.runtime_profile_id;
      const isActive = profile.profile_id === payload.active_profile_id;
      const isRunningElsewhere = !isRuntime && Boolean(profile.running);
      const deleteDisabled = isRuntime || isActive || isRunningElsewhere;
      const switchDisabled = isRuntime && isActive;
      const switchLabel = switchDisabled
        ? "已选择"
        : isActive && !isRunningElsewhere
          ? "重启生效"
          : "切换账号";
      const badges = [
        isRuntime ? '<span class="profile-badge is-runtime">当前运行</span>' : "",
        isActive && !isRuntime
          ? '<span class="profile-badge is-next">下次启动</span>'
          : "",
        isRunningElsewhere ? '<span class="profile-badge is-runtime">其他窗口运行中</span>' : "",
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
            ${isRuntime ? "" : `<button type="button" class="btn btn-secondary btn-compact btn-pill" data-profile-activate="${escapeHtml(profile.profile_id)}" ${switchDisabled ? "disabled" : ""}>${switchLabel}</button>`}
            ${isRuntime ? "" : isRunningElsewhere
              ? `<button type="button" class="btn btn-secondary btn-compact btn-pill" data-profile-open="${escapeHtml(profile.profile_id)}" ${profile.runtime_ready ? "" : "disabled"}>${profile.runtime_ready ? "打开窗口" : "启动中..."}</button>`
              : `<button type="button" class="btn btn-secondary btn-compact btn-pill" data-profile-launch="${escapeHtml(profile.profile_id)}">新窗口运行</button>`}
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
  if (profile.running && profile.profile_id !== currentPayload?.runtime_profile_id) {
    showToast("该账号已经在其它窗口运行，请先退出该窗口或点击“打开窗口”。", "info");
    return;
  }
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

function openProfileWindow(profileId: string): void {
  const profile = currentPayload?.profiles.find((item) => item.profile_id === profileId);
  const port = profile?.runtime_port;
  if (!profile?.runtime_ready || !port || !Number.isInteger(port)) {
    showToast("目标窗口尚未就绪，请稍后刷新账号列表。", "info");
    return;
  }
  const url = `http://127.0.0.1:${port}/`;
  const opened = window.open(url, "_blank");
  if (!opened) showToast(`浏览器阻止了新窗口，请手动访问 ${url}`, "info");
}

async function launchProfile(profileId: string, button: HTMLButtonElement): Promise<void> {
  if (mutating) return;
  mutating = true;
  setButtonLoading(button, true, { label: "正在启动..." });
  try {
    const result = await fetchJSON<{
      already_running: boolean;
      ready: boolean;
      url: string;
      browser_opened?: boolean;
    }>(`/api/profiles/${encodeURIComponent(profileId)}/launch`, {
      method: "POST", timeoutMs: 60000,
    });
    await loadProfiles();
    if (result.ready && !result.already_running && result.browser_opened) {
      showToast("账号已在新窗口启动。", "success");
    } else if (result.ready) {
      showToast(`账号已就绪，请点击“打开窗口”；地址：${result.url}`, "info");
    } else {
      showToast("该账号正在启动，请稍后点击“打开窗口”。", "info");
    }
  } catch (error) {
    showToast(errorMessage(error), "error");
    await loadProfiles().catch(() => undefined);
  } finally {
    mutating = false;
    setButtonLoading(button, false);
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
    const launchId = target.dataset.profileLaunch;
    const openId = target.dataset.profileOpen;
    const deleteId = target.dataset.profileDelete;
    if (activateId) activateProfile(activateId, target).catch(() => undefined);
    if (launchId) launchProfile(launchId, target).catch(() => undefined);
    if (openId) openProfileWindow(openId);
    if (deleteId) deleteProfile(deleteId).catch(() => undefined);
  });
}


// ---------- M7：扫码登录新账号（server-side onboarding，不再要求先建空 Profile） ----------

const onboardingStartBtn = document.getElementById("profile-onboarding-start") as HTMLButtonElement | null;

const ONBOARDING_POLL_INTERVAL_MS = 2000;
const ONBOARDING_MAX_POLL_ROUNDS = 120;
const ONBOARDING_QR_TIMEOUT_MS = 15000;
const ONBOARDING_APP_HINT = "请使用哔哩哔哩 App 扫码，并在手机端确认登录";

let onboardingFlowToken = 0;

type ApiEnvelope<T> = { ok?: boolean; result?: T };

type OnboardingPollResult = {
  onboarding_id?: string;
  status?: string;
  reason?: string;
};

type OnboardingCompleteResult = {
  profile_id?: string;
  created_new?: boolean;
  reused_existing?: boolean;
  display_name?: string;
  uid?: string;
  restart_required?: boolean;
  already_running?: boolean;
};

function errorMessageOnboarding(error: unknown): string {
  return String(error && typeof error === "object" && "message" in error ? (error as { message: string }).message : error);
}

/** Backend onboarding routes 返回 {ok, result}；这里强制解包，缺 result 视为协议错误。 */
function readEnvelopeResult<T>(response: ApiEnvelope<T> | null | undefined, action: string): T {
  const result = response && typeof response === "object" ? response.result : undefined;
  if (!result || typeof result !== "object") {
    throw new Error(`${action}失败：响应缺少 result`);
  }
  return result;
}

async function sleepOnboarding(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export async function runOnboardingFlow(): Promise<void> {
  if (mutating) return;
  mutating = true;
  const flowToken = ++onboardingFlowToken;
  if (onboardingStartBtn) onboardingStartBtn.disabled = true;
  let closeRequested = false;
  let closeResolve: (() => void) | null = null;
  const closeSignal = new Promise<void>((resolve) => {
    closeResolve = resolve;
  });
  const requestClose = (): void => {
    if (closeRequested) return;
    closeRequested = true;
    onboardingFlowToken += 1; // 本 flow 所有 in-flight 结果立即失效
    closeResolve?.();
  };
  const isActive = (modal: HTMLElement): boolean =>
    flowToken === onboardingFlowToken && !closeRequested && document.body.contains(modal);

  try {
    const startResponse = await fetchJSON<ApiEnvelope<{ onboarding_id: string; qr_url: string }>>(
      "/api/accounts/onboarding/start",
      { method: "POST" },
    );
    const started = readEnvelopeResult(startResponse, "创建扫码会话");
    if (!started.onboarding_id) {
      throw new Error("创建扫码会话失败：响应缺少 onboarding_id");
    }
    const onboardingId = started.onboarding_id;
    document.querySelectorAll(".onboarding-qr-modal").forEach((element) => element.remove());
    showToast(ONBOARDING_APP_HINT, "info");
    const modal = document.createElement("div");
    modal.className = "onboarding-qr-modal";
    modal.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:99";
    modal.innerHTML =
      `<div class="panel" style="min-width:320px;text-align:center;padding:18px">
         <h3 class="section-title">扫码登录新账号</h3>
         <img id="onboarding-qr-img" alt="登录二维码" style="width:220px;height:220px;image-rendering:pixelated" />
         <p class="caption" id="onboarding-status">${ONBOARDING_APP_HINT}</p>
         <button type="button" class="btn btn-ghost btn-compact btn-pill" id="onboarding-close">取消</button>
       </div>`;
    document.body.appendChild(modal);
    const img = modal.querySelector("#onboarding-qr-img") as HTMLImageElement | null;
    const statusEl = modal.querySelector("#onboarding-status");
    const closeBtn = modal.querySelector("#onboarding-close");
    closeBtn?.addEventListener("click", () => {
      requestClose();
      modal.remove();
    }, { once: true });
    const qrReady = await new Promise<boolean>((resolve) => {
      let settled = false;
      const finish = (ready: boolean): void => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeoutId);
        resolve(ready);
      };
      const fail = (message: string): void => {
        if (statusEl) statusEl.textContent = message;
        showToast(message, "error");
        finish(false);
      };
      const timeoutId = window.setTimeout(
        () => fail("二维码加载超时，请关闭后重试。"),
        ONBOARDING_QR_TIMEOUT_MS,
      );
      closeBtn?.addEventListener("click", () => {
        modal.remove();
        finish(false);
      }, { once: true });
      if (!img) {
        fail("二维码显示组件不可用，请关闭后重试。");
        return;
      }
      img.addEventListener("load", () => finish(true), { once: true });
      img.addEventListener(
        "error",
        () => fail("二维码加载失败，请关闭后重试。"),
        { once: true },
      );
      img.src = `/api/accounts/onboarding/${encodeURIComponent(onboardingId)}/qr`;
    });
    if (!qrReady || !isActive(modal)) return;

    // 二维码确认展示后才轮询；只使用 onboarding_id，绝不接触 Cookie/UID。
    let result: { profile_id: string; created_new: boolean; already_running: boolean } | null = null;
    let terminal = false;
    const failOnboarding = (message: string, reason = ""): void => {
      terminal = true;
      if (statusEl) statusEl.textContent = message;
      modal.querySelector("#onboarding-qr-img")?.remove();
      showToast(reason || message, "error");
    };
    for (let round = 0; round < ONBOARDING_MAX_POLL_ROUNDS && !terminal; round += 1) {
      await Promise.race([sleepOnboarding(ONBOARDING_POLL_INTERVAL_MS), closeSignal]);
      if (!isActive(modal)) return;
      const pollRaced = await Promise.race([
        fetchJSON<ApiEnvelope<OnboardingPollResult>>(
          `/api/accounts/onboarding/${encodeURIComponent(onboardingId)}/poll`,
          { method: "POST" },
        ).then((value) => ({ kind: "poll" as const, value })),
        closeSignal.then(() => ({ kind: "closed" as const })),
      ]);
      if (pollRaced.kind !== "poll" || !isActive(modal)) return;
      const poll = readEnvelopeResult(pollRaced.value, "登录状态查询");
      const status = String(poll.status || "");
      if (status === "success") {
        if (statusEl) statusEl.textContent = "登录成功，正在完成账号创建…";
        let completed: OnboardingCompleteResult | null = null;
        try {
          const completeRaced = await Promise.race([
            fetchJSON<ApiEnvelope<OnboardingCompleteResult>>(
              "/api/accounts/onboarding/complete",
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ onboarding_id: onboardingId }),
              },
            ).then((value) => ({ kind: "complete" as const, value })),
            closeSignal.then(() => ({ kind: "closed" as const })),
          ]);
          if (completeRaced.kind !== "complete" || !isActive(modal)) return;
          completed = readEnvelopeResult(completeRaced.value, "完成登录");
        } catch (error) {
          failOnboarding("账号创建失败，请重新获取二维码后重试", errorMessageOnboarding(error));
          break;
        }
        const profileId = String(completed?.profile_id || "").trim();
        if (!profileId) {
          failOnboarding("账号创建失败：响应缺少 profile_id，请重新获取二维码后重试");
          break;
        }
        onboardingFlowToken += 1; // 成功：使任何陈旧续作失效
        result = {
          profile_id: profileId,
          created_new: Boolean(completed?.created_new),
          already_running: Boolean(completed?.already_running),
        };
        terminal = true;
        if (statusEl) {
          statusEl.textContent = result.created_new
            ? "账号创建成功，可在列表中点击“新窗口运行”。"
            : result.already_running
              ? "该账号已在其他窗口运行，未覆盖 Cookie；请点击“打开窗口”。"
              : "该 B站账号已存在，登录状态已更新。";
        }
      } else if (status === "scanned") {
        if (statusEl) statusEl.textContent = "已扫码，请在手机端确认登录";
      } else if (status === "waiting") {
        // 正常等待：不提示错误
      } else if (status === "expired") {
        terminal = true;
        if (statusEl) statusEl.textContent = "二维码已过期，请重新获取";
        modal.querySelector("#onboarding-qr-img")?.remove();
      } else if (status === "failed") {
        terminal = true;
        if (statusEl) statusEl.textContent = "登录失败，请重新获取二维码";
        modal.querySelector("#onboarding-qr-img")?.remove();
      } else {
        throw new Error("登录状态查询失败：响应缺少有效状态");
      }
    }
    if (!terminal) {
      if (statusEl) statusEl.textContent = "登录超时，请重新获取二维码";
      showToast("登录超时，请重新获取二维码", "error");
    }
    window.setTimeout(() => modal.remove(), 2500);
    if (result) {
      showToast(result.already_running
        ? "该账号已在其他窗口运行，未覆盖 Cookie；请点击“打开窗口”。"
        : "账号已就绪，可点击“新窗口运行”。", "success");
      try {
        await loadProfiles();
      } catch {
        // Profile 已持久化；列表刷新失败不改变账号状态。
      }
    }
  } catch (error) {
    showToast(errorMessageOnboarding(error), "error");
  } finally {
    mutating = false;
    if (onboardingStartBtn) onboardingStartBtn.disabled = false;
  }
}
