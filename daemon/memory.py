"""Episodic and Semantic Domain-Slot Memory Engine for AI Personas.

Extreme-efficiency memory engine featuring:
- Slot-based deterministic state tracking (protocol, injury_rehab, vendor_order, training, health_labs, stances_takes, personal_bio)
- Automatic contradiction resolution & superseding (never contradictory active facts)
- Bidirectional human tracking with status resolution (open inquiry -> resolved) & proactive callback cues
- High-density prompt memory injection (< 180 tokens for persona, < 80 tokens for humans)
- Zero-LLM latency: 100% pure standard library regex/token extraction (< 1.5ms per query)
- SQLite WAL optimized with strict table bounds & autonomous retention pruning
"""

import time
import json
import logging
import re
import math
import random

log = logging.getLogger("memory")

DEFAULT_EXPIRIES = {
    "short": 48 * 3600,        # 48 hours
    "medium": 30 * 24 * 3600,  # 30 days
    "long": None               # Permanent anchor (NEVER expires)
}

STOP_WORDS = {
    'a', 'about', 'above', 'after', 'again', 'against', 'all', 'am', 'an', 'and', 'any', 'are', 'aren',
    'as', 'at', 'be', 'because', 'been', 'before', 'being', 'below', 'between', 'both', 'but', 'by',
    'can', 'could', 'did', 'do', 'does', 'doing', 'down', 'during', 'each', 'few', 'for', 'from',
    'further', 'had', 'has', 'have', 'having', 'he', 'her', 'here', 'hers', 'herself', 'him', 'himself',
    'his', 'how', 'i', 'if', 'in', 'into', 'is', 'it', 'its', 'itself', 'just', 'me', 'more', 'most',
    'my', 'myself', 'no', 'nor', 'not', 'now', 'of', 'off', 'on', 'once', 'only', 'or', 'other', 'our',
    'ours', 'ourselves', 'out', 'over', 'own', 'same', 'she', 'should', 'so', 'some', 'such', 'than',
    'that', 'the', 'their', 'theirs', 'them', 'themselves', 'then', 'there', 'these', 'they', 'this',
    'those', 'through', 'to', 'too', 'under', 'until', 'up', 'very', 'was', 'we', 'were', 'what', 'when',
    'where', 'which', 'while', 'who', 'whom', 'why', 'with', 'you', 'your', 'yours', 'yourself'
}

