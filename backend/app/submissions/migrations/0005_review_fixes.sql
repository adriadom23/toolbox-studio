ALTER TABLE artifact_objects ADD COLUMN submission_id TEXT REFERENCES submission_records(id) ON DELETE RESTRICT;
ALTER TABLE artifact_objects ADD COLUMN manifest_json TEXT;
ALTER TABLE artifact_objects ADD COLUMN input_sha256 TEXT;
ALTER TABLE artifact_objects ADD COLUMN harness_sha256 TEXT;
ALTER TABLE preview_objects ADD COLUMN runtime_ref TEXT;
ALTER TABLE preview_sessions ADD COLUMN actor_id TEXT;

CREATE TABLE IF NOT EXISTS submission_phases (
    submission_id TEXT NOT NULL REFERENCES submission_records(id) ON DELETE RESTRICT,
    phase TEXT NOT NULL CHECK(phase IN ('validation','snapshot','build','package','register','notify')),
    status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','delivery_unknown')),
    error_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(submission_id, phase)
);

CREATE TABLE IF NOT EXISTS preview_runtime_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    preview_id TEXT NOT NULL REFERENCES preview_objects(id) ON DELETE RESTRICT,
    actor_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    method TEXT NOT NULL,
    runtime_path TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    correlation_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS preview_runtime_events_no_update
BEFORE UPDATE ON preview_runtime_events BEGIN
    SELECT RAISE(ABORT, 'preview runtime events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS preview_runtime_events_no_delete
BEFORE DELETE ON preview_runtime_events BEGIN
    SELECT RAISE(ABORT, 'preview runtime events are append-only');
END;
