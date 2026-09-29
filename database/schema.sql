-- Durable state for the zero2sudo job monitor. SQLite is the source of truth for
-- "have we seen/sent this?" — never the LLM's memory.

CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_key TEXT UNIQUE NOT NULL,      -- Instagram media pk
    first_seen_at TEXT NOT NULL,
    taken_at INTEGER,
    content_hash TEXT,                   -- sha256 of the processed image
    text_content TEXT,                   -- raw LLM JSON, for debugging
    source_url TEXT,                     -- unwrapped link sticker URL
    is_job INTEGER,                      -- NULL until classified
    relevance TEXT,
    confidence REAL,
    processed INTEGER DEFAULT 0,         -- 1 once classified (or given up on)
    attempts INTEGER DEFAULT 0,
    last_error TEXT,
    notification_sent INTEGER DEFAULT 0
);

-- Jobs and events both live here (kind = 'job' | 'event'). For events: company = organizer,
-- role = event name, season = event type, application_url = registration link,
-- event_time = date/time text.
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'job',
    dedup_key TEXT UNIQUE NOT NULL,      -- kind|normalized company|role (+|date for events)
    url_key TEXT UNIQUE,                 -- kind|company|url; only set when the Story had one item
    company TEXT,
    role TEXT,
    event_time TEXT,
    employment_type TEXT,
    season TEXT,
    location TEXT,
    compensation TEXT,
    application_url TEXT,
    additional_details TEXT,
    first_seen_at TEXT NOT NULL,
    story_id INTEGER REFERENCES stories(id),
    notification_sent INTEGER DEFAULT 0,
    notified_at TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
