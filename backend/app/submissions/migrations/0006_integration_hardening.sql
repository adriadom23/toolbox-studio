ALTER TABLE build_jobs ADD COLUMN start_marker TEXT;
ALTER TABLE build_jobs ADD COLUMN external_id TEXT;
ALTER TABLE submission_builds ADD COLUMN external_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_build_jobs_start_marker
ON build_jobs(start_marker)
WHERE start_marker IS NOT NULL;

CREATE TABLE IF NOT EXISTS project_create_reservations (
    slug TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('reserved','completed','diagnostic')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
