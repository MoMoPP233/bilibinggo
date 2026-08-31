// @vitest-environment jsdom

import { describe, expect, it } from "vitest";
import { buildActivityParticipateBtn, buildActivityLastNote } from "./index";

describe("guarded activity display", () => {
  it("keeps a clear confirmation reason without a participation button", () => {
    const html = buildActivityParticipateBtn({
      dynamic_id: "1220000000000000001",
      can_participate: false,
      participation_blocked: true,
      skip_reason: '待确认 <script>"',
    });
    expect(html).toContain("需确认");
    expect(html).toContain("待确认 &lt;script&gt;&quot;");
    expect(html).not.toContain("data-action");
  });

  it("keeps the ordinary participation button and real previous history", () => {
    expect(buildActivityParticipateBtn({ dynamic_id: "1220000000000000001", can_participate: true }))
      .toContain('data-action="participate"');
    expect(buildActivityLastNote({ participation_blocked: true, last_participation: { status: "failed", message: "原始评论失败" } }))
      .toContain("原始评论失败");
  });
});
