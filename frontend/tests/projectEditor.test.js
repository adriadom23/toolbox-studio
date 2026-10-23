import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { editorControls, openProject, saveProjectFiles } = vi.hoisted(() => ({
  editorControls: [],
  openProject: vi.fn(),
  saveProjectFiles: vi.fn(),
}));

vi.mock("../src/studio/api/studioHttp", () => ({ openProject, saveProjectFiles }));
vi.mock("../src/studio/codemirrorSetup", () => ({
  createEditor: ({ doc, onChange }) => {
    let value = doc;
    const control = {
      setValue(next) {
        value = next;
        onChange(next);
      },
      view: { destroy: vi.fn() },
      getValue: () => value,
    };
    editorControls.push(control);
    return control;
  },
}));

import "../src/studio/i18n/studioMessages";
import ProjectEditor from "../src/studio/ProjectEditor.vue";

beforeEach(() => {
  vi.clearAllMocks();
  editorControls.length = 0;
  openProject.mockResolvedValue({
    files: [
      { path: "frontend/Demo.vue", type: "editable", content: "<template />", size: 12, sha256: "a".repeat(64) },
      { path: "frontend/image.bin", type: "opaque", size: 4, sha256: "b".repeat(64) },
    ],
  });
  saveProjectFiles.mockResolvedValue({ modified_at: "2026-01-01T00:00:00Z" });
});

describe("ProjectEditor file saves", () => {
  it("shows opaque metadata as read-only and saves only the dirty editable file", async () => {
    const wrapper = mount(ProjectEditor, {
      props: { name: "demo" },
      global: { stubs: { ComponentCatalog: true } },
    });
    await flushPromises();
    editorControls.at(-1).setValue("<template>changed</template>");

    const opaqueButton = wrapper.findAll(".project-editor__file button").find((button) => button.text().includes("image.bin"));
    await opaqueButton.trigger("click");
    await flushPromises();
    expect(wrapper.get(".project-editor__opaque").text()).toContain("solo lectura");
    expect(wrapper.get(".project-editor__opaque").text()).toContain("4 B");
    expect(wrapper.get(".project-editor__opaque").text()).toContain("b".repeat(64));
    expect(wrapper.find("textarea").exists()).toBe(false);

    await wrapper.get("button.tb-button--primary").trigger("click");
    await flushPromises();
    expect(saveProjectFiles).toHaveBeenCalledWith("demo", [
      { path: "frontend/Demo.vue", content: "<template>changed</template>", base_sha256: "a".repeat(64) },
    ]);
  });

  it("splits more than 100 dirty files into bounded batches", async () => {
    openProject.mockResolvedValue({
      files: Array.from({ length: 101 }, (_, index) => ({
        path: `frontend/file-${index}.js`,
        type: "editable",
        content: `old-${index}`,
        size: 8,
        sha256: index.toString(16).padStart(64, "0"),
      })),
    });
    const wrapper = mount(ProjectEditor, {
      props: { name: "large-project" },
      global: { stubs: { ComponentCatalog: true } },
    });
    await flushPromises();
    const fileState = wrapper.vm.$.setupState.files;
    editorControls.at(-1).setValue(`new-${fileState[0].path}`);
    for (const file of fileState.slice(1)) {
      file.draft = `new-${file.path}`;
      file.dirty = true;
    }

    await wrapper.get("button.tb-button--primary").trigger("click");
    await flushPromises();
    expect(saveProjectFiles).toHaveBeenCalledTimes(2);
    expect(saveProjectFiles.mock.calls.map((call) => call[1].length)).toEqual([100, 1]);
    await vi.waitFor(() => expect(fileState.every((file) => file.dirty === false)).toBe(true));
  });
});
