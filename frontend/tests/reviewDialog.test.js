import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import "../src/studio/i18n/studioMessages";
import ReviewActionDialog from "../src/studio/ReviewActionDialog.vue";
import { reviewSubmission } from "../src/studio/api/studioHttp";

vi.mock("../src/studio/api/studioHttp", () => ({ reviewSubmission: vi.fn() }));

describe("ReviewActionDialog", () => {
  it("accepts and sends an optional approval comment", async () => {
    reviewSubmission.mockResolvedValue({ state: "approved", row_version: 3 });
    const wrapper = mount(ReviewActionDialog, {
      props: {
        modelValue: true,
        submissionId: "sub_11111111111111111111111111111111",
        state: "under_review",
        rowVersion: 2,
      },
      global: { stubs: { Modal: { template: "<div><slot /><slot name='footer' /></div>" } } },
    });
    await wrapper.get("select").setValue("approve");
    await wrapper.get("textarea").setValue("Looks good");
    await wrapper.get(".tb-button--primary").trigger("click");
    await flushPromises();
    expect(reviewSubmission).toHaveBeenCalledWith(
      "sub_11111111111111111111111111111111",
      {
        action: "approve",
        expected_row_version: 2,
        reason: "Looks good",
        publication_target: "none",
      },
    );
  });
});
