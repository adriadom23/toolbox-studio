ALTER TABLE submission_records ADD COLUMN legacy INTEGER NOT NULL DEFAULT 0;
ALTER TABLE submission_records ADD COLUMN artifact_provider TEXT NOT NULL DEFAULT 'filesystem';

CREATE TABLE IF NOT EXISTS submission_comments (
    id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES submission_records(id) ON DELETE RESTRICT,
    seq INTEGER NOT NULL,
    author_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(submission_id, seq)
);
CREATE TRIGGER IF NOT EXISTS submission_comments_no_update
BEFORE UPDATE ON submission_comments BEGIN
    SELECT RAISE(ABORT, 'submission comments are append-only');
END;
CREATE TRIGGER IF NOT EXISTS submission_comments_no_delete
BEFORE DELETE ON submission_comments BEGIN
    SELECT RAISE(ABORT, 'submission comments are append-only');
END;
CREATE TRIGGER IF NOT EXISTS submission_events_no_update
BEFORE UPDATE ON submission_operation_events BEGIN
    SELECT RAISE(ABORT, 'submission events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS submission_events_no_delete
BEFORE DELETE ON submission_operation_events BEGIN
    SELECT RAISE(ABORT, 'submission events are append-only');
END;

CREATE TABLE IF NOT EXISTS outbox_effects (
    id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL REFERENCES submission_records(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK(kind IN ('notification','publication')),
    generation INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK(status IN ('pending','delivered','failed','delivery_unknown')),
    payload_json TEXT NOT NULL,
    provider_id TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effect_attempts (
    effect_id TEXT NOT NULL REFERENCES outbox_effects(id) ON DELETE RESTRICT,
    generation INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(effect_id, generation, attempt)
);
CREATE TRIGGER IF NOT EXISTS effect_attempts_no_update
BEFORE UPDATE ON effect_attempts BEGIN
    SELECT RAISE(ABORT, 'effect attempts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS effect_attempts_no_delete
BEFORE DELETE ON effect_attempts BEGIN
    SELECT RAISE(ABORT, 'effect attempts are append-only');
END;

CREATE TABLE IF NOT EXISTS legacy_sources (
    id TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    submission_id TEXT NOT NULL REFERENCES submission_records(id) ON DELETE RESTRICT,
    imported_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'indexed',
    UNIQUE(relative_path, sha256)
);
CREATE TABLE IF NOT EXISTS legacy_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    code TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(relative_path, sha256, code)
);

CREATE TABLE IF NOT EXISTS storage_reservations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    state TEXT NOT NULL,
    expires_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preview_objects (
    id TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('submission','workspace')),
    subject_id TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    harness_sha256 TEXT NOT NULL,
    digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('building','ready','failed','unavailable','quarantined')),
    manifest_json TEXT NOT NULL DEFAULT '{}',
    root_ref TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_kind, subject_id, digest)
);
CREATE TABLE IF NOT EXISTS preview_grants (
    id TEXT PRIMARY KEY,
    preview_id TEXT NOT NULL REFERENCES preview_objects(id) ON DELETE RESTRICT,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    nonce_sha256 TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preview_sessions (
    id TEXT PRIMARY KEY,
    preview_id TEXT NOT NULL REFERENCES preview_objects(id) ON DELETE RESTRICT,
    token_sha256 TEXT NOT NULL UNIQUE,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
