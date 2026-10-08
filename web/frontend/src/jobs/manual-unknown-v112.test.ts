// @vitest-environment jsdom

import { describe, expect, it, vi } from "vitest";
import { buildActivityParticipateBtn } from "../activities/index";
import { participationClickParams } from "./index";

describe("V112 uniform manual participation", () => {
  it("renders the same 参与 button for ordinary and UNKNOWN rows across profiles", () => {
    const normal = buildActivityParticipateBtn({
      dynamic_id: "123456789", can_participate: true,
    });
    const uncertain = buildActivityParticipateBtn({
      dynamic_id: "123456789", can_participate: false, manual_retry_available: true,
    });
    expect(uncertain).toBe(normal);
    expect(uncertain).not.toContain("重新参与");
    expect(uncertain).not.toContain("重试可能重复");
  });

  it("a click carries one manual invocation without phrase, action choice or text", () => {
    const prompt = vi.spyOn(window, "prompt");
    expect(participationClickParams("123456789")).toEqual({
      dynamic_id: "123456789", manual_unknown_retry: true,
    });
    expect(prompt).not.toHaveBeenCalled();
    prompt.mockRestore();
  });
});
