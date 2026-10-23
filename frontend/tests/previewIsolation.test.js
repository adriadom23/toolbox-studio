import { describe, expect, it, vi } from "vitest";

import { matchPreviewPath, postPreviewGrant } from "../src/projects/_previewEntry";

describe("preview isolation bridge", () => {
  it("parses the shell-owned preview route", () => {
    expect(matchPreviewPath("/projects/demo-ping/preview")).toBe("demo-ping");
    expect(matchPreviewPath("/projects/demo-ping")).toBeNull();
  });

  it("posts a one-use grant only to the preview origin", () => {
    const frameWindow = { postMessage: vi.fn() };
    const sent = postPreviewGrant(
      frameWindow,
      { preview_id: "prv_1", nonce: "secret", bootstrap_url: "http://127.0.0.1:5001/bootstrap" },
      { kind: "submission", id: "sub_1" },
    );

    expect(sent).toBe(true);
    expect(frameWindow.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ type: "toolbox-preview-grant" }),
      "http://127.0.0.1:5001",
    );
  });
});
