ALTER TABLE project_create_reservations ADD COLUMN display_name TEXT;

CREATE TABLE IF NOT EXISTS project_create_items (
    operation_id TEXT NOT NULL REFERENCES project_create_reservations(operation_id) ON DELETE RESTRICT,
    item_kind TEXT NOT NULL CHECK(item_kind IN ('frontend','backend')),
    staging_name TEXT NOT NULL,
    target_name TEXT NOT NULL,
    expected_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('prepared','promoted','diagnostic')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(operation_id, item_kind)
);

CREATE TRIGGER IF NOT EXISTS project_create_items_no_delete
BEFORE DELETE ON project_create_items BEGIN
    SELECT RAISE(ABORT, 'project creation items are append-only');
END;
