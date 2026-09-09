// @vitest-environment jsdom
// Notification 状态文案（V12）：在途 dispatching=“发送中”，已恢复 delivery_unknown=“结果未知”。

import { describe, expect, it } from "vitest";

import { statusBadge } from "./index";

describe("notification record status labels", () => {
  it("dispatching is shown as 发送中 (in-flight)", () => {
    expect(statusBadge("dispatching")).toContain("发送中");
    expect(statusBadge("dispatching")).not.toContain("结果未知");
  });

  it("delivery_unknown is shown as 结果未知 (resolved unknown)", () => {
    expect(statusBadge("delivery_unknown")).toContain("结果未知");
    expect(statusBadge("delivery_unknown")).not.toContain("发送中");
  });

  it("sent / failed keep existing labels", () => {
    expect(statusBadge("sent")).toContain("已发送");
    expect(statusBadge("failed")).toContain("失败");
  });
});
