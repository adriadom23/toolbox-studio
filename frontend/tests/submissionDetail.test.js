import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../src/studio/i18n/studioMessages";
import SubmissionDetail from "../src/studio/SubmissionDetail.vue";
import { getPreviewOperation, getSubmission, retrySubmissionEffect, retrySubmissionPreview } from "../src/studio/api/studioHttp";

vi.mock("../src/studio/api/studioHttp", () => ({
  addSubmissionComment: vi.fn(),
  getPreviewOperation: vi.fn(),
  getSubmission: vi.fn(),
  publishSubmission: vi.fn(),
  retrySubmissionEffect: vi.fn(),
  retrySubmissionPreview: vi.fn(),
  submissionArtifactUrl: vi.fn(() => "/api/artifact"),
}));

const detail = {
  id: "sub_11111111111111111111111111111111",
  project_slug: "demo-ping",
  version_label: "v1",
  title: "Demo",
  description: "Ready",
  state: "submitted",
  row_version: 1,
  actions_allowed: ["submission.download"],
  comments: [{ id: "c1", author: "alice", body: "<img src=x onerror=alert(1)>", created_at: "2026-01-01T00:00:00Z" }],
  events: [],
  effects: [],
  build: { provider: "local-subprocess", status: "succeeded" },
  build_result: "succeeded",
  git: { commit: "abcdef1", branch: "main" },
  predecessor: { id: "sub_22222222222222222222222222222222", version_label: "v0", state: "changes_requested" },
  preview: null,
};

