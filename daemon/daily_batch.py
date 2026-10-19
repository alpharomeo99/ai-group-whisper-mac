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
        """Unified, robust candidate resolution for a group across group_personas, group_accounts, and account fallbacks."""
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

        # 3. Fallback: group owner account's persona
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

        # 4. Global fallback: active accounts with personas if group mapping wasn't populated
        if len(candidates) < 2:
            all_active_accs = self.store.rows(
                "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name, a.phone as account_phone, a.username as account_username "
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

        # Determine topic context before formatting persona summaries so query_text is grounded
        if topic_override:
            topic_prompt = topic_override
            topic_query = topic_override
        elif transcript_lines:
            recent_snippet = " ".join(m.get("text", "") for m in recent_msgs[-4:] if m.get("text"))[:180]
            topic_prompt = (
                "Organic continuation of ongoing community conversation based on recent messages above. "
                "Freely touch upon nootropics (Semax, Selank, Bromantane), general fitness & training splits, "
                "wellness (sleep, sauna, recovery), helping out a peer with questions or dosing math, "
                "or lighthearted unserious gym banter and joking around."
            )
            topic_query = recent_snippet or "fitness nootropics wellness recovery banter"
        else:
            default_themes = [
                ("Natural community discussion on training splits, workout recovery, and general fitness progress.", "fitness training recovery"),
                ("Casual chat about nootropics, morning focus stacks (Semax, Bromantane, L-Tyrosine), and clean energy without brain fog.", "nootropics semax focus energy"),
                ("Everyday wellness discussion on sleep quality, sauna, magnesium glycinate, and staying healthy.", "wellness sleep recovery magnesium"),
                ("Community peer helping another member with quick reconstitution math, insulin syringe units, or routine advice.", "peptides dosing reconstitution help"),
                ("Completely relaxed, unserious gym banter, joking about tired mornings, meme supplements, and daily laughs.", "banter gym jokes funny")
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
        group_domain = group.get("domain_knowledge") or "Biohacking, research peptides, protocols, sourcing, and health optimization."

        system_prompt = (
            "You orchestrate realistic, organic human conversations in specialist Telegram communities.\n"
            "You write authentic, casual group chat exchanges between real people texting from their phones.\n\n"
            "CRITICAL GUIDELINES FOR AUTHENTIC HUMAN MESSAGING:\n"
            "1. WILDLY ASYMMETRICAL LENGTH DIVERSITY (MANDATORY):\n"
            "   - Real humans NEVER type uniform-length messages! Distribute message lengths naturally:\n"
            "     * ULTRA-SHORT REACTIONS (25-30% of turns): 1 to 4 words. Examples: 'lol nah', 'fr tho💀', 'wait what?', 'bro😭', 'nah 2ml is plenty', 'same honestly', 'legend', 'yea 100%'.\n"
            "     * QUICK TAKES & QUESTIONS (40-45% of turns): 5 to 14 words. Examples: 'how was the fatigue on heavy squat day?', 'dropped caffeine after 1pm and sleep is way better', 'did you run subq or im for that?'.\n"
            "     * DETAILED REPLIES & EXPLANATIONS (25-30% of turns): 18 to 45 words. Examples: 'if it\'s a 5mg vial and you put in 2ml bac water, every 10 units on an insulin pin is 250mcg. super straightforward once you do it once, just draw slow so no air bubbles'.\n"
            "   - NEVER make every message the same length!\n\n"
            "2. GENUINE TELEGRAM REPLIES & THREADING:\n"
            "   - Group dialogue happens in conversational clusters of 2-3 messages.\n"
            "   - When a persona is responding to, answering, or bantering with a previous message in the conversation, specify 'reply_to_step': <0-indexed step of target message>.\n"
            "   - If starting a new conversational topic or check-in, set 'reply_to_step': null.\n\n"
            "3. NATURAL @TAGS AND MENTIONS:\n"
            "   - Group members DO tag each other when asking a direct question, calling out their advice, or bantering (e.g. '@alex did you see this?', 'ask @leila she knows the math', 'lmao @dave 💀').\n"
            "   - Use natural @tag mentions in 1-2 messages across the dialogue using the participants' provided Telegram Tags.\n\n"
            "4. NATURAL EMOJIS & EXPRESSION:\n"
            "   - Use emojis naturally (💀, 😂, 😭, 🔥, 👀, 💪, 🤷‍♂️, 🤝, 🫠) according to each persona's signature emojis. Don't spam, but use them for reactions, jokes, and emphasis.\n\n"
            "5. REAL MOBILE TEXTING IMPERFECTIONS & CASUAL TYPING:\n"
            "   - Real people type fast on phone glass screens with their thumbs. They make mistakes!\n"
            "   - Skip apostrophes on common contractions ('dont', 'cant', 'wont', 'im', 'youre', 'thats', 'its').\n"
            "   - Use lowercase starts, missing periods at the end of short messages, and casual abbreviations ('rn', 'tbh', 'idk', 'ngl', 'prob', 'def', 'bcuz', 'kinda').\n"
            "   - Occasional natural phone typos when typing fast ('teh', 'yea', 'definately'). No one texts like an academic textbook!\n\n"
            "6. DISTINCT PERSONALITIES & VIBES (NO CLONES):\n"
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

        # Enforce natural Telegram dynamics and strip any robotic name-calling across all items
        try:
            cand_names = [c.get("persona_name") or c.get("account_name") for c in candidates if c.get("persona_name") or c.get("account_name")]
            for it in items:
                if it.get("text"):
                    it["text"] = self.ai.format_telegram_message(it["text"], names=cand_names, max_chars=360)
        except Exception as e:
            log.warning("Error formatting batch items: %s", e)

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

    def _fallback_dialogue(self, candidates, domain, count):
        """Natural community discussion starter templates with realistic casual gym chatter, emojis, @tags, and reply links."""
        p1 = candidates[0]
        p2 = candidates[1] if len(candidates) > 1 else candidates[0]
        p3 = candidates[2] if len(candidates) > 2 else candidates[0]
        p4 = candidates[3] if len(candidates) > 3 else p1

        t1 = p1.get("account_username") or p1.get("persona_name", "member").split()[0].lower()
        t2 = p2.get("account_username") or p2.get("persona_name", "member").split()[0].lower()
        t3 = p3.get("account_username") or p3.get("persona_name", "member").split()[0].lower()

        topics = [
            [
                (p1, 15, "switched back to ppl this week, leg day was brutal haha", None),
                (p2, 17, "leg day fatigue hits different fr💀", 0),
                (p3, 20, f"what split were you running before @{t1}?", 0),
                (p1, 23, "upper lower, was solid but felt like i was in teh gym forever doing 6 exercises per workout tbh. ppl lets me get in and out in 45 mins", 2),
                (p2, 25, "yeah volume creeps up fast if you dont cap sets. stick to 2 heavy working sets to failure now and recovery is way better", 3),
                (p3, 27, "like 2 heavy sets per exercise is plenty tbh", 4),
                (p4, 28, "lol nah 3 minimum", 5),
                (p1, 75, f"bench feeling any better on elbows @{t2}?", None),
                (p2, 78, "way better since tucking elbows in more, zero flare up 💪", 7),
                (p3, 80, "wrist wraps helped me a ton too", 7),
                (p4, 82, "same here", 9),
                (p1, 140, "hitting shoulders tomorrow or rest day?", None),
                (p2, 142, "rest day prob, need the recovery 😂", 11),
                (p3, 144, "smart move", 12),
                (p1, 145, "enjoy the food", 12)
            ],
            [
                (p1, 20, "quick math check, 2ml bac water in a 5mg vial is 250mcg per 10 units right?", None),
                (p2, 22, "yeah exactly, 10 units on a standard 100u pin is 250mcg. draw slow so no air bubbles 👍", 0),
                (p3, 24, "just trickle the bac water down the glass slow so it doesnt foam up", 0),
                (p1, 26, "got it, keeping it chilled in fridge now", 1),
                (p4, 27, "legend", 3),
                (p2, 85, f"running subq or im @{t1}?", None),
                (p3, 87, "subq with a 31g, barely feel anything tbh", 5),
                (p1, 89, "31g is the way to go fr", 6),
                (p2, 91, "rotate injection sites each time, keeps it painless", 7),
                (p1, 93, "appreciate the help guys 🤝", 8),
                (p4, 94, "anytime", 9),
                (p3, 150, "anyone pinning glute or strictly belly?", None),
                (p2, 152, "ventroglute is clutch once you find the spot", 11),
                (p1, 153, "belly fat pinch is just too easy lol", 11),
                (p4, 155, "facts", 13)
            ],
            [
                (p1, 18, "anyone running semax or bromantane for work focus lately?", None),
                (p2, 20, "na-semax-amidate is top tier for cognitive clarity honestly. zero jitters or afternoon crash, just clean quiet focus all morning", 0),
                (p3, 22, f"how does it compare to regular caffeine @{t2}?", 1),
                (p2, 25, "caffeine gives me restless anxiety after 2 cups tbh, semax just clears all brain fog without elevated heart rate", 2),
                (p4, 27, "stacking it with l-tyrosine hits different too 🧠", 3),
                (p1, 29, "wait really? dosage?", 4),
                (p4, 31, "like 500mg tyrosine on empty stomach with morning water, then 200mcg semax spray. super smooth drive for deep work", 5),
                (p1, 33, "ordering some today lol", 6),
                (p3, 80, "sleep quality still good on it?", None),
                (p2, 82, "yeah just dont dose it late afternoon", 8),
                (p3, 84, "makes sense", 9),
                (p1, 140, "magnesium glycinate before bed completely fixed my rem sleep btw", None),
                (p2, 142, "glycinate or threonate is a must have for recovery", 11),
                (p4, 143, "100%", 12),
                (p3, 145, "gonna add it to my stack tonight", 12)
            ],
            [
                (p1, 30, "forgot my shaker bottle in my gym bag for 4 days... opened it today and almost passed out 💀", None),
                (p2, 32, "bro that bottle is a biohazard now just throw it away lmao", 0),
                (p3, 34, "nothing on earth smells worse than old whey protein residue 😭", 1),
                (p4, 36, "dish soap wont save it, straight to the bin haha", 2),
                (p1, 38, f"already in the trash @{t2}, buying a 3 pack on amazon rn 😂", 3),
                (p2, 40, "rite of passage for every lifter honestly", 4),
                (p3, 41, "bro💀", 5),
                (p4, 43, "never leave it in the trunk in summer, pure toxic waste", 4),
                (p1, 45, "lesson learned the hard way haha", 7),
                (p2, 105, "stainless steel shakers are worth every penny btw, plastic absorbs smell forever", None),
                (p3, 107, "best gym investment i ever made", 9),
                (p1, 109, "added to cart haha", 10),
                (p4, 110, "smart man", 11),
                (p2, 170, "who is hitting gym tonight?", None),
                (p3, 172, "push day in 20 mins 💪", 13)
            ]
        ]
        chosen = random.choice(topics)
        items = []
        for row in chosen[:count]:
            cand = row[0]
            offset = row[1]
            text = row[2]
            rep_step = row[3] if len(row) > 3 else None
            items.append({
                "persona_id": cand["persona_id"],
                "account_id": cand["account_id"],
                "sender_name": cand["persona_name"],
                "offset_minutes": offset,
                "text": text,
                "reply_to_step": rep_step
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
            "session_interval_seconds": int(self.store.get("batch_session_interval_seconds", 3600)),
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
        """Cancels all pending and postponed scheduled batch messages for today."""
        today = self.today_str()
        if chat_id:
            items = self.store.rows(
                "SELECT id FROM daily_batch_items WHERE chat_id=? AND status IN ('pending', 'postponed')", (int(chat_id),)
            )
            for it in items:
                self.store.q("UPDATE daily_batch_items SET status='cancelled' WHERE id=?", (it["id"],))
                self.store.q("UPDATE queue SET status='cancelled' WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("UPDATE queue SET status='cancelled' WHERE kind='batch_scheduled_send' AND chat_id=? AND status IN ('pending', 'postponed')", (int(chat_id),))
        else:
            items = self.store.rows("SELECT id FROM daily_batch_items WHERE status IN ('pending', 'postponed')")
            for it in items:
                self.store.q("UPDATE daily_batch_items SET status='cancelled' WHERE id=?", (it["id"],))
                self.store.q("UPDATE queue SET status='cancelled' WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("UPDATE queue SET status='cancelled' WHERE kind='batch_scheduled_send' AND status IN ('pending', 'postponed')")
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
