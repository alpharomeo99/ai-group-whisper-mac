"""Episodic and Semantic Memory Engine for AI Personas.

Manages multi-tier persona memory retention:
- 'long': Permanent anchors, personal biographical facts, chronic health/rehab history.
- 'medium': 7-day thread storylines, active orders, weekly protocols, commitments.
- 'short': 24-48h ephemeral context, short-term banter, transient reactions.

Includes an autonomous Memory Pruning Agent that regularly purges expired,
useless, and overextended memories while preserving key factual anchors.
"""

import time
import json
import logging
import re

log = logging.getLogger("memory")

DEFAULT_EXPIRIES = {
    "short": 48 * 3600,       # 48 hours
    "medium": 7 * 24 * 3600,  # 7 days
    "long": None              # Permanent anchor (no automatic expiry)
}


class MemoryEngine:
    def __init__(self, store, ai_client=None):
        self.store = store
        self.ai = ai_client
        self.ensure_tables()

    def ensure_tables(self):
        """Creates persona_memories and indices if they do not exist."""
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
            self.store.db.commit()

    def add_memory(self, persona_id, content, account_id=None, chat_id=None, kind="statement",
                   retention_tier="medium", salience=0.7, expires_in_seconds=None):
        """Adds a new memory node for a persona."""
        now_ts = int(time.time())
        tier = retention_tier if retention_tier in ("short", "medium", "long") else "medium"
        
        if expires_in_seconds is not None:
            expires_at = now_ts + int(expires_in_seconds) if expires_in_seconds > 0 else None
        else:
            default_dur = DEFAULT_EXPIRIES.get(tier)
            expires_at = now_ts + default_dur if default_dur else None

        clean_content = str(content or "").strip()
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
        """Autonomous Memory Pruning Agent.
        
        1. Identifies expired records (expires_at <= now).
        2. Deletes expired short-term transient memories and low-salience (<0.35) items.
        3. For medium-term items: if high salience (>=0.85) and accessed >= 2 times,
           promotes to a permanent anchor; otherwise prunes.
        4. Removes duplicate statements within the same persona.
        """
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
                    # Prune ephemeral chatter
                    cur.execute("DELETE FROM persona_memories WHERE id=?", (mid,))
                    pruned += 1
                elif tier == "medium":
                    if salience >= 0.85 and accesses >= 2:
                        # Meaningful ongoing storyline promoted to permanent anchor
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

    def get_prompt_memory_context(self, persona_id, chat_id=None, limit=7):
        """Compiles active memories into an organic, bulleted memory context for AI injection.
        Updates access_count and last_accessed_at for retrieved nodes.
        """
        if not persona_id:
            return ""

        now_ts = int(time.time())
        # Retrieve valid, non-expired memories: prioritize anchors, then high salience
        sql = """
            SELECT id, kind, content, retention_tier, salience
            FROM persona_memories
            WHERE persona_id=? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY CASE retention_tier WHEN 'long' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, salience DESC, created_at DESC
            LIMIT ?
        """
        rows = self.store.rows(sql, (persona_id, now_ts, limit))
        if not rows:
            return ""

        # Update access stats
        ids = [r["id"] for r in rows]
        with self.store.lock:
            cur = self.store.db.cursor()
            placeholders = ",".join("?" * len(ids))
            cur.execute(
                f"UPDATE persona_memories SET access_count=access_count+1, last_accessed_at=? WHERE id IN ({placeholders})",
                [now_ts] + ids
            )
            self.store.db.commit()

        lines = [
            "--- YOUR ESTABLISHED MEMORIES & PAST STATEMENTS (STAY ACCURATELY CONSISTENT) ---"
        ]
        for r in rows:
            tier_label = (
                "[Permanent Fact]" if r["retention_tier"] == "long"
                else "[Recent Storyline]" if r["retention_tier"] == "medium"
                else "[Recent Note]"
            )
            lines.append(f"{tier_label} {r['content']}")

        lines.append("Never contradict any of your past statements or established facts above.")
        return "\n".join(lines)

    def auto_record_statement(self, persona_id, account_id, chat_id, text, is_reply_to=None):
        """Parses outgoing message text and autonomously stores key statements as episodic memories."""
        clean = (text or "").strip()
        if not clean or len(clean) < 10:
            return None

        # Filter out purely generic noise
        if re.match(r"^(ok|okay|yes|no|thanks|thx|lol|haha|sure|sounds good)[.!?\s]*$", clean, re.IGNORECASE):
            return None

        # Determine retention tier and salience based on substance
        is_substantial = any(kw in clean.lower() for kw in [
            "pin", "pinned", "recon", "reconstituted", "bpc", "tb500", "tirz", "reta", "sema",
            "batch", "order", "shipping", "xbiolabs", "janoshik", "bloodwork", "dose", "mg", "mcg",
            "cycle", "trt", "test", "taking", "started", "running", "shoulder", "knee", "gut"
        ])

        if is_substantial:
            tier = "medium"  # retain for 7 days
            salience = 0.85
            summary = f"Shared in chat: {clean[:240]}"
        else:
            tier = "short"   # ephemeral banter, auto-expire in 48 hours
            salience = 0.5
            summary = f"Recent comment: {clean[:160]}"

        return self.add_memory(
            persona_id=persona_id,
            content=summary,
            account_id=account_id,
            chat_id=chat_id,
            kind="statement",
            retention_tier=tier,
            salience=salience
        )
