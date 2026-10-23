CREATE TABLE IF NOT EXISTS submission_records (
    id TEXT PRIMARY KEY,
    project_slug TEXT NOT NULL REFERENCES projects(slug) ON DELETE RESTRICT,
    version_seq INTEGER NOT NULL,
    version_label TEXT NOT NULL,
    state TEXT NOT NULL,
    author_id TEXT NOT NULL,
    author_display TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    input_sha256 TEXT,
    harness_sha256 TEXT,
    artifact_ref TEXT,
    artifact_sha256 TEXT,
    artifact_size INTEGER,
    manifest_json TEXT,
    git_json TEXT NOT NULL DEFAULT '{}',
    row_version INTEGER NOT NULL DEFAULT 1,
    correlation_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_slug, version_seq)
);
CREATE INDEX IF NOT EXISTS idx_submission_records_list ON submission_records(id, state, project_slug);
CREATE TABLE IF NOT EXISTS artifact_objects (
    object_id TEXT PRIMARY KEY,
    ref TEXT UNIQUE,
    expected_sha256 TEXT,
    expected_size INTEGER,
    state TEXT NOT NULL,
    provider TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submission_builds (
    id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL UNIQUE REFERENCES submission_records(id) ON DELETE RESTRICT,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    input_sha256 TEXT,
    harness_sha256 TEXT,
    error_code TEXT,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS build_jobs (
    submission_id TEXT PRIMARY KEY REFERENCES submission_records(id) ON DELETE RESTRICT,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    heartbeat_at TEXT,
    last_error_code TEXT
);
CREATE TABLE IF NOT EXISTS idempotency_records (
    scope TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    route_template TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    subject_id TEXT,
    status_code INTEGER,
    response_json TEXT,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(scope, actor_id, route_template, resource_id, key)
);
CREATE TABLE IF NOT EXISTS submission_log_refs (
    submission_id TEXT NOT NULL REFERENCES submission_records(id) ON DELETE RESTRICT,
    phase TEXT NOT NULL,
    log_ref TEXT NOT NULL,
    PRIMARY KEY(submission_id, phase)
);
CREATE TABLE IF NOT EXISTS submission_operation_events (
    submission_id TEXT NOT NULL REFERENCES submission_records(id) ON DELETE RESTRICT,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(submission_id, seq)
);
