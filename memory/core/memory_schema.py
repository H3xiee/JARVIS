"""SQLite schema for the unified memory database.

`memories` holds the common record for every memory type; the per-type tables
(episodes, facts, procedures, lessons, intentions) hold the structured fields
that only that type has, keyed by the same memory_id. Bump SCHEMA_VERSION and
add a step to MIGRATIONS for every change; never edit an applied step.
"""

SCHEMA_VERSION = 2

_V1 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    entity_id TEXT,
    source_type TEXT NOT NULL,
    source_ref TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    verification_status TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    superseded_by TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_mem_type ON memories(type, status);
CREATE INDEX IF NOT EXISTS ix_mem_entity ON memories(entity_id, status);
CREATE INDEX IF NOT EXISTS ix_mem_category ON memories(category, status);
CREATE INDEX IF NOT EXISTS ix_mem_updated ON memories(updated_at);
-- duplicate lookup (dedupe is enforced by the repository: episodes may legitimately repeat)
CREATE INDEX IF NOT EXISTS ix_mem_hash ON memories(content_hash, status);

CREATE TABLE IF NOT EXISTS episodes (
    memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id) ON DELETE CASCADE,
    session_id TEXT, agent TEXT, goal TEXT, outcome TEXT, started_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS facts (
    memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id) ON DELETE CASCADE,
    subject TEXT, predicate TEXT, object TEXT
);
CREATE TABLE IF NOT EXISTS procedures (
    memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id) ON DELETE CASCADE,
    name TEXT, trigger TEXT, steps TEXT, success_count INTEGER DEFAULT 0, failure_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lessons (
    memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id) ON DELETE CASCADE,
    trigger TEXT, platform TEXT, context TEXT
);
CREATE TABLE IF NOT EXISTS intentions (
    memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id) ON DELETE CASCADE,
    due_at TEXT, done_at TEXT, owner TEXT
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                -- person | project | tool | organization | place | other
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entity_name ON entities(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS relationships (
    rel_id TEXT PRIMARY KEY,
    src_entity TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    dst_entity TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    memory_id TEXT REFERENCES memories(memory_id) ON DELETE SET NULL,   -- the fact that states it
    confidence REAL NOT NULL DEFAULT 1.0,
    valid_from TEXT, valid_to TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(src_entity, relation, dst_entity)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    channel TEXT,
    summary TEXT,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS session_messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    at TEXT NOT NULL,
    target TEXT
);
CREATE INDEX IF NOT EXISTS ix_msg_session ON session_messages(session_id, message_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_msg_dedupe ON session_messages(session_id, at, role, content);

CREATE TABLE IF NOT EXISTS memory_sources (
    source_id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    reference TEXT,
    excerpt TEXT,
    added_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_src_mem ON memory_sources(memory_id);
CREATE INDEX IF NOT EXISTS ix_src_ref ON memory_sources(reference);

CREATE TABLE IF NOT EXISTS memory_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    snapshot TEXT NOT NULL,           -- the full record before the change
    reason TEXT,
    changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ver_mem ON memory_versions(memory_id, version);

CREATE TABLE IF NOT EXISTS memory_access_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    action TEXT NOT NULL,             -- add | update | correct | supersede | delete | read | search
    memory_id TEXT,
    actor TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_log_at ON memory_access_log(at);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, category, tags, content='memories', content_rowid='rowid', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, category, tags) VALUES (new.rowid, new.content, new.category, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
    VALUES ('delete', old.rowid, old.content, old.category, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content, category, tags ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
    VALUES ('delete', old.rowid, old.content, old.category, old.tags);
    INSERT INTO memories_fts(rowid, content, category, tags) VALUES (new.rowid, new.content, new.category, new.tags);
END;
"""

# v2: embeddings for semantic (vector) search, one row per memory per embedding model
_V2 = """
CREATE TABLE IF NOT EXISTS memory_vectors (
    memory_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    content_hash TEXT NOT NULL,       -- re-embed when the text changes
    vector BLOB NOT NULL,             -- float32, L2-normalised
    PRIMARY KEY (memory_id, model)
);
CREATE INDEX IF NOT EXISTS ix_vec_model ON memory_vectors(model);
"""

# version -> SQL that brings the database from version-1 to version
MIGRATIONS = {1: _V1, 2: _V2}

TYPE_TABLES = {
    "episodic": ("episodes", ("session_id", "agent", "goal", "outcome", "started_at", "ended_at")),
    "semantic": ("facts", ("subject", "predicate", "object")),
    "procedural": ("procedures", ("name", "trigger", "steps", "success_count", "failure_count")),
    "reflective": ("lessons", ("trigger", "platform", "context")),
    "prospective": ("intentions", ("due_at", "done_at", "owner")),
}
