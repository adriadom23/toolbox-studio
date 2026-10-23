CREATE TABLE IF NOT EXISTS effect_retry_events (
    effect_id TEXT NOT NULL REFERENCES outbox_effects(id) ON DELETE RESTRICT,
    generation INTEGER NOT NULL,
    actor_id TEXT NOT NULL,
    actor_roles_json TEXT NOT NULL,
    correlation_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (effect_id, generation)
);
CREATE TRIGGER IF NOT EXISTS effect_retry_events_no_update
BEFORE UPDATE ON effect_retry_events BEGIN
    SELECT RAISE(ABORT, 'effect retry events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS effect_retry_events_no_delete
BEFORE DELETE ON effect_retry_events BEGIN
    SELECT RAISE(ABORT, 'effect retry events are append-only');
END;

CREATE UNIQUE INDEX IF NOT EXISTS idx_preview_build_operations_active_submission_digest
ON preview_build_operations(subject_kind, subject_id, input_sha256, harness_sha256)
WHERE subject_kind = 'submission' AND state IN ('queued', 'running');

DROP TRIGGER IF EXISTS proxy_replay_nonces_no_delete;
CREATE INDEX IF NOT EXISTS idx_proxy_replay_nonces_expires_at
ON proxy_replay_nonces(expires_at);
CREATE TRIGGER IF NOT EXISTS proxy_replay_nonces_protect_active
BEFORE DELETE ON proxy_replay_nonces
WHEN OLD.expires_at >= strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
BEGIN
    SELECT RAISE(ABORT, 'active proxy replay nonces cannot be deleted');
END;
