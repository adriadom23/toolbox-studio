ALTER TABLE submission_records ADD COLUMN snapshot_ref TEXT;
ALTER TABLE submission_records ADD COLUMN build_output_ref TEXT;
ALTER TABLE preview_build_operations ADD COLUMN subject_kind TEXT NOT NULL DEFAULT 'workspace';
ALTER TABLE preview_build_operations ADD COLUMN subject_id TEXT;
ALTER TABLE preview_build_operations ADD COLUMN source_preview_id TEXT REFERENCES preview_objects(id) ON DELETE RESTRICT;
ALTER TABLE preview_build_operations ADD COLUMN correlation_id TEXT;
ALTER TABLE preview_build_operations ADD COLUMN reason TEXT;
CREATE INDEX IF NOT EXISTS idx_preview_build_operations_subject
ON preview_build_operations(subject_kind, subject_id, created_at);
