// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchJSONMock, showToastMock } = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(),
  showToastMock: vi.fn(),
}));

vi.mock("../api/client", () => ({ fetchJSON: fetchJSONMock }));
vi.mock("../account/index", () => ({ isSetupComplete: () => true }));
vi.mock("../shell/toast", () => ({ showToast: showToastMock }));
vi.mock("../jobs/index", () => ({
  acceptJobUpdate: () => true,
  bindActionButtons: () => {},
  updateJobUI: () => {},
}));
vi.mock("../watch/index", () => ({ renderSources: () => {} }));

if (!window.matchMedia) {
  window.matchMedia = (query) => ({
    matches: true, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}

function failure(id: string) {
  return {
    dynamic_id: id,
    activity_status: "失败",
    failure_reason: "测试失败记录",
    failed_at: 1_700_000_000,
    source_url: "",
  };
}

function list(items: ReturnType<typeof failure>[]) {
  return { items, page: 1, pages: 1, total: items.length };
}

function deferred() {
  let resolve: (value: unknown) => void = () => {};
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function deleteCalls() {
  return fetchJSONMock.mock.calls.filter(([, options]) => options?.method === "DELETE");
}

function setupDom() {
  document.body.innerHTML = `
    <div id="activities-body"></div><div id="activities-cards"></div>
    <div id="filter-result-summary"></div><div id="activities-status"></div>
    <div id="pagination"></div><div id="triple-participate-bar"></div>
    <div id="triple-participate-desc"></div><div id="triple-participate-targets"></div>
    <button id="triple-participate-btn"></button><span id="triple-participate-btn-label"></span>`;
}

async function clickFailure(id = "123") {
  const mod = await import("./index");
  await mod.loadActivities();
  const button = document.querySelector(`#activities-body [data-action="delete-activity-failure"][data-dynamic-id="${id}"]`) as HTMLButtonElement;
  expect(button).not.toBeNull();
  button.click();
  return button;
}

describe("V103 failure-record delete feedback", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchJSONMock.mockReset();
    showToastMock.mockReset();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    setupDom();
  });

  it("deletes once, refreshes the failed category and shows one success", async () => {
    let deleted = false;
    fetchJSONMock.mockImplementation((url: string, options?: RequestInit) => {
      if (options?.method === "DELETE") {
        deleted = true;
        return Promise.resolve({ ok: true, deleted: true });
      }
      return Promise.resolve(list(deleted ? [] : [failure("123")]));
    });
    const { state } = await import("../state");
    state.filters.status = "失败";
    await clickFailure();
    await vi.waitFor(() => expect(showToastMock).toHaveBeenCalledTimes(1));

    expect(deleteCalls()).toHaveLength(1);
    expect(showToastMock).toHaveBeenCalledWith("失败记录已删除", "success");
    expect(document.getElementById("activities-body")?.textContent).not.toContain("123");
    expect(document.getElementById("pagination")?.textContent).toContain("共 0 条");
    expect(state.filters.status).toBe("失败");
  });

  it("a failed DELETE shows one error and keeps the row", async () => {
    fetchJSONMock.mockImplementation((url: string, options?: RequestInit) => {
      if (options?.method === "DELETE") return Promise.reject(new Error("删除失败"));
      return Promise.resolve(list([failure("123")]));
    });
    await clickFailure();
    await vi.waitFor(() => expect(showToastMock).toHaveBeenCalledTimes(1));
    expect(deleteCalls()).toHaveLength(1);
    expect(showToastMock).toHaveBeenCalledWith("删除失败", "error");
    expect(document.getElementById("activities-body")?.textContent).toContain("123");
  });

  it("rapid double-click cannot send a second DELETE or contradictory toasts", async () => {
    const first = deferred();
    fetchJSONMock.mockImplementation((url: string, options?: RequestInit) => {
      if (options?.method === "DELETE") {
        if (deleteCalls().length === 1) return first.promise;
        return Promise.reject(new Error("删除失败"));
      }
      return Promise.resolve(list(deleteCalls().length ? [] : [failure("123")]));
    });
    const button = await clickFailure();
    button.click();
    expect(deleteCalls()).toHaveLength(1);
    first.resolve({ ok: true, deleted: true });
    await vi.waitFor(() => expect(showToastMock).toHaveBeenCalledTimes(1));

    expect(deleteCalls()).toHaveLength(1);
    expect(showToastMock.mock.calls.map((call) => call[1])).toEqual(["success"]);
  });

  it("deleted:false refreshes without claiming deletion success", async () => {
    fetchJSONMock.mockImplementation((url: string, options?: RequestInit) => {
      if (options?.method === "DELETE") return Promise.resolve({ ok: true, deleted: false });
      return Promise.resolve(list(deleteCalls().length ? [] : [failure("123")]));
    });
    await clickFailure();
    await vi.waitFor(() => expect(showToastMock).toHaveBeenCalledTimes(1));
    expect(deleteCalls()).toHaveLength(1);
    expect(showToastMock).toHaveBeenCalledWith("失败记录已不存在", "info");
    expect(document.getElementById("activities-body")?.textContent).not.toContain("123");
  });

  it("a successful DELETE followed by refresh failure never says deletion failed", async () => {
    let deleted = false;
    fetchJSONMock.mockImplementation((url: string, options?: RequestInit) => {
      if (options?.method === "DELETE") {
        deleted = true;
        return Promise.resolve({ ok: true, deleted: true });
      }
      return deleted ? Promise.reject(new Error("刷新失败")) : Promise.resolve(list([failure("123")]));
    });
    await clickFailure();
    await vi.waitFor(() => expect(showToastMock).toHaveBeenCalledTimes(1));
    expect(showToastMock.mock.calls[0][1]).toBe("success");
    expect(document.getElementById("activities-status")?.textContent).toContain("活动刷新失败");
  });

  it("a later separate click sees deleted:false without a false second success", async () => {
    let deletes = 0;
    fetchJSONMock.mockImplementation((url: string, options?: RequestInit) => {
      if (options?.method === "DELETE") {
        deletes += 1;
        return Promise.resolve({ ok: true, deleted: deletes === 1 });
      }
      // Simulate a stale list still showing the row after the first response.
      return Promise.resolve(list([failure("123")]));
    });
    await clickFailure();
    await vi.waitFor(() => expect(showToastMock).toHaveBeenCalledTimes(1));
    const button = document.querySelector('#activities-body [data-action="delete-activity-failure"]') as HTMLButtonElement;
    button.click();
    await vi.waitFor(() => expect(showToastMock).toHaveBeenCalledTimes(2));
    expect(deleteCalls()).toHaveLength(2);
    expect(showToastMock.mock.calls.map((call) => call[1])).toEqual(["success", "info"]);
  });

  it("deleting another failed record is not blocked by the first record", async () => {
    const first = deferred();
    fetchJSONMock.mockImplementation((url: string, options?: RequestInit) => {
      if (options?.method === "DELETE") {
        if (url.endsWith("/123")) return first.promise;
        return Promise.resolve({ ok: true, deleted: true });
      }
      return Promise.resolve(list([failure("123"), failure("456")]));
    });
    const button = await clickFailure();
    const other = document.querySelector('#activities-body [data-dynamic-id="456"][data-action="delete-activity-failure"]') as HTMLButtonElement;
    other.click();
    await vi.waitFor(() => expect(deleteCalls()).toHaveLength(2));
    first.resolve({ ok: true, deleted: true });
    await vi.waitFor(() => expect(showToastMock).toHaveBeenCalledTimes(2));
    expect(button).not.toBeNull();
    expect(showToastMock.mock.calls.every((call) => call[1] === "success")).toBe(true);
  });

  it("an ambiguous HTTP 200 response is not presented as success", async () => {
    fetchJSONMock.mockImplementation((url: string, options?: RequestInit) => {
      if (options?.method === "DELETE") return Promise.resolve({ ok: true });
      return Promise.resolve(list([failure("123")]));
    });
    await clickFailure();
    await vi.waitFor(() => expect(showToastMock).toHaveBeenCalledTimes(1));
    expect(showToastMock.mock.calls[0][1]).toBe("info");
  });
});
