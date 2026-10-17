"""Episodic and Semantic Memory Engine for AI Personas.

Manages multi-tier persona memory retention:
- 'long': Permanent anchors, personal biographical facts, chronic health/rehab history, cycle/protocol specifics (NEVER expires).
- 'medium': 30-day extended storylines, active orders, monthly protocols, ongoing discussions.
- 'short': 48h ephemeral context, short-term banter, transient reactions.

Includes dynamic topic relevance scoring, prefix-clean storage, autonomous pruning,
and prompt injection that guarantees persona memory consistency.
"""

import time
import json
import logging
import re

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

def extract_keywords(text):
    """Extracts non-stopword alphanumeric tokens (3+ chars) in lowercase."""
    if not text:
        return set()
    return {w for w in re.findall(r'[a-zA-Z0-9_-]{3,}', text.lower()) if w not in STOP_WORDS}

def clean_memory_text(text):
    """Strips repetitive nested label prefixes from memory content."""
    if not text:
        return ""
    pattern = re.compile(
        r'^(?:Core Protocol/Fact|Established Fact|Shared in chat|Recent comment|Personal Fact|Key Fact|Core Established Facts)[:\s\-]+',
        flags=re.IGNORECASE
    )
    prev = None
    while prev != text:
        prev = text
        text = pattern.sub('', text).strip()
    return text


