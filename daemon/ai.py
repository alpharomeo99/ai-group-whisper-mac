"""AI client: fal.ai OpenAI-compatible router (Claude, GPT, Gemini, Llama, DeepSeek... via one fal key)."""
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
    if frequency_penalty is not None:
        body["frequency_penalty"] = float(frequency_penalty)
    if presence_penalty is not None:
        body["presence_penalty"] = float(presence_penalty)

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
    """Strips robotic name addressing, greeting prefixes, and @mentions from group chat messages.
    Preserves authentic casual interjections like 'Bro, ', 'Honestly, ', 'Yeah, '.
    """
    if not text:
        return ""
    text = text.strip()

    # 1. Strip leading @mentions: '@john what do you think?' -> 'what do you think?'
    text = re.sub(r'^@[\w.-]+[:,]?\s*', '', text)

    # 2. Targeted name stripping if names are provided
    target_names = set()
    if names:
        for n in names:
            if not n:
                continue
            raw = str(n).strip()
            if len(raw) >= 2:
                target_names.add(raw.lower())
                first = raw.split()[0].lower()
                if len(first) >= 2:
                    target_names.add(first)

    for name in target_names:
        esc = re.escape(name)
        # Leading greetings or acknowledgements followed by name: 'Hey Alice, ', 'Thanks Bob, '
        pat_leading = rf'^(?:(?:hey|hi|hello|yo|thanks|thank you|yeah|yes|nah|agreed|true|good point|spot on|same here)\s+)?@?{esc}[,:]\s*'
        text = re.sub(pat_leading, '', text, flags=re.IGNORECASE)
        # Leading bare name: 'Alice, ' or 'Alice: '
        text = re.sub(rf'^@?{esc}[,:]\s*', '', text, flags=re.IGNORECASE)
        # Mid-sentence / end-of-sentence name addressing: 'what do you think, Alice?' -> 'what do you think?'
        text = re.sub(rf',\s*@?{esc}([\.!?]|\s*$)', r'\1', text, flags=re.IGNORECASE)
        # 'Good point Alice' / 'Thanks Alice'
        text = re.sub(rf'\b(?:thanks|thank you)\s+@?{esc}\b', 'thanks', text, flags=re.IGNORECASE)
        text = re.sub(rf'\b(?:good point|spot on)\s+@?{esc}\b', 'good point', text, flags=re.IGNORECASE)
        text = re.sub(rf'\b(?:hey|hi)\s+@?{esc}\b', '', text, flags=re.IGNORECASE)

    # 3. Generic untargeted greeting + name cleaner
    # 'Hey/Hi/Yo/Hello <Word>[,:] <rest>' e.g. 'Hey David, how is it going?'
    m = re.match(r'^(?:hey|hi|hello|yo)\s+@?([a-zA-Z]{2,15})[,:]\s*(.*)$', text, flags=re.IGNORECASE)
    if m:
        target = m.group(1).lower()
        rest = m.group(2).strip()
        if target not in SAFE_INTERJECTIONS and rest:
            text = rest

    # '<Word>[,:] <rest>' where Word is capitalized and not an interjection
    m = re.match(r'^@?([a-zA-Z]{2,15})[,:]\s+(.*)$', text)
    if m:
        target = m.group(1).lower()
        rest = m.group(2).strip()
        if target not in SAFE_INTERJECTIONS and rest:
            text = rest

    # Clean leftover punctuation or double spaces
    text = re.sub(r'^\s*[,:;-]\s*', '', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text


def format_telegram_message(text, names=None, max_chars=360):
    """Enforces natural Telegram message dynamics (casual, authentic phrasing without robotic walls of text).
    Allows natural variation from quick punchy quips (3-10 words) up to detailed 2-3 sentence explanations (under 360 chars).
    Strips robotic name-calling, preserves multi-line quips, cleans quotes/artifacts, and prevents mid-word truncation.
    """
    if not text:
        return ""
    text = text.strip()
    # Strip LLM sender labels like '[Dave]: ' or 'Alice: '
    text = re.sub(r'^(?:\[[^\]]+\]:?|\w+:\s*)\s*', '', text).strip().strip("'\"")

    # Clean out robotic name-calling / salutations
    text = strip_addressing_names(text, names=names)

    # Strip robotic validation prefixes and agreement cliches
    cleaned_prefix = re.sub(
        r'^(?:(?:Makes sense|Agreed|Spot on|For sure|Definitely|Good point|Totally agreed|Fair point|100% facts|100%|True that|Totally)(?:\s+(?:mate|man|bro|dude))?[,.!]?\s+)+',
        '', text, flags=re.IGNORECASE
    )
    # If stripping left a dangling vocative like 'mate. ' or 'bro, '
    cleaned_prefix = re.sub(r'^(?:mate|man|bro|dude)[,.!]?\s*', '', cleaned_prefix, flags=re.IGNORECASE).strip()
    if cleaned_prefix:
        text = cleaned_prefix

    # Strip formal conversation starter phrases
    cleaned_formal = re.sub(
        r'^(?:In my experience|To be honest|From my experience|As a rule of thumb)[,:]\s*',
        '', text, flags=re.IGNORECASE
    ).strip()
    if cleaned_formal:
        text = cleaned_formal

    raw_lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not raw_lines:
        return ""

    clean_lines = []
    for l in raw_lines[:3]:
        # Only strip list markers like '1. ', '1) ', '-', '*', '•', '#' - DO NOT strip bare numbers like '10 units' or '2.5ml'
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

    text = text.strip()
    # Real people texting on mobile rarely end short casual messages with a single period
    if len(text) < 130 and text.endswith('.') and not text.endswith('..') and not text.endswith('etc.'):
        text = text[:-1].rstrip()
    return text


def format_short_telegram_text(text, names=None, max_chars=360):
    """Backward-compatible wrapper for format_telegram_message with natural dynamic length."""
    return format_telegram_message(text, names=names, max_chars=max_chars)


async def draft_reply_with_meta(settings, title, persona, msgs, trigger_sender=None, trigger_text=None, known_names=None, temperature=0.76):
    sep = chr(10)
    persona_prompt = persona or "You are a regular member of this Telegram group."
    rules = [
        "CRITICAL RULES - TEXT LIKE A REAL HUMAN IN TELEGRAM, NEVER AN AI:",
        "- NEVER START WITH BOT-LIKE AGREEMENT OR VALIDATION: Do NOT start messages with 'Makes sense', 'For sure', 'Definitely', 'Agreed', 'Spot on', 'Good point', 'Totally', '100%', or 'Fair point'. Real group members don't validate each other like customer support agents. Jump straight into your thought or question.",
        "- NO SCRIPTED NAME-DROPPING: NEVER address anyone by name (no 'Hey John', 'Dave, ...', or 'Thanks mate'). Telegram's reply bubble visually links the message.",
        "- WRITE CASUALLY IN MOBILE TEXTING STYLE: Write like you're typing quickly with your thumbs on a phone. Use lowercase or relaxed casing, skip periods at the end of short single-sentence messages, use natural abbreviations (tbh, rn, idk, prob, imo, lol), and speak in casual fragments rather than complete academic paragraphs.",
        "- NOT AN ASSISTANT OR LECTURER: You are NOT an AI assistant, advisor, or instructor. Do NOT provide comprehensive lectures, unsolicited safety warnings, disclaimers, or multi-step guides. Give brief personal opinions, reactions, or casual personal experiences.",
        "- NATURAL LENGTH VARIATION: Keep it concise and natural. Quick banter/reactions: 2-8 words ('same here honestly', 'what dose though?'). Typical comments: 1 casual sentence (10-20 words). Occasional experience: 2 short sentences. NEVER write essay paragraphs or structured lists.",
        "- ZERO CORPORATE OR AI JARGON: Never say 'delve', 'crucial', 'testament', 'landscape', 'dynamic', 'furthermore', 'optimal', 'holistic', 'facilitate', 'utilize', 'ensure'.",
        "- STAY CASUALLY GROUNDED: Do NOT invent unrequested stories about vendors, orders, shipping, or drama unless someone directly asked.",
        "- Output ONLY the raw telegram message text."
    ]
    system = persona_prompt + sep + sep + sep.join(rules)

    user_parts = [f"Group: {title}"]
    if msgs:
        user_parts.append(f"Recent Chat History:{sep}{transcript(msgs)}")
    if trigger_text:
        user_parts.append(f'You are reacting or replying directly to this message in the chat (do NOT address the sender by name):{sep}"{trigger_text}"')
    elif msgs:
        user_parts.append("Respond naturally to the latest conversation above (do NOT address anyone by name).")
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
