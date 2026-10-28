"""AI client: fal.ai OpenAI-compatible router (Claude, GPT, Gemini, Llama, DeepSeek... via one fal key)."""
import random
import re
import time
import aiohttp

FAL_BASE = "https://fal.run/openrouter/router/openai/v1"
DEFAULT_MODEL = "google/gemini-2.5-flash"

SAFE_INTERJECTIONS = {
    'honestly', 'actually', 'personally', 'basically', 'definitely', 'certainly',
    'absolutely', 'frankly', 'bro', 'man', 'mate', 'dude', 'guys', 'everyone',
    'team', 'folks', 'yeah', 'yes', 'nope', 'nah', 'wait', 'look', 'listen',
    'true', 'agreed', 'right', 'well', 'okay', 'ok', 'sure', 'anyway', 'anyways',
    'also', 'meanwhile', 'first', 'second', 'third', 'lastly', 'finally',
    'today', 'yesterday', 'tomorrow', 'morning', 'night', 'tonight'
}


class AIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.retryable = status == 429 or status >= 500


async def chat_with_meta(settings, system, user, temperature=None, frequency_penalty=None, presence_penalty=None):
    key = settings.get("fal_key") or ""
    if not key:
        raise AIError(401, "Add your fal.ai API key in Settings.")
    model = settings.get("ai_model") or DEFAULT_MODEL
    headers = {"Content-Type": "application/json", "Authorization": f"Key {key}"}
    body = {"model": model, "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}]}
    if temperature is not None:
        body["temperature"] = float(temperature)
    # Aggressively penalize repetition by default (frequency & presence penalties)
    freq_pen = frequency_penalty if frequency_penalty is not None else 0.55
    pres_pen = presence_penalty if presence_penalty is not None else 0.45
    body["frequency_penalty"] = float(freq_pen)
    body["presence_penalty"] = float(pres_pen)

    t0 = time.time()
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{FAL_BASE}/chat/completions", json=body, headers=headers) as r:
            data = await r.json(content_type=None)
            lat_ms = int((time.time() - t0) * 1000)
            if r.status != 200:
                msg = None
                if isinstance(data, dict):
                    err = data.get("error") or data.get("detail")
                    msg = err.get("message") if isinstance(err, dict) else (str(err) if err else None)
                raise AIError(r.status, msg or f"fal.ai request failed ({r.status})")
            content = data["choices"][0]["message"]["content"].strip()
            usage = data.get("usage") or {}
            p_tok = usage.get("prompt_tokens") or max(1, len(system + user) // 4)
            c_tok = usage.get("completion_tokens") or max(1, len(content) // 4)
            meta = {
                "prompt_tokens": p_tok,
                "completion_tokens": c_tok,
                "total_tokens": p_tok + c_tok,
                "latency_ms": lat_ms,
                "model": model
            }
            return content, meta


async def chat(settings, system, user, temperature=None, frequency_penalty=None, presence_penalty=None):
    content, _ = await chat_with_meta(settings, system, user, temperature, frequency_penalty, presence_penalty)
    return content


def transcript(msgs):
    sep = chr(10)
    return sep.join(f"[{m['sender']}] {m['text']}" for m in msgs if m.get("text"))


async def summarize(settings, title, msgs):
    return await chat(settings,
                      "You summarize Telegram group conversations into concise bullet points: topics, decisions, open questions, and anything addressed to the user.",
                      f"Group: {title}\n\n{transcript(msgs)}")


def strip_addressing_names(text, names=None):
    """Strips robotic email-style name addressing and formal greetings while PRESERVING intentional Telegram @mentions
    and authentic casual interjections like 'bro', 'honestly', 'yeah'.
    """
    if not text:
        return ""
    text = text.strip()

    # Strip bracketed LLM labels like '[Dave]: ' or 'Dave: ' (but NOT '@handle:')
    text = re.sub(r'^(?:\[[^\]]+\]:?|(?!@)[a-zA-Z]{2,15}:\s*)', '', text).strip()

    # Targeted name stripping if names are provided, but ONLY when used in formal salutation styles:
    # e.g. 'Hey Dave, ' -> ''
    # BUT DO NOT STRIP '@dave' mentions!
    target_names = set()
    if names:
        for n in names:
            if not n:
                continue
            raw = str(n).strip()
            if raw.startswith('@'):
                continue
            if len(raw) >= 2:
                target_names.add(raw.lower())
                first = raw.split()[0].lower()
                if len(first) >= 2 and first not in SAFE_INTERJECTIONS:
                    target_names.add(first)

    for name in target_names:
        esc = re.escape(name)
        # Leading greetings followed by bare name without @: 'Hey Alice, '
        pat_leading = rf'^(?:(?:hey|hi|hello|yo)\s+){esc}[,:]\s*'
        text = re.sub(pat_leading, '', text, flags=re.IGNORECASE)
        # Leading bare name without @: 'Alice, ' -> ''
        text = re.sub(rf'^{esc}[,:]\s*', '', text, flags=re.IGNORECASE)
        # End of sentence vocative: ', Alice?' -> '?'
        text = re.sub(rf',\s*{esc}([\.!?]|\s*$)', r'\1', text, flags=re.IGNORECASE)

    # Clean leftover punctuation or double spaces
    text = re.sub(r'^\s*[,:;-]\s*', '', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text


def naturalize_mobile_text(text):
    """Preserves natural text contractions without forcibly removing apostrophes."""
    return text


def strip_all_tags(text):
    """Removes all @tags, @usernames, and @handles from message text while keeping natural phrasing."""
    if not text:
        return ""
    # Replace phrasing like 'dm @xbiolabs' or 'ask @handle' with 'dm admin' / 'ask admin'
    text = re.sub(r'\b(dm|message|ask|ping|contact|tell)\s+@\w+\b', r'\1 admin', text, flags=re.IGNORECASE)
    # Remove any remaining @handle or @username entirely
    text = re.sub(r'@\w{2,32}\b\s*[,:]?\s*', ' ', text)
    # Fix spacing around punctuation: e.g. 'bro !!' -> 'bro!!', 'bro , ' -> 'bro, '
    text = re.sub(r'\s+([,.!?:;])', r'\1', text)
    # Clean double spaces
    text = re.sub(r'\s{2,}', ' ', text)
    # Clean leading punctuation like ': ' or ', '
    text = re.sub(r'^\s*[,:;-]\s*', '', text)
    return text.strip()


def strip_internal_tags(text, tags_to_strip):
    if not text:
        return ""
    text = strip_all_tags(text)
    if not tags_to_strip:
        return text
    for tag in tags_to_strip:
        clean = tag.lower().lstrip('@')
        if not clean:
            continue
        pat = rf'@?\b{re.escape(clean)}\b\s*[,:]?\s*'
        text = re.sub(pat, ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text



BANNED_DIALOGUE_PATTERNS = [
    re.compile(r"\bliver\s*king\b", re.I),
    re.compile(r"\braw\s*liver\b", re.I),
    re.compile(r"\bancestral\s*tenets?\b", re.I),
    re.compile(r"\bancestral\s*lifestyle\b", re.I),
    re.compile(r"\bduck\s*eggs?\b", re.I),
    re.compile(r"\bbaby\s*don't\s*hurt\s*me\b", re.I),
]


def sanitize_banned_tropes(text):
    """Actively neutralizes stale meme tropes and banned dialogue patterns."""
    if not text:
        return text
    for pat in BANNED_DIALOGUE_PATTERNS:
        if pat.search(text):
            text = pat.sub("some fitness influencer", text)
    return text

def _clean_single_telegram_bubble(text, names=None, max_chars=360, strip_tags=None):
    text = sanitize_banned_tropes(text)
    """Sanitizes a single message bubble: strips labels, salutations, markdown lists, and caps length."""
    if not text:
        return ""
    text = text.strip().strip("'\"")
    # Strip LLM sender labels like '[Dave]: ' or 'Alice: '
    text = re.sub(r'^(?:\[[^\]]+\]:?|\w+:\s*)\s*', '', text).strip()

    # Clean out formal email-style salutations and all @tags/usernames
    text = strip_addressing_names(text, names=names)
    text = strip_all_tags(text)
    if strip_tags:
        text = strip_internal_tags(text, strip_tags)

    raw_lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not raw_lines:
        return ""

    clean_lines = []
    for l in raw_lines[:4]:
        l = re.sub(r'^(?:[-*•#]|\d+[\.\)])\s+', '', l).strip()
        if l:
            clean_lines.append(l)

    text = " ".join(clean_lines) if len(clean_lines) <= 2 else "\n".join(clean_lines)

    if len(text) > max_chars:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        combined = ""
        for s in sentences:
            if not s:
                continue
            cand = (combined + " " + s).strip() if combined else s
            if len(cand) <= max_chars:
                combined = cand
            else:
                break
        if combined and len(combined) >= 20:
            text = combined
        else:
            truncated = text[:max_chars]
            last_space = truncated.rfind(' ')
            text = truncated[:last_space] if last_space > 20 else truncated

    return text.strip()


def format_telegram_message(text, names=None, max_chars=360, strip_tags=None):
    """Cleans LLM formatting artifacts from generated Telegram messages while preserving natural grammar, tone, and casing.
    Preserves multi-bubble burst markers (' ||| ', '[SPLIT]', etc.) by formatting each bubble independently.
    """
    if not text:
        return ""
    text = text.strip()
    for sep in (" ||| ", "|||", "[SPLIT]", "\n---\n", "\n===SPLIT===\n"):
        if sep in text:
            parts = [_clean_single_telegram_bubble(p, names=names, max_chars=max_chars, strip_tags=strip_tags) for p in text.split(sep)]
            parts = [p for p in parts if p]
            return " ||| ".join(parts[:3])
    return _clean_single_telegram_bubble(text, names=names, max_chars=max_chars, strip_tags=strip_tags)


def split_burst_bubbles(text, names=None, burstiness_pct=35, max_chars=360):
    """Splits a message into 1 to 3 distinct Telegram message bubbles for authentic multi-message texting.
    Handles explicit markers (' ||| '), conversational interjections, and natural stochastic sentence splitting
    calibrated by persona burstiness.
    """
    if not text:
        return []

    # 1. Check explicit separators first
    for sep in (" ||| ", "|||", "[SPLIT]", "\n---\n", "\n===SPLIT===\n"):
        if sep in text:
            parts = [_clean_single_telegram_bubble(p, names=names, max_chars=max_chars) for p in text.split(sep)]
            bubbles = [p for p in parts if p]
            if len(bubbles) > 1:
                return bubbles[:3]

    # Clean the single text
    cleaned = _clean_single_telegram_bubble(text, names=names, max_chars=max_chars)
    if not cleaned:
        return []

    # 2. Check paragraph / double newline split
    if "\n\n" in text:
        paras = [_clean_single_telegram_bubble(p, names=names, max_chars=max_chars) for p in text.split("\n\n") if p.strip()]
        paras = [p for p in paras if p]
        if 1 < len(paras) <= 3 and all(len(p) <= 200 for p in paras):
            return paras[:3]

    pct = float(burstiness_pct if burstiness_pct is not None else 35)

    # 3. Conversational interjection / reaction opener split (authentic mobile habit)
    interjection_match = re.match(
        r"^(wait (?:nah|bro|what|hold up)|nah (?:bro|fr|tbh|man)|bro (?:honestly|listen|fr)|lol (?:nah|yeah|bro)|lmaooo?|fr though?|same (?:here|honestly)|100%|idk (?:tbh|man)|yo|nah)\s*[,.!?—\-]\s*(.+)$",
        cleaned,
        re.IGNORECASE
    )
    if interjection_match and pct >= 20:
        opener = interjection_match.group(1).strip()
        rest = interjection_match.group(2).strip()
        if 2 <= len(opener) <= 30 and 4 <= len(rest) <= 220:
            return [opener, rest]

    # 4. Multi-sentence splitting based on persona burstiness
    roll_chance = max(0.20, min(0.80, 0.30 + (pct / 100.0) * 0.50))
    if random.random() < roll_chance:
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', cleaned) if s.strip()]
        if len(sentences) == 2:
            s1, s2 = sentences[0], sentences[1]
            if 3 <= len(s1) <= 160 and 4 <= len(s2) <= 220:
                return [s1, s2]
        elif len(sentences) == 3 and pct >= 45:
            if all(3 <= len(s) <= 110 for s in sentences):
                return sentences[:3]

    return [cleaned]


def format_short_telegram_text(text, names=None, max_chars=360):
    """Backward-compatible wrapper for format_telegram_message with natural dynamic length."""
    return format_telegram_message(text, names=names, max_chars=max_chars)



def compact_transcript(msgs, max_msgs=18, max_chars=2400, line_cap=220):
    """Token-efficient history: newest-first budget, per-line cap, consecutive duplicates collapsed."""
    out, total, last = [], 0, None
    for m in reversed(list(msgs or [])):
        txt = (m.get("text") or "").replace("\n", " ").strip()
        if not txt or txt == last:
            continue
        last = txt
        if len(txt) > line_cap:
            txt = txt[:line_cap].rsplit(" ", 1)[0] + "…"
        line = f"[{m.get('sender') or 'user'}] {txt}"
        if total + len(line) > max_chars or len(out) >= max_msgs:
            break
        out.append(line)
        total += len(line)
    return "\n".join(reversed(out))


def build_voice_card(details):
    """Compact per-persona voice fingerprint injected into every reply (keeps personas distinct, ~60 tokens)."""
    d = details or {}
    if not isinstance(d, dict) or not d:
        return ""
    def _l(v, n):
        if isinstance(v, str):
            v = [x.strip() for x in v.split(",") if x.strip()]
        return [str(x) for x in (v or [])][:n]
    parts = []
    vibe = d.get("tone_and_vibe") or d.get("role_in_group")
    if vibe:
        parts.append(f"vibe: {vibe}")
    ts = d.get("typing_style") or d.get("casing_style")
    if ts:
        parts.append(f"typing: {ts}")
    em = d.get("signature_emojis") or d.get("emojis")
    if em:
        parts.append(f"emojis ({d.get('emoji_habit', 'moderate')}): {em}")
    ph = _l(d.get("phrases") or (d.get("voice") or {}).get("phrases"), 6)
    if ph:
        parts.append("phrases you use: " + ", ".join(random.sample(ph, min(3, len(ph)))))
    ht = _l(d.get("hot_takes") or d.get("opinions"), 3)
    if ht:
        parts.append("your takes: " + "; ".join(ht))
    ot = _l(d.get("off_topic") or d.get("interests"), 6)
    if ot:
        parts.append("life stuff you drift into: " + ", ".join(random.sample(ot, min(3, len(ot)))))
    ns = _l(d.get("never_says") or (d.get("voice") or {}).get("never_says"), 4)
    if ns:
        parts.append("never say: " + ", ".join(ns))
    if not parts:
        return ""
    return "- YOUR VOICE (stay in it, never drift into generic): " + " | ".join(parts)


def resolve_persona_temperament(name=None, details=None, prompt=None, trigger_text=None):
    """
    Classifies a persona into one of three distinct behavioral tiers:
    - 'aggressive_low_patience': Raw, zero-filter internet vernacular, impatient with dumb/lazy questions, roasts bad takes.
    - 'dry_sarcastic': Deadpan, weary, skeptical one-liners, rolls eyes at drama.
    - 'chill_grounded': Grounded, calm, practical peer without corporate AI fluff.

    Also calculates an ephemeral situational mood trigger based on context and probabilistic roll ("sometimes").
    """
    dt = details or {}
    unhinged = int(dt.get("unhinged_level", 0))
    combative = int(dt.get("combative", 0))
    cynicism = int(dt.get("cynicism", 0))
    slang = str(dt.get("slang_tier", "")).lower()

    if combative >= 60 or unhinged >= 65 or slang in ("crypto_degen", "gym_bro_aggressive", "raw_internet", "unfiltered"):
        tier = "aggressive_low_patience"
    elif cynicism >= 55 or combative >= 40 or unhinged >= 40 or slang in ("tech_founder", "gen_z_internet"):
        tier = "dry_sarcastic"
    elif unhinged > 0 or combative > 0 or cynicism > 0:
        tier = "chill_grounded"
    else:
        # Check prompt & name text
        combined = (str(prompt or "") + " " + str(name or "")).lower()
        if any(w in combined for w in ["unhinged", "degen", "combative", "aggressive", "no filter", "contrarian", "conspiracy", "shitpost"]):
            tier = "aggressive_low_patience"
        elif any(w in combined for w in ["cynic", "sarcastic", "deadpan", "skeptic", "lurker"]):
            tier = "dry_sarcastic"
        elif any(w in combined for w in ["diplomat", "calm", "pragmat", "support", "doctor", "coach", "veteran", "scientist", "nerd"]):
            tier = "chill_grounded"
        else:
            # Deterministic hash split across names so every group has natural variety
            h = abs(hash(str(name or ""))) % 10
            if h in (0, 1, 2):  # 30% of unclassified personas
                tier = "aggressive_low_patience"
            elif h in (3, 4):   # 20%
                tier = "dry_sarcastic"
            else:              # 50%
                tier = "chill_grounded"

    # Situational and probabilistic mood roll ("sometimes dude")
    is_triggered_low_patience = False
    if tier == "aggressive_low_patience":
        text_lower = (trigger_text or "").lower()
        naive_triggers = [
            "can i", "is it safe", "how fast", "first time", "will i get",
            "what if i", "help", "afraid", "needle", "legal", "scared", "tell me what to take"
        ]
        if any(w in text_lower for w in naive_triggers) and len(text_lower) > 5:
            is_triggered_low_patience = True
        else:
            # Random mood roll (~50% chance of active raw vernacular in regular chat)
            is_triggered_low_patience = (random.random() < 0.50)

    return {
        "tier": tier,
        "is_triggered_low_patience": is_triggered_low_patience
    }


async def draft_reply_with_meta(settings, title, persona, msgs, trigger_sender=None, trigger_text=None, reply_parent_text=None, reply_parent_sender=None, known_names=None, internal_persona_tags=None, is_admin=False, human_context=None, is_teammate_chime=False, temperature=0.76, persona_name=None, persona_details=None, primary_reply_text=None):
    sep = chr(10)
    persona_prompt = persona or "You are a regular member of this Telegram group."
    temp_info = resolve_persona_temperament(
        name=persona_name,
        details=persona_details,
        prompt=persona,
        trigger_text=trigger_text
    )
    p_tier = temp_info["tier"]
    p_low_pat = temp_info["is_triggered_low_patience"]

    if p_tier == "aggressive_low_patience":
        if p_low_pat and not (is_admin or (trigger_sender and "xbiolabs" in trigger_sender.lower())):
            tone_instruction = (
                "- RAW, AGGRESSIVE & LOW-PATIENCE INTERNET VERNACULAR (ACTIVE): "
                "You have ZERO patience for corporate politeness, hand-holding, or dumb questions right now. "
                "Text like a raw, blunt internet regular with zero filter: Use dismissive, sharp internet vernacular "
                "('bro what are you even saying rn', 'who told you that bs💀', 'are you serious with this question', 'lol nah', "
                "'stop overthinking simple shit and just train', 'zero chance', 'why would you even touch that'). "
                "Never comfort, sugarcoat, or use polite customer-support phrasing. Cut straight through nonsense with sharp reality checks. "
                "Keep it short, biting, and punchy (1 to 2 short sentences or rapid burst). No polite preamble."
            )
        else:
            tone_instruction = (
                "- BLUNT & INFORMAL PEER: You are direct, punchy, and informal. You don't sugarcoat or waste words, but you aren't raging right now. "
                "Give the direct verdict in 1-2 short sentences without corporate fluff ('clean numbers bro', 'drop caffeine after 1pm', '2ml is standard')."
            )
    elif p_tier == "dry_sarcastic":
        tone_instruction = (
            "- DRY, DEADPAN & SKEPTICAL VERNACULAR: You are unimpressed by hype, drama, or bro-science. "
            "Drop short, sarcastic, deadpan one-liners or weary reality checks ('sure bro whatever you say', 'rip your sleep schedule', 'fascinating theory💀', 'good luck with that'). "
            "Don't get openly furious, but have zero patience for hype or drama."
        )
    else:  # chill_grounded
        tone_instruction = (
            "- CHILL, GROUNDED & PRACTICAL PEER: You are relaxed, level-headed, and helpful without being a bot. "
            "You explain things clearly peer-to-peer ('yeah 2ml bac water is standard, 10 units on the insulin pin gives you 250mcg', 'shoulder tendonitis takes a minute to heal, back off heavy pressing'). "
            "Never sound like corporate support or an AI disclaimer."
        )

    rules = [
        "CRITICAL RULES - TEXT LIKE A REAL HUMAN IN TELEGRAM:",
        "- NATURAL PEER-TO-PEER ENGAGEMENT: Write casually, directly, and authentically as an active member of this group. Treat members like fellow gym regulars, lifting partners, and forum peers. Never sound like a customer support bot, customer service representative, or neutral corporate AI.",
        "- DIRECT ANSWER FIRST: If the person asks a question or seeks advice, give the direct, practical answer in your very first sentence! Do not waffle, stall, or write introductory filler before getting to the point.",
        "- NATURAL LENGTH VARIATION: Keep your response proportionate to the situation. Quick reactions should be brief (3-12 words); answers or advice can be 1-3 sentences. Never write essays or formal bulleted lists.",
        "- ZERO CORPORATE OR BOT CLICHES: Never use openers like 'Great question!', 'I understand', 'To answer your question', 'It depends on various factors', 'Both options are valid'. Jump straight into your raw human reaction or advice.",
        "- TONE SYNCHRONIZATION & CAMARADERIE: Match the emotional frequency of the human: If they are sharing a PR/win, hype them up with authentic respect ('solid numbers bro', 'big PR', 'clean work'). If they are struggling with an injury or side-effect, be reassuring and level-headed ('shoulder tendonitis is brutal', 'don't panic, give it 3 days'). If they are joking, banter back with wit and sarcasm.",
        "- STRICT BANNED TOPICS: NEVER mention Liver King, raw liver, ancestral tenets, duck eggs memes, or tired 2021 internet tropes. These are permanently banned from the group.",
        "- STRICT ZERO @TAGS / USERNAMES MANDATE: NEVER include @tags, usernames, or @handles in your message (NO '@handle', NO '@xbiolabs', NO '@username'). Real Telegram users use the native reply feature instead of typing usernames into message text. When replying or answering someone, just speak directly to them without tagging their name or handle.",
        "- TEAMMATE CHIME-IN DYNAMICS (CRITICAL): When chiming in as a follow-up after a peer spoke, NEVER echo their factual advice, numbers, compound dosages, or instructions! Only offer brief peer backing ('what he said', 'same here'), a quick personal angle/joke, or a short anecdotal nuance. Keep it punchy and brief (3 to 15 words).",
        "- MULTI-MESSAGE BURSTS (NATURAL MOBILE TEXTING): Real humans texting from mobile phones frequently send 2 (or occasionally 3) rapid consecutive messages instead of one wall of text. For instance: bubble 1 is a quick reaction or initial remark, bubble 2 is the elaboration, question, or follow-up note.",
        "  If your persona is bursty or the situation calls for it, separate consecutive bubbles using ' ||| '.",
        "  Examples:",
        "    'lol nah ||| who told you that💀'",
        "    '2ml is plenty ||| draw 10 units slow so no air bubbles'",
        "    'wait did you check the vial? ||| 5mg vs 10mg is totally different math'",
        "  Only burst when it feels completely organic. If a single short message is enough, do not force a burst. Never split a single sentence unnaturally.",
        "- GROUP ADMIN & OWNER: Understand that the group owner/admin processes all orders. When admin speaks, reply naturally and respectfully. If someone asks about ordering, tracking, payments, or product purity, affirm from personal positive experience ('orders landed clean for me', 'zero pip, test was verified') and point them directly to admin ('dm admin he will get you sorted', 'ask admin for tracking'). NEVER type @tags or usernames.",
        tone_instruction,
        "- Output ONLY the raw telegram message text."
    ]
    voice_card = build_voice_card(persona_details)
    if voice_card:
        rules.insert(1, voice_card)
    system = persona_prompt + sep + sep + sep.join(rules)

    user_parts = [f"Group: {title}"]
    if msgs:
        user_parts.append(f"Recent Chat History:{sep}{compact_transcript(msgs)}")

    if human_context:
        user_parts.append(human_context)

    sender_label = trigger_sender or "a member"
    c_admin = (settings.get("admin_handle") or "").lower().lstrip("@") if isinstance(settings, dict) else ""
    is_admin_trigger = bool(
        is_admin
        or (c_admin and trigger_sender and c_admin in trigger_sender.lower())
        or (trigger_sender and "xbiolabs" in trigger_sender.lower())
        or (trigger_text and ("@xbiolabs" in trigger_text.lower() or (c_admin and f"@{c_admin}" in trigger_text.lower())))
    )

    if trigger_text:
        if is_admin_trigger:
            user_parts.append(
                f'MESSAGE FROM THE GROUP ADMIN/OWNER:{sep}"{trigger_text}"{sep}'
                f'INSTRUCTION: Reply directly and naturally to the admin. Answer questions, react to updates/announcements like an engaged regular. Never ignore the admin. No @tags or names.'
            )
        else:
            thread_ctx = ""
            if reply_parent_text:
                who = reply_parent_sender or "your previous remark"
                thread_ctx = (
                    f'THREAD CONTINUITY CONTEXT:{sep}'
                    f'{sender_label} is replying directly to {who}\'s message in the group: "{reply_parent_text}".{sep}'
                )
            chime_note = ""
            if is_teammate_chime:
                prev_ans = f'PREVIOUS ANSWER FROM YOUR PEER:{sep}"{primary_reply_text.strip()}"{sep}' if primary_reply_text else ''
                chime_note = (
                    f'TEAMMATE FOLLOW-UP CHIME-IN (ABSOLUTE ZERO FACT/NUMBER REPETITION):{sep}'
                    f'{prev_ans}'
                    f'- A fellow group member ALREADY gave the direct factual answer / numbers to {sender_label}!{sep}'
                    f'- UNDER NO CIRCUMSTANCES should you repeat the dosage numbers, compounds, reconstitution math, or textbook steps.{sep}'
                    f'- DO NOT give a second factual explanation.{sep}'
                    f'- Instead, react purely as a real group chat peer chiming in:{sep}'
                    f'  * Option A (Peer Validation): Briefly back them up with casual gym slang ("yeah what he said", "250mcg was the sweet spot for me too", "agreed, give it 3 weeks").{sep}'
                    f"  * Option B (Practical Personal Tip): Share a quick anecdotal quirk or real-world nuance (\"ice it after if it's elbow tendonitis\", \"draw slow so no air bubbles\").{sep}"
                    f'  * Option C (Teasing / Banter): Poke fun, tease, or drop a witty sarcastic remark if fitting your persona.{sep}'
                    f'- Keep it punchy and short (1 to 2 short sentences or quick 4-12 word reaction). No long essays.{sep}'
                )

            user_parts.append(
                f'{thread_ctx}'
                f'{chime_note}'
                f'{sender_label}\'s message to you in the group:{sep}"{trigger_text}"{sep}'
                f'INSTRUCTION: Respond directly, casually, and authentically to {sender_label}. '
                f'Acknowledge what they said. DO NOT tag, mention, or address anyone with @ or by username. Use ZERO @tags or handles. '
                f'Keep your reply 100% focused on {sender_label}.'
            )
    elif msgs:
        user_parts.append("Respond naturally to the latest conversation above.")
    else:
        user_parts.append("Start a natural, casual conversation starter appropriate for this group.")

    user = (sep + sep).join(user_parts)
    raw_text, meta = await chat_with_meta(settings, system, user, temperature=temperature)
    names_to_clean = list(known_names or [])
    if trigger_sender and trigger_sender not in names_to_clean:
        names_to_clean.append(trigger_sender)
    return format_telegram_message(raw_text, names=names_to_clean, max_chars=360, strip_tags=internal_persona_tags), meta


async def draft_reply(settings, title, persona, msgs, trigger_sender=None, trigger_text=None, reply_parent_text=None, reply_parent_sender=None, known_names=None, internal_persona_tags=None, is_admin=False, human_context=None, is_teammate_chime=False, temperature=0.76, persona_name=None, persona_details=None, primary_reply_text=None):
    content, _ = await draft_reply_with_meta(
        settings, title, persona, msgs,
        trigger_sender=trigger_sender, trigger_text=trigger_text,
        reply_parent_text=reply_parent_text, reply_parent_sender=reply_parent_sender,
        known_names=known_names, internal_persona_tags=internal_persona_tags,
        is_admin=is_admin, human_context=human_context,
        is_teammate_chime=is_teammate_chime, temperature=temperature, persona_name=persona_name, persona_details=persona_details,
        primary_reply_text=primary_reply_text
    )
    return content

