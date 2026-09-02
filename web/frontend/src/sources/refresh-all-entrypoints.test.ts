// @vitest-environment jsdom

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const indexHtmlPath = resolve(process.cwd(), "index.html");
const html = readFileSync(indexHtmlPath, "utf8");

describe("refresh-all UI entrypoints", () => {
  it("keeps exactly one user-visible serial update-all button", () => {
    const matches = html.match(/id="update-all-sources-btn"/g) || [];
    expect(matches).toHaveLength(1);
    // 并行检查的旧入口按钮不再出现在页面上。
    expect(html).not.toContain('data-action="refresh_all"');
  });

  it("keeps per-source single-update buttons wired to refresh_source in the row renderer", () => {
    const watchIndexPath = resolve(process.cwd(), "src", "watch", "index.ts");
    const source = readFileSync(watchIndexPath, "utf8");
    // 源列表行仍由 renderSources 渲染出 refresh_source 更新按钮。
    expect(source).toContain('data-action="refresh_source"');
    expect(source).toContain("data-source-id=");
    expect(source).not.toContain('data-action="refresh_all"');
  });
});