describe("SubmissionDetail", () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.clearAllMocks();
    getSubmission.mockResolvedValue({ ...detail });
  });
  afterEach(() => vi.useRealTimers());

  it("renders untrusted comments as text and keeps partial panels isolated", async () => {
    const wrapper = mount(SubmissionDetail, {
      props: { submissionId: detail.id },
      global: { stubs: { ArtifactPreview: true, BuildLogPanel: true, ManifestTable: true, ReviewActionDialog: true, SubmissionStatusBadge: true } },
    });
    await flushPromises();

    expect(wrapper.get(".submission-detail__comment").text()).toBe("<img src=x onerror=alert(1)>");
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.findComponent({ name: "ManifestTable" }).exists()).toBe(true);
    expect(wrapper.findComponent({ name: "BuildLogPanel" }).exists()).toBe(true);
  });

  it("offers authorized retry for delivery-unknown effects", async () => {
    getSubmission.mockResolvedValue({
      ...detail,
      actions_allowed: ["submission.download", "submission.effects.retry"],
      effects: [{ id: "eff_1", kind: "notification", provider_key: "teams", status: "delivery_unknown", generation: 1 }],
    });
    retrySubmissionEffect.mockResolvedValue({ status: "delivered" });
    const wrapper = mount(SubmissionDetail, {
      props: { submissionId: detail.id },
      global: { stubs: { ArtifactPreview: true, BuildLogPanel: true, ManifestTable: true, ReviewActionDialog: true, SubmissionStatusBadge: true } },
    });
    await flushPromises();
    expect(wrapper.get(".submission-detail__effects").text()).toContain("teams");
    await wrapper.get(".submission-detail__effects button").trigger("click");
    await flushPromises();
    expect(retrySubmissionEffect).toHaveBeenCalledWith(detail.id, "eff_1", 1);
  });

  it("treats effect_in_flight as active work and polls until retry is safe", async () => {
    vi.useFakeTimers();
    const active = {
      ...detail,
      actions_allowed: ["submission.effects.retry"],
      effects: [{ id: "eff_active", kind: "notification", status: "delivery_unknown", generation: 1 }],
    };
    const expired = {
      ...active,
      effects: [{ id: "eff_active", kind: "notification", status: "delivery_unknown", generation: 1, retry_in_flight: false }],
    };
    getSubmission.mockResolvedValueOnce(active).mockResolvedValueOnce(expired);
    const conflict = new Error("effect delivery is already in progress");
    conflict.status = 409;
    conflict.payload = {
      error: "effect_in_flight",
      details: { status: "in_progress", retry_after: 1, poll_url: `/api/submissions/${detail.id}` },
    };
    retrySubmissionEffect.mockRejectedValue(conflict);
    const wrapper = mount(SubmissionDetail, {
      props: { submissionId: detail.id },
      global: { stubs: { ArtifactPreview: true, BuildLogPanel: true, ManifestTable: true, ReviewActionDialog: true, SubmissionStatusBadge: true } },
    });
    await flushPromises();
    await wrapper.get(".submission-detail__effects button").trigger("click");
    await flushPromises();
    expect(wrapper.get(".submission-detail__effects [role='status']").text()).toContain("curso");
    expect(wrapper.get(".submission-detail__effects button").attributes("disabled")).toBeDefined();
    await vi.advanceTimersByTimeAsync(1000);
    await flushPromises();
    expect(getSubmission).toHaveBeenCalledTimes(2);
    expect(retrySubmissionEffect).toHaveBeenCalledTimes(1);
    expect(wrapper.find(".submission-detail__effects [role='status']").exists()).toBe(false);
    expect(wrapper.get(".submission-detail__effects button").attributes("disabled")).toBeUndefined();
  });

  it("does not expose effect retry without the backend capability", async () => {
    getSubmission.mockResolvedValue({
      ...detail,
      effects: [{ id: "eff_2", kind: "notification", status: "failed", generation: 1 }],
    });
    const wrapper = mount(SubmissionDetail, {
      props: { submissionId: detail.id },
      global: { stubs: { ArtifactPreview: true, BuildLogPanel: true, ManifestTable: true, ReviewActionDialog: true, SubmissionStatusBadge: true } },
    });
    await flushPromises();
    expect(wrapper.find(".submission-detail__effects button").exists()).toBe(false);
  });

  it("shows safe preview diagnostics and retries only recoverable authorized states", async () => {
    vi.useFakeTimers();
    getSubmission
      .mockResolvedValueOnce({
        ...detail,
        actions_allowed: ["submission.download", "submission.preview.retry"],
        preview: { id: "prv_1", state: "building", error_code: "build_capacity_exceeded", recoverable: true },
      })
      .mockResolvedValueOnce({ ...detail, preview: { id: "prv_2", state: "failed", error_code: "preview_integrity_failed", recoverable: false } });
    retrySubmissionPreview.mockResolvedValue({ operation_id: "pop_1", state: "running", phase: "build", poll_url: "/api/preview-operations/pop_1", timeout_seconds: 900, created_at: new Date().toISOString() });
    getPreviewOperation.mockResolvedValue({ operation_id: "pop_1", state: "ready", phase: "ready", preview_id: "prv_2" });
    const wrapper = mount(SubmissionDetail, {
      props: { submissionId: detail.id },
      global: { stubs: { ArtifactPreview: true, BuildLogPanel: true, ManifestTable: true, ReviewActionDialog: true, SubmissionStatusBadge: true } },
    });
    await flushPromises();
    expect(wrapper.get(".submission-detail__preview-state").text()).toContain("build_capacity_exceeded");
    const retry = wrapper.get(".submission-detail__preview-state button");
    await retry.trigger("click");
    await flushPromises();
    expect(wrapper.get("[role='status']").text()).toContain("build");
    await vi.advanceTimersByTimeAsync(500);
    await flushPromises();
    expect(retrySubmissionPreview).toHaveBeenCalledWith(detail.id, expect.any(String));
    expect(getPreviewOperation).toHaveBeenCalledWith("/api/preview-operations/pop_1");
    expect(wrapper.get(".submission-detail__preview-state").text()).toContain("preview_integrity_failed");
    expect(wrapper.find(".submission-detail__preview-state button").exists()).toBe(false);
  });

  it("persists an active preview retry and resumes it after remount", async () => {
    vi.useFakeTimers();
    getSubmission.mockResolvedValue({
      ...detail,
      actions_allowed: ["submission.preview.retry"],
      preview: { id: "prv_1", state: "failed", error_code: "build_timeout", recoverable: true },
    });
    retrySubmissionPreview.mockResolvedValue({
      operation_id: "pop_resume", state: "running", phase: "build",
      poll_url: "/api/preview-operations/pop_resume", timeout_seconds: 900, created_at: new Date().toISOString(),
    });
    const options = {
      props: { submissionId: detail.id },
      global: { stubs: { ArtifactPreview: true, BuildLogPanel: true, ManifestTable: true, ReviewActionDialog: true, SubmissionStatusBadge: true } },
    };
    const first = mount(SubmissionDetail, options);
    await flushPromises();
    await first.get(".submission-detail__preview-state button:last-of-type").trigger("click");
    await flushPromises();
    expect(sessionStorage.getItem(`toolbox.preview-retry.${detail.id}`)).toContain("pop_resume");
    first.unmount();

    getPreviewOperation.mockResolvedValue({ operation_id: "pop_resume", state: "ready", phase: "ready", preview_id: "prv_2" });
    const second = mount(SubmissionDetail, options);
    await flushPromises();
    expect(getPreviewOperation).toHaveBeenCalledWith("/api/preview-operations/pop_resume");
    expect(retrySubmissionPreview).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(`toolbox.preview-retry.${detail.id}`)).toBeNull();
    second.unmount();
  });

  it("uses the server timeout and pauses requests while the tab is hidden", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    getSubmission.mockResolvedValue({
      ...detail,
      actions_allowed: ["submission.preview.retry"],
      preview: { id: "prv_1", state: "failed", error_code: "build_timeout", recoverable: true },
    });
    retrySubmissionPreview.mockResolvedValue({
      operation_id: "pop_timeout", state: "running", phase: "build",
      poll_url: "/api/preview-operations/pop_timeout", timeout_seconds: 4, created_at: "2026-01-01T00:00:00Z",
    });
    getPreviewOperation.mockResolvedValue({
      operation_id: "pop_timeout", state: "running", phase: "build",
      poll_url: "/api/preview-operations/pop_timeout", timeout_seconds: 4, created_at: "2026-01-01T00:00:00Z",
    });
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    const wrapper = mount(SubmissionDetail, {
      props: { submissionId: detail.id },
      global: { stubs: { ArtifactPreview: true, BuildLogPanel: true, ManifestTable: true, ReviewActionDialog: true, SubmissionStatusBadge: true } },
    });
    await flushPromises();
    await wrapper.get(".submission-detail__preview-state button:last-of-type").trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(1000);
    expect(getPreviewOperation).not.toHaveBeenCalled();
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(getPreviewOperation).toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(2500);
    await flushPromises();
    expect(wrapper.text()).toContain("preview_poll_timeout");
    expect(sessionStorage.getItem(`toolbox.preview-retry.${detail.id}`)).toContain("pop_timeout");
    wrapper.unmount();
  });
});
