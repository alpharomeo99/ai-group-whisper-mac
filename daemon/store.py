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
  auto_reply INTEGER DEFAULT 0, persona TEXT DEFAULT '', account_id INTEGER
);
CREATE TABLE IF NOT EXISTS group_accounts (
  chat_id INTEGER, account_id INTEGER, PRIMARY KEY(chat_id, account_id)
);
CREATE TABLE IF NOT EXISTS messages (
  chat_id INTEGER, msg_id INTEGER, sender TEXT, text TEXT, ts INTEGER,
  PRIMARY KEY (chat_id, msg_id)
);
CREATE TABLE IF NOT EXISTS direct_chats (
  account_id INTEGER, peer_id INTEGER, peer_name TEXT, peer_username TEXT,
  peer_phone TEXT, last_msg TEXT, last_ts INTEGER, unread_count INTEGER DEFAULT 0,
  auto_reply INTEGER DEFAULT 0, persona_id INTEGER,
  PRIMARY KEY(account_id, peer_id)
);
CREATE TABLE IF NOT EXISTS direct_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, peer_id INTEGER,
  msg_id INTEGER, sender_name TEXT, incoming INTEGER DEFAULT 1, text TEXT, ts INTEGER
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
  username TEXT, session TEXT, active INTEGER DEFAULT 1, created INTEGER,
  last_synced_at INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS proxies (
  id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, url TEXT, last_ip TEXT,
  last_check INTEGER, ok INTEGER DEFAULT 0, created INTEGER
);
CREATE TABLE IF NOT EXISTS personas (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, prompt TEXT DEFAULT '', color TEXT DEFAULT '#2fc4b2',
  bio TEXT DEFAULT '', details TEXT DEFAULT '{}', created INTEGER
);
CREATE TABLE IF NOT EXISTS group_personas (
  chat_id INTEGER, persona_id INTEGER, account_id INTEGER, created INTEGER, PRIMARY KEY(chat_id, persona_id)
);
CREATE TABLE IF NOT EXISTS daily_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, date_str TEXT, chat_id INTEGER,
  topic TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS daily_batch_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER, chat_id INTEGER,
  account_id INTEGER, persona_id INTEGER, sender_name TEXT, text TEXT,
  scheduled_ts INTEGER, status TEXT DEFAULT 'pending', sent_ts INTEGER,
  error TEXT, created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_batch_items_sched ON daily_batch_items(status, scheduled_ts);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, not_before);
