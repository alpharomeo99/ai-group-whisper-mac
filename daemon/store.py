"""Local SQLite storage for AI Group Whisper."""
import json
import sqlite3
import threading
import time

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS groups (
  chat_id INTEGER PRIMARY KEY, title TEXT, watched INTEGER DEFAULT 0,
  auto_reply INTEGER DEFAULT 0, persona TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
  chat_id INTEGER, msg_id INTEGER, sender TEXT, text TEXT, ts INTEGER,
  PRIMARY KEY (chat_id, msg_id)
);
CREATE TABLE IF NOT EXISTS summaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, body TEXT, created INTEGER
);
-- Outgoing work queue. status: pending | in_flight | done | failed | cancelled
CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, chat_id INTEGER, payload TEXT,
  status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, last_error TEXT,
  not_before INTEGER DEFAULT 0, created INTEGER, updated INTEGER,
  dedupe_key TEXT UNIQUE
);
-- Telegram accounts; each has its own session file in data/sessions/
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE, phone TEXT, name TEXT,
  username TEXT, session TEXT, active INTEGER DEFAULT 1, created INTEGER
);
CREATE TABLE IF NOT EXISTS proxies (
  id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, url TEXT, last_ip TEXT,
  last_check INTEGER, ok INTEGER DEFAULT 0, created INTEGER
);
CREATE TABLE IF NOT EXISTS personas (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, prompt TEXT DEFAULT '', color TEXT DEFAULT '#2fc4b2', created INTEGER
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, not_before);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)
        for ddl in ("ALTER TABLE groups ADD COLUMN account_id INTEGER",
                    "ALTER TABLE accounts ADD COLUMN api_id INTEGER",
                    "ALTER TABLE accounts ADD COLUMN api_hash TEXT",
                    "ALTER TABLE accounts ADD COLUMN proxy_id INTEGER",
                    "ALTER TABLE accounts ADD COLUMN persona_id INTEGER",
                    "ALTER TABLE personas ADD COLUMN bio TEXT DEFAULT ''",
                    "ALTER TABLE personas ADD COLUMN details TEXT DEFAULT '{}'"):
            try:
                self.db.execute(ddl)
            except sqlite3.OperationalError:
                pass  # already migrated
        self.db.commit()

    def q(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def rows(self, sql, args=()):
        return [dict(r) for r in self.q(sql, args).fetchall()]

    # settings
    def get(self, key, default=None):
        r = self.q("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, key, value):
        self.q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    # messages
    def add_message(self, chat_id, msg_id, sender, text, ts):
        self.q("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?)", (chat_id, msg_id, sender, text, ts))

    def recent(self, chat_id, limit=80):
        return list(reversed(self.rows(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT ?", (chat_id, limit))))

    # queue
    def enqueue(self, kind, chat_id, payload, dedupe_key=None, delay=0):
        now = int(time.time())
        self.q("INSERT OR IGNORE INTO queue(kind,chat_id,payload,created,updated,not_before,dedupe_key) "
               "VALUES(?,?,?,?,?,?,?)", (kind, chat_id, json.dumps(payload), now, now, now + delay, dedupe_key))

    def claim(self, limit=3):
        now = int(time.time())
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM queue WHERE status='pending' AND not_before<=? ORDER BY id LIMIT ?",
                (now, limit)).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self.db.execute(f"UPDATE queue SET status='in_flight', updated=? WHERE id IN ({','.join('?'*len(ids))})",
                                (now, *ids))
                self.db.commit()
            return [dict(r) for r in rows]

    def finish(self, qid, ok, error=None, retry_in=None):
        now = int(time.time())
        if ok:
            self.q("UPDATE queue SET status='done', updated=? WHERE id=?", (now, qid))
        elif retry_in is not None:
            self.q("UPDATE queue SET status='pending', attempts=attempts+1, last_error=?, not_before=?, updated=? "
                   "WHERE id=?", (error, now + retry_in, now, qid))
        else:
            self.q("UPDATE queue SET status='failed', attempts=attempts+1, last_error=?, updated=? WHERE id=?",
                   (error, now, qid))

    def requeue_in_flight(self, delay=5):
        """Sleep/wake protection: anything interrupted mid-flight goes back to pending."""
        now = int(time.time())
        return self.q("UPDATE queue SET status='pending', not_before=?, updated=? WHERE status='in_flight'",
                      (now + delay, now)).rowcount
