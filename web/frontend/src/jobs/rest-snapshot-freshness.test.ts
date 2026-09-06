// @vitest-environment jsdom
/* eslint-disable */
// V19：无 server seq 的 REST /api/jobs/current 快照本地新鲜度（generation + authority）。
// 全 mock fetch（deferred Promise 控制时序），无真实网络、无 sleep 断言。

import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchJSON: vi.fn(),
  startRealtime: vi.fn(),
}));

vi.mock("../api/client", () => ({ fetchJSON: mocks.fetchJSON }));
vi.mock("../realtime/sse", () => ({ startRealtime: mocks.startRealtime }));

const JOB_DOM_HTML = `
  <div class="log-dock is-idle" id="log-dock" data-tone="idle">
    <div class="log-dock-panel" id="log-dock-panel" aria-hidden="true">
      <span id="log-dock-status">空闲</span>
      <p id="job-message"></p>
      <div id="log-dock-body"><pre id="job-log"></pre><span id="log-dock-pin-hint" hidden></span></div>
    </div>
  </div>
  <div id="progress-banner" hidden data-percent="0"></div>
  <div id="progress-fill"></div>
  <div id="progress-fill-glow"></div>
  <div id="progress-label"></div>
  <div id="progress-detail"></div>
  <div id="progress-percent"></div>
  <span id="progress-percent-suffix">%</span>
  <svg><circle id="progress-ring"></circle></svg>
  <div id="progress-track"></div>
  <div id="progress-chip"></div>
  <div id="progress-steps"></div>
  <div id="toast-stack"></div>
`;

type AnyState = {
  polling: unknown;
  currentJob: Record<string, any> | null;
  sseHealthy: boolean;
};

function deferred<T = unknown>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

let jobs: Record<string, any>;
let stateRef: AnyState;

