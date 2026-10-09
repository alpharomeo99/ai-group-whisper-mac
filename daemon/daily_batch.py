"""Daily Batch Dialogue Engine.

Pre-generates organic multi-persona daily conversational arcs for groups once a day.
Dormant accounts connect on-demand at scheduled timestamps to type and send their message,
then disconnect immediately.

Live AI Orchestration triggers ONLY when an external human speaks in the group.
Bot-to-bot interactions are strictly governed by this daily batch schedule.
"""
import asyncio
import json
import logging
import math
import random
import re
import time
from datetime import datetime

log = logging.getLogger("whisper.batch")


class DailyBatchEngine:
    def __init__(self, store, ai_module):
        self.store = store
        self.ai = ai_module

    def today_str(self):
        return datetime.now().strftime("%Y-%m-%d")

    async def generate_batch_for_group(self, chat_id, count=6, topic_override=None):
        """Generates today's batch schedule for a watched group."""
        chat_id = int(chat_id)
        g_rows = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
        if not g_rows:
            raise ValueError(f"Group {chat_id} not found")
        group = g_rows[0]

        # 1. Fetch assigned personas with accounts
        candidates = self.store.rows(
            "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, gp.account_id, a.name as account_name "
            "FROM group_personas gp "
            "JOIN personas p ON p.id=gp.persona_id "
            "LEFT JOIN accounts a ON a.id=gp.account_id "
            "WHERE gp.chat_id=? AND gp.account_id IS NOT NULL",
            (chat_id,)
        )

        if len(candidates) < 2:
            # Fallback: check accounts in group with personas assigned
            acc_rows = self.store.rows(
                "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name "
                "FROM group_accounts ga "
                "JOIN accounts a ON a.id=ga.account_id "
                "JOIN personas p ON p.id=a.persona_id "
                "WHERE ga.chat_id=?",
                (chat_id,)
            )
            existing_pids = {c["persona_id"] for c in candidates}
            for ar in acc_rows:
                if ar["persona_id"] not in existing_pids:
                    candidates.append(ar)
                    existing_pids.add(ar["persona_id"])

        if not candidates:
            raise ValueError("No assigned personas with accounts found for this group. Assign personas first.")

        # 2. Get recent chat context for continuity
        recent_msgs = self.store.recent(chat_id, limit=12)
        transcript_lines = []
        for m in recent_msgs:
            if m.get("text"):
                transcript_lines.append(f"[{m.get('sender', 'Member')}]: {m.get('text')}")
        recent_transcript = "\n".join(transcript_lines) or "(Group chat is fresh / quiet)"

        # 3. Format persona summaries for the AI prompt
        persona_summaries = []
        for c in candidates:
            dt = {}
            try:
                dt = json.loads(c.get("details") or "{}") if isinstance(c.get("details"), str) else (c.get("details") or {})
            except Exception:
                dt = {}
            role = dt.get("role_in_group") or "Experienced regular"
            slang = dt.get("slang_and_vocabulary") or "Authentic telegram slang"
            persona_summaries.append(
                f"- ID: {c['persona_id']}, AccountID: {c['account_id']}, Name: {c['persona_name']}\n"
                f"  Role: {role}\n"
                f"  Style/Slang: {slang}\n"
                f"  Brief: {c.get('prompt', '')[:180]}..."
            )
        persona_str = "\n\n".join(persona_summaries)

        group_title = group.get("title") or f"Group {chat_id}"
        group_about = group.get("about") or "Telegram community discussion"
        group_domain = group.get("domain_knowledge") or "Biohacking, research peptides, protocols, sourcing, and health optimization."
        group_rules = group.get("rules") or "Be authentic, direct, and knowledgeable."

        topic_prompt = topic_override or "Natural day of community discussion, protocol updates, reconstitution tips, batch experiences, and shop talk."

        system_prompt = (
            "You are a master orchestrator of realistic human conversation in Telegram groups.\n"
            "You write authentic, multi-turn dialogue between real humans discussing in a specialist community.\n"
            "CRITICAL RULES:\n"
            "1. Absolutely NO generic corporate AI speak, no robotic disclaimers ('as always consult a doctor'), no cheesy pleasantries.\n"
            "2. Talk like real veteran community members: authentic slang, personal experience, direct answers, concise messaging, realistic casing and typos.\n"
            "3. Alternate between the provided personas naturally like an organic discussion thread.\n"
            "4. Return ONLY a valid JSON array of message objects matching this exact format:\n"
            "[\n"
            "  {\n"
            "    \"persona_id\": <int>,\n"
            "    \"account_id\": <int>,\n"
            "    \"sender_name\": \"<string>\",\n"
            "    \"offset_minutes\": <int>,\n"
            "    \"text\": \"<exact telegram message text>\"\n"
            "  }\n"
            "]"
        )

        user_prompt = (
            f"COMMUNITY GROUP: {group_title}\n"
            f"ABOUT: {group_about}\n"
            f"DOMAIN KNOWLEDGE & TOPICS:\n{group_domain}\n"
            f"RULES: {group_rules}\n\n"
            f"TODAY'S CONVERSATIONAL TOPIC FOCUS: {topic_prompt}\n\n"
            f"AVAILABLE PERSONAS IN THIS GROUP:\n{persona_str}\n\n"
            f"RECENT MESSAGES IN GROUP:\n{recent_transcript}\n\n"
            f"TASK:\n"
            f"Create a natural {count}-message conversational thread for today between these personas.\n"
            f"Spread 'offset_minutes' across realistic intervals (e.g. message 1 at offset 15, message 2 at 45, message 3 at 110, message 4 at 240, etc.).\n"
            f"Output strictly the JSON array, nothing else."
        )

        settings = {k: self.store.get(k) for k in ("fal_key", "ai_model")}
        items = []

        try:
            raw_ai, meta = await self.ai.chat_with_meta(settings, system_prompt, user_prompt, temperature=0.88)
            match = re.search(r"\[\s*\{.*\}\s*\]", raw_ai, re.DOTALL)
            if match:
                items = json.loads(match.group(0))
            now_ts = int(time.time())
            p_tok = meta.get("prompt_tokens", 0)
            c_tok = meta.get("completion_tokens", 0)
            tot_tok = p_tok + c_tok
            cost_est = (tot_tok / 1000.0) * 0.0003
            self.store.q("""
                INSERT INTO usage_logs(timestamp, event_type, chat_id, tokens_prompt, tokens_completion,
                                       tokens_total, latency_ms, model, cost_est)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (now_ts, "ai_batch", chat_id, p_tok, c_tok, tot_tok, meta.get("latency_ms", 0),
                  meta.get("model", ""), cost_est))
        except Exception as e:
            log.warning("AI batch generation failed (%s), using domain fallback: %s", type(e).__name__, e)

        # Fallback generator if AI call wasn't available or JSON parse failed
        if not items:
            items = self._fallback_dialogue(candidates, group_domain, count)

        # 4. Schedule the items throughout today
        today = self.today_str()
        cur_batch = self.store.rows("SELECT id FROM daily_batches WHERE chat_id=? AND date_str=?", (chat_id, today))
        if cur_batch:
            batch_id = cur_batch[0]["id"]
        else:
            cur = self.store.q(
                "INSERT INTO daily_batches(date_str, chat_id, topic, created_at) VALUES(?,?,?,?)",
                (today, chat_id, topic_prompt, int(time.time()))
            )
            batch_id = cur.lastrowid

        # Calculate base time: start 5 minutes from now, or spread through the day
        now_ts = int(time.time())
        created_items = []

        # Jitter and time distribution
        accumulated_offset = 5  # start 5 mins from now
        for idx, item in enumerate(items):
            pid = item.get("persona_id")
            aid = item.get("account_id")
            # Validate persona and account ID
            cand_match = [c for c in candidates if c["persona_id"] == pid and c["account_id"] == aid]
            if not cand_match:
                # Fallback to cycling candidates
                cand_match = [candidates[idx % len(candidates)]]
                pid = cand_match[0]["persona_id"]
                aid = cand_match[0]["account_id"]

            sender_name = cand_match[0]["persona_name"]
            text = (item.get("text") or "").strip()
            if not text:
                continue

            # Offset spacing: if provided in item, use with jitter, otherwise distribute naturally
            suggested_offset = item.get("offset_minutes")
            if suggested_offset and suggested_offset > accumulated_offset:
                accumulated_offset = suggested_offset
            else:
                # Random interval between 25 and 110 minutes + Poisson jitter
                step = random.randint(25, 85) + int(random.expovariate(0.08))
                accumulated_offset += min(step, 140)

            jitter_sec = random.randint(-180, 240)
            scheduled_ts = now_ts + (accumulated_offset * 60) + jitter_sec

            cur_item = self.store.q(
                "INSERT INTO daily_batch_items(batch_id, chat_id, account_id, persona_id, sender_name, text, scheduled_ts, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,'pending',?)",
                (batch_id, chat_id, aid, pid, sender_name, text, scheduled_ts, now_ts)
            )
            item_id = cur_item.lastrowid

            # Enqueue into work queue for automated wake-and-send
            self.store.enqueue(
                "batch_scheduled_send",
                chat_id,
                {"batch_item_id": item_id, "account_id": aid, "persona_id": pid, "text": text},
                dedupe_key=f"batch_send:{item_id}",
                delay=max(0, scheduled_ts - now_ts)
            )

            created_items.append({
                "id": item_id,
                "persona_id": pid,
                "account_id": aid,
                "sender_name": sender_name,
                "scheduled_ts": scheduled_ts,
                "text": text,
                "status": "pending"
            })

        return {
            "batch_id": batch_id,
            "chat_id": chat_id,
            "count": len(created_items),
            "date": today,
            "items": created_items
        }

    def _fallback_dialogue(self, candidates, domain, count):
        """Authentic peptide/xbiolabs domain fallback template generator."""
        p1 = candidates[0]
        p2 = candidates[1] if len(candidates) > 1 else candidates[0]
        p3 = candidates[2] if len(candidates) > 2 else candidates[0]

        templates = [
            (p1, 15, "anyone pinned the latest xbiolabs BPC-157 batch? looking to reconstitute 5mg for my shoulder"),
            (p2, 48, "yeah mixed with 2ml bacteriostatic water last week. smooth injection, zero post-injection pip"),
            (p3, 115, "make sure to let the bac water drip down the inside glass slowly, don't spray straight onto the puck"),
            (p1, 185, "good shout. keeping it chilled in the fridge. did you notice any systemic benefits or mostly localized?"),
            (p2, 290, "mostly localized on tendon inflammation by day 5, also gut felt notably calmer with morning subQ"),
            (p3, 380, "solid. coa checked out at 99.3% purity on janoshik anyway, shipping was fast too")
        ]
        items = []
        for cand, offset, text in templates[:count]:
            items.append({
                "persona_id": cand["persona_id"],
                "account_id": cand["account_id"],
                "sender_name": cand["persona_name"],
                "offset_minutes": offset,
                "text": text
            })
        return items

    def get_status(self):
        """Returns today's batch schedule, execution metrics, and scout configuration."""
        today = self.today_str()
        batches = self.store.rows("SELECT * FROM daily_batches WHERE date_str=? ORDER BY id DESC", (today,))
        all_items = self.store.rows(
            "SELECT dbi.*, g.title as group_title, p.name as persona_name, a.name as account_name "
            "FROM daily_batch_items dbi "
            "LEFT JOIN groups g ON g.chat_id=dbi.chat_id "
            "LEFT JOIN personas p ON p.id=dbi.persona_id "
            "LEFT JOIN accounts a ON a.id=dbi.account_id "
            "WHERE dbi.created_at >= ? "
            "ORDER BY dbi.scheduled_ts ASC",
            (int(time.time()) - 86400,)
        )

        total = len(all_items)
        pending = sum(1 for x in all_items if x["status"] == "pending")
        sent = sum(1 for x in all_items if x["status"] == "sent")
        postponed = sum(1 for x in all_items if x["status"] == "postponed")
        cancelled = sum(1 for x in all_items if x["status"] == "cancelled")

        # Find next scheduled execution
        now_ts = int(time.time())
        next_items = [x for x in all_items if x["status"] == "pending" and x["scheduled_ts"] >= now_ts]
        next_ts = next_items[0]["scheduled_ts"] if next_items else None

        # Check scout account
        scout_aid = self.store.get("scout_account_id")
        scout_info = None
        if scout_aid:
            acc = self.store.rows("SELECT id, name, phone, username, active FROM accounts WHERE id=?", (scout_aid,))
            if acc:
                scout_info = acc[0]
        if not scout_info:
            acc = self.store.rows("SELECT id, name, phone, username, active FROM accounts WHERE active=1 ORDER BY id ASC LIMIT 1")
            if acc:
                scout_info = acc[0]

        return {
            "date": today,
            "batches_count": len(batches),
            "stats": {
                "total": total,
                "pending": pending,
                "sent": sent,
                "postponed": postponed,
                "cancelled": cancelled
            },
            "next_scheduled_ts": next_ts,
            "items": all_items,
            "scout_account": scout_info,
            "live_ai_mode": "Humans Only (Bot replies strictly filtered)"
        }

    def clear_today(self, chat_id=None):
        """Cancels all pending scheduled batch messages for today."""
        today = self.today_str()
        if chat_id:
            items = self.store.rows(
                "SELECT id FROM daily_batch_items WHERE chat_id=? AND status='pending'", (int(chat_id),)
            )
            for it in items:
                self.store.q("UPDATE daily_batch_items SET status='cancelled' WHERE id=?", (it["id"],))
                self.store.q("UPDATE queue SET status='cancelled' WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
        else:
            items = self.store.rows("SELECT id FROM daily_batch_items WHERE status='pending'")
            for it in items:
                self.store.q("UPDATE daily_batch_items SET status='cancelled' WHERE id=?", (it["id"],))
                self.store.q("UPDATE queue SET status='cancelled' WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
        return len(items)

    def trigger_next_now(self):
        """Forces the next pending scheduled message to send immediately."""
        now_ts = int(time.time())
        pending = self.store.rows(
            "SELECT id, chat_id FROM daily_batch_items WHERE status='pending' ORDER BY scheduled_ts ASC LIMIT 1"
        )
        if not pending:
            return None
        it_id = pending[0]["id"]
        self.store.q("UPDATE daily_batch_items SET scheduled_ts=? WHERE id=?", (now_ts, it_id))
        self.store.q("UPDATE queue SET not_before=? WHERE dedupe_key=?", (now_ts, f"batch_send:{it_id}"))
        return it_id
