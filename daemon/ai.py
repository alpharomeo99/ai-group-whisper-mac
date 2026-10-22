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


def _clean_single_telegram_bubble(text, names=None, max_chars=360):
    """Sanitizes a single message bubble: strips labels, salutations, markdown lists, and caps length."""
    if not text:
        return ""
    text = text.strip().strip("'\"")
    # Strip LLM sender labels like '[Dave]: ' or 'Alice: '
    text = re.sub(r'^(?:\[[^\]]+\]:?|\w+:\s*)\s*', '', text).strip()

    # Clean out formal email-style salutations (e.g. 'Hey Dave, ' -> '')
    text = strip_addressing_names(text, names=names)

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


def format_telegram_message(text, names=None, max_chars=360):
    """Cleans LLM formatting artifacts from generated Telegram messages while preserving natural grammar, tone, and casing.
    Preserves multi-bubble burst markers (' ||| ', '[SPLIT]', etc.) by formatting each bubble independently.
    """
    if not text:
        return ""
    text = text.strip()
    for sep in (" ||| ", "|||", "[SPLIT]", "\n---\n", "\n===SPLIT===\n"):
        if sep in text:
            parts = [_clean_single_telegram_bubble(p, names=names, max_chars=max_chars) for p in text.split(sep)]
            parts = [p for p in parts if p]
            return " ||| ".join(parts[:3])
    return _clean_single_telegram_bubble(text, names=names, max_chars=max_chars)


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


async def draft_reply_with_meta(settings, title, persona, msgs, trigger_sender=None, trigger_text=None, known_names=None, temperature=0.76):
    sep = chr(10)
    persona_prompt = persona or "You are a regular member of this Telegram group."
    rules = [
        "CRITICAL RULES - TEXT LIKE A REAL HUMAN IN TELEGRAM:",
        "- NATURAL CONVERSATIONAL TONE: Write casually, directly, and authentically according to your persona.",
        "- NATURAL LENGTH VARIATION: Keep your response proportionate to the situation. Quick reactions should be brief; explanations or answers can take a few sentences. Do not force every message into a rigid template.",
        "- NO CORPORATE OR BOT CLICHES: Jump straight into your point without starting with hollow phrases or generic validation.",
        "- AUTHENTIC EMOJIS & REPLIES: Use emojis naturally where fitting. Use Telegram reply quotes and @mentions when directly addressing a peer.",
        "- MULTI-MESSAGE BURSTS (NATURAL MOBILE TEXTING): Real humans texting from mobile phones frequently send 2 (or occasionally 3) rapid consecutive messages instead of one wall of text. For instance: bubble 1 is a quick reaction or initial remark, bubble 2 is the elaboration, question, or follow-up note.",
        "  If your persona is bursty or the situation calls for it, separate consecutive bubbles using ' ||| '.",
        "  Examples:",
        "    'lol nah ||| who told you that💀'",
        "    '2ml is plenty ||| draw 10 units slow so no air bubbles'",
        "    'wait did you check the vial? ||| 5mg vs 10mg is totally different math'",
        "  Only burst when it feels completely organic. If a single short message is enough, do not force a burst. Never split a single sentence unnaturally.",
        "- GROUP ADMIN & OWNER (@xbiolabs): Understand that @xbiolabs is the group admin, owner, and the one processing all orders. If someone is new to the group, asks where or how to order, asks about order tracking, payments, or processing, or if you or someone else has an admin question, naturally point them to or mention @xbiolabs (e.g. 'dm @xbiolabs he handles processing', 'ask @xbiolabs', '@xbiolabs can confirm that', 'pinging @xbiolabs').",
        "- GROUNDED & HELPFUL: Give straightforward, practical advice or banter. Do not invent unprompted stories or fabrications.",
        "- Output ONLY the raw telegram message text."
    ]
    system = persona_prompt + sep + sep + sep.join(rules)

    user_parts = [f"Group: {title}"]
    if msgs:
        user_parts.append(f"Recent Chat History:{sep}{transcript(msgs)}")
    if trigger_text:
        user_parts.append(f'You are reacting or replying directly to this message in the chat:{sep}"{trigger_text}"')
    elif msgs:
        user_parts.append("Respond naturally to the latest conversation above.")
    else:
        user_parts.append("Start a natural, casual conversation starter appropriate for this group.")

    user = (sep + sep).join(user_parts)
    raw_text, meta = await chat_with_meta(settings, system, user, temperature=temperature)
    names_to_clean = list(known_names or [])
    if trigger_sender and trigger_sender not in names_to_clean:
        names_to_clean.append(trigger_sender)
    return format_telegram_message(raw_text, names=names_to_clean, max_chars=360), meta


async def draft_reply(settings, title, persona, msgs, trigger_sender=None, trigger_text=None, known_names=None, temperature=0.76):
    content, _ = await draft_reply_with_meta(
        settings, title, persona, msgs,
        trigger_sender=trigger_sender, trigger_text=trigger_text,
        known_names=known_names, temperature=temperature
    )
    return content
