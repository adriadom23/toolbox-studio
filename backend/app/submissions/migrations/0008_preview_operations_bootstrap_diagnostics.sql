CREATE TABLE IF NOT EXISTS preview_build_operations (
    operation_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    project_slug TEXT NOT NULL REFERENCES projects(slug) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','ready','failed')),
    phase TEXT NOT NULL CHECK(phase IN ('reserved','snapshot','build','seal','ready','failed')),
    preview_id TEXT REFERENCES preview_objects(id) ON DELETE RESTRICT,
    input_sha256 TEXT,
    harness_sha256 TEXT,
    snapshot_ref TEXT,
    output_ref TEXT,
    reservation_id TEXT NOT NULL,
    error_code TEXT,
    recoverable INTEGER NOT NULL DEFAULT 0 CHECK(recoverable IN (0,1)),
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(actor_id, project_slug, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_preview_build_operations_recovery
ON preview_build_operations(state, lease_until);

CREATE TABLE IF NOT EXISTS project_bootstrap_diagnostics (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT
);
