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

    def get_group_candidates(self, chat_id):
        """Unified, robust candidate resolution for a group across group_personas, group_accounts, and account fallbacks."""
        chat_id = int(chat_id)
        candidates = []
        seen = set()

        # 1. group_personas with explicit account_id
        gp_rows = self.store.rows(
            "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, gp.account_id, a.name as account_name, a.phone as account_phone "
            "FROM group_personas gp "
            "JOIN personas p ON p.id=gp.persona_id "
            "JOIN accounts a ON a.id=gp.account_id "
            "WHERE gp.chat_id=? AND a.active=1",
            (chat_id,)
        )
        for r in gp_rows:
            key = (r["persona_id"], r["account_id"])
            if key not in seen:
                candidates.append(r)
                seen.add(key)

        # 2. group_accounts where account has persona_id assigned
        ga_rows = self.store.rows(
            "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name, a.phone as account_phone "
            "FROM group_accounts ga "
            "JOIN accounts a ON a.id=ga.account_id "
            "JOIN personas p ON p.id=a.persona_id "
            "WHERE ga.chat_id=? AND a.active=1",
            (chat_id,)
        )
        for r in ga_rows:
            key = (r["persona_id"], r["account_id"])
            if key not in seen:
                candidates.append(r)
                seen.add(key)

        # 3. Fallback: group owner account's persona
        g_rows = self.store.rows("SELECT account_id FROM groups WHERE chat_id=?", (chat_id,))
        if g_rows and g_rows[0].get("account_id"):
            g_aid = g_rows[0]["account_id"]
            owner_rows = self.store.rows(
                "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name, a.phone as account_phone "
                "FROM accounts a "
                "JOIN personas p ON p.id=a.persona_id "
                "WHERE a.id=? AND a.active=1",
                (g_aid,)
            )
            for r in owner_rows:
                key = (r["persona_id"], r["account_id"])
                if key not in seen:
                    candidates.append(r)
                    seen.add(key)

        # 4. Global fallback: active accounts with personas if group mapping wasn't populated
        if len(candidates) < 2:
            all_active_accs = self.store.rows(
                "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name, a.phone as account_phone "
                "FROM accounts a "
                "JOIN personas p ON p.id=a.persona_id "
                "WHERE a.active=1"
            )
            for r in all_active_accs:
                key = (r["persona_id"], r["account_id"])
                if key not in seen:
                    candidates.append(r)
                    seen.add(key)

        return candidates

    async def generate_all_groups(self, count=None):
        """Generates today's batch schedule for all eligible groups in one shot."""
        if count is None:
            count = int(self.store.get("batch_messages_count", 6))
        today = self.today_str()
        groups = self.store.rows("SELECT * FROM groups")
        group_cids = {g["chat_id"] for g in groups}
        ga_cids = {r["chat_id"] for r in self.store.rows("SELECT DISTINCT chat_id FROM group_accounts")}
        all_cids = list(group_cids.union(ga_cids))

        results = []
        for cid in all_cids:
            cands = self.get_group_candidates(cid)
            if len(cands) >= 2:
                try:
                    res = await self.generate_batch_for_group(cid, count=count)
                    g_match = [g for g in groups if g["chat_id"] == cid]
                    title = g_match[0].get("title") if g_match else f"Group {cid}"
                    results.append({"chat_id": cid, "title": title, "count": res.get("count", 0)})
                except Exception as e:
                    log.warning("Failed to generate batch for group %s: %s", cid, e)
        return results

    async def generate_batch_for_group(self, chat_id, count=6, topic_override=None):
        """Generates today's batch schedule for a watched group."""
        chat_id = int(chat_id)
        g_rows = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
        if not g_rows:
            raise ValueError(f"Group {chat_id} not found")
        group = g_rows[0]

        # 1. Fetch assigned personas with accounts using unified candidate resolver
        candidates = self.get_group_candidates(chat_id)

        if not candidates:
            raise ValueError("No active accounts with personas found for this group. Link personas to accounts first.")

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

        if topic_override:
            topic_prompt = topic_override
        elif transcript_lines:
            topic_prompt = "Organic continuation of ongoing community conversation based on recent messages above, discussing recovery, workout progress, training splits, and peptide protocol math."
        else:
            topic_prompt = "Natural community discussion on training splits, workout recovery, reconstitution ratios, and general fitness progress."

        system_prompt = (
            "You orchestrate realistic, organic human conversations in specialist Telegram communities.\n"
            "You write authentic, multi-turn dialogue between real humans.\n\n"
            "CRITICAL GUIDELINES:\n"
            "1. CONTEXT GROUNDING: Ground the conversation naturally in the community's subject matter (fitness, training splits, recovery, peptide protocol calculations). Do NOT hallucinate unrequested stories about placing orders, fake package tracking, website checkout glitches, or fake drama.\n"
            "2. VENDOR POLICY: Do NOT initiate unprompted conversations about vendors, suppliers, or purchasing. If members talk about protocols, discuss dosing math and technique naturally. All personas have had smooth, positive peer experiences.\n"
            "3. CASUAL TELEGRAM STYLE: Write like real community members: concise (1-2 short sentences per message), authentic phrasing, realistic casual casing and typos, zero corporate fluff or robotic bullet points.\n"
            "4. ORGANIC TURN-TAKING: Distribute turns naturally across the provided personas.\n"
            "5. OUTPUT FORMAT: Return ONLY a valid JSON array of message objects matching this exact format:\n"
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
            f"DOMAIN TOPICS: {group_domain}\n\n"
            f"RECENT MESSAGES IN THIS GROUP:\n{recent_transcript}\n\n"
            f"AVAILABLE PERSONAS:\n{persona_str}\n\n"
            f"CONVERSATIONAL FOCUS: {topic_prompt}\n\n"
            f"TASK:\n"
            f"Create a natural {count}-message conversational thread for today between these personas.\n"
            f"Spread 'offset_minutes' across realistic intervals (e.g. 15, 45, 110, 240).\n"
            f"Keep messages concise, grounded, and authentic. Return strictly the JSON array."
        )

        settings = {k: self.store.get(k) for k in ("fal_key", "ai_model")}
        items = []

        try:
            raw_ai, meta = await self.ai.chat_with_meta(settings, system_prompt, user_prompt, temperature=0.58)
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

        # Post-filter: Sanitize any negative vendor sentiment or complaint leaks
        clean_items = []
        forbidden_patterns = [
            r'\b(shit\s*show|payment\s*portal|portal.*bugg|card.*clear|recurring\s*nightmare|weak\s*batch|bad\s*batch|bad\s*vial|distraction.*fact|jaded|scam)\b'
        ]
        for it in items:
            txt = it.get("text", "")
            has_forbidden = any(re.search(pat, txt, re.IGNORECASE) for pat in forbidden_patterns)
            if not has_forbidden:
                clean_items.append(it)
            else:
                log.warning("Purged negative vendor dialogue item: %s", txt)

        items = clean_items
        if len(items) < 2:
            log.info("Batch items purged or insufficient; loading pristine domain fallback dialogue")
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

        # Calculate organic clustered conversational arcs (bursts of 2-3 messages, then quiet intervals)
        now_ts = int(time.time())
        created_items = []

        # Determine cluster timing
        offsets_min = []
        curr_min = 2.0  # Thread 1 starts 2 minutes from now
        item_idx = 0
        total_items = len(items)

        while item_idx < total_items:
            cluster_size = min(random.choice([2, 3]), total_items - item_idx)
            for c in range(cluster_size):
                if c == 0 and item_idx > 0:
                    # Natural gap between conversational topics: 35 to 75 minutes
                    curr_min += random.randint(35, 75)
                elif c > 0:
                    # Natural conversational response interval: 1.5 to 3.5 minutes
                    curr_min += random.uniform(1.5, 3.5)
                offsets_min.append(curr_min)
                item_idx += 1

        for idx, item in enumerate(items):
            pid = item.get("persona_id")
            aid = item.get("account_id")
            cand_match = [c for c in candidates if c["persona_id"] == pid and c["account_id"] == aid]
            if not cand_match:
                cand_match = [candidates[idx % len(candidates)]]
                pid = cand_match[0]["persona_id"]
                aid = cand_match[0]["account_id"]

            sender_name = cand_match[0]["persona_name"]
            text = (item.get("text") or "").strip()
            if not text:
                continue

            scheduled_min = offsets_min[idx] if idx < len(offsets_min) else (curr_min + (idx * 5))
            jitter_sec = random.randint(-15, 25)
            scheduled_ts = now_ts + int(scheduled_min * 60) + jitter_sec

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
        """Natural community discussion starter templates."""
        p1 = candidates[0]
        p2 = candidates[1] if len(candidates) > 1 else candidates[0]
        p3 = candidates[2] if len(candidates) > 2 else candidates[0]

        topics = [
            [
                (p1, 15, "what split is everyone running lately? thinking of switching back to push pull legs"),
                (p2, 48, "ppl is hard to beat for consistency, especially if recovery is on point"),
                (p3, 115, "yeah just don't neglect rear delts and rotator cuffs on push days"),
                (p1, 185, "for sure, warming up shoulders thoroughly makes a huge difference"),
                (p2, 290, "what's your weekly volume per muscle group looking like?"),
                (p3, 380, "usually hover around 12-16 sets per week, beyond that fatigue catches up"),
            ],
            [
                (p1, 20, "quick question on reconstitution, how much bac water do you guys prefer per 5mg vial?"),
                (p2, 52, "2ml makes the math super straightforward, 250mcg per 10 units on 100u syringe"),
                (p3, 120, "agreed, 2ml is ideal. just let it slide down the glass slow so it doesn't foam"),
                (p1, 190, "perfect, appreciate the confirmation. keeping it chilled in the fridge now"),
                (p2, 295, "smooth subQ or IM?"),
                (p3, 385, "subQ is easiest for daily routine, virtually painless with 31g"),
            ],
            [
                (p1, 25, "anyone notice recovery tanks pretty fast if sleep drops below 7 hours?"),
                (p2, 58, "100%. magnesium glycinate and a cold room made a big difference for deep sleep"),
                (p3, 130, "same here, consistent sleep timing does more than half the supplements out there"),
                (p1, 200, "definitely, dialed in sleep makes every workout feel 10x better"),
                (p2, 305, "hydration first thing in morning with pinch of salt helps too"),
                (p3, 395, "electrolytes on point keeps energy level all day"),
            ]
        ]
        chosen = random.choice(topics)
        items = []
        for cand, offset, text in chosen[:count]:
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

        auto_pilot = bool(self.store.get("daily_batch_auto_pilot", True))
        batch_count = int(self.store.get("batch_messages_count", 6))
        human_pause = int(self.store.get("batch_human_pause_seconds", 600)) // 60
        last_human = self.store.get("last_human_event")

        return {
            "date": today,
            "batches_count": len(batches),
            "auto_pilot": auto_pilot,
            "batch_messages_count": batch_count,
            "human_pause_minutes": human_pause,
            "last_human_event": last_human,
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
            self.store.q("UPDATE queue SET status='cancelled' WHERE kind='batch_scheduled_send' AND chat_id=? AND status='pending'", (int(chat_id),))
        else:
            items = self.store.rows("SELECT id FROM daily_batch_items WHERE status='pending'")
            for it in items:
                self.store.q("UPDATE daily_batch_items SET status='cancelled' WHERE id=?", (it["id"],))
                self.store.q("UPDATE queue SET status='cancelled' WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("UPDATE queue SET status='cancelled' WHERE kind='batch_scheduled_send' AND status='pending'")
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
