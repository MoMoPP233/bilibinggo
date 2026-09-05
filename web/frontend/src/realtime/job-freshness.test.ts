// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from "vitest";

// 只测试 freshness gate 纯函数；不触达 DOM/网络。
// 模块级状态按文件隔离，前后重置。
import {
  acceptJobUpdate,
  isJobTerminalAccepted,
  isJobTerminalState,
  resetJobStreamFreshness,
} from "../jobs/index";

describe("job realtime freshness gate", () => {
  beforeEach(() => {
    resetJobStreamFreshness();
  });

  it("accepts running then terminal for same job", () => {
    expect(acceptJobUpdate({ id: 7, state: "running" })).toBe(true);
    expect(acceptJobUpdate({ id: 7, state: "success" })).toBe(true);
    expect(isJobTerminalState("success")).toBe(true);
  });

  it("terminal of same job is absorb state: later running/snapshot ignored", () => {
    acceptJobUpdate({ id: 7, state: "running" });
    acceptJobUpdate({ id: 7, state: "success" });
    expect(acceptJobUpdate({ id: 7, state: "running" })).toBe(false);
    expect(acceptJobUpdate({ id: 7, state: "error" })).toBe(false);
    expect(isJobTerminalAccepted({ id: 7 })).toBe(true);
  });

  it("old progress/log of terminal job are ignored", () => {
    acceptJobUpdate({ id: 7, state: "running" });
    acceptJobUpdate({ id: 7, state: "cancelled" });
    expect(acceptJobUpdate({ id: 7, state: "running" })).toBe(false);
  });

  it("different job B running is allowed after A terminal (not global absorb)", () => {
    acceptJobUpdate({ id: 1, state: "running" });
    acceptJobUpdate({ id: 1, state: "success" });
    expect(acceptJobUpdate({ id: 2, state: "running" })).toBe(true);
    expect(acceptJobUpdate({ id: 2, state: "running", seq: 10 })).toBe(true);
  });

  it("late events of old job A cannot pollute authoritative job B", () => {
    acceptJobUpdate({ id: 1, state: "success" }); // A terminal 先确认
    acceptJobUpdate({ id: 2, state: "running" }); // B authoritative
    // A 的 progress/log/terminal 晚到 → 全部拒绝
    expect(acceptJobUpdate({ id: 1, state: "running" })).toBe(false);
    expect(acceptJobUpdate({ id: 1, state: "success" })).toBe(false);
    expect(acceptJobUpdate({ id: 1, state: "running" })).toBe(false);
  });

  it("seq <= snapshot watermark frames are stale (queued old events)", () => {
    // 快照 watermark=10（snapshot 已包含该 seq 之前的一切）
    expect(acceptJobUpdate({ id: 9, state: "running" }, { seq: 10 })).toBe(true);
    // 订阅队列里早于 watermark 的旧帧：拒绝，不能倒灌覆盖新 snapshot。
    expect(acceptJobUpdate({ id: 9, state: "running" }, { seq: 5 })).toBe(false);
    expect(acceptJobUpdate({ id: 9, state: "running" }, { seq: 9 })).toBe(false);
    // watermark 之后的新事件正常接受。
    expect(acceptJobUpdate({ id: 9, state: "running" }, { seq: 11 })).toBe(true);
  });

  it("backend restart: reset window allows small seqs again", () => {
    expect(acceptJobUpdate({ id: 9, state: "running" }, { seq: 5000 })).toBe(true);
    resetJobStreamFreshness();
    // 新 backend seq 从 1 重新开始，不得被旧窗口永久拒绝。
    expect(acceptJobUpdate({ id: 9, state: "running" }, { seq: 1 })).toBe(true);
  });

  it("terminal snapshot then same terminal event completes only once via gate", () => {
    // snapshot terminal 首次：接受（由 handler 走 finishJobOnce）
    expect(acceptJobUpdate({ id: 8, state: "success" }, { seq: 20 })).toBe(true);
    // 后续相同 terminal event：拒绝（不会二次 completion）
    expect(acceptJobUpdate({ id: 8, state: "success" }, { seq: 21 })).toBe(false);
    // REST terminal（无 seq）：拒绝
    expect(acceptJobUpdate({ id: 8, state: "success" })).toBe(false);
  });
});
