// V6 测试外网隔离：默认禁止真实外部 fetch。
//
// 说明：当前前端测试全部对 API 层做 mock（vi.mock("../api/client") 等），
// 不使用真实网络。此文件提供 fail-closed 默认：任何测试意外调用全局 fetch
// 会立刻本地抛错，提示用 fake（vi.stubGlobal("fetch", ...) / vi.mock），
// 而不是真正请求外网。测试自身 stub 全局能力不受影响（configurable 属性可覆盖）。

function blockedFetch(..._args: unknown[]): never {
  throw new Error(
    "[vitest] External network access is forbidden in tests: global fetch was called. " +
      "Mock/fake this request instead (e.g. vi.stubGlobal('fetch', ...) or vi.mock).",
  );
}

const globalObject = globalThis as typeof globalThis & {
  fetch?: unknown;
};

if (typeof globalObject.fetch === "undefined" || globalObject.fetch) {
  Object.defineProperty(globalObject, "fetch", {
    configurable: true,
    writable: true,
    value: blockedFetch,
  });
}
