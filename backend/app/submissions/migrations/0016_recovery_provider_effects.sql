ALTER TABLE submission_records ADD COLUMN snapshot_manifest_json TEXT;
ALTER TABLE outbox_effects ADD COLUMN provider_key TEXT;
CREATE INDEX IF NOT EXISTS idx_outbox_effects_submission_provider
ON outbox_effects(submission_id, kind, provider_key, created_at);
