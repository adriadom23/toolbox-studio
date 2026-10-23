ALTER TABLE projects ADD COLUMN owner_id TEXT;
ALTER TABLE submission_records ADD COLUMN predecessor_id TEXT REFERENCES submission_records(id) ON DELETE RESTRICT;
ALTER TABLE submission_operation_events ADD COLUMN actor_roles_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE submission_operation_events ADD COLUMN old_state TEXT;
ALTER TABLE submission_operation_events ADD COLUMN new_state TEXT;
ALTER TABLE submission_operation_events ADD COLUMN correlation_id TEXT;
ALTER TABLE outbox_effects ADD COLUMN lease_owner TEXT;
ALTER TABLE outbox_effects ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox_effects ADD COLUMN lease_until TEXT;

CREATE TABLE IF NOT EXISTS project_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_slug TEXT NOT NULL REFERENCES projects(slug) ON DELETE RESTRICT,
    user_id TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    version INTEGER NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_slug, user_id, version)
);
CREATE INDEX IF NOT EXISTS idx_project_memberships_current
ON project_memberships(project_slug, user_id, version DESC);

CREATE TABLE IF NOT EXISTS proxy_replay_nonces (
    nonce_hash TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS project_memberships_no_update
BEFORE UPDATE ON project_memberships BEGIN
    SELECT RAISE(ABORT, 'project memberships are append-only');
END;
CREATE TRIGGER IF NOT EXISTS project_memberships_no_delete
BEFORE DELETE ON project_memberships BEGIN
    SELECT RAISE(ABORT, 'project memberships are append-only');
END;
CREATE TRIGGER IF NOT EXISTS proxy_replay_nonces_no_update
BEFORE UPDATE ON proxy_replay_nonces BEGIN
    SELECT RAISE(ABORT, 'proxy replay nonces are append-only');
END;
CREATE TRIGGER IF NOT EXISTS proxy_replay_nonces_no_delete
BEFORE DELETE ON proxy_replay_nonces BEGIN
    SELECT RAISE(ABORT, 'proxy replay nonces are append-only');
END;

CREATE TRIGGER IF NOT EXISTS submission_artifact_required_on_insert
BEFORE INSERT ON submission_records
WHEN NEW.state IN ('submitted','under_review','changes_requested','rejected','approved','published')
 AND (NEW.artifact_ref IS NULL OR NEW.artifact_sha256 IS NULL OR NEW.artifact_size IS NULL OR NEW.manifest_json IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'terminal submission requires verified artifact');
END;
CREATE TRIGGER IF NOT EXISTS submission_artifact_required_on_update
BEFORE UPDATE OF state,artifact_ref,artifact_sha256,artifact_size,manifest_json ON submission_records
WHEN NEW.state IN ('submitted','under_review','changes_requested','rejected','approved','published')
 AND (NEW.artifact_ref IS NULL OR NEW.artifact_sha256 IS NULL OR NEW.artifact_size IS NULL OR NEW.manifest_json IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'terminal submission requires verified artifact');
END;

CREATE TABLE IF NOT EXISTS project_security_events (
    project_slug TEXT NOT NULL REFERENCES projects(slug) ON DELETE RESTRICT,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    old_state TEXT,
    new_state TEXT,
    data_json TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_slug, seq)
);
CREATE TRIGGER IF NOT EXISTS project_security_events_no_update
BEFORE UPDATE ON project_security_events BEGIN
    SELECT RAISE(ABORT, 'project security events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS project_security_events_no_delete
BEFORE DELETE ON project_security_events BEGIN
    SELECT RAISE(ABORT, 'project security events are append-only');
END;
