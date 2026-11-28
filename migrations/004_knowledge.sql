CREATE TABLE knowledge_documents (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    PRIMARY KEY (profile_id, source_id)
);

CREATE TABLE knowledge_chunks (
    chunk_id INTEGER PRIMARY KEY,
    profile_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    text TEXT NOT NULL,
    UNIQUE (profile_id, source_id, position),
    FOREIGN KEY (profile_id, source_id)
        REFERENCES knowledge_documents(profile_id, source_id) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    text, content='knowledge_chunks', content_rowid='chunk_id',
    tokenize='unicode61'
);

CREATE TRIGGER knowledge_chunks_insert AFTER INSERT ON knowledge_chunks BEGIN
    INSERT INTO knowledge_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;

CREATE TRIGGER knowledge_chunks_delete AFTER DELETE ON knowledge_chunks BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, text)
    VALUES ('delete', old.chunk_id, old.text);
END;
