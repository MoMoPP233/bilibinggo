import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const overviewHtml = readFileSync(resolve(__dirname, "../index.html"), "utf8");

describe("overview presentation", () => {
  it("keeps account, stats, participation, LLM and diagnostics surfaces", () => {
    expect(overviewHtml).toContain('id="profile-list"');
    expect(overviewHtml).toContain("扫码登录新账号");
    expect(overviewHtml).toContain('id="stats-grid"');
    expect(overviewHtml).toContain('id="participate-text-input"');
    expect(overviewHtml).toContain('id="llm-settings-panel"');
    expect(overviewHtml).toContain('id="export-diagnostics"');
    expect(overviewHtml).toContain("开发者工具");
  });

  it("removes duplicate and project-promotion modules from the overview", () => {
    expect(overviewHtml).not.toContain('id="profile-current"');
    expect(overviewHtml).not.toContain('class="panel quick-actions"');
    expect(overviewHtml).not.toContain('class="panel project-showcase"');
    expect(overviewHtml).not.toContain("Repository");
    expect(overviewHtml).not.toContain("OPEN SOURCE");
    expect(overviewHtml).not.toContain("检查更新");
    expect(overviewHtml).not.toContain("Releases");
  });
});