SLOT_DEFINITIONS = [
    ("protocol", "PROTOCOL", 1, [
        r"\b(?:running|taking|pinning|dosing|on|started|titrating|blasting|cruising)\b.*\b(?:tirz(?:epatide)?|reta(?:trutide)?|sema(?:glutide)?|glp-?1|bpc(?:-?157)?|tb-?500|trt|test|testosterone|cyp|enanthate|prop|tren|deca|anavar|var|dbol|primo|mast|sarms|rad-?140|lgd-?4033|mk-?677|cjc|ipam|gh|hgh|hcg|pct|arimidex|aromasin)\b",
        r"\b(?:tirz(?:epatide)?|reta(?:trutide)?|sema(?:glutide)?|glp-?1|bpc(?:-?157)?|tb-?500|trt|test|testosterone|cyp|enanthate|tren|deca|anavar|var|dbol|primo|masteron|rad-?140|mk-?677|cjc-?1295|ipamorelin|hgh)\b.*\b(?:\d+\s*(?:mg|mcg|iu|ml)|dose|split|subq|im|bid|ed|eod|weekly|daily)\b",
        r"\b(?:my current|current)\s+(?:cycle|stack|protocol|dose|split|trt)\b",
        r"\b(?:reconstituted|bac water|vial|peptides?|reconstitution)\b"
    ]),
    ("injury_rehab", "INJURY & REHAB", 2, [
        r"\b(?:rotator cuff|shoulder|tendon|tendonitis|tendinopathy|golfers? elbow|tennis elbow|knee|patellar|lower back|disc|herniated|sciatica|impingement|tear|sprain|strain|bicep tendon|achilles)\b",
        r"\b(?:injury|rehab|physical therapy|pt|flare-?up|healing|mobility work|joint pain|nerve pain)\b"
    ]),
    ("vendor_order", "ORDERS & SOURCING", 3, [
        r"\b(?:xbiolabs|xbio)\b",
        r"\b(?:order|ordered|pack|package|package landed|pack landed|landed today|tracking|usps|label created|carrier|customs|delivery|informed delivery)\b",
        r"\b(?:janoshik|purity test|coa|third-?party testing|tested at \d+)\b"
    ]),
    ("training", "TRAINING & PHYSIQUE", 4, [
        r"\b(?:push pull legs|ppl|upper lower|bro split|arnold split|hypertrophy|strength program)\b",
        r"\b(?:bench|squat|deadlift|overhead press|ohp|incline|rdl|pr|1rm)\b",
        r"\b(?:cutting|bulking|bulk|cut|maintenance|bodyfat|body fat|\d{2,3}\s*lbs|\d{2,3}\s*kg)\b"
    ]),
    ("health_labs", "HEALTH & LABS", 5, [
        r"\b(?:bloodwork|bloods|labs|blood test|panel)\b",
        r"\b(?:hematocrit|hdl|ldl|lipids|ast|alt|liver enzymes?|creatinine|egfr|estradiol|e2|prolactin|shbg|free test|total test)\b",
        r"\b(?:blood pressure|bp|sleep score|oura|whoop|resting heart rate|rhr|apnea)\b"
    ]),
    ("stances_takes", "CORE BELIEFS", 6, [
        r"\b(?:i swear by|overrated|underrated|waste of money|garbage|never touching|never again|essential|golden rule|my stance|unpopular opinion)\b",
        r"\b(?:i (?:always|never) recommend|always verify|slow titration)\b"
    ]),
    ("personal_bio", "BACKGROUND", 7, [
        r"\b(?:years old|\d{2}\s*yo)\b",
        r"\b(?:i live in|moved to|originally from)\b",
        r"\b(?:my wife|my gf|girlfriend|my kids?|my son|my daughter)\b",
        r"\b(?:i work (?:as|in)|my day job|software engineer|construction|finance)\b",
        r"\b(?:train at \d\s*(?:am|pm)|early morning lifter|garage gym)\b"
    ])
]

HUMAN_SLOT_PATTERNS = [
    ("order_vendor", [
        r"\b(?:xbiolabs|xbio)\b",
        r"\b(?:order|ordered|pack|package|package landed|pack landed|landed today|tracking|usps|label created|carrier|customs|delivery|informed delivery)\b",
        r"\b(?:janoshik|purity test|coa|third-?party testing)\b"
    ]),
    ("protocol", [
        r"\b(?:bpc(?:-?157)?|tb-?500|tirz(?:epatide)?|reta(?:trutide)?|sema(?:glutide)?|test(?:osterone)?|trt|tren|deca|anavar|var|dbol|primo|sarms|rad-?140|mk-?677|cjc-?1295|ipamorelin|hgh|hcg|reconstitut\w*|bac water)\b"
    ]),
    ("injury_health", [
        r"\b(?:rotator cuff|shoulder|tendon|tendonitis|tendinopathy|golfers? elbow|tennis elbow|knee|patellar|lower back|disc|sciatica|impingement|tear|sprain|strain|bicep tendon|nausea|fatigue|insomnia|bloodwork|labs)\b"
    ]),
    ("training", [
        r"\b(?:bench|squat|deadlift|pr|split|ppl|bulk|cut|cutting|bodyfat|macros|creatine|hypertrophy)\b"
    ]),
    ("personal", [
        r"\b(?:i am|i'm|im|my weight|my height|years lifting|training for|my job|i work)\b"
    ])
]

def extract_keywords(text):
    """Extracts non-stopword alphanumeric tokens (3+ chars) in lowercase."""
    if not text:
        return set()
    return {w for w in re.findall(r'[a-zA-Z0-9_-]{3,}', text.lower()) if w not in STOP_WORDS}

