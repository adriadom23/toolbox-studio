CREATE TABLE IF NOT EXISTS operational_status (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    error_code TEXT,
    updated_at TEXT NOT NULL
);
