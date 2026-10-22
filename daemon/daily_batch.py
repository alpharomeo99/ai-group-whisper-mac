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
    def __init__(self, store, ai_module, memory_engine=None):
        self.store = store
        self.ai = ai_module
        self.memory = memory_engine

    def today_str(self):
        return datetime.now().strftime("%Y-%m-%d")

    def get_group_candidates(self, chat_id):
        """Candidate resolution strictly limited to accounts and personas explicitly assigned or belonging to this group."""
        chat_id = int(chat_id)
        candidates = []
        seen = set()

        # 1. group_personas with explicit account_id
        gp_rows = self.store.rows(
            "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, gp.account_id, a.name as account_name, a.phone as account_phone, a.username as account_username "
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
            "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name, a.phone as account_phone, a.username as account_username "
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

        # 3. Group owner account's persona if explicitly linked to this group
        g_rows = self.store.rows("SELECT account_id FROM groups WHERE chat_id=?", (chat_id,))
        if g_rows and g_rows[0].get("account_id"):
            g_aid = g_rows[0]["account_id"]
            owner_rows = self.store.rows(
                "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name, a.phone as account_phone, a.username as account_username "
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

        # 4. Broad Fallback for Watched Groups: Ensure all active accounts with personas participate
        g_watch = self.store.rows("SELECT watched FROM groups WHERE chat_id=?", (chat_id,))
        is_watched = bool(g_watch and g_watch[0].get("watched"))
        if is_watched or len(candidates) < 2:
            all_active = self.store.rows(
                "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name, a.phone as account_phone, a.username as account_username "
                "FROM accounts a "
                "JOIN personas p ON p.id=a.persona_id "
                "WHERE a.active=1"
            )
            for r in all_active:
                key = (r["persona_id"], r["account_id"])
                if key not in seen:
                    candidates.append(r)
                    seen.add(key)

        return candidates

    async def generate_all_groups(self, count=None):
        """Generates today's batch schedule for all eligible groups in one shot."""
        if count is None:
            count = int(self.store.get("batch_messages_count", 24))
        today = self.today_str()
        groups = self.store.rows("SELECT * FROM groups WHERE watched=1")
        all_cids = [g["chat_id"] for g in groups]

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

    async def generate_batch_for_group(self, chat_id, count=24, topic_override=None):
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

        # Determine topic context before formatting persona summaries so query_text is grounded
        if topic_override:
            topic_prompt = topic_override
            topic_query = topic_override
        elif transcript_lines:
            recent_snippet = " ".join(m.get("text", "") for m in recent_msgs[-4:] if m.get("text"))[:180]
            topic_prompt = (
                "Organic continuation of ongoing community conversation based on recent messages above. "
                "Talk like real people with full lives: training, gear/TRT/SARMs experiences, peptides, nootropics, "
                "bloodwork, diet, sleep, injuries, but ALSO work, relationships, sports, news, money, travel, "
                "shows, cars and pure banter. Drift between topics naturally like a real group chat."
            )
            topic_query = recent_snippet or "fitness nootropics wellness recovery banter"
        else:
            default_themes = [
                ("Training talk: splits, PRs, plateaus, form checks, leg day suffering.", "training gym split pr"),
                ("Gear talk between experienced lifters: first cycle experiences, sides they dealt with, TRT life, bloodwork results, what they'd never run again.", "gear trt cycle bloodwork sides"),
                ("SARMs chat: people comparing experiences, skepticism about quality, suppression, whether it was worth it vs just running TRT.", "sarms rad lgd ostarine suppression"),
                ("Peptides and research compounds: what people noticed, healing injuries, GLP-1 appetite stories.", "peptides bpc tirz reta healing"),
                ("Nootropics and focus: morning stacks, caffeine, brain fog, work productivity.", "nootropics focus caffeine work"),
                ("Diet and food: meal prep, cheat meals, protein struggles, what they ate today, cooking fails.", "food diet protein meal cheat"),
                ("Bloodwork and health: lab results, doctor visits, blood pressure, lipids, hair and skin worries.", "bloodwork labs doctor health"),
                ("Sleep, recovery, sauna, cold plunge, injuries and physio.", "sleep recovery sauna injury"),
                ("Current news and what's going on in the world, sports results, UFC fight night, F1, football, viral stuff.", "news sports ufc football viral"),
                ("Work and money: annoying boss, side hustles, crypto/stocks losses, job stress.", "work job money crypto"),
                ("Relationships and family: girlfriend/wife complaining about gym time, kids, dating stories.", "girlfriend wife kids dating"),
                ("Weekend plans, travel, cars, movies and shows people are watching.", "weekend travel car movie show"),
                ("Pure unserious banter, roasting each other, gym memes, gym crush stories, weird gym people.", "banter jokes roast meme"),
                ("Someone newer asking the group general questions and veterans sharing honest experience and warnings.", "newbie question advice experience"),
                ("Physique updates: bulk vs cut debates, scale weight, mirror check, bodyfat guesses.", "bulk cut weight physique"),
            ]
            chosen_theme, chosen_query = random.choice(default_themes)
            topic_prompt = chosen_theme
            topic_query = chosen_query

        # 3. Format persona summaries with Telegram tags and distinct styles
        persona_summaries = []
        for c in candidates:
            dt = {}
            try:
                dt = json.loads(c.get("details") or "{}") if isinstance(c.get("details"), str) else (c.get("details") or {})
            except Exception:
                dt = {}
            role = dt.get("role_in_group") or dt.get("tone_and_vibe") or "Experienced regular"
            slang = dt.get("slang_and_vocabulary") or "Authentic telegram slang"
            emojis = dt.get("signature_emojis") or "💀, 😂"
            emoji_habit = dt.get("emoji_habit") or "moderate"
            typing = dt.get("typing_style") or "casual phone texting"
            uname = c.get("account_username")
            tag = f"@{uname}" if uname else f"@{(c.get('persona_name') or 'member').split()[0].lower()}"

            mem_lines = []
            if self.memory:
                mem_ctx = self.memory.get_prompt_memory_context(c["persona_id"], chat_id=chat_id, limit=8, query_text=topic_query)
                if mem_ctx:
                    mem_lines.append(mem_ctx)
            elif hasattr(self.store, "rows"):
                m_rows = self.store.rows(
                    "SELECT content FROM persona_memories WHERE persona_id=? AND (expires_at IS NULL OR expires_at > ?) "
                    "ORDER BY CASE retention_tier WHEN 'long' THEN 1 ELSE 2 END, salience DESC LIMIT 4",
                    (c["persona_id"], int(time.time()))
                )
                if m_rows:
                    mem_lines.append("Core Established Facts:\n" + "\n".join(f"  * {mr['content']}" for mr in m_rows))
            mem_str = ("\n  " + "\n  ".join(mem_lines)) if mem_lines else ""

            persona_summaries.append(
                f"- ID: {c['persona_id']}, AccountID: {c['account_id']}, Name: {c['persona_name']}, Telegram Tag: {tag}\n"
                f"  Vibe/Role: {role}\n"
                f"  Signature Emojis: {emojis} (habit: {emoji_habit})\n"
                f"  Typing: {typing}\n"
                f"  Brief: {c.get('prompt', '')[:160]}...{mem_str}"
            )
        persona_str = "\n\n".join(persona_summaries)

        group_title = group.get("title") or f"Group {chat_id}"
        group_about = group.get("about") or "Telegram community discussion"
        group_domain = group.get("domain_knowledge") or "Lifting, gear, TRT, SARMs, peptides, nootropics, health, plus everyday life, news and banter."

        system_prompt = (
            "You orchestrate realistic, organic human conversations in specialist Telegram communities.\n"
            "You write authentic, casual group chat exchanges between real people texting from their phones.\n\n"
            "CRITICAL GUIDELINES FOR AUTHENTIC HUMAN MESSAGING:\n"
            "1. WILDLY ASYMMETRICAL LENGTH DIVERSITY (MANDATORY):\n"
            "   - Real humans NEVER type uniform-length messages! Distribute message lengths naturally:\n"
            "     * ULTRA-SHORT REACTIONS (25-30% of turns): 1 to 4 words. Examples: 'lol nah', 'fr tho💀', 'wait what?', 'bro😭', 'nah 2ml is plenty', 'same honestly', 'legend', 'yea 100%'.\n"
            "     * QUICK TAKES & QUESTIONS (40-45% of turns): 5 to 14 words. Examples: 'how was the fatigue on heavy squat day?', 'dropped caffeine after 1pm and sleep is way better', 'did you run subq or im for that?'.\n"
            "     * DETAILED REPLIES & EXPLANATIONS (25-30% of turns): 18 to 45 words. Examples: 'if it\'s a 5mg vial and you put in 2ml bac water, every 10 units on an insulin pin is 250mcg. super straightforward once you do it once, just draw slow so no air bubbles'.\n"
            "     * MULTI-MESSAGE BURSTS: Mobile texters often send 2 quick consecutive bubbles instead of one chunk. If fitting for the persona, separate consecutive bubbles with ' ||| ' (e.g. 'wait did you check the vial? ||| 5mg vs 10mg is totally different math').\n"
            "   - NEVER make every message the same length!\n\n"
            "2. GENUINE TELEGRAM REPLIES & THREADING:\n"
            "   - Group dialogue happens in conversational clusters of 2-3 messages.\n"
            "   - When a persona is responding to, answering, or bantering with a previous message in the conversation, specify 'reply_to_step': <0-indexed step of target message>.\n"
            "   - If starting a new conversational topic or check-in, set 'reply_to_step': null.\n\n"
            "3. NATURAL @TAGS AND MENTIONS:\n"
            "   - Group members DO tag each other when asking a direct question, calling out their advice, or bantering (e.g. '@alex did you see this?', 'ask @leila she knows the math', 'lmao @dave 💀').\n"
            "   - Use natural @tag mentions in 1-2 messages across the dialogue using the participants' provided Telegram Tags.\n\n"
            "   - GROUP ADMIN & OWNER (@xbiolabs): Personas recognize that @xbiolabs is the group admin, owner, and order processor. Personas can naturally tag or mention @xbiolabs if someone is new, asking about orders or processing, or if they have an admin question (e.g. 'ask @xbiolabs he handles processing', 'dm @xbiolabs', 'pinging @xbiolabs').\n\n"
            "4. NATURAL EMOJIS & EXPRESSION:\n"
            "   - Use emojis naturally (💀, 😂, 😭, 🔥, 👀, 💪, 🤷‍♂️, 🤝, 🫠) according to each persona's signature emojis. Don't spam, but use them for reactions, jokes, and emphasis.\n\n"
            "5. REAL MOBILE TEXTING IMPERFECTIONS & CASUAL TYPING:\n"
            "   - Real people type fast on phone glass screens with their thumbs. They make mistakes!\n"
            "   - Skip apostrophes on common contractions ('dont', 'cant', 'wont', 'im', 'youre', 'thats', 'its').\n"
            "   - Use lowercase starts, missing periods at the end of short messages, and casual abbreviations ('rn', 'tbh', 'idk', 'ngl', 'prob', 'def', 'bcuz', 'kinda').\n"
            "   - Occasional natural phone typos when typing fast ('teh', 'yea', 'definately'). No one texts like an academic textbook!\n\n"
            "6. DISTINCT PERSONALITIES & MANDATORY EQUAL ROTATION (NO MONOPOLIES):\n"
            "   - EVERY SINGLE persona listed under AVAILABLE PERSONAS MUST speak in this dialogue thread!\n"
            "   - Distribute speaking turns evenly across ALL candidates. Do NOT let one persona dominate the chat.\n"
            "   - Quieter or supporting personas must ask questions, banter, drop short reactions (1-4 words), or follow up.\n"
            "   - Ban single-persona monopolization: no single persona may take more than 35% of the total dialogue steps.\n"
            "   - Give each participant their own unique tone, opinions, and attitude. Do not make everyone agree or sound like clones.\n"
            "   - Stop constantly agreeing or validating. Ban 'Makes sense', 'For sure', 'Definitely', 'Agreed', 'Spot on', 'Good point', '100% facts'. Real people banter, tease, share hot takes, ask questions, or react directly.\n\n"
            "7. TOPIC SPECTRUM (FITNESS, NOOTROPICS, WELLNESS, HELPING OUT & BANTER):\n"
            "   - General fitness: PPL, lifting splits, PRs, squat/bench/deadlift form, volume management, deloads, cutting/bulking, nutrition.\n"
            "   - Nootropics & focus: Semax, Selank, Bromantane, L-Tyrosine, Alpha-GPC, clean drive, avoiding caffeine crashes.\n"
            "   - Wellness & longevity: Sleep quality, magnesium, sauna, cold plunges, joint recovery, hydration.\n"
            "   - Helping out: Welcoming peers who ask questions, giving clear, casual reconstitution math or tips.\n"
            "   - Unserious banter & jokes: Cracking jokes, playful teasing, gym humor, meme moments, and laughing off tough days.\n\n"
            "8. VENDOR POLICY:\n"
            "   - Do NOT initiate unprompted conversations about vendors, suppliers, or purchasing. All personas have had smooth, positive peer experiences.\n\n"
            "9. OUTPUT FORMAT: Return ONLY a valid JSON array of message objects matching this exact format:\n"
            "[\n"
            "  {\n"
            "    \"persona_id\": <int>,\n"
            "    \"account_id\": <int>,\n"
            "    \"sender_name\": \"<string>\",\n"
            "    \"reply_to_step\": <int or null>,\n"
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
            f"AVAILABLE PERSONAS & TELEGRAM TAGS:\n{persona_str}\n\n"
            f"CONVERSATIONAL FOCUS: {topic_prompt}\n\n"
            f"TASK:\n"
            f"Create a natural {count}-message conversational thread for today between these personas.\n"
            f"Spread 'offset_minutes' across realistic intervals (e.g. 15, 45, 110, 240).\n"
            f"Embrace varied message lengths (ultra-short reactions, medium takes, detailed explanations), emojis, and realistic casual texting flaws. Return strictly the JSON array."
        )

        settings = {k: self.store.get(k) for k in ("fal_key", "ai_model")}
        items = []

        try:
            raw_ai, meta = await self.ai.chat_with_meta(settings, system_prompt, user_prompt, temperature=0.78)
            raw = raw_ai.strip()
            raw = re.sub(r"^```(?:json)?", "", raw).strip()
            raw = re.sub(r"```$", "", raw).strip()
            match = re.search(r"\[\s*\{.*\}\s*\]", raw, re.DOTALL)
            if match:
                clean_text = re.sub(r",\s*([\]\}])", r"\1", match.group(0))
                try:
                    items = json.loads(clean_text)
                except Exception:
                    pass
            if not items:
                for m_obj in re.finditer(r"\{[^{}]*\}", raw):
                    try:
                        clean_obj = re.sub(r",\s*([\]\}])", r"\1", m_obj.group(0))
                        items.append(json.loads(clean_obj))
                    except Exception:
                        pass
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
            log.error("AI batch generation failed for group %s: %s", chat_id, e)
            raise RuntimeError(f"AI generation failed: {e}") from e

        # Enforce natural Telegram dynamics and strip any robotic name-calling across all items
        try:
            cand_names = [c.get("persona_name") or c.get("account_name") for c in candidates if c.get("persona_name") or c.get("account_name")]
            for it in items:
                if it.get("text"):
                    it["text"] = self.ai.format_telegram_message(it["text"], names=cand_names, max_chars=360)
        except Exception as e:
            log.warning("Error formatting batch items: %s", e)

        # 4. Auto-clear past failed and cancelled items for this chat to keep queue fresh
        try:
            self.store.q("DELETE FROM daily_batch_items WHERE chat_id=? AND status IN ('failed', 'cancelled')", (chat_id,))
            self.store.q("DELETE FROM queue WHERE chat_id=? AND kind='batch_scheduled_send' AND status IN ('failed', 'cancelled')", (chat_id,))
        except Exception as e:
            log.debug("Auto-cleanup failed items: %s", e)

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

        # Calculate organic clustered conversational arcs with high activity and varied rhythm
        now_ts = int(time.time())
        created_items = []

        # Determine cluster timing: non-patterned, dynamic clusters (1 to 5 messages) and active lulls
        offsets_min = []
        curr_min = random.uniform(0.5, 2.0)  # Starts promptly
        item_idx = 0
        total_items = len(items)

        while item_idx < total_items:
            # Diverse cluster sizes: 1 (solo drop-in/reaction), 2-3 (dialogue), 4-5 (multi-persona active chatter)
            # Weights favor 2-3 but frequently include solo reactions and lively 4-5 message exchanges
            cluster_weights = [0.20, 0.35, 0.25, 0.14, 0.06]
            cluster_size = min(random.choices([1, 2, 3, 4, 5], weights=cluster_weights)[0], total_items - item_idx)
            for c in range(cluster_size):
                if c == 0 and item_idx > 0:
                    # Dynamic active gap between conversational threads: 4 to 18 minutes (occasional 8-22m lull)
                    curr_min += random.choice([
                        random.uniform(3.5, 8.5),
                        random.uniform(6.0, 14.0),
                        random.uniform(10.0, 20.0)
                    ])
                elif c > 0:
                    # Rapid mobile texting reply interval: 18 to 85 seconds (0.3 to 1.4 minutes)
                    curr_min += random.uniform(0.3, 1.4)
                offsets_min.append(curr_min)
                item_idx += 1

        cand_usage = { (c["persona_id"], c["account_id"]): 0 for c in candidates }

        for idx, item in enumerate(items):
            pid = item.get("persona_id")
            aid = item.get("account_id")
            sname = (item.get("sender_name") or "").lower()

            cand_match = [c for c in candidates if c["persona_id"] == pid and c["account_id"] == aid]
            if not cand_match and pid:
                cand_match = [c for c in candidates if c["persona_id"] == pid]
            if not cand_match and aid:
                cand_match = [c for c in candidates if c["account_id"] == aid]
            if not cand_match and sname:
                cand_match = [c for c in candidates if sname in (c.get("persona_name") or "").lower() or sname in (c.get("account_name") or "").lower()]
            if not cand_match:
                least_used = sorted(candidates, key=lambda c: cand_usage.get((c["persona_id"], c["account_id"]), 0))[0]
                cand_match = [least_used]

            chosen_cand = cand_match[0]
            pid = chosen_cand["persona_id"]
            aid = chosen_cand["account_id"]
            sender_name = chosen_cand["persona_name"]
            cand_usage[(pid, aid)] = cand_usage.get((pid, aid), 0) + 1

            text = (item.get("text") or "").strip()
            if not text:
                continue

            scheduled_min = offsets_min[idx] if idx < len(offsets_min) else (curr_min + (idx * 2))
            jitter_sec = random.randint(-18, 25)
            scheduled_ts = max(now_ts + 5, now_ts + int(scheduled_min * 60) + jitter_sec)

            # Determine parent_item_id for native Telegram reply quotes:
            rep_step = item.get("reply_to_step")
            parent_item_id = None
            if rep_step is not None and isinstance(rep_step, int) and 0 <= rep_step < len(created_items):
                parent_item_id = created_items[rep_step]["id"]
            elif idx > 0 and (offsets_min[idx] - offsets_min[idx-1]) < 8.0 and created_items:
                # Same conversation burst: automatically reply to previous message in cluster
                parent_item_id = created_items[-1]["id"]

            cur_item = self.store.q(
                "INSERT INTO daily_batch_items(batch_id, chat_id, account_id, persona_id, sender_name, text, scheduled_ts, status, created_at, reply_to_item_id) "
                "VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                (batch_id, chat_id, aid, pid, sender_name, text, scheduled_ts, now_ts, parent_item_id)
            )
            item_id = cur_item.lastrowid

            # Enqueue into work queue for automated wake-and-send
            self.store.enqueue(
                "batch_scheduled_send",
                chat_id,
                {
                    "batch_item_id": item_id,
                    "account_id": aid,
                    "persona_id": pid,
                    "text": text,
                    "reply_to_item_id": parent_item_id
                },
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
                "status": "pending",
                "reply_to_item_id": parent_item_id
            })

        return {
            "batch_id": batch_id,
            "chat_id": chat_id,
            "count": len(created_items),
            "date": today,
            "items": created_items
        }

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
            "WHERE COALESCE(dbi.created_at, dbi.scheduled_ts, 0) >= ? "
            "ORDER BY dbi.scheduled_ts ASC",
            (int(time.time()) - 86400,)
        )

        total = len(all_items)
        pending = sum(1 for x in all_items if x["status"] == "pending")
        sent = sum(1 for x in all_items if x["status"] == "sent")
        postponed = sum(1 for x in all_items if x["status"] == "postponed")
        failed = sum(1 for x in all_items if x["status"] == "failed")
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
        batch_count = int(self.store.get("batch_messages_count", 24))
        human_pause = int(self.store.get("batch_human_pause_seconds", 600)) // 60
        last_human = self.store.get("last_human_event")

        return {
            "date": today,
            "batches_count": len(batches),
            "auto_pilot": auto_pilot,
            "batch_messages_count": batch_count,
            "human_pause_minutes": human_pause,
            "session_interval_seconds": int(self.store.get("batch_session_interval_seconds", 3600)),
            "last_human_event": last_human,
            "stats": {
                "total": total,
                "pending": pending,
                "sent": sent,
                "postponed": postponed,
                "failed": failed,
                "cancelled": cancelled
            },
            "next_scheduled_ts": next_ts,
            "items": all_items,
            "scout_account": scout_info,
            "live_ai_mode": "Humans Only (Bot replies strictly filtered)"
        }

    def clear_today(self, chat_id=None):
        """Cancels and purges all pending, postponed, and failed scheduled batch messages for today."""
        today = self.today_str()
        if chat_id:
            items = self.store.rows(
                "SELECT id FROM daily_batch_items WHERE chat_id=? AND status IN ('pending', 'postponed', 'failed')", (int(chat_id),)
            )
            for it in items:
                self.store.q("DELETE FROM daily_batch_items WHERE id=?", (it["id"],))
                self.store.q("DELETE FROM queue WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("DELETE FROM queue WHERE kind='batch_scheduled_send' AND chat_id=? AND status IN ('pending', 'postponed', 'failed')", (int(chat_id),))
        else:
            items = self.store.rows("SELECT id FROM daily_batch_items WHERE status IN ('pending', 'postponed', 'failed')")
            for it in items:
                self.store.q("DELETE FROM daily_batch_items WHERE id=?", (it["id"],))
                self.store.q("DELETE FROM queue WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("DELETE FROM queue WHERE kind='batch_scheduled_send' AND status IN ('pending', 'postponed', 'failed')")
        return len(items)

    def clear_failed(self, chat_id=None):
        """Specifically removes and purges all failed batch items and failed queue entries."""
        if chat_id:
            items = self.store.rows("SELECT id FROM daily_batch_items WHERE chat_id=? AND status='failed'", (int(chat_id),))
            for it in items:
                self.store.q("DELETE FROM daily_batch_items WHERE id=?", (it["id"],))
                self.store.q("DELETE FROM queue WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("DELETE FROM queue WHERE kind='batch_scheduled_send' AND chat_id=? AND status='failed'", (int(chat_id),))
        else:
            items = self.store.rows("SELECT id FROM daily_batch_items WHERE status='failed'")
            for it in items:
                self.store.q("DELETE FROM daily_batch_items WHERE id=?", (it["id"],))
                self.store.q("DELETE FROM queue WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("DELETE FROM queue WHERE status='failed'")
        return len(items)

    def trigger_next_now(self):
        """Forces the next pending or postponed scheduled message to send immediately."""
        now_ts = int(time.time())
        pending = self.store.rows(
            "SELECT id, chat_id FROM daily_batch_items WHERE status IN ('pending', 'postponed') ORDER BY scheduled_ts ASC LIMIT 1"
        )
        if not pending:
            return None
        it_id = pending[0]["id"]
        self.store.q("UPDATE daily_batch_items SET status='pending', scheduled_ts=? WHERE id=?", (now_ts, it_id))
        self.store.q("UPDATE queue SET status='pending', not_before=? WHERE dedupe_key=?", (now_ts, f"batch_send:{it_id}"))
        return it_id
