// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

import { terminalSnapshotShouldComplete } from "./sse";

describe("terminal snapshot: baseline vs reconnect recovery", () => {
  it("initial load with historical terminal snapshot never replays completion", () => {
    // 页面基线：没有正在运行的 Job（currentJob 为空或已是该终态）
    expect(
      terminalSnapshotShouldComplete({ id: 5, state: "success" }, null),
    ).toBe(false);
    expect(
      terminalSnapshotShouldComplete(
        { id: 5, state: "success" },
        { id: 5, state: "success" },
      ),
    ).toBe(false);
    // 另一历史任务也不重放
    expect(
      terminalSnapshotShouldComplete({ id: 4, state: "error" }, { id: 5, state: "success" }),
    ).toBe(false);
  });

  it("initial running snapshot keeps running state (not terminal path)", () => {
    expect(terminalSnapshotShouldComplete({ id: 6, state: "running" }, null)).toBe(false);
  });

  it("reconnect: job finished while disconnected → terminal snapshot completes once", () => {
    expect(
      terminalSnapshotShouldComplete(
        { id: 7, state: "success" },
        { id: 7, state: "running" },
      ),
    ).toBe(true);
    expect(
      terminalSnapshotShouldComplete(
        { id: 7, state: "cancelled" },
        { id: 7, state: "running" },
      ),
    ).toBe(true);
  });

  it("different job id while current running → treat as baseline, no replay", () => {
    expect(
      terminalSnapshotShouldComplete({ id: 8, state: "success" }, { id: 9, state: "running" }),
    ).toBe(false);
  });
});
