// Thin one-line wrappers around the vendored shared/api/http.js's
// request() (design.md §5.1/§5.4 step 4), each calling one of FEAT-003's
// backend routes (design.md §4.3). request() prefixes "/api", does JSON
// in/out, retries transient network/5xx failures, and throws an Error with
// a readable .message + .status/.payload on failure.
import { request } from "../../shared/api/http";

export function listProjects() {
  return request("/projects");
}

export function createProject(name) {
  return request("/projects", { method: "POST", body: JSON.stringify({ name }) });
}

export function openProject(name) {
  return request(`/projects/${encodeURIComponent(name)}`);
}

export function saveProjectFiles(name, files) {
  return request(`/projects/${encodeURIComponent(name)}/files`, {
    method: "PUT",
    body: JSON.stringify({ files }),
  });
}

export function deleteProject(name) {
  return request(`/projects/${encodeURIComponent(name)}`, {
    method: "DELETE",
    body: JSON.stringify({ confirm: true }),
  });
}

export function submitProject(name) {
  // The backend derives `submitted_by` from `git config user.name/email`
  // on the ToolboxStudio checkout (packaging.default_submitted_by), so the
  // UI sends an empty body — it has no better source for the submitter.
  return request(`/projects/${encodeURIComponent(name)}/submit`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

// Read-only exposure of the configured owner/notification list (design.md
// §3.2's "This will notify: adriadom, zesparzj" framing) — backs the
// "Enviar código" confirmation dialog. See FEAT-004's findings for why
// this small route was added (no existing route exposed it read-only).
export function getNotificationTargets() {
  return request("/config/owners");
}
