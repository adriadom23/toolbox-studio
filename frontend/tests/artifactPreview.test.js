import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

const { buildWorkspacePreview, createPreviewGrant, getPreviewOperation, getPreviewStatus, postPreviewGrant } = vi.hoisted(() => ({
  buildWorkspacePreview: vi.fn(),
  createPreviewGrant: vi.fn(),
  getPreviewOperation: vi.fn(),
  getPreviewStatus: vi.fn(() => new Promise(() => {})),
  postPreviewGrant: vi.fn(() => true),
}));

vi.mock("../src/studio/api/studioHttp", () => ({
  buildWorkspacePreview,
  createPreviewGrant,
  getPreviewOperation,
  getPreviewStatus,
}));
vi.mock("../src/projects/_previewEntry", () => ({
  postPreviewGrant,
  previewOrigin: (value) => new URL(value).origin,
}));

import "../src/studio/i18n/studioMessages";
import ArtifactPreview from "../src/studio/ArtifactPreview.vue";

afterEach(() => {
  vi.clearAllMocks();
  getPreviewStatus.mockImplementation(() => new Promise(() => {}));
  vi.useRealTimers();
  window.sessionStorage.clear();
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
});

describe("ArtifactPreview explicit consent", () => {
  it("does not request a grant or create an iframe before the reviewer clicks", async () => {
    createPreviewGrant.mockResolvedValue({
      preview_id: "prv_1",
      nonce: "one-use",
      bootstrap_url: "http://127.0.0.1:5001/bootstrap",
    });
    const wrapper = mount(ArtifactPreview, {
      props: { subjectKind: "submission", subjectId: "sub_1", previewId: "prv_1" },
    });

    await flushPromises();
    expect(createPreviewGrant).not.toHaveBeenCalled();
    expect(wrapper.find("iframe").exists()).toBe(false);

    await wrapper.get("button.tb-button--primary").trigger("click");
    await flushPromises();
    expect(createPreviewGrant).toHaveBeenCalledTimes(1);
    expect(wrapper.find("iframe").exists()).toBe(true);
    await wrapper.get("iframe").trigger("load");
    expect(postPreviewGrant).toHaveBeenCalledTimes(1);

    await wrapper.get("button.tb-button--ghost").trigger("click");
    expect(wrapper.find("iframe").exists()).toBe(false);
    await wrapper.get("button.tb-button--primary").trigger("click");
    await flushPromises();
    expect(createPreviewGrant).toHaveBeenCalledTimes(2);
  });

  it("discards an in-flight grant when the submission changes", async () => {
    let resolveGrant;
    createPreviewGrant.mockReturnValue(new Promise((resolve) => { resolveGrant = resolve; }));
    const wrapper = mount(ArtifactPreview, {
      props: { subjectKind: "submission", subjectId: "sub_1", previewId: "prv_1" },
    });
    await wrapper.get("button.tb-button--primary").trigger("click");
    await wrapper.setProps({ subjectId: "sub_2", previewId: "prv_2" });
    resolveGrant({ preview_id: "prv_1", nonce: "stale", bootstrap_url: "http://127.0.0.1:5001/bootstrap" });
    await flushPromises();
    expect(wrapper.find("iframe").exists()).toBe(false);
    expect(postPreviewGrant).not.toHaveBeenCalled();
  });

  it("rotates a durable workspace idempotency key after a terminal build", async () => {
    vi.useFakeTimers();
    buildWorkspacePreview
      .mockResolvedValueOnce({ operation_id: "pop_1", state: "running", phase: "build", poll_url: "/api/preview-operations/pop_1" })
      .mockResolvedValueOnce({ operation_id: "pop_1", state: "ready", phase: "ready", preview_id: "prv_1" });
    getPreviewOperation.mockResolvedValue({ operation_id: "pop_1", state: "ready", phase: "ready", preview_id: "prv_1" });
    createPreviewGrant.mockResolvedValue({ preview_id: "prv_1", nonce: "one-use", bootstrap_url: "http://127.0.0.1:5001/bootstrap" });
    const wrapper = mount(ArtifactPreview, { props: { subjectKind: "workspace", subjectId: "demo" } });

    await wrapper.get("button.tb-button--primary").trigger("click");
    await flushPromises();
    expect(wrapper.get("[role='status']").text()).toContain("build");
    await vi.advanceTimersByTimeAsync(250);
    await flushPromises();
    expect(getPreviewOperation).toHaveBeenCalledWith("/api/preview-operations/pop_1");
    expect(wrapper.find("iframe").exists()).toBe(true);
    const firstKey = buildWorkspacePreview.mock.calls[0][1];

    await wrapper.get("button.tb-button--ghost").trigger("click");
    await wrapper.get("button.tb-button--primary").trigger("click");
    await flushPromises();
    expect(buildWorkspacePreview.mock.calls[1][1]).not.toBe(firstKey);
  });

  it("uses the durable server deadline beyond two minutes and pauses while hidden", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    buildWorkspacePreview.mockResolvedValue({
      operation_id: "pop_long", state: "running", phase: "build",
      poll_url: "/api/preview-operations/pop_long", created_at: "2026-01-01T00:00:00Z", timeout_seconds: 900,
    });
    getPreviewOperation.mockResolvedValue({
      operation_id: "pop_long", state: "running", phase: "build",
      poll_url: "/api/preview-operations/pop_long", created_at: "2026-01-01T00:00:00Z", timeout_seconds: 900,
    });
    createPreviewGrant.mockResolvedValue({ preview_id: "prv_long", nonce: "long", bootstrap_url: "http://127.0.0.1:5001/bootstrap" });
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    const wrapper = mount(ArtifactPreview, { props: { subjectKind: "workspace", subjectId: "long-build" } });

    await wrapper.get("button.tb-button--primary").trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(3000);
    expect(getPreviewOperation).not.toHaveBeenCalled();

    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    await vi.advanceTimersByTimeAsync(121000);
    await flushPromises();
    expect(wrapper.find("[role='alert']").exists()).toBe(false);
    expect(wrapper.get("[role='status']").text()).toContain("build");

    getPreviewOperation.mockResolvedValue({ operation_id: "pop_long", state: "ready", phase: "ready", preview_id: "prv_long" });
    await vi.advanceTimersByTimeAsync(5000);
    await flushPromises();
    expect(wrapper.find("iframe").exists()).toBe(true);
  });

  it("closes an invalidated workspace iframe and reopens the current preview without rebuilding", async () => {
    vi.useFakeTimers();
    buildWorkspacePreview.mockResolvedValue({
      operation_id: "pop_old", state: "ready", phase: "ready", preview_id: "prv_old",
    });
    createPreviewGrant
      .mockResolvedValueOnce({ preview_id: "prv_old", nonce: "old", bootstrap_url: "http://127.0.0.1:5001/bootstrap" })
      .mockResolvedValueOnce({ preview_id: "prv_current", nonce: "current", bootstrap_url: "http://127.0.0.1:5001/bootstrap" });
    getPreviewStatus.mockResolvedValue({
      preview_id: "prv_old", state: "unavailable", error_code: "preview_superseded",
      current: false, current_preview_id: "prv_current",
    });
    const wrapper = mount(ArtifactPreview, {
      attachTo: document.body,
      props: { subjectKind: "workspace", subjectId: "demo" },
    });

    await wrapper.get("button.tb-button--primary").trigger("click");
    await flushPromises();
    expect(wrapper.find("iframe").exists()).toBe(true);
    await vi.advanceTimersByTimeAsync(1000);
    await flushPromises();

    expect(wrapper.find("iframe").exists()).toBe(false);
    expect(wrapper.get("[role='alert']").text()).toContain("obsoleto");
    expect(document.activeElement).toBe(wrapper.get("button.tb-button--primary").element);

    await wrapper.get("button.tb-button--primary").trigger("click");
    await flushPromises();
    expect(buildWorkspacePreview).toHaveBeenCalledTimes(1);
    expect(createPreviewGrant).toHaveBeenLastCalledWith({
      subjectKind: "workspace", subjectId: "demo", previewId: "prv_current",
    });
    expect(wrapper.find("iframe").exists()).toBe(true);
    wrapper.unmount();
  });

  it("rotates the workspace key after a failed operation", async () => {
    buildWorkspacePreview
      .mockResolvedValueOnce({ operation_id: "pop_failed", state: "failed", phase: "build", error_code: "build_timeout" })
      .mockResolvedValueOnce({ operation_id: "pop_ready", state: "ready", phase: "ready", preview_id: "prv_2" });
    createPreviewGrant.mockResolvedValue({ preview_id: "prv_2", nonce: "new", bootstrap_url: "http://127.0.0.1:5001/bootstrap" });
    const wrapper = mount(ArtifactPreview, { props: { subjectKind: "workspace", subjectId: "demo" } });

    await wrapper.get("button.tb-button--primary").trigger("click");
    await flushPromises();
    const failedKey = buildWorkspacePreview.mock.calls[0][1];
    expect(wrapper.get("[role='alert']").text()).toContain("build_timeout");

    await wrapper.get("button.tb-button--primary").trigger("click");
    await flushPromises();
    expect(buildWorkspacePreview.mock.calls[1][1]).not.toBe(failedKey);
    expect(wrapper.find("iframe").exists()).toBe(true);
  });
});