async function loadJobs(needDom: boolean): Promise<void> {
  if (needDom) {
    document.body.innerHTML = JOB_DOM_HTML;
  }
  vi.resetModules();
  mocks.fetchJSON.mockReset();
  mocks.startRealtime.mockReset();
  // jsdom 无 matchMedia：提供 reduce=false 桩（不触发真实动画分支）。
  window.matchMedia = (() => ({
    matches: false,
    media: "",
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
  jobs = await import("./index");
  stateRef = (await import("../state")).state as AnyState;
  // 阻断真实轮询定时器/递归：把 polling 置为占位真值（startPolling 直接返回）。
  stateRef.polling = { __sentinel: true };
}

beforeEach(async () => {
  await loadJobs(false);
});

function pushFetch(defer: { promise: Promise<unknown> }) {
  mocks.fetchJSON.mockReturnValueOnce(defer.promise);
}

describe("V19 REST snapshot freshness gates", () => {
  it("A: REST A → SSE/B authority → REST A late keeps B", async () => {
    const restA = deferred();
    pushFetch(restA);
    const inflight = jobs.fetchFreshCurrentJob();
    jobs.commitCurrentJob({ id: 2, state: "running", action: "refresh_all" }); // SSE/POST 已接受 B
    restA.resolve({ id: 1, state: "running", action: "refresh_source", progress_step: 3 });
    expect(jobs.isStaleJobSnapshot(await inflight)).toBe(true);
    expect(stateRef.currentJob?.id).toBe(2);
  });

  it("B: POST new job while REST A in flight → old REST dropped", async () => {
    const restA = deferred();
    pushFetch(restA);
    const inflight = jobs.fetchFreshCurrentJob();
    // 等价于 startJob 的 POST 结果被接受并写入 currentJob。
    jobs.commitCurrentJob({ id: 20, state: "running", action: "refresh_all" });
    restA.resolve({ id: 19, state: "running", action: "refresh_source" });
    expect(jobs.isStaleJobSnapshot(await inflight)).toBe(true);
    expect(stateRef.currentJob?.id).toBe(20);
  });

  it("C: same job REST 20 cannot overwrite SSE 80", async () => {
    const rest20 = deferred();
    pushFetch(rest20);
    const inflight = jobs.fetchFreshCurrentJob();
    jobs.commitCurrentJob({
      id: 7, state: "running", action: "refresh_source",
      progress_step: 80, progress_total: 100,
    });
    rest20.resolve({
      id: 7, state: "running", action: "refresh_source",
      progress_step: 20, progress_total: 100,
    });
    expect(jobs.isStaleJobSnapshot(await inflight)).toBe(true);
    expect(stateRef.currentJob?.progress_step).toBe(80);
  });

  it("D: two REST requests out of order → latest request wins", async () => {
    const req1 = deferred();
    const req2 = deferred();
    pushFetch(req1);
    const first = jobs.fetchFreshCurrentJob();
    pushFetch(req2);
    const second = jobs.fetchFreshCurrentJob();

    // #2 先回来并被应用
    req2.resolve({ id: 30, state: "running", action: "refresh_source", progress_step: 2 });
    const freshSecond = await second;
    expect(jobs.isStaleJobSnapshot(freshSecond)).toBe(false);
    jobs.commitCurrentJob(freshSecond);

    // #1 后回来：必须丢弃，不允许覆盖 #2
    req1.resolve({ id: 29, state: "running", action: "refresh_source", progress_step: 1 });
    expect(jobs.isStaleJobSnapshot(await first)).toBe(true);
    expect(stateRef.currentJob?.id).toBe(30);
  });

  it("E: stale null/idle cannot clear a new accepted job", async () => {
    const restIdle = deferred();
    pushFetch(restIdle);
    const inflight = jobs.fetchFreshCurrentJob();
    jobs.commitCurrentJob({ id: 40, state: "running", action: "refresh_all" });
    restIdle.resolve({ state: "idle", action: "", message: "", log: "" });
    expect(jobs.isStaleJobSnapshot(await inflight)).toBe(true);
    expect(stateRef.currentJob?.id).toBe(40);
  });

  it("F: no race → REST snapshot still applies (trackCurrentJob)", async () => {
    await loadJobs(true);
    const rest = deferred();
    pushFetch(rest);
    const result = jobs.trackCurrentJob();
    rest.resolve({ id: 50, state: "running", action: "refresh_source", progress_step: 4 });
    const current = await result;
    expect(current?.id).toBe(50);
    expect(stateRef.currentJob?.id).toBe(50);
    expect(mocks.startRealtime).toHaveBeenCalled();
  });

  it("G: SSE reconnect resets stream seq only, never authority version", async () => {
    const v0 = jobs.currentJobAuthorityVersion();
    expect(jobs.acceptJobUpdate({ id: 60, state: "running" }, { seq: 900 })).toBe(true);
    jobs.commitCurrentJob({ id: 60, state: "running", action: "refresh_source" });
    const v1 = jobs.currentJobAuthorityVersion();
    expect(v1).toBeGreaterThan(v0);

    jobs.resetJobStreamFreshness(); // 模拟 EventSource 重连
    expect(jobs.currentJobAuthorityVersion()).toBe(v1); // 本地 authority 不回退

    // 重连后新连接可接受较小 server seq（backend restart 兼容保持不变）
    expect(jobs.acceptJobUpdate({ id: 61, state: "running" }, { seq: 1 })).toBe(true);
  });

  it("H: terminal absorbing still holds (not weakened)", async () => {
    expect(jobs.acceptJobUpdate({ id: 70, state: "success" })).toBe(true);
    expect(jobs.acceptJobUpdate({ id: 70, state: "running" })).toBe(false);
    expect(jobs.acceptJobUpdate({ id: 70, state: "success" }, { seq: 5 })).toBe(false);
    // 不同 job 仍可接受
    expect(jobs.acceptJobUpdate({ id: 71, state: "running" })).toBe(true);
  });

  it("I: bootstrap fallback still seeds current job when nothing raced", async () => {
    await loadJobs(true);
    const rest = deferred();
    pushFetch(rest);
    const current = jobs.fetchFreshCurrentJob();
    rest.resolve({ id: 80, state: "running", action: "refresh_all" });
    const snapshot = await current;
    expect(jobs.isStaleJobSnapshot(snapshot)).toBe(false);
    if (!jobs.isStaleJobSnapshot(snapshot)) {
      jobs.commitCurrentJob(snapshot);
    }
    expect(stateRef.currentJob?.id).toBe(80);
  });

  it("J: after one accepted REST update the next REST still works", async () => {
    await loadJobs(true);
    const first = deferred();
    pushFetch(first);
    const p1 = jobs.fetchFreshCurrentJob();
    first.resolve({ id: 90, state: "running", action: "refresh_source", progress_step: 1 });
    const s1 = await p1;
    expect(jobs.isStaleJobSnapshot(s1)).toBe(false);
    jobs.commitCurrentJob(s1);

    // 第二轮 capture 新版本 → 仍然 fresh（不会永久锁死 REST）
    const second = deferred();
    pushFetch(second);
    const p2 = jobs.fetchFreshCurrentJob();
    second.resolve({ id: 90, state: "running", action: "refresh_source", progress_step: 2 });
    const s2 = await p2;
    expect(jobs.isStaleJobSnapshot(s2)).toBe(false);
    expect((s2 as { progress_step: number }).progress_step).toBe(2);
  });

  it("K: latest request failure never clears currentJob; next request works", async () => {
    jobs.commitCurrentJob({ id: 100, state: "running", action: "refresh_all" });
    mocks.fetchJSON.mockRejectedValueOnce(new Error("network down"));
    await expect(jobs.fetchFreshCurrentJob()).rejects.toThrow("network down");
    expect(stateRef.currentJob?.id).toBe(100);

    const next = deferred();
    pushFetch(next);
    const p = jobs.fetchFreshCurrentJob();
    next.resolve({ id: 101, state: "running", action: "refresh_source" });
    const fresh = await p;
    expect(jobs.isStaleJobSnapshot(fresh)).toBe(false);
    expect((fresh as { id: number }).id).toBe(101);
  });
});
