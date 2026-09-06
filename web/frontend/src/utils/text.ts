/* eslint-disable */
/** Migrated from web/static/app.js — logic preserved. */

function replaceAll(value: string, search: string | RegExp, replacement: string): string {
  if (search instanceof RegExp) return value.replace(search, replacement);
  return value.split(search).join(replacement);
}

export function sanitizeUserText(text: unknown): string {
  const raw = String(text || "").trim();
  if (!raw) return "";
  const internalLinePattern =
    /traceback|nameerror|attributeerror|typeerror|keyerror|modulenotfounderror|oserror|systemexit|uvicorn|asyncio|^file\s/i;
  const internalFragmentPattern = /line \d+|\.py\b|errno|winerror/i;
  const lines = raw
    .split("\n")
    .map((line: string) => {
      const trimmed = line.trim();
      if (!trimmed) return "";
      if (internalLinePattern.test(trimmed)) return "";
      if (internalFragmentPattern.test(trimmed) && !/^===\s/.test(trimmed)) return "";
      return replaceAll(
        replaceAll(trimmed, /[A-Za-z]:\\[^\s"']+/g, "[本地文件]"),
        /→\s*\S+/g,
        "→ 已保存",
      );
    })
    .filter(Boolean);
  return lines.join("\n");
}

export function escapeHtml(value: unknown): string {
  const text = String(value ?? "");
  return replaceAll(replaceAll(replaceAll(replaceAll(text, "&", "&amp;"), "<", "&lt;"), ">", "&gt;"), '"', "&quot;");
}

export function truncateText(text: unknown, maxLen = 28): string {
  const value = String(text || "").trim();
  if (value.length <= maxLen) return value;
  return `${value.slice(0, maxLen - 1)}…`;
}
