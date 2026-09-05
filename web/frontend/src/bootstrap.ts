// @ts-nocheck
/* eslint-disable */
/** Migrated from web/static/app.js — logic preserved. */

import { state } from "./state";
import { bindOnboardingPanel, loadAccount, loadAccountExtras, logoutAccount, requestLogoutConfirm, syncProjectState } from "./account/index";
import { bindFilterPills, loadActivities, loadSummary } from "./activities/index";
import { bindAutoDock, fetchAutoStatus, setAutoDockOpen } from "./auto/index";
import { sidebarLogoutBtn, sidebarRefreshBtn } from "./dom";
import { bindActionButtons, setLogDockOpen, startPolling } from "./jobs/index";
import { startRealtime } from "./realtime/sse";
import { bindLlmApiKeyToggle, bindParticipateSettings, bindSettingsDirtyTracking, loadSettings, refreshLlmSettings, resetParticipateText, saveLlmSettings, saveParticipateText, testLlmSettings } from "./settings/index";
import { bindNavigation } from "./shell/nav";
import { initSystemPreferences } from "./shell/theme";
import { showToast } from "./shell/toast";
import { playOverviewEnter, setButtonLoading } from "./utils/motion";
import { sanitizeUserText } from "./utils/text";
import { bindDiagnosticsExport } from "./diagnostics/index";
import { bindCheckUpdates, loadRuntimeInfo } from "./runtime/index";
import { bindWatchUsers, loadWatchUsers } from "./watch/index";
import { bindProfiles, loadProfiles } from "./profiles/index";
import { bindRepostCleanup } from "./repost-cleanup/index";
import { bindUpdateAllDatasources } from "./sources/update-all";

/**
 * 单步隔离执行器：只阻断该步自身，不向后续初始化传播异常。
 * 不产生全局 toast；失败以 [bootstrap] 前缀 console.warn 记录，便于诊断。
 */
function runBootstrapStep(name, task) {
  return Promise.resolve()
    .then(task)
    .catch((error) => {
      console.warn(
        `[bootstrap] ${name} failed:`,
        error?.message || error
      );
      return undefined;
    });
}

export async function init() {
  initSystemPreferences();
  setLogDockOpen(false);
  setAutoDockOpen(false);
  bindNavigation();
  bindAutoDock();
  bindFilterPills();
  bindParticipateSettings();
  bindSettingsDirtyTracking();
  bindLlmApiKeyToggle();
  bindWatchUsers();
  bindProfiles();
  bindRepostCleanup();
  bindUpdateAllDatasources();
  bindOnboardingPanel();
  bindActionButtons();
  bindDiagnosticsExport();
  bindCheckUpdates();
  loadRuntimeInfo().catch(() => {});
  // 账号/设置：login-state-v1 语义在 loadAccount 内部（latest-wins/快照保留），
  // 此处只隔离意外抛错，绝不自行写 logged_in/expired。
  await runBootstrapStep("account+settings", () => syncProjectState());
  await runBootstrapStep("profiles", () => loadProfiles());

  // summary / auto status / watch / activities 彼此独立：任一失败都不阻断其它模块。
  const summaryResult = await runBootstrapStep("summary", async () => {
    const job = await loadSummary();
    if (job) {
      state.currentJob = job;
    }
    return { ok: true, job: job ?? null };
  });
  const summaryFailed = !summaryResult?.ok;
  const seededJob = summaryResult?.job ?? null;
  await runBootstrapStep("auto status", () => fetchAutoStatus());
  await runBootstrapStep("watch users", () => loadWatchUsers());
  await runBootstrapStep("activities", () => loadActivities());

  // Realtime 不再依赖 summary 成功：无论数据模块成败都尝试建立连接。
  try {
    startRealtime({ initial: true });
  } catch (error) {
    console.warn("[bootstrap] realtime start failed:", error?.message || error);
  }
  if (seededJob?.state === "running") {
    startPolling();
  } else if (summaryFailed) {
    // summary 失败时用 /api/jobs/current 补一次权威种子（仅在失败路径，
    // 成功路径不新增请求），避免“运行中任务但没有任何通道知道它”。
    await runBootstrapStep("current job seed", async () => {
      const { fetchJSON } = await import("./api/client");
      const current = await fetchJSON("/api/jobs/current");
      const { acceptJobUpdate, updateJobUI } = await import("./jobs/index");
      if (acceptJobUpdate(current)) {
        state.currentJob = current;
        if (current?.state === "running") {
          updateJobUI(current);
          startPolling();
        }
      }
    });
  }
  playOverviewEnter();
}

document.getElementById("refresh-llm-settings")?.addEventListener("click", () => {
  refreshLlmSettings().catch(() => {});
});

document.getElementById("test-llm-settings")?.addEventListener("click", () => {
  testLlmSettings().catch(() => {});
});

document.getElementById("save-llm-settings")?.addEventListener("click", () => {
  saveLlmSettings().catch(() => {});
});

document.getElementById("save-participate-text")?.addEventListener("click", () => {
  saveParticipateText().catch(() => {});
});

document.getElementById("reset-participate-text")?.addEventListener("click", () => {
  resetParticipateText().catch(() => {});
});

sidebarRefreshBtn?.addEventListener("click", async () => {
  setButtonLoading(sidebarRefreshBtn, true, { label: "刷新中…" });
  try {
    const account = await loadAccount();
    const merged = (await loadAccountExtras()) || account;
    await loadSettings();
    if (!merged?.at_alert?.increased) {
      showToast("状态已同步", "success");
    }
  } catch (error) {
    showToast(String(error.message || error), "error");
  } finally {
    setButtonLoading(sidebarRefreshBtn, false);
  }
});

sidebarLogoutBtn?.addEventListener("click", async () => {
  const confirmed = await requestLogoutConfirm();
  if (!confirmed) return;
  sidebarLogoutBtn.disabled = true;
  try {
    await logoutAccount();
  } catch (error) {
    showToast(String(error.message || error), "error");
  } finally {
    sidebarLogoutBtn.disabled = false;
  }
});


window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  syncProjectState().catch((error) => {
    showToast(String(error.message || error), "error");
  });
});
