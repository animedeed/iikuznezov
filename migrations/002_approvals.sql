CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY NOT NULL,
    profile_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    approver_ref TEXT NOT NULL CHECK (approver_ref IN ('leader', 'meeting-initiator')),
    approver_telegram_id INTEGER NOT NULL CHECK (approver_telegram_id > 0),
    state TEXT NOT NULL CHECK (state IN ('pending_approval', 'approved', 'rejected',
                                        'revision_requested', 'superseded')),
    proposed_effects TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decision TEXT CHECK (decision IN ('approve', 'reject', 'revise')),
    decided_by INTEGER,
    decided_at TEXT,
    comment TEXT,
    UNIQUE (profile_id, document_id, version),
    FOREIGN KEY (profile_id, document_id, version)
        REFERENCES document_versions(profile_id, document_id, version) ON DELETE RESTRICT
);

CREATE TABLE approval_audit (
    id INTEGER PRIMARY KEY,
    profile_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    approval_id TEXT NOT NULL REFERENCES approvals(approval_id) ON DELETE RESTRICT,
    event TEXT NOT NULL CHECK (event IN ('opened', 'decided', 'superseded')),
    actor_telegram_id INTEGER,
    decision TEXT,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (profile_id, document_id, version)
        REFERENCES document_versions(profile_id, document_id, version) ON DELETE RESTRICT
);

CREATE TABLE preferences (
    preference_id TEXT PRIMARY KEY NOT NULL,
    profile_id TEXT NOT NULL,
    source_document_id TEXT NOT NULL,
    actor_telegram_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (profile_id, source_document_id, text),
    FOREIGN KEY (profile_id, source_document_id)
        REFERENCES documents(profile_id, document_id) ON DELETE RESTRICT
);