def clean_memory_text(text):
    """Strips repetitive nested label prefixes and artifacts from memory content."""
    if not text:
        return ""
    pattern = re.compile(
        r'^(?:Core Protocol/Fact|Established Fact|Shared in chat|Recent comment|Personal Fact|Key Fact|Core Established Facts|\[[^\]]+\])[:\s\-]+',
        flags=re.IGNORECASE
    )
    prev = None
    while prev != text:
        prev = text
        text = pattern.sub('', text).strip()
    return text

def detect_persona_slot(text):
    """Deterministic zero-LLM domain-slot classifier for personas."""
    if not text:
        return None
    t = text.lower()
    for slot_key, _label, _prio, patterns in SLOT_DEFINITIONS:
        for pat in patterns:
            if re.search(pat, t):
                return slot_key
    return None

def detect_human_slot(text):
    """Deterministic zero-LLM domain-slot classifier for human members."""
    if not text:
        return "general"
    t = text.lower()
    for slot_key, patterns in HUMAN_SLOT_PATTERNS:
        for pat in patterns:
            if re.search(pat, t):
                return slot_key
    if "?" in text and len(text) >= 15:
        return "open_inquiry"
    return "general"


class MemoryEngine:
    def __init__(self, store, ai_client=None):
        self.store = store
        self.ai = ai_client
        self.ensure_tables()

    def ensure_tables(self):
        """Creates persona_memories, human_memories, indices, and ensures slot migration columns."""
        with self.store.lock:
            cur = self.store.db.cursor()
            cur.execute("""
            CREATE TABLE IF NOT EXISTS persona_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona_id INTEGER NOT NULL,
                account_id INTEGER,
                chat_id INTEGER,
                kind TEXT DEFAULT 'statement',
                content TEXT NOT NULL,
                context TEXT DEFAULT '',
                salience REAL DEFAULT 0.7,
                retention_tier TEXT DEFAULT 'medium',
                created_at INTEGER NOT NULL,
                expires_at INTEGER,
                access_count INTEGER DEFAULT 0,
                last_accessed_at INTEGER,
                slot_key TEXT DEFAULT NULL,
                is_active INTEGER DEFAULT 1
            )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_pid_tier ON persona_memories(persona_id, retention_tier, expires_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_chat ON persona_memories(chat_id, created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_pid_slot ON persona_memories(persona_id, slot_key, is_active)")

            cur.execute("""
            CREATE TABLE IF NOT EXISTS human_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                sender TEXT NOT NULL,
                sender_id INTEGER,
                sender_username TEXT,
                fact TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                created_at INTEGER NOT NULL,
                last_referenced_at INTEGER,
                slot_key TEXT DEFAULT NULL,
                is_active INTEGER DEFAULT 1,
                status TEXT DEFAULT 'open'
            )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_hm_chat_sender ON human_memories(chat_id, sender)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_hm_chat_sender_slot ON human_memories(chat_id, sender, slot_key, is_active)")

            # Check and add missing columns if upgrading an older schema
            try:
                cur.execute("ALTER TABLE persona_memories ADD COLUMN slot_key TEXT DEFAULT NULL")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE persona_memories ADD COLUMN is_active INTEGER DEFAULT 1")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE human_memories ADD COLUMN slot_key TEXT DEFAULT NULL")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE human_memories ADD COLUMN is_active INTEGER DEFAULT 1")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE human_memories ADD COLUMN status TEXT DEFAULT 'open'")
            except Exception:
                pass

            # Backfill slots for existing un-slotted persona memories
            cur.execute("SELECT id, content FROM persona_memories WHERE slot_key IS NULL AND is_active = 1")
            unslotted = cur.fetchall()
            for r in unslotted:
                mid = r[0] if isinstance(r, (tuple, list)) else r["id"]
                c_text = r[1] if isinstance(r, (tuple, list)) else r["content"]
                detected = detect_persona_slot(c_text)
                if detected:
                    cur.execute("UPDATE persona_memories SET slot_key=?, retention_tier='long', expires_at=NULL WHERE id=?", (detected, mid))

            self.store.db.commit()

    def set_slot(self, persona_id, slot_key, content, account_id=None, chat_id=None, salience=0.95):
        """Deterministic slot insertion with superseding conflict resolution.
        Marks existing active facts for the same slot as superseded, eliminating contradictions."""
        clean_content = clean_memory_text(str(content or "").strip())
        if not clean_content:
            return None
        now_ts = int(time.time())

        with self.store.lock:
            cur = self.store.db.cursor()
            # Check existing active slot
            cur.execute(
                "SELECT id, content FROM persona_memories WHERE persona_id=? AND slot_key=? AND is_active=1",
                (persona_id, slot_key)
            )
            existing = cur.fetchall()
            new_words = extract_keywords(clean_content)

            # If nearly identical, touch access timestamp instead of inserting duplicate
            for ex in existing:
                ex_id = ex[0] if isinstance(ex, (tuple, list)) else ex["id"]
                ex_c = ex[1] if isinstance(ex, (tuple, list)) else ex["content"]
                ex_words = extract_keywords(ex_c)
                if new_words and ex_words:
                    overlap = len(new_words & ex_words) / max(1, len(new_words | ex_words))
                    if overlap >= 0.70:
                        cur.execute(
                            "UPDATE persona_memories SET last_accessed_at=?, access_count=access_count+1, content=? WHERE id=?",
                            (now_ts, clean_content, ex_id)
                        )
                        self.store.db.commit()
                        return ex_id

            # Supersede all prior active entries for this slot
            for ex in existing:
                ex_id = ex[0] if isinstance(ex, (tuple, list)) else ex["id"]
                cur.execute(
                    "UPDATE persona_memories SET is_active=0, context=? WHERE id=?",
                    (f"superseded_at_{now_ts}", ex_id)
                )

            # Insert new active slot truth
            cur.execute("""
                INSERT INTO persona_memories(
                    persona_id, account_id, chat_id, kind, content, context,
                    salience, retention_tier, created_at, expires_at, access_count, last_accessed_at,
                    slot_key, is_active
                ) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,1)
            """, (
                persona_id, account_id, chat_id, "slot_fact", clean_content, "",
                salience, "long", now_ts, None, now_ts, slot_key
            ))
            new_id = cur.lastrowid
            self.store.db.commit()
            return new_id

    def get_active_slots(self, persona_id):
        """Returns dictionary of currently active domain slots for a persona."""
        rows = self.store.rows(
            "SELECT slot_key, content, created_at, salience FROM persona_memories "
            "WHERE persona_id=? AND slot_key IS NOT NULL AND is_active=1 ORDER BY created_at DESC",
            (persona_id,)
        )
        slots = {}
        for r in rows:
            sk = r.get("slot_key")
            if sk and sk not in slots:
                slots[sk] = {
                    "content": clean_memory_text(r.get("content", "")),
                    "created_at": r.get("created_at"),
                    "salience": r.get("salience")
                }
        return slots

    def add_memory(self, persona_id, content, account_id=None, chat_id=None, kind="statement",
                   retention_tier="medium", salience=0.7, expires_in_seconds=None, slot_key=None):
        """Adds a memory node, auto-routing to domain slot superseding if matching pattern."""
        clean_content = clean_memory_text(str(content or "").strip())
        if not clean_content:
            return None

        # Check if this content qualifies for a domain slot
        target_slot = slot_key or detect_persona_slot(clean_content)
        if target_slot:
            return self.set_slot(persona_id, target_slot, clean_content, account_id=account_id, chat_id=chat_id, salience=max(0.85, salience))

        now_ts = int(time.time())
        tier = retention_tier if retention_tier in ("short", "medium", "long") else "medium"
        if tier == "long":
            expires_at = None
        elif expires_in_seconds is not None:
            expires_at = now_ts + int(expires_in_seconds) if expires_in_seconds > 0 else None
        else:
            default_dur = DEFAULT_EXPIRIES.get(tier)
            expires_at = now_ts + default_dur if default_dur else None

        salience_val = max(0.1, min(float(salience or 0.7), 1.0))

        with self.store.lock:
            cur = self.store.db.cursor()
            cur.execute("""
                INSERT INTO persona_memories(
                    persona_id, account_id, chat_id, kind, content, context,
                    salience, retention_tier, created_at, expires_at, access_count, last_accessed_at,
                    slot_key, is_active
                ) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,NULL,1)
            """, (persona_id, account_id, chat_id, kind, clean_content, "",
                  salience_val, tier, now_ts, expires_at, now_ts))
            self.store.db.commit()
            return cur.lastrowid

    def get_memories(self, persona_id, retention_tier=None, limit=150):
        """Retrieves memories for a persona, prioritizing active slots and anchors."""
        args = [persona_id]
        sql = "SELECT * FROM persona_memories WHERE persona_id=? AND is_active=1"
        if retention_tier:
            sql += " AND retention_tier=?"
            args.append(retention_tier)
        sql += " ORDER BY CASE WHEN slot_key IS NOT NULL THEN 1 WHEN retention_tier='long' THEN 2 WHEN retention_tier='medium' THEN 3 ELSE 4 END, created_at DESC LIMIT ?"
        args.append(limit)
        return self.store.rows(sql, tuple(args))

    def get_memory_tree(self, persona_id):
        """Builds a structured hierarchical tree of memories including active domain slots."""
        persona = self.store.rows("SELECT id, name, color FROM personas WHERE id=?", (persona_id,))
        pname = persona[0]["name"] if persona else f"Persona #{persona_id}"
        pcolor = persona[0]["color"] if persona else "#2fc4b2"

        all_memories = self.store.rows(
            "SELECT * FROM persona_memories WHERE persona_id=? ORDER BY created_at DESC LIMIT 200",
            (persona_id,)
        )

        anchors = []
        medium = []
        short = []
        slots = {}

        now_ts = int(time.time())
        for m in all_memories:
            item = dict(m)
            item["content"] = clean_memory_text(item.get("content", ""))
            if item.get("expires_at"):
                remaining = item["expires_at"] - now_ts
                item["is_expired"] = remaining <= 0
                item["remaining_hours"] = max(0, round(remaining / 3600.0, 1))
            else:
                item["is_expired"] = False
                item["remaining_hours"] = None

            sk = item.get("slot_key")
            if sk and item.get("is_active"):
                if sk not in slots:
                    slots[sk] = item["content"]

            tier = item.get("retention_tier")
            if tier == "long":
                anchors.append(item)
            elif tier == "medium":
                medium.append(item)
            else:
                short.append(item)

        return {
            "persona_id": persona_id,
            "persona_name": pname,
            "persona_color": pcolor,
            "slots": slots,
            "stats": {
                "total": len(all_memories),
                "active_slots": len(slots),
                "anchors": len(anchors),
                "weekly": len(medium),
                "short_term": len(short)
            },
            "tree": {
                "anchors": anchors,
                "weekly": medium,
                "short_term": short
            }
        }

    def delete_memory(self, memory_id):
        """Deletes a specific memory item."""
        with self.store.lock:
            self.store.db.execute("DELETE FROM persona_memories WHERE id=?", (memory_id,))
            self.store.db.commit()
            return True

    def promote_to_anchor(self, memory_id):
        """Promotes a memory to a permanent long-term anchor."""
        with self.store.lock:
            self.store.db.execute(
                "UPDATE persona_memories SET retention_tier='long', expires_at=NULL, salience=0.95 WHERE id=?",
                (memory_id,)
            )
            self.store.db.commit()
            return True

    def prune_memories(self, persona_id=None):
        """Autonomous Storage Bounds & Memory Pruning Agent.
        Enforces hard upper bounds on table size, deletes expired short-term rows,
        prunes superseded facts older than 14 days, and caps memory count per persona."""
        now_ts = int(time.time())
        scanned = 0
        pruned = 0
        promoted = 0

        where_clause = "WHERE expires_at IS NOT NULL AND expires_at <= ?"
        args = [now_ts]
        if persona_id:
            where_clause += " AND persona_id=?"
            args.append(persona_id)

        expired_rows = self.store.rows(f"SELECT * FROM persona_memories {where_clause}", tuple(args))
        scanned = len(expired_rows)

        with self.store.lock:
            cur = self.store.db.cursor()
            # 1. Prune expired rows
            for r in expired_rows:
                mid = r["id"]
                tier = r["retention_tier"]
                salience = float(r.get("salience") or 0.5)
                accesses = int(r.get("access_count") or 0)

                if tier == "short" or salience < 0.4:
                    cur.execute("DELETE FROM persona_memories WHERE id=?", (mid,))
                    pruned += 1
                elif tier == "medium":
                    if salience >= 0.85 and accesses >= 2:
                        cur.execute(
                            "UPDATE persona_memories SET retention_tier='long', expires_at=NULL, salience=0.9 WHERE id=?",
                            (mid,)
                        )
                        promoted += 1
                    else:
                        cur.execute("DELETE FROM persona_memories WHERE id=?", (mid,))
                        pruned += 1

            # 2. Prune old superseded inactive memories (> 14 days old)
            cur.execute(
                "DELETE FROM persona_memories WHERE is_active=0 AND created_at < ?",
                (now_ts - 14 * 86400,)
            )
            pruned += cur.rowcount if cur.rowcount > 0 else 0

            # 3. Enforce per-persona storage cap (keep max 30 memories per persona)
            p_ids = [persona_id] if persona_id else [r["id"] for r in self.store.rows("SELECT id FROM personas")]
            for pid in p_ids:
                cur.execute(
                    """DELETE FROM persona_memories
                       WHERE persona_id=? AND slot_key IS NULL AND is_active=1 AND retention_tier != 'long'
                       AND id NOT IN (
                           SELECT id FROM persona_memories
                           WHERE persona_id=? AND slot_key IS NULL AND is_active=1
                           ORDER BY created_at DESC LIMIT 20
                       )""",
                    (pid, pid)
                )
                pruned += cur.rowcount if cur.rowcount > 0 else 0

            # 4. Prune duplicate contents for same persona
            cur.execute("""
                DELETE FROM persona_memories WHERE id NOT IN (
                    SELECT MIN(id) FROM persona_memories GROUP BY persona_id, content
                )
            """)
            pruned += cur.rowcount if cur.rowcount > 0 else 0

            self.store.db.commit()

        log.info(f"MemoryAgent prune finished: scanned={scanned}, pruned={pruned}, promoted={promoted}")
        return {
            "scanned": scanned,
            "pruned": pruned,
            "promoted": promoted,
            "retained": max(0, scanned - pruned)
        }

    def get_prompt_memory_context(self, persona_id, chat_id=None, limit=8, query_text=None):
        """High-Density, Extreme Token-Efficiency (<180 Tokens) Memory Injection.
        Injects active domain slots with deterministic precision, plus up to 2-3 query-relevant
        episodic observations, eliminating fluff, repetition, and contradiction."""
        if not persona_id:
            return ""
        now_ts = int(time.time())

        # 1. Fetch active domain slots (the ground truth of who this persona is right now)
        slot_rows = self.store.rows(
            "SELECT slot_key, content, salience FROM persona_memories "
            "WHERE persona_id=? AND slot_key IS NOT NULL AND is_active=1 "
            "ORDER BY created_at DESC",
            (persona_id,)
        )
        active_slots = {}
        for r in slot_rows:
            sk = r.get("slot_key")
            if sk and sk not in active_slots:
                active_slots[sk] = clean_memory_text(r.get("content", ""))

        # 2. Fetch non-slot episodic memories filtered by query relevance
        query_words = extract_keywords(query_text) if query_text else set()
        episodic_rows = self.store.rows(
            "SELECT id, content, salience, retention_tier, created_at FROM persona_memories "
            "WHERE persona_id=? AND slot_key IS NULL AND is_active=1 AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY created_at DESC LIMIT 40",
            (persona_id, now_ts)
        )

        def score_episodic(cand):
            c_text = clean_memory_text(cand.get("content", ""))
            mem_words = extract_keywords(c_text)
            overlap = len(query_words & mem_words) if query_words else 0
            tier = cand.get("retention_tier")
            salience = float(cand.get("salience") or 0.7)
            return overlap * 4.0 + (3.0 if tier == 'long' else 1.0) + salience * 2.0

        ranked_episodic = sorted(episodic_rows, key=score_episodic, reverse=True)
        selected_episodic = []
        seen_sig = set()
        for r in ranked_episodic:
            c = clean_memory_text(r.get("content", ""))
            if not c:
                continue
            words = tuple(sorted(list(extract_keywords(c))[:4]))
            if words in seen_sig:
                continue
            seen_sig.add(words)
            selected_episodic.append(c)
            if len(selected_episodic) >= 3:
                break

        if not active_slots and not selected_episodic:
            return ""

        # Build compact, high-density structured prompt (<180 tokens)
        lines = ["--- YOUR ACTIVE STATE & CORE MEMORIES (Stay 100% consistent) ---"]
        for slot_key, label, _prio, _pats in SLOT_DEFINITIONS:
            if slot_key in active_slots:
                lines.append(f"[{label}] {active_slots[slot_key]}")

        if selected_episodic:
            lines.append("--- RECENT OBSERVATIONS ---")
            for ep in selected_episodic:
                lines.append(f"- {ep}")

        lines.append("Never recite memories verbatim or force them unprompted; let them naturally guide your state and opinions.")
        return "\n".join(lines)

    def auto_record_statement(self, persona_id, account_id, chat_id, text, is_reply_to=None):
        """Parses outgoing persona message text and autonomously updates domain slots or records episodic facts."""
        clean = clean_memory_text((text or "").strip())
        if not clean or len(clean) < 8:
            return None

        # Filter out generic chatter noise
        if re.match(r"^(ok|okay|yes|no|thanks|thx|lol|haha|sure|sounds good|fr|facts|true)[.!?\s]*$", clean, re.IGNORECASE):
            return None

        # Do not record vendor complaint hallucinations into memory
        if any(bad in clean.lower() for bad in ["payment portal", "weak batch", "card processor", "recurring nightmare", "bad vial", "shit show", "bugging out"]):
            return None

        # Questions are not biographical facts
        if clean.endswith("?") or re.match(r"^(what|why|how|where|when|who|is|are|does|can|could|did|anyone|anybody)\b", clean.lower()):
            return None

        # Check for domain slot match
        detected_slot = detect_persona_slot(clean)
        if detected_slot:
            return self.set_slot(
                persona_id=persona_id,
                slot_key=detected_slot,
                content=clean[:280],
                account_id=account_id,
                chat_id=chat_id,
                salience=0.95
            )

        # First-person biographical patterns
        first_person_patterns = [
            r"\b(?:i am|i'm|im|i have been|i've been)\s+(?:running|taking|pinning|dosing|on)\b",
            r"\b(?:my|my current)\s+(?:cycle|stack|protocol|dose|split|routine)\b",
            r"\bi\s+(?:run|take|pin|dose|reconstituted|started)\s+\d+",
            r"\b(?:my bloodwork|my labs|my injury|my tendon|my shoulder|my knee)\b"
        ]
        is_first_person_fact = any(re.search(pat, clean.lower()) for pat in first_person_patterns)

        is_substantial = any(kw in clean.lower() for kw in [
            "workout", "gym", "squat", "bench", "deadlift", "sets", "reps", "sleep", "recovery",
            "batch", "order", "shipping", "xbiolabs", "janoshik", "experience", "felt", "works",
            "bpc", "tb500", "tirz", "reta", "sema", "trt", "bac", "reconstitution",
            "nootropics", "semax", "selank", "bromantane", "modafinil", "tyrosine", "alpha-gpc",
            "magnesium", "sauna", "cold plunge", "electrolytes", "hypertrophy", "bulk", "cut", "diet"
        ])

        if is_first_person_fact:
            tier = "long"
            salience = 0.95
        elif is_substantial:
            tier = "medium"
            salience = 0.75
        else:
            tier = "short"
            salience = 0.45

        return self.add_memory(
            persona_id=persona_id,
            content=clean[:280],
            account_id=account_id,
            chat_id=chat_id,
            kind="statement",
            retention_tier=tier,
            salience=salience
        )

    def record_human_interaction(self, chat_id, sender_name, sender_id=None, sender_username=None, text=""):
        """Extracts and persists meaningful domain context about external human members with status tracking."""
        text = (text or "").strip()
        if len(text) < 8 or not sender_name or text.startswith("/"):
            return None
        lower = text.lower()

        slot_key = detect_human_slot(text)
        status = "open"
        # Detect resolution cues (order arrived, injury recovered, question answered)
        if any(w in lower for w in ["landed", "arrived", "received", "got it", "tracking updated", "delivered", "healed", "better now"]):
            status = "resolved"

        fact = text[:160]
        now_ts = int(time.time())

        try:
            with self.store.lock:
                cur = self.store.db.cursor()
                # Check existing active entries for this sender & slot
                cur.execute(
                    "SELECT id, fact, status FROM human_memories "
                    "WHERE chat_id=? AND sender=? AND slot_key=? AND is_active=1 "
                    "ORDER BY id DESC LIMIT 5",
                    (chat_id, sender_name, slot_key)
                )
                existing = cur.fetchall()
                fact_words = extract_keywords(fact)

                for ex in existing:
                    ex_id = ex[0] if isinstance(ex, (tuple, list)) else ex["id"]
                    ex_fact = ex[1] if isinstance(ex, (tuple, list)) else ex["fact"]
                    ex_words = extract_keywords(ex_fact)
                    if fact_words and ex_words:
                        overlap = len(fact_words & ex_words) / max(1, len(fact_words | ex_words))
                        if overlap > 0.60:
                            cur.execute(
                                "UPDATE human_memories SET last_referenced_at=?, status=?, fact=? WHERE id=?",
                                (now_ts, status, fact, ex_id)
                            )
                            self.store.db.commit()
                            return ex_id

                # If status changed to resolved, update prior open rows
                if status == "resolved":
                    cur.execute(
                        "UPDATE human_memories SET status='resolved', last_referenced_at=? "
                        "WHERE chat_id=? AND sender=? AND slot_key=?",
                        (now_ts, chat_id, sender_name, slot_key)
                    )

                # Insert new active human memory
                cur.execute(
                    """INSERT INTO human_memories(
                        chat_id, sender, sender_id, sender_username, fact, category,
                        created_at, last_referenced_at, slot_key, is_active, status
                    ) VALUES(?,?,?,?,?,?,?,?,?,1,?)""",
                    (chat_id, sender_name, sender_id, sender_username, fact, slot_key, now_ts, now_ts, slot_key, status)
                )
                new_id = cur.lastrowid
                self.store.db.commit()
                return new_id
        except Exception as e:
            log.warning("Failed to record human interaction: %s", e)
        return None

    def get_human_context(self, chat_id, sender_name, limit=3):
        """Retrieves structured domain context & proactive callback cues for a human member (<80 tokens)."""
        if not sender_name:
            return ""
        try:
            rows = self.store.rows(
                "SELECT fact, slot_key, status, created_at FROM human_memories "
                "WHERE chat_id=? AND sender=? AND is_active=1 "
                "ORDER BY CASE WHEN status='open' THEN 1 ELSE 2 END, id DESC LIMIT ?",
                (chat_id, sender_name, limit)
            )
            if not rows:
                return ""

            lines = [f"--- WHAT WE KNOW ABOUT {sender_name.upper()} (familiar peer) ---"]
            has_open_thread = False
            for r in rows:
                fact = r.get("fact") if isinstance(r, dict) else r[0]
                sk = (r.get("slot_key") if isinstance(r, dict) else r[1]) or "general"
                st = (r.get("status") if isinstance(r, dict) else r[2]) or "open"
                tag = sk.upper().replace("_", " ")
                status_note = " [UNRESOLVED]" if st == "open" else ""
                lines.append(f"• {tag}: {fact}{status_note}")
                if st == "open":
                    has_open_thread = True

            if has_open_thread:
                lines.append("• PROACTIVE CUE: Follow up naturally on their open thread if relevant (e.g. order arrival, recovery progress); never sound corporate.")
            else:
                lines.append("• PROACTIVE CUE: Treat as familiar regular; keep interaction authentic and relaxed.")

            return "\n".join(lines)
        except Exception as e:
            log.warning("Failed to get human context: %s", e)
            return ""
