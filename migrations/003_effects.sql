CREATE TABLE IF NOT EXISTS effects (
    effect_id TEXT PRIMARY KEY NOT NULL,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE RESTRICT,
    document_id TEXT,
    version INTEGER CHECK (version IS NULL OR version > 0),
    key TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 512),
    kind TEXT NOT NULL CHECK (kind IN ('email', 'telegram', 'task', 'calendar_event')),
    target_ref TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'retryable', 'unknown', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_token TEXT,
    lease_expires_at REAL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    external_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (profile_id, key),
    CHECK ((document_id IS NULL) = (version IS NULL)),
    FOREIGN KEY (profile_id, document_id, version)
        REFERENCES document_versions(profile_id, document_id, version) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS effects_ready
ON effects (status, next_attempt_at, created_at);

CREATE INDEX IF NOT EXISTS effects_document
ON effects (profile_id, document_id, version, status);


CREATE TABLE effect_retry_audit (
    id INTEGER PRIMARY KEY,
    effect_id TEXT NOT NULL REFERENCES effects(effect_id) ON DELETE RESTRICT,
    verified_by TEXT NOT NULL,
    verified_at TEXT NOT NULL
);
