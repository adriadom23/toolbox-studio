import { apiUrl, request } from "../../shared/api/http";

export function getSession() {
  return request("/session");
}

export function listProjects() {
  return request("/projects");
}

export function createProject(name, idempotencyKey) {
  return request("/projects", {
    method: "POST",
    includeLocale: false,
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ name }),
  });
}

export function openProject(name) {
  return request(`/projects/${encodeURIComponent(name)}`);
}

export function saveProjectFiles(name, files) {
  return request(`/projects/${encodeURIComponent(name)}/files`, {
    method: "PUT", includeLocale: false,
    body: JSON.stringify({ files }),
  });
}

export function getSubmissionContext(projectSlug) {
  return request(`/projects/${encodeURIComponent(projectSlug)}/submission-context`);
}

export function createSubmission({ projectSlug, title, description, predecessorId, idempotencyKey }) {
  return request("/submissions", {
    method: "POST", includeLocale: false,
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ project_slug: projectSlug, title, description, predecessor_id: predecessorId || null }),
  });
}

export function getSubmissionOperation(operationIdOrUrl) {
  const path = String(operationIdOrUrl).startsWith("/api/")
    ? String(operationIdOrUrl).slice(4)
    : `/submission-operations/${encodeURIComponent(operationIdOrUrl)}`;
  return request(path);
}

export function listSubmissions({ cursor, projectSlug, status, author, createdFrom, createdTo, limit = 25 } = {}) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) params.set("cursor", cursor);
  if (projectSlug) params.set("project_slug", projectSlug);
  if (status) params.set("status", status);
  if (author) params.set("author", author);
  if (createdFrom) params.set("created_from", createdFrom);
  if (createdTo) params.set("created_to", createdTo);
  return request(`/submissions?${params.toString()}`);
}

export function getSubmission(submissionId) {
  return request(`/submissions/${encodeURIComponent(submissionId)}`);
}

export function getSubmissionManifest(submissionId, { cursor, pageSize = 250 } = {}) {
  const params = new URLSearchParams({ page_size: String(pageSize) });
  if (cursor) params.set("cursor", cursor);
  return request(`/submissions/${encodeURIComponent(submissionId)}/manifest?${params.toString()}`);
}

export function getSubmissionLogs(submissionId, { phase = "frontend", cursor, lineLimit = 200 } = {}) {
  const params = new URLSearchParams({ phase, line_limit: String(lineLimit) });
  if (cursor) params.set("cursor", cursor);
  return request(`/submissions/${encodeURIComponent(submissionId)}/logs?${params.toString()}`);
}

export function addSubmissionComment(submissionId, text) {
  return request(`/submissions/${encodeURIComponent(submissionId)}/comments`, {
    method: "POST", includeLocale: false,
    body: JSON.stringify({ text }),
  });
}

export function reviewSubmission(submissionId, payload) {
  return request(`/submissions/${encodeURIComponent(submissionId)}/review`, {
    method: "POST", includeLocale: false,
    body: JSON.stringify(payload),
  });
}

export function publishSubmission(submissionId, expectedRowVersion) {
  return request(`/submissions/${encodeURIComponent(submissionId)}/publish`, {
    method: "POST", includeLocale: false,
    body: JSON.stringify({ expected_row_version: expectedRowVersion, target: "local-index" }),
  });
}

export function retrySubmissionEffect(submissionId, effectId, expectedGeneration) {
  return request(`/submissions/${encodeURIComponent(submissionId)}/effects/${encodeURIComponent(effectId)}/retry`, {
    method: "POST", includeLocale: false,
    body: JSON.stringify({ expected_generation: expectedGeneration }),
  });
}

export function buildWorkspacePreview(projectSlug, idempotencyKey) {
  return request(`/projects/${encodeURIComponent(projectSlug)}/preview-builds`, {
    method: "POST", includeLocale: false,
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({}),
  });
}

export function retrySubmissionPreview(submissionId, idempotencyKey) {
  return request(`/submissions/${encodeURIComponent(submissionId)}/preview-retries`, {
    method: "POST", includeLocale: false,
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({}),
  });
}

export function getPreviewOperation(operationIdOrUrl) {
  const path = String(operationIdOrUrl).startsWith("/api/")
    ? String(operationIdOrUrl).slice(4)
    : `/preview-operations/${encodeURIComponent(operationIdOrUrl)}`;
  return request(path);
}

export function getPreviewStatus(previewId) {
  return request(`/preview-status/${encodeURIComponent(previewId)}`);
}

export function createPreviewGrant({ subjectKind, subjectId, previewId }) {
  return request("/preview-grants", {
    method: "POST", includeLocale: false,
    body: JSON.stringify({
      subject_kind: subjectKind,
      subject_id: subjectId,
      preview_id: previewId || null,
    }),
  });
}

export function submissionArtifactUrl(submissionId) {
  return apiUrl(`/submissions/${encodeURIComponent(submissionId)}/artifact`);
}
