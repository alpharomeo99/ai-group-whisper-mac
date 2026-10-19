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
    """Subtly relaxes formal contractions into authentic casual phone texting (drops apostrophes)."""
    if not text:
        return ""
    contractions = [
        (r"\b[Dd]on't\b", "dont"),
        (r"\b[Cc]an't\b", "cant"),
        (r"\b[Ww]on't\b", "wont"),
        (r"\b[Ii]'m\b", "im"),
        (r"\b[Yy]ou're\b", "youre"),
        (r"\b[Tt]hat's\b", "thats"),
        (r"\b[Ii]t's\b", "its"),
        (r"\b[Ww]e're\b", "were"),
        (r"\b[Dd]idn't\b", "didnt"),
        (r"\b[Cc]ouldn't\b", "couldnt"),
        (r"\b[Ww]ouldn't\b", "wouldnt"),
    ]
    for pat, rep in contractions:
        text = re.sub(pat, rep, text)
    return text


def format_telegram_message(text, names=None, max_chars=360):
    """Enforces natural Telegram message dynamics (casual, authentic phrasing without robotic walls of text).
    Allows natural variation from quick punchy quips (3-10 words) up to detailed 2-3 sentence explanations (under 360 chars).
    Strips robotic name-calling, preserves multi-line quips, cleans quotes/artifacts, and prevents mid-word truncation.
    """
    if not text:
        return ""
    text = text.strip()
    text = naturalize_mobile_text(text)
    # Strip LLM sender labels like '[Dave]: ' or 'Alice: '
    text = re.sub(r'^(?:\[[^\]]+\]:?|\w+:\s*)\s*', '', text).strip().strip("'\"")

    # Clean out robotic name-calling / salutations
    text = strip_addressing_names(text, names=names)

    # Strip robotic validation prefixes and agreement cliches (preserve natural 'bro' if not agreement phrase)
    has_agreement = re.search(r'^(?:Makes sense|Agreed|Spot on|For sure|Definitely|Good point|Totally agreed|Fair point|100% facts|100%|True that|Totally)\b', text, flags=re.IGNORECASE)
    if has_agreement:
        text = re.sub(
            r'^(?:(?:Makes sense|Agreed|Spot on|For sure|Definitely|Good point|Totally agreed|Fair point|100% facts|100%|True that|Totally)(?:\s+(?:mate|man|bro|dude))?[,.!]?\s+)+',
            '', text, flags=re.IGNORECASE
        )
        text = re.sub(r'^(?:mate|man|bro|dude)[,.!]?\s*', '', text, flags=re.IGNORECASE).strip()

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
        "- WILDLY ASYMMETRICAL LENGTH DIVERSITY: Real humans NEVER type uniform-length messages! Dynamically adapt length to context:",
        "  * Quick banter / reactions: Ultra-short 1 to 5 words ('lol nah', 'fr tho💀', 'wait what?', 'bro😭', 'same here honestly', 'legend').",
        "  * Casual takes / questions: 5 to 14 words ('how was the fatigue on leg day?', 'dropped caffeine after 1pm and sleep is way better').",
        "  * Direct help / explanations: 18 to 45 words ('if it\'s a 5mg vial and you put in 2ml bac water, every 10 units on an insulin pin is 250mcg. super straightforward once you do it once').",
        "  * NEVER write uniform paragraphs or essay lists!",
        "- NEVER START WITH BOT-LIKE AGREEMENT OR VALIDATION: Do NOT start messages with 'Makes sense', 'For sure', 'Definitely', 'Agreed', 'Spot on', 'Good point', 'Totally', '100%', or 'Fair point'. Jump straight into your reaction, question, or thought.",
        "- NATURAL EMOJIS & EXPRESSION: Use emojis naturally (💀, 😂, 😭, 🔥, 👀, 💪, 🤷‍♂️, 🤝) matching your persona. Real chatters react with emojis!",
        "- NATURAL TELEGRAM TAGS & REPLIES: Never use email-style greetings (no 'Hey Dave, I hope you are well'). DO use natural Telegram @mentions when addressing or bantering with a peer (e.g. '@marcus how was leg day?', 'ask @leila she knows the math', 'lmao @dave 💀'). Telegram reply quotes link the conversation.",
        "- WRITE CASUALLY IN MOBILE TEXTING STYLE: Type like you are on a phone with thumbs. Skip apostrophes on common contractions (dont, cant, wont, im, youre, thats, its), use casual lowercase or relaxed casing, skip trailing periods, and use natural abbreviations (rn, tbh, idk, ngl, prob, def, kinda, lol, haha).",
        "- HELPING COMMUNITY MEMBERS: When someone asks for help (reconstitution, dosing math, gym splits, joint recovery, nootropics, wellness), reply helpfully, casually, and directly as a knowledgeable friend or gym buddy. Give clear practical tips without sounding like a medical textbook.",
        "- CASUAL BANTER & UNSERIOUS JOKING: It's a casual group chat. Joke around, banter, tease lightly, share funny observations, and keep things relaxed and unserious.",
        "- BROAD DOMAIN COMFORT: Speak naturally about peptides, nootropics (Semax, Selank, Bromantane), wellness (sleep, sauna, magnesium, electrolytes), and general fitness (lifting routines, hypertrophy, cutting/bulking).",
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
