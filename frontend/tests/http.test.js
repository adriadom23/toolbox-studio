import { beforeEach, describe, expect, it, vi } from "vitest";

import { request } from "../src/shared/api/http";
import { createSubmission } from "../src/studio/api/studioHttp";

function jsonResponse(payload, { ok = true, status = 200 } = {}) {
  return { ok, status, json: vi.fn().mockResolvedValue(payload) };
}

describe("shared API request", () => {
  beforeEach(() => {
    vi.useRealTimers();
    document.cookie = "studio_csrf=csrf-token; path=/";
    global.fetch = vi.fn();
  });

  it("adds session credentials and CSRF to mutations without retrying them", async () => {
    fetch.mockRejectedValue(new TypeError("offline"));

    await expect(request("/submissions", { method: "POST", body: "{}" })).rejects.toThrow();

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][1]).toMatchObject({
      credentials: "same-origin",
      method: "POST",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
    });
  });

  it("keeps strict submission payloads free of undeclared locale fields", async () => {
    fetch.mockResolvedValue(jsonResponse({ operation_id: "op_1", status: "building" }, { status: 202 }));

    await createSubmission({
      projectSlug: "demo-ping",
      title: "Demo",
      description: "Ready",
      idempotencyKey: "stable-key",
    });

    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      project_slug: "demo-ping",
      title: "Demo",
      description: "Ready",
      predecessor_id: null,
    });
  });

  it("retries a transient GET once", async () => {
    vi.useFakeTimers();
    fetch
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce(jsonResponse({ items: [] }));

    const response = request("/submissions");
    await vi.advanceTimersByTimeAsync(1500);

    await expect(response).resolves.toEqual({ items: [] });
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
