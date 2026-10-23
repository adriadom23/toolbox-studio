CREATE TABLE IF NOT EXISTS projects (
    slug TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT,
    modified_at TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    version INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'filesystem'
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_slug TEXT NOT NULL REFERENCES projects(slug),
    status TEXT NOT NULL,
    package_path TEXT,
    package_sha256 TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS submission_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER REFERENCES submissions(id),
    project_slug TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    dedupe_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS local_claims (
    claim_key TEXT PRIMARY KEY,
    identity TEXT NOT NULL,
    claimed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_submission_events_project ON submission_events(project_slug, id);
