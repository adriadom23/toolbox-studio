import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../src/studio/i18n/studioMessages";
import SubmitProjectDialog from "../src/studio/SubmitProjectDialog.vue";
import { createSubmission, getSubmissionContext, getSubmissionOperation } from "../src/studio/api/studioHttp";

vi.mock("../src/studio/api/studioHttp", () => ({
  createSubmission: vi.fn(),
  getSubmissionContext: vi.fn(),
  getSubmissionOperation: vi.fn(),
}));

describe("SubmitProjectDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getSubmissionContext.mockResolvedValue({
      project_slug: "demo-ping",
      next_version: 2,
      actor: { id: "alice", display_name: "Alice" },
      git: { branch: "main", commit: "abcdef1", remote: "https://git.example/toolbox" },
      providers: { artifact: "local", build: "local-subprocess", logs: "local", notifications: ["local"], publication: "local" },
      warnings: ["Build local sin sandbox fuerte"],
      predecessors: [],
      actions_allowed: ["submission.create"],
    });
    createSubmission.mockResolvedValue({
      operation_id: "op_1",
      submission_id: "sub_1",
      status: "build_failed",
      error: "vite_build_failed",
      phases: [
        { phase: "validation", status: "succeeded", error_code: null },
        { phase: "snapshot", status: "succeeded", error_code: null },
        { phase: "build", status: "failed", error_code: "vite_build_failed" },
        { phase: "package", status: "pending", error_code: null },
      ],
    });
  });

  it("keeps durable backend phases visible after a terminal failure", async () => {
    const wrapper = mount(SubmitProjectDialog, {
      props: { modelValue: false, name: "demo-ping" },
      global: { stubs: { Modal: { template: "<div><slot /><slot name='footer' /></div>" } } },
    });
    await wrapper.setProps({ modelValue: true });
    await flushPromises();
    expect(wrapper.get(".submit-dialog__context").text()).toContain("Alice");
    expect(wrapper.get(".submit-dialog__context").text()).toContain("abcdef1");
    expect(wrapper.get(".submit-dialog__context").text()).toContain("local-subprocess");
    expect(wrapper.get(".submit-dialog__warnings").text()).toContain("sin sandbox fuerte");
    await wrapper.get("textarea").setValue("Review context");
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    const phases = wrapper.findAll(".submit-dialog__phases li");
    expect(phases).toHaveLength(4);
    expect(phases[2].classes()).toContain("is-failed");
    expect(phases[2].text()).toContain("vite_build_failed");
    expect(wrapper.get('[role="alert"]').text()).toContain("vite_build_failed");
  });

  it("preserves success across reopen and creates a new key only on Nueva entrega", async () => {
    createSubmission.mockResolvedValue({
      operation_id: "op_success",
      submission_id: "sub_success",
      status: "submitted",
      version: 2,
      artifact_sha256: "a".repeat(64),
      phases: [{ phase: "register", status: "succeeded" }],
    });
    const wrapper = mount(SubmitProjectDialog, {
      attachTo: document.body,
      props: { modelValue: false, name: "demo-ping" },
      global: { stubs: { Modal: { template: "<div><slot /><slot name='footer' /></div>" } } },
    });
    await wrapper.setProps({ modelValue: true });
    await flushPromises();
    await wrapper.get("textarea").setValue("First review context");
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    const firstKey = createSubmission.mock.calls[0][0].idempotencyKey;
    expect(wrapper.get(".submit-dialog__result").text()).toContain("sub_success");

    await wrapper.setProps({ modelValue: false });
    await wrapper.setProps({ modelValue: true });
    await flushPromises();
    expect(wrapper.get(".submit-dialog__result").text()).toContain("sub_success");
    expect(createSubmission).toHaveBeenCalledTimes(1);

    const newSubmission = wrapper.findAll("button").find((button) => button.text().includes("Nueva entrega"));
    await newSubmission.trigger("click");
    await flushPromises();
    expect(wrapper.find(".submit-dialog__result").exists()).toBe(false);
    expect(wrapper.get("textarea").element.value).toBe("");
    expect(document.activeElement).toBe(wrapper.get("input").element);

    await wrapper.get("textarea").setValue("Second review context");
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    const secondKey = createSubmission.mock.calls[1][0].idempotencyKey;
    expect(secondKey).not.toBe(firstKey);
    wrapper.unmount();
  });

  it("waits for terminal notify and shows each channel without blocking the submission", async () => {
    vi.useFakeTimers();
    createSubmission.mockResolvedValue({
      operation_id: "op_notify",
      submission_id: "sub_notify",
      status: "submitted",
      version: 3,
      phases: [{ phase: "notify", status: "running" }],
      notifications: [
        { provider_key: "ses", status: "delivered", error_code: null },
        { provider_key: "teams", status: "pending", error_code: null },
      ],
    });
    getSubmissionOperation.mockResolvedValue({
      operation_id: "op_notify",
      submission_id: "sub_notify",
      status: "submitted",
      version: 3,
      artifact_sha256: "b".repeat(64),
      phases: [{ phase: "notify", status: "delivery_unknown", error_code: "notification_delivery_unknown" }],
      notifications: [
        { provider_key: "ses", status: "delivered", error_code: null },
        { provider_key: "teams", status: "delivery_unknown", error_code: "notification_delivery_unknown" },
      ],
    });
    const wrapper = mount(SubmitProjectDialog, {
      props: { modelValue: false, name: "demo-ping" },
      global: { stubs: { Modal: { template: "<div><slot /><slot name='footer' /></div>" } } },
    });
    await wrapper.setProps({ modelValue: true });
    await vi.runAllTimersAsync();
    await wrapper.get("textarea").setValue("Notify reviewers");
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(wrapper.find(".submit-dialog__result").exists()).toBe(false);

    await vi.advanceTimersByTimeAsync(300);
    await flushPromises();
    expect(getSubmissionOperation).toHaveBeenCalledWith("op_notify");
    expect(wrapper.get(".submit-dialog__result").text()).toContain("sub_notify");
    expect(wrapper.get(".submit-dialog__notifications").text()).toContain("ses · delivered");
    expect(wrapper.get(".submit-dialog__notifications").text()).toContain("teams · delivery_unknown");
    expect(wrapper.findAll("button").some((button) => button.text().includes("Abrir entrega"))).toBe(true);
    wrapper.unmount();
    vi.useRealTimers();
  });
});
