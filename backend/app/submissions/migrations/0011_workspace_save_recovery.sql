CREATE TABLE IF NOT EXISTS workspace_save_recovery (
    journal_id TEXT PRIMARY KEY,
    project_slug TEXT NOT NULL REFERENCES projects(slug) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('recovering','completed','failed')),
    manifest_sha256 TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workspace_save_recovery_project_status
    ON workspace_save_recovery(project_slug, status);

CREATE TABLE IF NOT EXISTS workspace_save_items (
    journal_id TEXT NOT NULL REFERENCES workspace_save_recovery(journal_id) ON DELETE RESTRICT,
    item_index INTEGER NOT NULL CHECK (item_index >= 0),
    canonical_path TEXT NOT NULL,
    expected_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('prepared','promoted')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (journal_id, item_index),
    UNIQUE (journal_id, canonical_path)
);