class MemoryEngine:
    def __init__(self, store, ai_client=None):
        self.store = store
        self.ai = ai_client
        self.ensure_tables()

    def ensure_tables(self):
        """Creates persona_memories, indices, and sanitizes existing stored records."""
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
                last_accessed_at INTEGER
            )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_pid_tier ON persona_memories(persona_id, retention_tier, expires_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_chat ON persona_memories(chat_id, created_at)")

            # Upgrade all protocol, cycle, health, or biographical memories to permanent anchors (long)
            cur.execute("""
                UPDATE persona_memories
                SET retention_tier='long', expires_at=NULL, salience=0.95
                WHERE retention_tier != 'long' AND (
                    lower(content) LIKE '%running%' OR lower(content) LIKE '%cycle%' OR
                    lower(content) LIKE '%bpc%' OR lower(content) LIKE '%tb500%' OR
                    lower(content) LIKE '%tirz%' OR lower(content) LIKE '%reta%' OR
                    lower(content) LIKE '%sema%' OR lower(content) LIKE '%trt%' OR
                    lower(content) LIKE '%test%' OR lower(content) LIKE '%dose%' OR
                    lower(content) LIKE '%pin%' OR lower(content) LIKE '%mg%' OR
                    lower(content) LIKE '%mcg%' OR lower(content) LIKE '%split%' OR
                    lower(content) LIKE '%injury%' OR lower(content) LIKE '%protocol%' OR
                    lower(content) LIKE '%stack%'
                )
            """)

            # Clean up nested prefix artifacts from stored memory contents
            cur.execute("SELECT id, content FROM persona_memories WHERE content LIKE '%:%'")
            rows = cur.fetchall()
            for r in rows:
                mid = r[0] if isinstance(r, (tuple, list)) else r["id"]
                orig_c = r[1] if isinstance(r, (tuple, list)) else r["content"]
                cleaned = clean_memory_text(orig_c)
                if cleaned and cleaned != orig_c:
                    cur.execute("UPDATE persona_memories SET content=? WHERE id=?", (cleaned, mid))

            self.store.db.commit()

    def add_memory(self, persona_id, content, account_id=None, chat_id=None, kind="statement",
                   retention_tier="medium", salience=0.7, expires_in_seconds=None):
        """Adds a new memory node for a persona."""
        now_ts = int(time.time())
        tier = retention_tier if retention_tier in ("short", "medium", "long") else "medium"

        if tier == "long":
            expires_at = None
        elif expires_in_seconds is not None:
            expires_at = now_ts + int(expires_in_seconds) if expires_in_seconds > 0 else None
        else:
            default_dur = DEFAULT_EXPIRIES.get(tier)
            expires_at = now_ts + default_dur if default_dur else None

        clean_content = clean_memory_text(str(content or "").strip())
        if not clean_content:
            return None

        # Clamp salience between 0.1 and 1.0
        salience_val = max(0.1, min(float(salience or 0.7), 1.0))

        with self.store.lock:
            cur = self.store.db.cursor()
            cur.execute("""
                INSERT INTO persona_memories(
                    persona_id, account_id, chat_id, kind, content, context,
                    salience, retention_tier, created_at, expires_at, access_count, last_accessed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,0,?)
            """, (persona_id, account_id, chat_id, kind, clean_content, "",
                  salience_val, tier, now_ts, expires_at, now_ts))
            self.store.db.commit()
            return cur.lastrowid

    def get_memories(self, persona_id, retention_tier=None, limit=150):
        """Retrieves memories for a persona, optionally filtered by tier."""
        args = [persona_id]
        sql = "SELECT * FROM persona_memories WHERE persona_id=?"
        if retention_tier:
            sql += " AND retention_tier=?"
            args.append(retention_tier)
        sql += " ORDER BY CASE retention_tier WHEN 'long' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC LIMIT ?"
        args.append(limit)
        return self.store.rows(sql, tuple(args))

    def get_memory_tree(self, persona_id):
        """Builds a structured hierarchical tree of memories for visual UI inspection."""
        persona = self.store.rows("SELECT id, name, color FROM personas WHERE id=?", (persona_id,))
        pname = persona[0]["name"] if persona else f"Persona #{persona_id}"
        pcolor = persona[0]["color"] if persona else "#2fc4b2"

        all_memories = self.get_memories(persona_id, limit=200)

        anchors = []
        medium = []
        short = []

        now_ts = int(time.time())
        for m in all_memories:
            item = dict(m)
            item["content"] = clean_memory_text(item.get("content", ""))
            # Add relative status
            if item.get("expires_at"):
                remaining = item["expires_at"] - now_ts
                item["is_expired"] = remaining <= 0
                item["remaining_hours"] = max(0, round(remaining / 3600.0, 1))
            else:
                item["is_expired"] = False
                item["remaining_hours"] = None

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
            "stats": {
                "total": len(all_memories),
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
        """Autonomous Memory Pruning Agent."""
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

            # Prune duplicate contents for same persona
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

    def get_prompt_memory_context(self, persona_id, chat_id=None, limit=10, query_text=None):
        """Retrieves and ranks persona memories by topic relevance, tier anchor status, and salience.
        Compiles them into clean, bulleted facts for seamless AI prompt injection.
        """
        if not persona_id:
            return ""

        now_ts = int(time.time())
        # Fetch candidate active memories (permanent anchors and non-expired records)
        sql = """
            SELECT id, kind, content, retention_tier, salience, created_at
            FROM persona_memories
            WHERE persona_id=? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY CASE retention_tier WHEN 'long' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC
            LIMIT 40
        """
        candidates = self.store.rows(sql, (persona_id, now_ts))
        if not candidates:
            return ""

        query_keywords = extract_keywords(query_text) if query_text else set()

        def score_cand(cand):
            c_text = clean_memory_text(cand.get("content", ""))
            mem_words = extract_keywords(c_text)
            overlap = len(query_keywords.intersection(mem_words)) if query_keywords else 0
            tier = cand.get("retention_tier")
            tier_score = 4.0 if tier == "long" else (2.0 if tier == "medium" else 0.8)
            salience = float(cand.get("salience") or 0.7)
            return (overlap * 5.0) + tier_score + (salience * 2.0)

        # Rank candidates by relevance
        ranked = sorted(candidates, key=score_cand, reverse=True)
        selected = ranked[:limit]

        # Update access stats
        ids = [r["id"] for r in selected]
        with self.store.lock:
            cur = self.store.db.cursor()
            placeholders = ",".join("?" * len(ids))
            cur.execute(
                f"UPDATE persona_memories SET access_count=access_count+1, last_accessed_at=? WHERE id IN ({placeholders})",
                [now_ts] + ids
            )
            self.store.db.commit()

        lines = [
            "--- YOUR ESTABLISHED MEMORIES & BACKGROUND FACTS (STAY 100% CONSISTENT) ---"
        ]
        for r in selected:
            clean_c = clean_memory_text(r["content"])
            if not clean_c:
                continue
            tier_label = (
                "[Permanent Fact]" if r["retention_tier"] == "long"
                else "[Recent Context]"
            )
            lines.append(f"{tier_label} {clean_c}")

        lines.append("ACCURACY & MEMORY CONSISTENCY: These facts represent your established history and past statements. Never contradict them or invent conflicting claims.")
        return "\n".join(lines)

    def auto_record_statement(self, persona_id, account_id, chat_id, text, is_reply_to=None):
        """Parses outgoing message text and autonomously stores key statements as episodic memories."""
        clean = clean_memory_text((text or "").strip())
        if not clean or len(clean) < 8:
            return None

        # Filter out purely generic noise
        if re.match(r"^(ok|okay|yes|no|thanks|thx|lol|haha|sure|sounds good)[.!?\s]*$", clean, re.IGNORECASE):
            return None

        # Do not record vendor complaint / checkout glitch hallucinations into episodic memory
        if any(bad in clean.lower() for bad in ["payment portal", "weak batch", "card processor", "recurring nightmare", "bad vial", "shit show", "bugging out"]):
            return None

        # Questions and inquiries are not biographical statements of fact
        if clean.endswith("?") or re.match(r"^(what|why|how|where|when|who|is|are|does|can|could|did|anyone|anybody)\b", clean.lower()):
            return None

        # Identify explicit first-person protocol, health, or biographical commitments for long-term memory
        first_person_patterns = [
            r'\b(?:i am|i\'m|im|i have been|i\'ve been)\s+(?:running|taking|pinning|dosing|on)\b',
            r'\b(?:my|my current)\s+(?:cycle|stack|protocol|dose|split|routine)\b',
            r'\bi\s+(?:run|take|pin|dose|reconstituted|started)\s+\d+',
            r'\b(?:my bloodwork|my labs|my injury|my tendon|my shoulder|my knee)\b'
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
