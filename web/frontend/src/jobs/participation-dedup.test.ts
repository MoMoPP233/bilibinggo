// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function joined(dynamicId = "1220000000000000001") {
  return {
    dynamic_id: dynamicId,
    lottery_type: "转发抽奖",
    status: "joined",
    actions: ["like", "follow", "favorite", "repost", "comment"].map((action) => ({ action, ok: true })),
  };
}

function skipped(reason = "repost_unknown") {
  return {
    dynamic_id: "1220000000000000002",
    status: "skipped",
    skipped: true,
    skip_reason: reason,
    message: "转发状态待确认，已跳过以避免重复操作",
    actions: [],
  };
}

describe("participation dedup results", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
    document.body.innerHTML = `
      <section id="job-result-banner" hidden></section>
      <div id="job-result-title"></div><div id="job-result-body"></div>
      <div id="job-result-hint"></div><div id="job-result-summary"></div>
      <div id="job-result-actions"></div>`;
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("counts mixed skips separately from joined and failed", async () => {
    const { summarizeTripleResult } = await import("./index");
    expect(summarizeTripleResult({ items: [joined(), skipped()] })).toEqual({
      joined: 1, skipped: 1, failed: 0, total: 2,
    });
    expect(summarizeTripleResult({ items: [skipped(), skipped("already_joined")] })).toEqual({
      joined: 0, skipped: 2, failed: 0, total: 2,
    });
  });

  it("does not treat arbitrary business skips or malformed success as dedup", async () => {
    const { summarizeTripleResult, payloadDedupSkipped } = await import("./index");
    expect(payloadDedupSkipped(skipped("activity_ended"))).toBe(false);
    expect(payloadDedupSkipped({ ...skipped(), actions: [{ action: "repost", ok: false }] })).toBe(false);
    expect(summarizeTripleResult({ items: [skipped("activity_ended"), { ...joined(), actions: [] }] })).toEqual({
      joined: 0, skipped: 0, failed: 2, total: 2,
    });
  });

  it("renders uncertain skips as needing confirmation without a failure result", async () => {
    const { renderTripleParticipationResults } = await import("./index");
    const html = renderTripleParticipationResults({ items: [joined(), skipped()] });
    expect(html).toContain("需确认（已跳过）");
    expect(html).toContain("转发状态待确认");
    expect(html).not.toContain('participation-result-status failed');
  });

  it("shows a neutral mixed completion and no retry/failure help", async () => {
    const { showParticipationResult } = await import("./index");
    showParticipationResult({
      action: "participate_triple", state: "success",
      message: "三连参与完成：成功 1 个，已跳过 1 个",
      result: { items: [joined(), skipped()], joined: 1, skipped_count: 1, failed: 0 },
    });
    const banner = document.getElementById("job-result-banner")!;
    expect(banner.hidden).toBe(false);
    expect(banner.classList.contains("is-error")).toBe(false);
    expect(document.getElementById("job-result-title")!.textContent).toBe("三连参与完成（含跳过）");
    expect(document.getElementById("job-result-hint")!.hidden).toBe(true);
  });

  it("does not open an error banner for all-skipped results", async () => {
    const { showParticipationResult } = await import("./index");
    showParticipationResult({ action: "participate_triple", state: "success", result: { skipped: true, items: [skipped()] } });
    expect(document.getElementById("job-result-banner")!.hidden).toBe(true);
  });
});
