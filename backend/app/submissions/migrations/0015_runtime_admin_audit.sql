ALTER TABLE preview_runtime_events ADD COLUMN error_code TEXT;

CREATE TABLE IF NOT EXISTS admin_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    correlation_id TEXT,
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS admin_audit_events_no_update
BEFORE UPDATE ON admin_audit_events BEGIN
    SELECT RAISE(ABORT, 'admin audit events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS admin_audit_events_no_delete
BEFORE DELETE ON admin_audit_events BEGIN
    SELECT RAISE(ABORT, 'admin audit events are append-only');
END;