CREATE INDEX IF NOT EXISTS idx_dm_chat ON direct_messages(account_id, peer_id, ts);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)
        for ddl in (
            "ALTER TABLE groups ADD COLUMN account_id INTEGER",
            "ALTER TABLE accounts ADD COLUMN api_id INTEGER",
            "ALTER TABLE accounts ADD COLUMN api_hash TEXT",
            "ALTER TABLE accounts ADD COLUMN proxy_id INTEGER",
            "ALTER TABLE accounts ADD COLUMN persona_id INTEGER",
            "ALTER TABLE personas ADD COLUMN bio TEXT DEFAULT ''",
            "ALTER TABLE personas ADD COLUMN details TEXT DEFAULT '{}'",
            "ALTER TABLE groups ADD COLUMN profile TEXT DEFAULT ''",
            "CREATE TABLE IF NOT EXISTS group_accounts (chat_id INTEGER, account_id INTEGER, PRIMARY KEY(chat_id, account_id))",
            "CREATE TABLE IF NOT EXISTS direct_chats (account_id INTEGER, peer_id INTEGER, peer_name TEXT, peer_username TEXT, peer_phone TEXT, last_msg TEXT, last_ts INTEGER, unread_count INTEGER DEFAULT 0, auto_reply INTEGER DEFAULT 0, persona_id INTEGER, PRIMARY KEY(account_id, peer_id))",
                        "CREATE TABLE IF NOT EXISTS direct_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, peer_id INTEGER, msg_id INTEGER, sender_name TEXT, incoming INTEGER DEFAULT 1, text TEXT, ts INTEGER)",
            "ALTER TABLE groups ADD COLUMN topic TEXT DEFAULT ''",
            "ALTER TABLE groups ADD COLUMN about TEXT DEFAULT ''",
            "ALTER TABLE groups ADD COLUMN domain_knowledge TEXT DEFAULT ''",
            "ALTER TABLE groups ADD COLUMN tags TEXT DEFAULT ''",
            "ALTER TABLE groups ADD COLUMN rules TEXT DEFAULT ''",
            "CREATE TABLE IF NOT EXISTS daily_batches (id INTEGER PRIMARY KEY AUTOINCREMENT, date_str TEXT, chat_id INTEGER, topic TEXT, created_at INTEGER)",
            "CREATE TABLE IF NOT EXISTS daily_batch_items (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER, chat_id INTEGER, account_id INTEGER, persona_id INTEGER, sender_name TEXT, text TEXT, scheduled_ts INTEGER, status TEXT DEFAULT 'pending', sent_ts INTEGER, error TEXT, created_at INTEGER)",
            "ALTER TABLE messages ADD COLUMN is_bot INTEGER DEFAULT 0",
            "ALTER TABLE messages ADD COLUMN sender_id INTEGER",
            "CREATE TABLE IF NOT EXISTS persona_memories (id INTEGER PRIMARY KEY AUTOINCREMENT, persona_id INTEGER NOT NULL, account_id INTEGER, chat_id INTEGER, kind TEXT DEFAULT 'statement', content TEXT NOT NULL, context TEXT DEFAULT '', salience REAL DEFAULT 0.7, retention_tier TEXT DEFAULT 'medium', created_at INTEGER NOT NULL, expires_at INTEGER, access_count INTEGER DEFAULT 0, last_accessed_at INTEGER)",
            "CREATE INDEX IF NOT EXISTS idx_mem_pid_tier ON persona_memories(persona_id, retention_tier, expires_at)",
            "CREATE TABLE IF NOT EXISTS usage_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER NOT NULL, event_type TEXT NOT NULL, persona_id INTEGER, account_id INTEGER, chat_id INTEGER, tokens_prompt INTEGER DEFAULT 0, tokens_completion INTEGER DEFAULT 0, tokens_total INTEGER DEFAULT 0, latency_ms INTEGER DEFAULT 0, model TEXT DEFAULT '', cost_est REAL DEFAULT 0.0)",
            "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_logs(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_usage_pid ON usage_logs(persona_id, timestamp)",
            "ALTER TABLE accounts ADD COLUMN last_synced_at INTEGER DEFAULT 0"
):
            try:
                self.db.execute(ddl)
            except sqlite3.OperationalError:
                pass  # already migrated
        self.db.commit()
        self.seed_xbiolabs_and_personas()

    def seed_xbiolabs_and_personas(self):
        """Seeds or updates xbiolabs group domain context and updates personas with peptide mastery."""
        try:
            xbiolabs_about = "Official vendor and community group for xbiolabs. While it is a vendor group, the community actively discusses everything related to peptides, underground biohacking, and health optimization."
            xbiolabs_domain = (
                "Comprehensive domain mastery across all topics discussed in xbiolabs: "
                "1. PEPTIDES & RESEARCH CHEMICALS: In-depth familiarity with BPC-157 (gut repair, tendon healing), TB-500/Thymosin Beta-4 (tissue regeneration), Semaglutide/Ozempic, Tirzepatide/Mounjaro, Retatrutide (triple GIP/GLP-1/glucagon agonist), GHK-Cu (copper peptide for tissue/skin/collagen), CJC-1295 no DAC & Ipamorelin (GH secretagogues), Sermorelin, MOTS-c (mitochondrial optimization), Epitalon (telomere elongation), NAD+ injections, Kisspeptin, MT-2. "
                "2. RECONSTITUTION & DOSING PROTOCOLS: Proper mixing techniques with bacteriostatic water (BAC), calculating mcg per tick on 100-unit/30-unit insulin syringes, sterile needle hygiene, subQ vs IM administration sites, cold fridge storage, avoiding vigorous shaking. "
                "3. VENDOR OPERATIONS & BUYING: Sourcing, batch COA purity verification, HPLC/Janoshik lab test reports, ordering procedures, payment methods (crypto/Bitcoin/USDT, wire), tracking numbers, customs handling, stealth domestic shipping, discrete packaging, pricing per vial vs kit (10 vials), reship policies, customer service resolution. "
                "4. GEAR & ANABOLIC PROTOCOLS: TRT bases (test cypionate, test enanthate), Primobolan, Masteron, Anavar/Oxandrolone, Deca/NPP, estrogen management (Arimidex/anastrozole, Aromasin), prolactin control (Cabergoline), post cycle therapy (PCT - Enclomiphene, Nolvadex, HCG). "
                "5. PROGRESS & HEALTH MONITORING: Bloodwork interpretation (CBC, CMP, lipid panels, ApoB, hs-CRP, total/free test, sensitive estradiol, ALT/AST liver enzymes, eGFR kidney health), DEXA scans, body composition, fatigue markers. "
                "6. SEXUAL ENHANCEMENT: PT-141/Bremelanotide (melanocortin receptor agonist for libido), PDE5 inhibitors (Tadalafil/Cialis daily vs as-needed, Sildenafil/Viagra), Melanotan-2 (tanning + libido synergy). "
                "7. WELLNESS & LONGEVITY: Mitochondrial health, sleep optimization, metabolic flexibility, anti-inflammatory protocols. "
                "8. NOOTROPICS & NEUROCHEMISTRY: Semax (NA-Semax-Amidate for BDNF and focus), Selank (anxiolytic peptide), Bromantane (dopamine upregulation), Modafinil/Armodafinil, L-Tyrosine, Alpha-GPC. "
                "9. TONE & PARTICIPATION: Speak as experienced, grounded community insiders who share real personal experiences, troubleshoot reconstitutions, exchange batch updates, and talk shop casually without generic corporate disclaimers."
            )
            xbiolabs_tags = "peptides, vendor, dosing, buying, shipping, pricing, coa, bpc157, retatrutide, tirzepatide, gear, trt, sexual-enhancement, pt141, nootropics, wellness, xbiolabs"
            xbiolabs_rules = "Be authentic, direct, and knowledgeable. Share real protocol experiences, discuss batch quality honestly, offer practical dosing and reconstitution tips, and respect vendor shipping/payment guidelines."

            cur = self.db.cursor()
            cur.execute("SELECT chat_id FROM groups WHERE LOWER(title) LIKE '%xbiolabs%' OR LOWER(title) LIKE '%xbio%'")
            rows = cur.fetchall()
            if rows:
                for r in rows:
                    cur.execute(
                        "UPDATE groups SET about=?, domain_knowledge=?, tags=?, rules=? WHERE chat_id=?",
                        (xbiolabs_about, xbiolabs_domain, xbiolabs_tags, xbiolabs_rules, r[0])
                    )
            else:
                cur.execute(
                    "INSERT INTO groups(chat_id, title, watched, auto_reply, about, domain_knowledge, tags, rules) "
                    "VALUES(?, ?, 1, 1, ?, ?, ?, ?)",
                    (-1001988234120, "xbiolabs", xbiolabs_about, xbiolabs_domain, xbiolabs_tags, xbiolabs_rules)
                )

            # Update personas: strict negative-inquiry trigger policy for vendor experience
            xbiolabs_rapport = (
                "\n\nVENDOR EXPERIENCE POLICY & TRIGGER RULES (xbiolabs):\n"
                "- STRICT NEGATIVE-INQUIRY TRIGGER RULE: NEVER volunteer unprompted vendor praise, package reviews, or order testimonials out of nowhere. Chat normally about compounds, fitness, biohacking, and everyday banter.\n"
                "- ADMISSION OF ONLY GOOD EXPERIENCE: ONLY if an external human in the chat specifically asks whether anyone had a BAD experience, issues, shipping problems, or asks if the vendor is legit/scam (e.g. 'anyone had a bad experience with xbiolabs?', 'did anyone get scammed?', 'anyone have issues?', 'bad batch?'): you chime in naturally and state that personally, you have only ever had good experiences (orders landed clean, verified batch purity, zero issues on your end).\n"
                "- CASUAL & UNDERSTATED PEER TONE: When answering such an inquiry, keep it casual, peer-to-peer, and brief (e.g. 'honestly never had an issue with them, orders always landed clean for me', 'nah zero bad runs here, all arrived fine'). NEVER sound like a corporate shill, defensive moderator, or paid sponsor."
            )

            import re
            cur.execute("SELECT id, name, prompt, details FROM personas")
            persona_rows = cur.fetchall()
            now_ts = int(time.time())
            for pid, pname, prompt, dt_raw in persona_rows:
                prompt_str = prompt or ""
                # Strip out old injections if present
                if "COMMUNITY DOMAIN MASTERY (xbiolabs & Peptides):" in prompt_str:
                    prompt_str = re.sub(r'\n*COMMUNITY DOMAIN MASTERY \(xbiolabs & Peptides\):.*?(?=(\n\n[A-Z]|$))', '', prompt_str, flags=re.DOTALL).strip()
                if "COMMUNITY RAPPORT & VENDOR EXPERIENCE (xbiolabs):" in prompt_str:
                    prompt_str = re.sub(r'\n*COMMUNITY RAPPORT & VENDOR EXPERIENCE \(xbiolabs\):.*?(?=(\n\n[A-Z]|$))', '', prompt_str, flags=re.DOTALL).strip()
                if "VENDOR EXPERIENCE POLICY & TRIGGER RULES (xbiolabs):" in prompt_str:
                    prompt_str = re.sub(r'\n*VENDOR EXPERIENCE POLICY & TRIGGER RULES \(xbiolabs\):.*?(?=(\n\n[A-Z]|$))', '', prompt_str, flags=re.DOTALL).strip()

                prompt_str = prompt_str.strip() + xbiolabs_rapport

                try:
                    d = json.loads(dt_raw or "{}")
                except Exception:
                    d = {}
                d["vendor_sentiment"] = "admit_good_only_on_negative_query"
                cur.execute("UPDATE personas SET prompt=?, details=? WHERE id=?", (prompt_str, json.dumps(d), pid))

            # Prune old fabricated vendor seed memories to avoid unprompted order claims
            cur.execute("DELETE FROM persona_memories WHERE content LIKE '%Frequent buyer at xbiolabs%' OR content LIKE '%Received fresh peptide kit%'")

            self.db.commit()
        except Exception:
            pass
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
    def add_message(self, chat_id, msg_id, sender, text, ts, is_bot=0, sender_id=None):
        self.q(
            "INSERT INTO messages(chat_id, msg_id, sender, text, ts, is_bot, sender_id) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id, msg_id) DO UPDATE SET "
            "text=excluded.text, sender=excluded.sender, is_bot=excluded.is_bot, sender_id=excluded.sender_id",
            (chat_id, msg_id, sender, text, ts, 1 if is_bot else 0, sender_id)
        )

    def get_last_human_message_ts(self, chat_id):
        r = self.q("SELECT ts FROM messages WHERE chat_id=? AND is_bot=0 ORDER BY ts DESC LIMIT 1", (chat_id,)).fetchone()
        return r[0] if r else None

    def mark_batch_item_sent(self, item_id, sent_ts):
        self.q("UPDATE daily_batch_items SET status='sent', sent_ts=? WHERE id=?", (sent_ts, item_id))

    def recent(self, chat_id, limit=80):
        return list(reversed(self.rows(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT ?", (chat_id, limit))))

    # direct messages & 1-on-1 chats
    def add_direct_message(self, account_id, peer_id, msg_id, sender_name, incoming, text, ts):
        self.q(
            "INSERT INTO direct_messages(account_id, peer_id, msg_id, sender_name, incoming, text, ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (account_id, peer_id, msg_id, sender_name, incoming, text, ts)
        )
        if incoming:
            self.q(
                "UPDATE direct_chats SET last_msg=?, last_ts=?, unread_count=unread_count+1 "
                "WHERE account_id=? AND peer_id=?",
                (text, ts, account_id, peer_id)
            )
        else:
            self.q(
                "UPDATE direct_chats SET last_msg=?, last_ts=? "
                "WHERE account_id=? AND peer_id=?",
                (text, ts, account_id, peer_id)
            )

    def direct_messages_recent(self, account_id, peer_id, limit=60):
        return self.rows(
            "SELECT * FROM direct_messages WHERE account_id=? AND peer_id=? ORDER BY ts ASC, id ASC LIMIT ?",
            (account_id, peer_id, limit)
        )

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
