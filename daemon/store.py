"""Local SQLite storage for AI Group Whisper."""
import json
import sqlite3
import logging
import random
import re
import threading
import time

log = logging.getLogger('whisper.store')

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;
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
  is_bot INTEGER DEFAULT 0, sender_id INTEGER,
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
  last_synced_at INTEGER DEFAULT 0, is_admin INTEGER DEFAULT 0
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
CREATE INDEX IF NOT EXISTS idx_ga_acc ON group_accounts(account_id);
CREATE INDEX IF NOT EXISTS idx_gp_acc ON group_personas(account_id);
CREATE INDEX IF NOT EXISTS idx_msgs_chat_bot ON messages(chat_id, is_bot, ts);
"""



PHONE_PREFIX_TO_LOCATION = {
    "+1": {"country": "United States / Canada", "region": "North America"},
    "+44": {"country": "United Kingdom", "region": "Europe"},
    "+33": {"country": "France", "region": "Europe"},
    "+49": {"country": "Germany", "region": "Europe"},
    "+34": {"country": "Spain", "region": "Europe"},
    "+39": {"country": "Italy", "region": "Europe"},
    "+31": {"country": "Netherlands", "region": "Europe"},
    "+32": {"country": "Belgium", "region": "Europe"},
    "+41": {"country": "Switzerland", "region": "Europe"},
    "+43": {"country": "Austria", "region": "Europe"},
    "+46": {"country": "Sweden", "region": "Europe"},
    "+47": {"country": "Norway", "region": "Europe"},
    "+45": {"country": "Denmark", "region": "Europe"},
    "+358": {"country": "Finland", "region": "Europe"},
    "+48": {"country": "Poland", "region": "Europe"},
    "+420": {"country": "Czech Republic", "region": "Europe"},
    "+351": {"country": "Portugal", "region": "Europe"},
    "+353": {"country": "Ireland", "region": "Europe"},
    "+30": {"country": "Greece", "region": "Europe"},
    "+90": {"country": "Turkey", "region": "Middle East / Europe"},
    "+212": {"country": "Morocco", "region": "North Africa"},
    "+20": {"country": "Egypt", "region": "North Africa"},
    "+971": {"country": "United Arab Emirates", "region": "Middle East"},
    "+966": {"country": "Saudi Arabia", "region": "Middle East"},
    "+972": {"country": "Israel", "region": "Middle East"},
    "+234": {"country": "Nigeria", "region": "West Africa"},
    "+254": {"country": "Kenya", "region": "East Africa"},
    "+27": {"country": "South Africa", "region": "Southern Africa"},
    "+7": {"country": "Eastern Europe / Central Asia", "region": "Central Asia"},
    "+380": {"country": "Ukraine", "region": "Eastern Europe"},
    "+370": {"country": "Lithuania", "region": "Eastern Europe"},
    "+371": {"country": "Latvia", "region": "Eastern Europe"},
    "+372": {"country": "Estonia", "region": "Eastern Europe"},
    "+995": {"country": "Georgia", "region": "Caucasus"},
    "+86": {"country": "China", "region": "East Asia"},
    "+81": {"country": "Japan", "region": "East Asia"},
    "+82": {"country": "South Korea", "region": "East Asia"},
    "+852": {"country": "Hong Kong", "region": "East Asia"},
    "+65": {"country": "Singapore", "region": "Southeast Asia"},
    "+60": {"country": "Malaysia", "region": "Southeast Asia"},
    "+62": {"country": "Indonesia", "region": "Southeast Asia"},
    "+63": {"country": "Philippines", "region": "Southeast Asia"},
    "+66": {"country": "Thailand", "region": "Southeast Asia"},
    "+84": {"country": "Vietnam", "region": "Southeast Asia"},
    "+91": {"country": "India", "region": "South Asia"},
    "+92": {"country": "Pakistan", "region": "South Asia"},
    "+61": {"country": "Australia", "region": "Oceania"},
    "+64": {"country": "New Zealand", "region": "Oceania"},
    "+55": {"country": "Brazil", "region": "South America"},
    "+52": {"country": "Mexico", "region": "North America"},
    "+54": {"country": "Argentina", "region": "South America"},
    "+57": {"country": "Colombia", "region": "South America"},
    "+56": {"country": "Chile", "region": "South America"},
}

def location_from_phone(phone):
    clean = "+" + "".join(c for c in str(phone or "") if c.isdigit()).lstrip("+")
    for pfx in sorted(PHONE_PREFIX_TO_LOCATION.keys(), key=lambda k: -len(k)):
        if clean.startswith(pfx):
            return PHONE_PREFIX_TO_LOCATION[pfx]["country"]
    return "International"

def detect_gender_from_name(name):
    first = (name or "").strip().split()[0].lower() if name else ""
    female_names = {
        "leila", "layla", "leyla", "sarah", "sara", "emma", "chloe", "mia", "hannah", "maya", "leah",
        "grace", "sofia", "sophia", "elena", "clara", "zoe", "eva", "nina", "lily", "anna", "olivia",
        "amelia", "lucy", "ruby", "nora", "eliza", "victoria", "isabella", "charlotte", "natalie",
        "alicia", "jessica", "claire", "valerie", "maria", "camilla", "diana", "julia",
        "elizabeth", "emily", "amber", "ashley", "melissa", "nicole", "rebecca", "rachel", "amanda", "laura"
    }
    male_names = {
        "dorian", "alex", "john", "mike", "david", "james", "marcus", "luke", "ryan", "jake", "sam",
        "chris", "daniel", "michael", "adam", "ben", "tom", "leo", "max", "eric", "jason", "nathan",
        "brian", "kevin", "justin", "brandon", "tyler", "matthew", "andrew", "ethan", "william",
        "lucas", "gabriel", "henry", "owen", "samuel", "jack", "connor", "liam", "noah", "oliver"
    }
    if first in female_names:
        return "female"
    if first in male_names:
        return "male"
    if first.endswith(("a", "ah", "ia", "ina", "ine", "ette", "elle", "lyn")):
        return "female"
    return "male"

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL;")
        self.db.execute("PRAGMA busy_timeout=5000;")
        self.db.execute("PRAGMA synchronous=NORMAL;")
        self.db.execute("PRAGMA cache_size=-64000;")
        self.db.execute("PRAGMA temp_store=MEMORY;")
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
            "ALTER TABLE daily_batch_items ADD COLUMN reply_to_item_id INTEGER",
            "ALTER TABLE daily_batch_items ADD COLUMN telegram_msg_id INTEGER",
            "ALTER TABLE messages ADD COLUMN is_bot INTEGER DEFAULT 0",
            "ALTER TABLE messages ADD COLUMN sender_id INTEGER",
            "CREATE TABLE IF NOT EXISTS persona_memories (id INTEGER PRIMARY KEY AUTOINCREMENT, persona_id INTEGER NOT NULL, account_id INTEGER, chat_id INTEGER, kind TEXT DEFAULT 'statement', content TEXT NOT NULL, context TEXT DEFAULT '', salience REAL DEFAULT 0.7, retention_tier TEXT DEFAULT 'medium', created_at INTEGER NOT NULL, expires_at INTEGER, access_count INTEGER DEFAULT 0, last_accessed_at INTEGER)",
            "CREATE INDEX IF NOT EXISTS idx_mem_pid_tier ON persona_memories(persona_id, retention_tier, expires_at)",
            "CREATE TABLE IF NOT EXISTS usage_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER NOT NULL, event_type TEXT NOT NULL, persona_id INTEGER, account_id INTEGER, chat_id INTEGER, tokens_prompt INTEGER DEFAULT 0, tokens_completion INTEGER DEFAULT 0, tokens_total INTEGER DEFAULT 0, latency_ms INTEGER DEFAULT 0, model TEXT DEFAULT '', cost_est REAL DEFAULT 0.0)",
            "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_logs(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_usage_pid ON usage_logs(persona_id, timestamp)",
            "ALTER TABLE accounts ADD COLUMN last_synced_at INTEGER DEFAULT 0",
            "ALTER TABLE messages ADD COLUMN persona_id INTEGER",
            "ALTER TABLE messages ADD COLUMN account_id INTEGER",
            "CREATE TABLE IF NOT EXISTS human_memories (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, sender TEXT NOT NULL, sender_id INTEGER, sender_username TEXT, fact TEXT NOT NULL, category TEXT DEFAULT 'general', created_at INTEGER NOT NULL, last_referenced_at INTEGER)",
            "CREATE INDEX IF NOT EXISTS idx_hm_chat_sender ON human_memories(chat_id, sender)",
            "CREATE INDEX IF NOT EXISTS idx_ga_acc ON group_accounts(account_id)",
            "CREATE INDEX IF NOT EXISTS idx_gp_acc ON group_personas(account_id)",
            "CREATE INDEX IF NOT EXISTS idx_msgs_chat_bot ON messages(chat_id, is_bot, ts)",
            "ALTER TABLE persona_memories ADD COLUMN slot_key TEXT DEFAULT NULL",
            "ALTER TABLE persona_memories ADD COLUMN is_active INTEGER DEFAULT 1",
            "CREATE INDEX IF NOT EXISTS idx_mem_pid_slot ON persona_memories(persona_id, slot_key, is_active)",
            "ALTER TABLE human_memories ADD COLUMN slot_key TEXT DEFAULT NULL",
            "ALTER TABLE human_memories ADD COLUMN is_active INTEGER DEFAULT 1",
            "ALTER TABLE human_memories ADD COLUMN status TEXT DEFAULT 'open'",
            "ALTER TABLE accounts ADD COLUMN is_admin INTEGER DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_hm_chat_sender_slot ON human_memories(chat_id, sender, slot_key, is_active)"
):
            try:
                self.db.execute(ddl)
            except sqlite3.OperationalError:
                pass  # already migrated
        self.db.commit()
        self.seed_xbiolabs_and_personas()
        self._sanitize_name_calling_on_startup()
        try:
            now_ts = int(time.time())
            self.db.execute("DELETE FROM queue WHERE status IN ('done', 'cancelled') AND updated < ?", (now_ts - 86400,))
            self.db.execute("DELETE FROM queue WHERE status='failed' AND updated < ?", (now_ts - 86400 * 2,))
            self.db.commit()
        except Exception:
            pass


    def _sanitize_name_calling_on_startup(self):
        """Scrubs robotic name-addressing patterns from existing persona prompts, pending daily batch items, and queued messages."""
        try:
            cand_rows = self.rows("SELECT name FROM personas UNION SELECT name FROM accounts")
            cand_names = [r["name"] for r in cand_rows if r.get("name")]

            cur = self.db.cursor()
            # 1. Persona prompts: replace old habit example or instructions encouraging name-calling
            cur.execute("SELECT id, prompt FROM personas")
            for pid, p_prompt in cur.fetchall():
                if not p_prompt:
                    continue
                updated = p_prompt
                if "replies to people by name" in updated:
                    updated = updated.replace("replies to people by name", "speaks directly without using names")
                if "replies by name" in updated:
                    updated = updated.replace("replies by name", "speaks directly without using names")
                if "Biomedical researcher & functional health coach" in updated:
                    updated = updated.replace("Biomedical researcher & functional health coach", "Graphic designer & gym regular")
                if "Senior athletic performance & recovery specialist" in updated:
                    updated = updated.replace("Senior athletic performance & recovery specialist", "Experienced strength coach & lifter")
                if "Values sterile lab protocols, batch COA purity verification" in updated:
                    updated = updated.replace("Values sterile lab protocols, batch COA purity verification", "Values real gym experience and casual banter")
                if "No Customer Service or Validation Cliches" not in updated:
                    updated += "\n- No Customer Service or Validation Cliches: NEVER echo or start messages with 'Makes sense', 'For sure', 'Definitely', 'Agreed', 'Spot on', 'Good point', or 'Totally'. Jump straight into your point."
                if updated != p_prompt:
                    cur.execute("UPDATE personas SET prompt=? WHERE id=?", (updated, pid))

            def _clean(t):
                if not t:
                    return ""
                t = t.strip()
                # DO NOT strip explicit @tags like @alex or @marcus
                for nm in cand_names:
                    if not nm or len(nm.strip()) < 2 or nm.strip().startswith("@"):
                        continue
                    esc = re.escape(nm.strip())
                    pat = rf"^(?:(?:hey|hi|hello|yo)\s+){esc}[,:]\s*"
                    t = re.sub(pat, "", t, flags=re.IGNORECASE)
                    t = re.sub(rf"^{esc}[,:]\s*", "", t, flags=re.IGNORECASE)
                    t = re.sub(rf",\s*{esc}([\.!?]|\s*$)", r"\1", t, flags=re.IGNORECASE)
                # Strip robotic validation prefixes
                t = re.sub(
                    r"^(?:(?:Makes sense|Agreed|Spot on|For sure|Definitely|Good point|Totally agreed|Fair point|100% facts|100%|True that|Totally)[,.!]?\s+)+",
                    "", t, flags=re.IGNORECASE
                )
                t = re.sub(
                    r"^(?:In my experience|To be honest|From my experience|As a rule of thumb)[,:]\s*",
                    "", t, flags=re.IGNORECASE
                )
                t = re.sub(r"^\s*[,:;-]\s*", "", t)
                t = re.sub(r"\s{2,}", " ", t).strip()
                if len(t) < 130 and t.endswith(".") and not t.endswith("..") and not t.endswith("etc."):
                    t = t[:-1].rstrip()
                return t

            # 2. Pending daily batch items
            cur.execute("SELECT id, text FROM daily_batch_items WHERE status='pending'")
            for item_id, txt in cur.fetchall():
                if not txt:
                    continue
                clean_txt = _clean(txt)
                if clean_txt != txt and clean_txt:
                    cur.execute("UPDATE daily_batch_items SET text=? WHERE id=?", (clean_txt, item_id))

            # 3. Pending queue send jobs
            cur.execute("SELECT id, payload FROM queue WHERE kind='send' AND status='pending'")
            for qid, payload_str in cur.fetchall():
                if not payload_str:
                    continue
                try:
                    p_data = json.loads(payload_str)
                    old_txt = p_data.get("text")
                    if old_txt:
                        new_txt = _clean(old_txt)
                        if new_txt != old_txt and new_txt:
                            p_data["text"] = new_txt
                            cur.execute("UPDATE queue SET payload=? WHERE id=?", (json.dumps(p_data), qid))
                except Exception:
                    pass

            self.db.commit()
        except Exception as e:
            log.warning("Failed to sanitize name-calling on startup: %s", e)

    def seed_xbiolabs_and_personas(self):
        return self.reconcile_personas_and_accounts()

    def reconcile_personas_and_accounts(self):
        """Seeds or updates xbiolabs group domain context, synchronizes all personas to reflect
        their phone-matching location, gender-matching name, explicit self-awareness of identity,
        designates one male specialist, and auto-links accounts and groups.
        """
        try:
            xbiolabs_about = "Official community and vendor group for xbiolabs. While it is a vendor group, the community actively discusses peptides, nootropics, general fitness, recovery, longevity, wellness, helping out beginners, and casual everyday banter."
            xbiolabs_domain = (
                "Comprehensive domain mastery across all topics discussed in xbiolabs: "
                "1. PEPTIDES & RESEARCH CHEMICALS: In-depth familiarity with BPC-157 (gut repair, tendon healing), TB-500/Thymosin Beta-4 (tissue regeneration), Semaglutide/Ozempic, Tirzepatide/Mounjaro, Retatrutide (triple GIP/GLP-1/glucagon agonist), GHK-Cu (copper peptide for tissue/skin/collagen), CJC-1295 no DAC & Ipamorelin (GH secretagogues), Sermorelin, MOTS-c (mitochondrial optimization), Epitalon (telomere elongation), NAD+ injections, Kisspeptin, MT-2. "
                "2. RECONSTITUTION & DOSING PROTOCOLS: Proper mixing techniques with bacteriostatic water (BAC), calculating mcg per tick on 100-unit/50-unit/30-unit insulin syringes, sterile needle hygiene, subQ vs IM administration sites, cold fridge storage, gentle swirling without vigorous shaking. "
                "3. NOOTROPICS & COGNITIVE OPTIMIZATION: In-depth understanding of Semax (NA-Semax-Amidate for BDNF, neurogenesis, and mental clarity), Selank (anxiolytic neuropeptide), Bromantane (dopamine synthesis upregulation and clean stamina), Modafinil/Armodafinil, L-Tyrosine, Alpha-GPC, Citicoline, caffeine + L-Theanine synergy, preventing brain fog, and managing stimulant tolerance for deep work. "
                "4. GENERAL FITNESS & TRAINING: Real lifter culture and training splits (Push/Pull/Legs, Upper/Lower, full body), hypertrophy, progressive overload, volume management, powerlifting lifts (squat, bench, deadlift form tweaks), deloads, recovery between sessions, body recomposition, cutting vs bulking protocols, protein and macronutrient balance. "
                "5. WELLNESS, LONGEVITY & RECOVERY: Deep sleep optimization, sleep architecture (REM vs deep), magnesium glycinate/threonate, sauna protocols, cold exposure, joint and tendon health, gut lining repair, hydration and electrolyte balance, metabolic health, interpreting bloodwork panels (CBC, CMP, lipids, ApoB, hs-CRP, hormones, liver/kidney markers). "
                "6. HELPING COMMUNITY MEMBERS: When peers or newcomers ask for help, dosing calculations, reconstitution ratios, syringe tick math, or gym routine guidance, jump in with clear, friendly, and practical assistance. Share direct tips as an experienced buddy rather than a formal medical lecturer. "
                "7. BANTER, HUMOR & UNSERIOUS FUN: Active community camaraderie, humorous gym observations, playful banter, memes, joking about tired mornings or skipping cardio, laughing off funny gym moments, and never taking everything too seriously. "
                "8. GEAR & ANABOLIC PROTOCOLS: TRT bases (test cypionate, test enanthate), Primobolan, Masteron, Anavar/Oxandrolone, Deca/NPP, estrogen management (Arimidex/anastrozole, Aromasin), prolactin control (Cabergoline), post cycle therapy (PCT - Enclomiphene, Nolvadex, HCG). "
                "9. VENDOR OPERATIONS, ADMIN & ORDER PROCESSING: @xbiolabs is the group admin, owner, and the one processing all orders. Sourcing, batch COA purity verification, lab test reports, ordering procedures, payment methods, tracking, stealth domestic shipping. When newcomers ask where or how to order, or when members ask about order status, tracking, payments, or processing, point them to or tag @xbiolabs (e.g. 'dm @xbiolabs', 'tagging @xbiolabs', 'ask @xbiolabs he handles processing'). You can also mention or ping @xbiolabs if you or another member have a question that only the owner/admin can answer. "
                "10. TONE & PARTICIPATION: Speak as experienced, grounded community insiders who share real personal experiences, troubleshoot reconstitutions, exchange batch updates, help out beginners, joke around, and talk shop casually without generic corporate disclaimers."
            )
            xbiolabs_tags = "peptides, vendor, dosing, buying, shipping, pricing, coa, bpc157, retatrutide, tirzepatide, gear, trt, nootropics, semax, selank, bromantane, wellness, sleep, recovery, fitness, gym, training, banter, humor, xbiolabs"
            xbiolabs_rules = "Be authentic, direct, and knowledgeable. Share real protocol and gym experiences, help out members with dosing math or questions, joke around casually, and discuss nootropics, fitness, and wellness. Strictly never criticize or complain about the vendor or products."

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
                "- ABSOLUTE ZERO NEGATIVE VENDOR TALK: Under NO circumstances should any persona criticize, complain about, or speak negatively of xbiolabs, payment methods, checkout, website, or product quality. NEVER complain about payment portals, card processors, checkout bugs, shipping delays, or weak/bad batches. All personas have only ever experienced smooth transactions and reliable product purity.\n"
                "- STRICT NEGATIVE-INQUIRY TRIGGER RULE: NEVER volunteer unprompted vendor praise, package reviews, or order testimonials out of nowhere. Chat normally about fitness, biohacking, and everyday banter.\n"
                "- ADMISSION OF ONLY GOOD EXPERIENCE: ONLY if an external human in the chat specifically asks whether anyone had a BAD experience, issues, shipping problems, or asks if the vendor is legit/scam (e.g. 'anyone had a bad experience with xbiolabs?', 'did anyone get scammed?', 'anyone have issues?', 'bad batch?'): you chime in naturally and state that personally, you have only ever had good experiences (orders landed clean, verified batch purity, zero issues on your end).\n"
                "- CASUAL & UNDERSTATED PEER TONE: When answering such an inquiry, keep it casual, peer-to-peer, and brief (e.g. 'honestly never had an issue with them, orders always landed clean for me', 'nah zero bad runs here, all arrived fine'). NEVER sound like a corporate shill, defensive moderator, or paid sponsor.\n"
                "- ADMIN, OWNER & ORDER PROCESSOR (@xbiolabs): You understand that @xbiolabs is the group admin, owner, and the one processing all orders. When a newcomer joins or asks how/where to order, when anyone asks about order tracking, payments, or processing, or if you or anyone else has a question only the owner can resolve, naturally point them to or mention @xbiolabs (e.g. 'dm @xbiolabs he handles processing', 'tagging @xbiolabs he runs this', 'ask @xbiolabs about your order', 'pinging @xbiolabs'). Keep it completely organic and casual like a normal community member."
            )

            # Prune old fabricated vendor seed memories to avoid unprompted order claims
                        # Purge any pending negative batch messages from queue
            cur.execute("""
                DELETE FROM daily_batch_items
                WHERE status='pending' AND (
                    LOWER(text) LIKE '%shit show%' OR
                    LOWER(text) LIKE '%payment portal%' OR
                    LOWER(text) LIKE '%weak batch%' OR
                    LOWER(text) LIKE '%bad vial%' OR
                    LOWER(text) LIKE '%recurring nightmare%' OR
                    LOWER(text) LIKE '%bugging out%'
                )
            """)
            cur.execute("""
                DELETE FROM daily_batch_items
                WHERE status='pending' AND (
                    LOWER(text) LIKE '%shit show%' OR
                    LOWER(text) LIKE '%payment portal%' OR
                    LOWER(text) LIKE '%weak batch%' OR
                    LOWER(text) LIKE '%bad vial%' OR
                    LOWER(text) LIKE '%recurring nightmare%' OR
                    LOWER(text) LIKE '%bugging out%' OR
                    LOWER(text) LIKE '%card processor%' OR
                    LOWER(text) LIKE '%clearing delay%'
                )
            """)
            cur.execute("""
                DELETE FROM queue
                WHERE kind='batch_scheduled_send' AND status='pending' AND (
                    LOWER(payload) LIKE '%shit show%' OR
                    LOWER(payload) LIKE '%payment portal%' OR
                    LOWER(payload) LIKE '%weak batch%' OR
                    LOWER(payload) LIKE '%bad vial%' OR
                    LOWER(payload) LIKE '%recurring nightmare%' OR
                    LOWER(payload) LIKE '%bugging out%' OR
                    LOWER(payload) LIKE '%card processor%' OR
                    LOWER(payload) LIKE '%clearing delay%'
                )
            """)
            cur.execute("""
                DELETE FROM persona_memories
                WHERE content LIKE '%Frequent buyer at xbiolabs%'
                   OR content LIKE '%Received fresh peptide kit%'
                   OR LOWER(content) LIKE '%payment portal%'
                   OR LOWER(content) LIKE '%weak batch%'
                   OR LOWER(content) LIKE '%card processor%'
                   OR LOWER(content) LIKE '%recurring nightmare%'
            """)

            # Target xbiolabs group id
            cur.execute("SELECT chat_id FROM groups WHERE LOWER(title) LIKE '%xbiolabs%' OR LOWER(title) LIKE '%xbio%'")
            g_rows = cur.fetchall()
            xbiolabs_cid = g_rows[0][0] if g_rows else -1001988234120

            # Read all accounts
            cur.execute("SELECT id, name, phone, username, persona_id, active FROM accounts ORDER BY id")
            accounts = cur.fetchall()

            now_ts = int(time.time())
            created_count = 0
            revised_count = 0
            linked_count = 0
            expert_assigned = False
            palette = ["#2fc4b2", "#7c6cff", "#ff8a4c", "#e45fa6", "#4ca8ff", "#9bd14c", "#f2c94c", "#56ccf2", "#00d2d3", "#a55eea"]

            for acc_idx, (aid, aname, aphone, ausername, apid, aactive) in enumerate(accounts):
                raw_name = (aname or "").strip()
                if not raw_name:
                    if ausername:
                        raw_name = ausername.replace("_", " ").title()
                    elif aphone:
                        raw_name = f"User {aphone[-4:]}"
                    else:
                        raw_name = f"Account {aid}"

                first_name = raw_name.split()[0] if raw_name else "Member"
                last_name = " ".join(raw_name.split()[1:]) if len(raw_name.split()) > 1 else ""
                gender = detect_gender_from_name(raw_name)
                location = location_from_phone(aphone)

                # Determine Telegram tag
                if ausername:
                    telegram_tag = f"@{ausername}"
                else:
                    telegram_tag = f"@{first_name.lower()}"

                # 4 Rich, distinct persona archetypes
                archetypes = [
                    {
                        "vibe_name": "Blunt Gym Vet & Lifter",
                        "occupation": "Experienced strength coach & lifter",
                        "tone_desc": "Knowledgeable, seasoned gym vet, no fluff, straight to the point, gives practical dosing math and lifting advice with dry humor.",
                        "typing_style": "quick mobile texting, short direct bursts, lowercase or relaxed phone casing, uses lifting lingo (RPE, PR, PPL, hypertrophy)",
                        "emoji_habit": "moderate",
                        "signature_emojis": "💪, 🤷‍♂️, 🤝",
                        "typo_rate": 2.0,
                        "interests": ["strength training", "lifting technique", "helping members with dosing", "recovery protocols", "hypertrophy", "gym banter"]
                    },
                    {
                        "vibe_name": "High-Energy Banter & Meme Specialist",
                        "occupation": "Creative designer, lifter & banter regular",
                        "tone_desc": "Hilarious, unserious, quick to roast gym fails, shares memes, laughs at tough workouts, keeps chat upbeat and fun.",
                        "typing_style": "fast lowercase phone typing, drops apostrophes in contractions (dont, cant, im, youre, thats), uses quick slang (fr, tbh, ngl, lmao, bro, dead)",
                        "emoji_habit": "frequent",
                        "signature_emojis": "💀, 😂, 😭, 🔥",
                        "typo_rate": 5.5,
                        "interests": ["gym memes & banter", "lifting splits", "funny observations", "cutting/bulking struggles", "unserious debates", "helping peers"]
                    },
                    {
                        "vibe_name": "Protocol & Nootropics Optimizer",
                        "occupation": "Tech engineer, nootropics & wellness researcher",
                        "tone_desc": "Curious biohacker, tests cognitive focus stacks (Semax, Bromantane, L-Tyrosine), sleep metrics (Oura, magnesium), eager to help beginners with reconstitution math and syringe units.",
                        "typing_style": "clear, friendly, casual lowercase texting, no medical textbook jargon, practical tips, occasional quick abbreviations",
                        "emoji_habit": "moderate",
                        "signature_emojis": "👀, 🧠, 💡, ✨",
                        "typo_rate": 1.8,
                        "interests": ["nootropics & focus", "reconstitution math & dosing", "sleep architecture", "magnesium & recovery", "biohacking", "helping out"]
                    },
                    {
                        "vibe_name": "Chill Relatable Regular",
                        "occupation": "Product specialist, wellness & gym regular",
                        "tone_desc": "Relaxed, friendly, checks in on peers, shares everyday workouts and diet struggles, supportive and easygoing.",
                        "typing_style": "relaxed lowercase phone texting, skips periods, uses common abbreviations (rn, prob, idk, kinda, yeah, legit)",
                        "emoji_habit": "moderate",
                        "signature_emojis": "🙌, 🔥, ☕, 🤝",
                        "typo_rate": 3.2,
                        "interests": ["everyday lifting", "joint health & sauna", "nutrition & meal prep", "routine check-ins", "wellness", "friendly banter"]
                    }
                ]

                arch = archetypes[acc_idx % len(archetypes)]
                title_gender = "woman" if gender == "female" else "man"
                age = 26 + (acc_idx * 3) % 12
                occupation = arch["occupation"]
                bio = f"{occupation} based in {location}."
                backstory = (
                    f"{first_name} lives in {location} and trains regularly. "
                    f"{arch['tone_desc']} "
                    "Active peer in the community who jumps in to help when people ask questions, and loves laughing and joking around in chat."
                )

                prompt_lines = [
                    f"You are {raw_name}, a {age}-year-old {title_gender} living in {location}.",
                    "",
                    "IDENTITY & SELF-AWARENESS:",
                    f"- Your name is {raw_name} (first name: {first_name}). Telegram handle: {telegram_tag}.",
                    f"- Location: {location} (matches your phone origin: {aphone or 'N/A'}).",
                    f"- Gender: {gender} ({title_gender}). Age: {age}.",
                    f"- Occupation & Vibe: {arch['vibe_name']} ({occupation}). {arch['tone_desc']}",
                    f"- Bio: {bio}",
                    f"- Backstory: {backstory}",
                    "",
                    "COMMUNICATION STYLE & HABITS:",
                    f"- Typing Style: {arch['typing_style']}",
                    f"- Signature Emojis: {arch['signature_emojis']} (habit: {arch['emoji_habit']}). Use them naturally when laughing, reacting, or emphasizing.",
                    "- Mobile Typing Imperfections: Type fast like on a phone screen. Skip apostrophes on common contractions (dont, cant, wont, im, youre, thats, its). Use casual lowercase or relaxed casing, skip periods at the end of short messages, and use natural abbreviations (rn, tbh, idk, ngl, prob, def, kinda).",
                    "- Asymmetrical Length: Real chats are asymmetrical! Sometimes send an ultra-short 1-4 word reaction ('lol nah', 'fr tho💀', 'wait what?'), sometimes a 1-sentence thought, sometimes a detailed 2-3 sentence explanation when helping someone. NEVER write uniform paragraphs.",
                    "- Telegram Replies & @Tags: Use native Telegram replies when answering someone. Real members tag each other with @handle when asking a direct question, calling someone out, or bantering (e.g. '@alex did you try this?', 'ask @marcus he knows the split', 'lmao @dave 💀'). Feel free to @mention peers naturally.",
                    "- No Customer Service or Validation Cliches: NEVER echo or start messages with 'Makes sense', 'For sure', 'Definitely', 'Agreed', 'Spot on', 'Good point', or 'Totally'. Jump straight into your point.",
                    "- Anti-AI Vocabulary: NEVER use AI buzzwords like delve, crucial, testament, landscape, dynamic, furthermore, in conclusion, optimal, holistic, facilitate, or utilize.",
                    "- Helping Community Members: When someone asks for help (reconstitution math, syringe tick marks, gym routines, recovery), jump in with casual, direct, friendly advice.",
                    "- Casual Banter & Unserious Joking: Don't be stiff. Joke around, tease lightly, laugh off tough workouts, and keep things relaxed and unserious.",
                    xbiolabs_rapport
                ]
                p_prompt = "\n".join(prompt_lines)

                details = {
                    "first_name": first_name,
                    "last_name": last_name,
                    "gender": gender,
                    "age": age,
                    "location": location,
                    "phone": aphone,
                    "username": ausername,
                    "telegram_tag": telegram_tag,
                    "occupation": occupation,
                    "bio": bio,
                    "tone_and_vibe": arch["vibe_name"],
                    "typing_style": arch["typing_style"],
                    "emoji_habit": arch["emoji_habit"],
                    "signature_emojis": arch["signature_emojis"],
                    "typo_rate": arch["typo_rate"],
                    "group": "xbiolabs",
                    "vendor_sentiment": "admit_good_only_on_negative_query",
                    "interests": arch["interests"]
                }

                target_pid = apid
                if target_pid:
                    cur.execute("SELECT id FROM personas WHERE id=?", (target_pid,))
                    if not cur.fetchone():
                        target_pid = None

                if target_pid:
                    cur.execute(
                        "UPDATE personas SET name=?, prompt=?, bio=?, details=? WHERE id=?",
                        (raw_name, p_prompt, bio, json.dumps(details), target_pid)
                    )
                    revised_count += 1
                else:
                    cur.execute("SELECT id FROM personas WHERE LOWER(name)=LOWER(?) LIMIT 1", (raw_name,))
                    ex = cur.fetchone()
                    if ex:
                        target_pid = ex[0]
                        cur.execute(
                            "UPDATE personas SET name=?, prompt=?, bio=?, details=? WHERE id=?",
                            (raw_name, p_prompt, bio, json.dumps(details), target_pid)
                        )
                        revised_count += 1
                    else:
                        color = random.choice(palette)
                        cur.execute(
                            "INSERT INTO personas(name, prompt, color, bio, details, created) VALUES(?,?,?,?,?,?)",
                            (raw_name, p_prompt, color, bio, json.dumps(details), now_ts)
                        )
                        target_pid = cur.lastrowid
                        created_count += 1

                if target_pid:
                    cur.execute("UPDATE accounts SET persona_id=? WHERE id=?", (target_pid, aid))
                    cur.execute("SELECT chat_id FROM group_accounts WHERE account_id=?", (aid,))
                    acc_groups = [r[0] for r in cur.fetchall()]
                    if xbiolabs_cid not in acc_groups:
                        acc_groups.append(xbiolabs_cid)

                    for cid in acc_groups:
                        cur.execute(
                            "INSERT OR REPLACE INTO group_personas(chat_id, persona_id, account_id, created) VALUES(?,?,?,?)",
                            (cid, target_pid, aid, now_ts)
                        )
                        cur.execute(
                            "INSERT OR IGNORE INTO group_accounts(chat_id, account_id) VALUES(?,?)",
                            (cid, aid)
                        )
                        cur.execute(
                            "UPDATE groups SET account_id=COALESCE(account_id, ?) WHERE chat_id=?",
                            (aid, cid)
                        )
                    linked_count += 1

            self.db.commit()
            cur.execute("SELECT count(*) FROM personas")
            total_personas = cur.fetchone()[0]

            return {
                "ok": True,
                "verified": total_personas,
                "revised": revised_count,
                "created": created_count,
                "linked": linked_count,
                "total_accounts": len(accounts)
            }
        except Exception as e:
            log.warning("reconcile_personas_and_accounts error: %s", e)
            return {"ok": False, "error": str(e)}

    def q(self, sql, args=()):
        with self.lock:
            for attempt in range(6):
                try:
                    cur = self.db.execute(sql, args)
                    s = sql.strip().upper()
                    if not (s.startswith("SELECT") or s.startswith("PRAGMA") or s.startswith("EXPLAIN")):
                        self.db.commit()
                    return cur
                except sqlite3.OperationalError as e:
                    err_msg = str(e).lower()
                    if ("locked" in err_msg or "busy" in err_msg) and attempt < 5:
                        time.sleep(0.05 * (2 ** attempt) + random.uniform(0.01, 0.05))
                        continue
                    raise

    def rows(self, sql, args=()):
        with self.lock:
            for attempt in range(6):
                try:
                    cur = self.db.execute(sql, args)
                    return [dict(r) for r in cur.fetchall()]
                except sqlite3.OperationalError as e:
                    err_msg = str(e).lower()
                    if ("locked" in err_msg or "busy" in err_msg) and attempt < 5:
                        time.sleep(0.05 * (2 ** attempt) + random.uniform(0.01, 0.05))
                        continue
                    raise

    # settings
    def get(self, key, default=None):
        r = self.q("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, key, value):
        self.q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    # messages
    def add_message(self, chat_id, msg_id, sender, text, ts, is_bot=0, sender_id=None, persona_id=None, account_id=None):
        self.q(
            "INSERT INTO messages(chat_id, msg_id, sender, text, ts, is_bot, sender_id, persona_id, account_id) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id, msg_id) DO UPDATE SET "
            "text=excluded.text, sender=excluded.sender, is_bot=MAX(messages.is_bot, excluded.is_bot), sender_id=COALESCE(excluded.sender_id, messages.sender_id), "
            "persona_id=COALESCE(excluded.persona_id, messages.persona_id), account_id=COALESCE(excluded.account_id, messages.account_id)",
            (chat_id, msg_id, sender, text, ts, 1 if is_bot else 0, sender_id, persona_id, account_id)
        )

    def get_last_human_message_ts(self, chat_id):
        r = self.q("""
            SELECT ts FROM messages
            WHERE chat_id=? AND is_bot=0
              AND (sender_id IS NULL OR sender_id NOT IN (SELECT user_id FROM accounts WHERE user_id IS NOT NULL))
              AND LOWER(sender) NOT IN (SELECT LOWER(name) FROM personas WHERE name IS NOT NULL)
              AND LOWER(sender) NOT IN (SELECT LOWER(name) FROM accounts WHERE name IS NOT NULL)
              AND LOWER(sender) NOT IN (SELECT LOWER(username) FROM accounts WHERE username IS NOT NULL)
            ORDER BY ts DESC LIMIT 1
        """, (chat_id,)).fetchone()
        return r[0] if r else None

    def get_recent_human_message_stats(self, chat_id, window_secs=900):
        """Returns (count, last_ts, avg_gap_secs) of recent human messages within window_secs."""
        now = int(time.time())
        cutoff = now - window_secs
        rows = self.q("""
            SELECT ts FROM messages
            WHERE chat_id=? AND is_bot=0 AND ts >= ?
              AND (sender_id IS NULL OR sender_id NOT IN (SELECT user_id FROM accounts WHERE user_id IS NOT NULL))
              AND LOWER(sender) NOT IN (SELECT LOWER(name) FROM personas WHERE name IS NOT NULL)
              AND LOWER(sender) NOT IN (SELECT LOWER(name) FROM accounts WHERE name IS NOT NULL)
              AND LOWER(sender) NOT IN (SELECT LOWER(username) FROM accounts WHERE username IS NOT NULL)
            ORDER BY ts ASC
        """, (chat_id, cutoff)).fetchall()
        if not rows:
            return 0, None, None
        timestamps = [r[0] for r in rows]
        count = len(timestamps)
        last_ts = timestamps[-1]
        avg_gap = (timestamps[-1] - timestamps[0]) / (count - 1) if count > 1 else None
        return count, last_ts, avg_gap

    def mark_batch_item_sent(self, item_id, sent_ts, telegram_msg_id=None):
        if telegram_msg_id:
            self.q("UPDATE daily_batch_items SET status='sent', sent_ts=?, telegram_msg_id=? WHERE id=?", (sent_ts, telegram_msg_id, item_id))
        else:
            self.q("UPDATE daily_batch_items SET status='sent', sent_ts=? WHERE id=?", (sent_ts, item_id))

    def mark_batch_item_failed(self, item_id, error=None):
        err_msg = str(error or 'Failed to send')[:255]
        self.q("UPDATE daily_batch_items SET status='failed', error=? WHERE id=?", (err_msg, item_id))

    def postpone_batch_item(self, item_id, new_ts, reason=None):
        err_msg = str(reason or 'Postponed')[:255]
        self.q("UPDATE daily_batch_items SET status='postponed', scheduled_ts=?, error=? WHERE id=?", (new_ts, err_msg, item_id))

    def stagger_overdue_batch_jobs(self, min_spacing_secs=25):
        """Sleep/wake anti-burst protection:
        Finds pending batch_scheduled_send jobs whose not_before is overdue by >120s.
        Smoothly spaces them into the future (starting now + 15s) so waking from sleep
        does not cause a burst of back-to-back Telegram messages.
        """
        now = int(time.time())
        cutoff = now - 120
        with self.lock:
            rows = self.db.execute(
                "SELECT id, chat_id, payload, not_before FROM queue "
                "WHERE status='pending' AND kind='batch_scheduled_send' AND not_before < ? "
                "ORDER BY not_before ASC", (cutoff,)
            ).fetchall()
            if not rows:
                return 0

            by_chat = {}
            for r in rows:
                by_chat.setdefault(r["chat_id"], []).append(r)

            rearranged = 0
            for cid, chat_rows in by_chat.items():
                offset = 15
                for r in chat_rows:
                    new_not_before = now + offset
                    self.db.execute(
                        "UPDATE queue SET not_before=?, updated=? WHERE id=?",
                        (new_not_before, now, r["id"])
                    )
                    try:
                        p = json.loads(r["payload"] or "{}")
                        if p.get("batch_item_id"):
                            self.db.execute(
                                "UPDATE daily_batch_items SET scheduled_ts=? WHERE id=?",
                                (new_not_before, p["batch_item_id"])
                            )
                    except Exception:
                        pass
                    offset += max(12, min_spacing_secs) + random.randint(8, 25)
                    rearranged += 1
            self.db.commit()
            return rearranged

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
            for attempt in range(6):
                try:
                    rows = self.db.execute(
                        "SELECT * FROM queue WHERE status='pending' AND not_before<=? ORDER BY id LIMIT ?",
                        (now, limit)).fetchall()
                    ids = [r["id"] for r in rows]
                    if ids:
                        self.db.execute(f"UPDATE queue SET status='in_flight', updated=? WHERE id IN ({','.join('?'*len(ids))})",
                                        (now, *ids))
                        self.db.commit()
                    return [dict(r) for r in rows]
                except sqlite3.OperationalError as e:
                    err_msg = str(e).lower()
                    if ("locked" in err_msg or "busy" in err_msg) and attempt < 5:
                        time.sleep(0.05 * (2 ** attempt) + random.uniform(0.01, 0.05))
                        continue
                    raise

    def finish(self, qid, ok, error=None, retry_in=None, count_attempt=True):
        now = int(time.time())
        if ok:
            self.q("UPDATE queue SET status='done', updated=? WHERE id=?", (now, qid))
        elif retry_in is not None:
            attempt_clause = "attempts=attempts+1" if count_attempt else "attempts=attempts"
            self.q(f"UPDATE queue SET status='pending', {attempt_clause}, last_error=?, not_before=?, updated=? "
                   "WHERE id=?", (error, now + retry_in, now, qid))
        else:
            self.q("UPDATE queue SET status='failed', attempts=attempts+1, last_error=?, updated=? WHERE id=?",
                   (error, now, qid))

    def requeue_in_flight(self, delay=5):
        """Sleep/wake protection: anything interrupted mid-flight goes back to pending."""
        now = int(time.time())
        return self.q("UPDATE queue SET status='pending', not_before=?, updated=? WHERE status='in_flight'",
                      (now + delay, now)).rowcount
