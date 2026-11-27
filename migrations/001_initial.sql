BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS profiles (
    profile_id TEXT PRIMARY KEY NOT NULL,
    timezone TEXT NOT NULL,
    quote_route TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bindings (
    kind TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE RESTRICT,
    PRIMARY KEY (kind, channel_id, profile_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_profile_per_mail_connection
ON bindings (channel_id) WHERE kind = 'mail';

CREATE TABLE IF NOT EXISTS inbox_events (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE RESTRICT,
    event_id TEXT NOT NULL,
    source TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    received_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, event_id),
    UNIQUE (profile_id, source, external_key)
);

CREATE TABLE IF NOT EXISTS documents (
    profile_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('mail_action', 'minutes')),
    PRIMARY KEY (profile_id, document_id),
    UNIQUE (profile_id, source_event_id, kind),
    FOREIGN KEY (profile_id, source_event_id)
        REFERENCES inbox_events(profile_id, event_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS document_versions (
    profile_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    candidate TEXT NOT NULL,
    mail_dispatch TEXT,
    PRIMARY KEY (profile_id, document_id, version),
    FOREIGN KEY (profile_id, document_id)
        REFERENCES documents(profile_id, document_id) ON DELETE RESTRICT
);

PRAGMA user_version = 1;
COMMIT;
