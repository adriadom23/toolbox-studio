import { mount } from "@vue/test-utils";
import { defineComponent, nextTick, ref } from "vue";
import { describe, expect, it } from "vitest";

import Modal from "../src/shared/components/Modal.vue";

function visibleRects() {
  return { length: 1, 0: { width: 10, height: 10 } };
}

describe("Modal keyboard behavior", () => {
  it("labels the dialog and closes the top modal with Escape", async () => {
    const wrapper = mount(Modal, {
      attachTo: document.body,
      props: { modelValue: true, title: "Review", ariaDescription: "Choose an action" },
      slots: { default: "Content" },
    });
    document.querySelectorAll(".tb-modal, .tb-modal__shell").forEach((element) => {
      element.getClientRects = visibleRects;
    });

    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog.getAttribute("aria-labelledby")).toBeTruthy();
    expect(dialog.getAttribute("aria-describedby")).toBeTruthy();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wrapper.vm.$nextTick();
    expect(wrapper.emitted("update:modelValue")?.[0]).toEqual([false]);
    wrapper.unmount();
  });

  it("restores the visible enabled opener without stealing deliberately moved focus", async () => {
    const Host = defineComponent({
      components: { Modal },
      setup() {
        const open = ref(false);
        return { open };
      },
      template: `
        <button class="opener" type="button" @click="open = true">Open</button>
        <button class="elsewhere" type="button">Elsewhere</button>
        <Modal v-model="open" title="Submit"><button class="inside" type="button">Inside</button></Modal>
      `,
    });
    const wrapper = mount(Host, { attachTo: document.body });
    const opener = wrapper.find(".opener").element;
    const elsewhere = wrapper.find(".elsewhere").element;
    opener.getClientRects = visibleRects;
    elsewhere.getClientRects = visibleRects;

    opener.focus();
    await wrapper.find(".opener").trigger("click");
    await nextTick();
    document.querySelectorAll(".tb-modal, .tb-modal__shell, .inside").forEach((element) => {
      element.getClientRects = visibleRects;
    });
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    wrapper.findComponent(Modal).vm.$.setupState.onAfterLeave();
    await nextTick();
    expect(document.activeElement).toBe(opener);

    opener.focus();
    await wrapper.find(".opener").trigger("click");
    await nextTick();
    document.querySelectorAll(".tb-modal, .tb-modal__shell, .inside").forEach((element) => {
      element.getClientRects = visibleRects;
    });
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    elsewhere.focus();
    await nextTick();
    wrapper.findComponent(Modal).vm.$.setupState.onAfterLeave();
    await nextTick();
    expect(document.activeElement).toBe(elsewhere);
    wrapper.unmount();
  });
});
