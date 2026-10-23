import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../src/studio/i18n/studioMessages";
import BuildLogPanel from "../src/studio/BuildLogPanel.vue";
import ManifestTable from "../src/studio/ManifestTable.vue";
import { getSubmissionLogs, getSubmissionManifest } from "../src/studio/api/studioHttp";

vi.mock("../src/studio/api/studioHttp", () => ({
  getSubmissionLogs: vi.fn(),
  getSubmissionManifest: vi.fn(),
}));

describe("submission manifest and log phases", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getSubmissionLogs.mockResolvedValue({ lines: ["remote build"], next_cursor: null });
    getSubmissionManifest
      .mockResolvedValueOnce({ format: 2, files: [{ path: "a", kind: "frontend", size: 1, sha256: "a" }], total: 2, next_cursor: "next" })
      .mockResolvedValueOnce({ format: 2, files: [{ path: "b", kind: "backend", size: 2, sha256: "b" }], total: 2, next_cursor: null });
  });

  it("selects an available remote CodeBuild phase", async () => {
    const wrapper = mount(BuildLogPanel, { props: { submissionId: "sub_1", availablePhases: ["remote"] } });
    await flushPromises();
    expect(wrapper.get('[role="tab"]').text()).toBe("remote");
    expect(getSubmissionLogs).toHaveBeenCalledWith("sub_1", { phase: "remote", cursor: null });
  });

  it("loads manifest pages incrementally", async () => {
    const wrapper = mount(ManifestTable, { props: { submissionId: "sub_1" } });
    await flushPromises();
    expect(wrapper.findAll("tbody tr")).toHaveLength(1);
    await wrapper.get(".tb-button").trigger("click");
    await flushPromises();
    expect(wrapper.findAll("tbody tr")).toHaveLength(2);
    expect(getSubmissionManifest).toHaveBeenLastCalledWith("sub_1", { cursor: "next" });
  });
});
