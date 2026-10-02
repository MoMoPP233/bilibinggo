// @vitest-environment jsdom

import { describe, expect, it } from "vitest";
import { buildActivityParticipateBtn, buildActivityLastNote, renderActivityTableRow } from "./index";

describe("guarded activity display", () => {
  it("does not expose a confirmation control for a protected row", () => {
    const html = buildActivityParticipateBtn({
      dynamic_id: "1220000000000000001",
      can_participate: false,
      participation_blocked: true,
      skip_reason: '待确认 <script>"',
    });
    expect(html).toContain("—");
    expect(html).not.toContain("需确认");
    expect(html).not.toContain("人工确认");
    expect(html).not.toContain("data-action");
  });

  it("keeps the ordinary participation button and real previous history", () => {
    expect(buildActivityParticipateBtn({ dynamic_id: "1220000000000000001", can_participate: true }))
      .toContain('data-action="participate"');
    expect(buildActivityLastNote({ participation_blocked: true, last_participation: { status: "failed", message: "原始评论失败" } }))
      .toContain("原始评论失败");
  });

  it("renders a local delete action beside the participation action", () => {
    const html = renderActivityTableRow({
      dynamic_id: "1220000000000000001",
      activity_title: "抽奖",
      lottery_type: "转发抽奖",
      activity_status: "未参加",
      can_participate: true,
      lottery_time: null,
    });
    expect(html).toContain('data-action="participate"');
    expect(html).toContain('data-action="delete-activity"');
    expect(html).toContain("删除");
  });
});
