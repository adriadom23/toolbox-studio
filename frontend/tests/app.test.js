import { flushPromises, mount } from "@vue/test-utils";
import { defineComponent, nextTick } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../src/studio/i18n/studioMessages";
import Modal from "../src/shared/components/Modal.vue";
import App from "../src/App.vue";
import {
  createSubmission,
  getSession,
  getSubmissionContext,
  listProjects,
} from "../src/studio/api/studioHttp";

vi.mock("../src/studio/api/studioHttp", async (importOriginal) => ({
  ...(await importOriginal()),
  createSubmission: vi.fn(),
  getSession: vi.fn(),
  getSubmissionContext: vi.fn(),
  getSubmissionOperation: vi.fn(),
  listProjects: vi.fn(),
}));

const originalGetClientRects = HTMLElement.prototype.getClientRects;

describe("App session and submission navigation", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/");
    vi.clearAllMocks();
    listProjects.mockResolvedValue([]);
  });

  afterEach(() => {
    HTMLElement.prototype.getClientRects = originalGetClientRects;
    document.body.innerHTML = "";
  });

  it("creates the local session before mounting protected views", async () => {
    const order = [];
    let release;
    getSession.mockImplementation(async () => {
      order.push("session-start");
      await new Promise((resolve) => { release = resolve; });
      order.push("session-ready");
      return { capabilities: ["submission.create"] };
    });
    const wrapper = mount(App, {
      global: {
        stubs: {
          StudioShell: true,
          ProjectList: { template: "<div data-test='projects' />", mounted: () => order.push("projects-mounted") },
          ProjectEditor: true,
          ArtifactPreview: true,
          SubmissionInbox: true,
          SubmissionDetail: true,
        },
      },
    });
    await flushPromises();
    expect(wrapper.find("[data-test='projects']").exists()).toBe(false);
    expect(order).toEqual(["session-start"]);

    release();
    await flushPromises();
    expect(wrapper.find("[data-test='projects']").exists()).toBe(true);
    expect(order).toEqual(["session-start", "session-ready", "projects-mounted"]);
  });

  it("keeps New project visible but disabled with an accessible explanation without capability", async () => {
    getSession.mockResolvedValue({ capabilities: [] });
    const wrapper = mount(App, {
      global: {
        stubs: {
          StudioShell: true,
          ProjectEditor: true,
          ArtifactPreview: true,
          SubmissionInbox: true,
          SubmissionDetail: true,
          NewProjectModal: true,
        },
      },
    });
    await flushPromises();
    const button = wrapper.get(".project-list__head button");
    expect(button.attributes("disabled")).toBeDefined();
    expect(button.attributes("aria-describedby")).toBe("project-create-hint");
    expect(wrapper.get("#project-create-hint").text()).toContain("no crear proyectos");
  });

  it("keeps terminal success in the modal and navigates only after explicit open", async () => {
    const submissionId = `sub_${"a".repeat(32)}`;
    const sha256 = "b".repeat(64);
    window.history.replaceState({}, "", "/projects/demo-ping");
    HTMLElement.prototype.getClientRects = () => ({ length: 1, 0: { width: 10, height: 10 } });
    getSession.mockResolvedValue({ capabilities: ["submission.create"] });
    getSubmissionContext.mockResolvedValue({
      project_slug: "demo-ping",
      next_version: 2,
      actor: { id: "alice", display_name: "Alice" },
      git: {},
      providers: { artifact: "local", build: "local-subprocess", logs: "local", notifications: [], publication: "local" },
      warnings: [],
      predecessors: [],
      actions_allowed: ["submission.create"],
    });
    createSubmission.mockResolvedValue({
      operation_id: `op_${"c".repeat(32)}`,
      submission_id: submissionId,
      status: "submitted",
      version: 2,
      artifact_sha256: sha256,
      phases: [{ phase: "register", status: "succeeded", error_code: null }],
    });
    const SubmissionDetailStub = defineComponent({
      emits: ["project-loaded"],
      mounted() { this.$emit("project-loaded", "demo-ping"); },
      template: "<div data-test='submission-detail' />",
    });
    const wrapper = mount(App, {
      attachTo: document.body,
      global: {
        stubs: {
          LanguageSwitcher: true,
          ThemeSwitcher: true,
          ProjectList: true,
          ProjectEditor: true,
          ArtifactPreview: true,
          SubmissionInbox: true,
          SubmissionDetail: SubmissionDetailStub,
        },
      },
    });
    await flushPromises();
    const opener = wrapper.get(".studio-shell__submit").element;
    opener.focus();
    await wrapper.get(".studio-shell__submit").trigger("click");
    await flushPromises();

    const description = document.querySelector(".submit-dialog textarea");
    description.value = "Contexto de revisión";
    description.dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector(".submit-dialog").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await flushPromises();
    await nextTick();

    expect(window.location.pathname).toBe("/projects/demo-ping");
    expect(wrapper.find("[data-test='submission-detail']").exists()).toBe(false);
    const result = document.querySelector(".submit-dialog__result");
    expect(result.getAttribute("aria-live")).toBe("polite");
    expect(result.textContent).toContain(submissionId);
    expect(result.textContent).toContain("Enviado");
    expect(result.textContent).toContain(sha256);
    expect(document.querySelector(".tb-modal").contains(document.activeElement)).toBe(true);

    const openButton = [...document.querySelectorAll(".tb-modal__footer button")]
      .find((button) => button.textContent.includes("Abrir entrega"));
    openButton.focus();
    openButton.click();
    await flushPromises();
    await nextTick();

    expect(window.location.pathname).toBe(`/submissions/${submissionId}`);
    expect(wrapper.find("[data-test='submission-detail']").exists()).toBe(true);
    wrapper.findComponent(Modal).vm.$.setupState.onAfterLeave();
    await nextTick();
    expect(document.activeElement).toBe(opener);
    wrapper.unmount();
  });

  it("resolves project ACL before enabling submit and refreshes revoked access", async () => {
    const submissionId = `sub_${"d".repeat(32)}`;
    window.history.replaceState({}, "", `/submissions/${submissionId}`);
    getSession.mockResolvedValue({ capabilities: ["submission.create"] });
    getSubmissionContext.mockRejectedValue(new Error("not found"));
    const SubmissionDetailStub = defineComponent({
      emits: ["project-loaded"],
      mounted() { this.$emit("project-loaded", "demo-ping"); },
      template: "<button data-test='refresh-project' @click=\"$emit('project-loaded', 'demo-ping')\">refresh</button>",
    });
    const wrapper = mount(App, {
      attachTo: document.body,
      global: {
        stubs: {
          LanguageSwitcher: true,
          ThemeSwitcher: true,
          ProjectList: true,
          ProjectEditor: true,
          ArtifactPreview: true,
          SubmissionInbox: true,
          SubmissionDetail: SubmissionDetailStub,
        },
      },
    });
    await flushPromises();
    const submit = wrapper.get(".studio-shell__submit");
    expect(submit.attributes("disabled")).toBeDefined();
    expect(wrapper.get("#studio-submit-hint").text()).toContain("Ya no tienes acceso");
    await submit.trigger("click");
    expect(createSubmission).not.toHaveBeenCalled();
    expect(document.querySelector(".tb-modal")).toBeNull();

    getSubmissionContext.mockResolvedValue({ actions_allowed: ["submission.create"] });
    await wrapper.get("[data-test='refresh-project']").trigger("click");
    await flushPromises();
    expect(submit.attributes("disabled")).toBeUndefined();
    expect(wrapper.find("#studio-submit-hint").exists()).toBe(false);
    wrapper.unmount();
  });
});
