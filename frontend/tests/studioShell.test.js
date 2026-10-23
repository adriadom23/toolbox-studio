import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import "../src/studio/i18n/studioMessages";
import StudioShell from "../src/studio/StudioShell.vue";

const global = {
  stubs: {
    LanguageSwitcher: true,
    ThemeSwitcher: true,
    SubmitProjectDialog: true,
  },
};

describe("StudioShell", () => {
  it("owns exactly one permanent official submit control", () => {
    const wrapper = mount(StudioShell, { props: { activeProject: "demo-ping", capabilities: ["submission.create"], projectSubmitState: "allowed" }, global });
    const controls = wrapper.findAll(".studio-shell__submit");
    expect(controls).toHaveLength(1);
    expect(controls[0].attributes("disabled")).toBeUndefined();
  });

  it("keeps submit visible but explained and disabled without a project", () => {
    const wrapper = mount(StudioShell, { global });
    const control = wrapper.get(".studio-shell__submit");
    expect(control.attributes("disabled")).toBeDefined();
    expect(control.attributes("title")).toContain("Selecciona");
    expect(control.attributes("aria-describedby")).toBe("studio-submit-hint");
    expect(wrapper.get('[role="status"]').text()).toContain("Selecciona");
  });

  it("does not enable submit when the session lacks project capability", () => {
    const wrapper = mount(StudioShell, { props: { activeProject: "demo-ping", capabilities: [] }, global });
    const control = wrapper.get(".studio-shell__submit");
    expect(control.attributes("disabled")).toBeDefined();
    expect(control.attributes("title")).toContain("permiso");
  });

  it("reacts accessibly when project ACL is revoked and restored", async () => {
    const wrapper = mount(StudioShell, {
      props: {
        activeProject: "demo-ping",
        capabilities: ["submission.create"],
        projectSubmitState: "denied",
      },
      global,
    });
    const control = wrapper.get(".studio-shell__submit");
    expect(control.attributes("disabled")).toBeDefined();
    expect(wrapper.get('[role="status"]').text()).toContain("Ya no tienes acceso");
    await control.trigger("click");
    expect(wrapper.findComponent({ name: "SubmitProjectDialog" }).props("modelValue")).toBe(false);

    await wrapper.setProps({ projectSubmitState: "allowed" });
    expect(control.attributes("disabled")).toBeUndefined();
    expect(wrapper.find('[role="status"]').exists()).toBe(false);
    await control.trigger("click");
    expect(wrapper.findComponent({ name: "SubmitProjectDialog" }).props("modelValue")).toBe(true);
  });
});
