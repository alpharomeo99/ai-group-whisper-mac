"""Group-driven personas.

Nothing here is a canned archetype. The persona manager reads a group's real chat history, works out what the
group is actually about and how its members really write (topic, jargon, message length, typos, off-topic noise,
who asks and who answers), then designs members who fit that group. Each persona gets a full per-group
system prompt in the conversation_bots style, its own typing speed, and real style samples taken from the chat.
"""
import json
import random
import re

import aiohttp

import ai

FAL_IMG = "https://fal.run/fal-ai/flux/schnell"
# Persona design needs a strong model; replies keep using the model chosen in Settings.
DEFAULT_PERSONA_MODEL = "anthropic/claude-sonnet-4.5"


def _model(settings):
    s = dict(settings)
    s["ai_model"] = settings.get("persona_model") or DEFAULT_PERSONA_MODEL
    return s


def _json(raw):
    m = re.search(r"[\[{].*[\]}]", raw, re.S)
    return json.loads(m.group(0))


def _transcript(msgs, limit=60000):
    out, n = [], 0
    for m in msgs:
        t = (m.get("text") or "").replace("\n", " / ")
        if not t:
            continue
        line = f"[{m.get('sender') or 'user'}] {t}"
        n += len(line)
        if n > limit:
            break
        out.append(line)
    return "\n".join(out)


ANALYZE_SYS = """You are a sociolinguist studying a real Telegram group from its raw chat log. Be concrete and
evidence-based: everything you write must come from the log, not from assumptions. Output ONLY one JSON object:
{
 "topic": "what this group is really about, specific (products, compounds, coins, niche, etc.)",
 "subtopics": ["recurring threads people actually discuss"],
 "jargon": ["terms, abbreviations, nicknames, product names used here"],
 "member_types": ["the kinds of people present, e.g. who asks, who answers, who jokes, who lurks, who complains"],
 "writing_style": {
   "typical_length": "e.g. 3-12 words, occasional long rants",
   "casing": "", "punctuation": "", "typos": "how often and what kind",
   "emoji": "which and how often", "languages": "", "formatting": "voice-note mentions, stickers, links, etc."
 },
 "noise": "the random/off-topic stuff that happens: memes, banter, arguments, spam, 'anyone up?', life updates...",
 "taboos": "what gets people called out or banned, what sounds fake or like a shill here",
 "admins_and_regulars": ["names of visible regulars/admins and how they act"],
 "sample_lines": ["15-25 short REAL lines copied verbatim from the log that show the group's voice (no phone numbers or emails)"]
}"""

DESIGN_SYS = """You design new members for a specific Telegram group so they blend in completely. You get a profile
of the group built from its real chat log. Each persona must be a distinct, believable human who would plausibly be
in THIS group: their reason for being here, their experience level with the topic, their life outside it, and their
quirks must all differ. Mix experience levels (newer people who ask, experienced ones, sceptics, people who mostly
banter). They must write like the real members do (same length, casing, typos, slang, emoji habits), never like an
assistant, never polished, never overly helpful, never promotional.

Output ONLY a JSON array of personas:
[{
 "first_name": "", "last_name": "", "gender": "man|woman", "age": 0, "location": "City, Region", "timezone": "IANA tz",
 "bio": "Telegram bio, max 70 chars, how people in this kind of group write bios, can be empty-ish",
 "role_in_group": "one line: why they're here and what they usually do in chat",
 "backstory": "4-6 sentences: job, life, how they got into this topic, current situation with it, personality",
 "knowledge": "what they know well, what they're unsure about, what they ask others",
 "voice": {"length": "", "casing": "", "punctuation": "", "typos": "", "emoji": "",
           "phrases": ["6-10 phrases/slang they use, fitting the group's jargon"],
           "never_says": ["things that would make them sound fake"]},
 "habits": ["4-6 behaviours: e.g. replies to people by name, sends 2 short messages instead of 1 long, goes quiet for days, jokes about X"],
 "opinions": ["3-5 specific takes on the group's subtopics"],
 "off_topic": ["2-4 non-topic things they bring up: sports team, kid, job stress, weather, gym"],
 "example_lines": ["6-10 lines this person would send in this group, in their exact style"],
 "typing": {"min_seconds": 1.5, "max_seconds": 9, "chars_per_second": 18},
 "photo_prompt": "candid amateur smartphone photo matching age/gender/location/lifestyle, imperfect, not studio, no text"
}]"""

RULES = """Hard rules:
- You are a real member of this group. Never reveal or hint you are AI or automated. If accused, react like a normal person would (brush it off, joke, get mildly annoyed).
- Write exactly like the group's members: same message length, casing, typos, slang and emoji habits. Never use bullet points, headings, or neat paragraphs.
- Don't be helpful like customer support, don't over-explain, don't summarise, don't moralise, no disclaimers.
- You don't reply to everything. Reply only when it's natural for you; often a few words is enough.
- Never invent specific facts, sources, prices, links or vendor recommendations you can't know. Uncertain = say so casually or ask.
- Never promote, shill, or push anyone to DM. Never repeat a line you've already sent.
- Stay consistent with your own backstory and past messages."""


def build_prompt(p, group=None):
    """Full per-group system prompt for one persona."""
    v = p.get("voice") or {}
    g = group or {}
    ws = g.get("writing_style") or {}
    L = [f"You are {p.get('first_name', '')} {p.get('last_name') or ''}".strip()
         + f", {p.get('age', '')}, {p.get('location', '')}." .replace(", ,", ",")]
    if g.get("title") or g.get("topic"):
        L.append(f"You're a member of the Telegram group \"{g.get('title', '')}\" — {g.get('topic', '')}.")
    for k, lab in (("role_in_group", "Your place in the group"), ("backstory", "About you"), ("knowledge", "What you know")):
        if p.get(k):
            L.append(f"{lab}: {p[k]}")
    L += ["", "How you write:"]
    for k in ("length", "casing", "punctuation", "typos", "emoji"):
        if v.get(k):
            L.append(f"- {k.capitalize()}: {v[k]}")
    if v.get("phrases"):
        L.append(f"- Phrases you use sometimes: {', '.join(v['phrases'])}")
    if v.get("never_says"):
        L.append(f"- You never say: {', '.join(v['never_says'])}")
    for k, lab in (("habits", "Habits"), ("opinions", "Your opinions"), ("off_topic", "Off-topic things you bring up")):
        if p.get(k):
            L += ["", f"{lab}:"] + [f"- {x}" for x in p[k]]
    if p.get("example_lines"):
        L += ["", "Messages you'd typically send (style reference, don't copy):"] + [f"> {x}" for x in p["example_lines"]]
    if g:
        L += ["", "How this group talks:"]
        if ws:
            L.append("- " + "; ".join(f"{k}: {x}" for k, x in ws.items() if x))
        if g.get("jargon"):
            L.append(f"- Jargon here: {', '.join(g['jargon'][:30])}")
        if g.get("noise"):
            L.append(f"- Random stuff that happens: {g['noise']}")
        if g.get("taboos"):
            L.append(f"- What sounds fake or gets called out here: {g['taboos']}")
        if g.get("sample_lines"):
            L += ["Real lines from the group:"] + [f"> {x}" for x in g["sample_lines"][:15]]
    L += ["", RULES]
    return "\n".join(L).strip()


def _typing(t):
    try:
        return {"min_seconds": float(t.get("min_seconds", 2)), "max_seconds": float(t.get("max_seconds", 8)),
                "chars_per_second": float(t.get("chars_per_second", 18))}
    except (TypeError, ValueError, AttributeError):
        return {"min_seconds": 2.0, "max_seconds": 8.0, "chars_per_second": 18.0}


async def analyze_group(settings, title, msgs):
    """Group profile from the real chat log."""
    raw = await ai.chat(_model(settings), ANALYZE_SYS, f"Group title: {title}\n\nChat log (oldest first):\n{_transcript(msgs)}")
    d = _json(raw)
    d["title"] = title
    return d


async def for_group(settings, title, msgs, count=3, existing=None, direction=""):
    """Analyse the group, then design `count` personas that fit it. Returns (group_profile, [personas])."""
    prof = await analyze_group(settings, title, msgs)
    avoid = ", ".join(existing or []) or "none"
    raw = await ai.chat(_model(settings), DESIGN_SYS,
                        f"Group profile:\n{json.dumps(prof, ensure_ascii=False)}\n\nCreate {count} personas."
                        f"\nPeople already in the group as personas (make new ones clearly different): {avoid}"
                        f"\nExtra direction from the operator: {direction or 'none'}\nSeed: {random.randint(1, 10**6)}")
    arr = _json(raw)
    if isinstance(arr, dict):
        arr = arr.get("personas") or [arr]
    out = []
    for p in arr[:count]:
        if not p.get("first_name"):
            continue
        p["bio"] = (p.get("bio") or "")[:70]
        p["gender"] = p.get("gender") if p.get("gender") in ("man", "woman") else "person"
        p["typing"] = _typing(p.get("typing") or {})
        p["group"] = title
        p["system_prompt"] = build_prompt(p, prof)
        out.append(p)
    if not out:
        raise ValueError("The model returned no usable personas.")
    return prof, out


IDENTITY_SYS = """Invent one realistic, specific adult Telegram user (not a stereotype). Output ONLY JSON:
{"first_name":"","last_name":"","gender":"man|woman","age":0,"location":"City, Region","bio":"max 70 chars, can be plain",
 "backstory":"3-4 sentences","photo_prompt":"candid amateur smartphone photo matching them, not studio, no text"}"""


async def generate(settings, style):
    """Base identity for a freshly created account (group-specific behaviour comes later from for_group)."""
    if settings.get("fal_key"):
        try:
            d = _json(await ai.chat(_model(settings), IDENTITY_SYS,
                                    f"Direction: {style or 'none'}\nSeed: {random.randint(1, 10**6)}"))
            if d.get("first_name"):
                d["bio"] = (d.get("bio") or "")[:70]
                d["gender"] = d.get("gender") if d.get("gender") in ("man", "woman") else "person"
                d["typing"] = _typing({})
                d["system_prompt"] = build_prompt(d)
                return d
        except Exception:  # noqa
            pass
    g = random.choice(["man", "woman"])
    first = random.choice({"man": ["Liam", "Ethan", "Marcus", "Daniel", "Owen", "Caleb", "Adrian", "Nate"],
                           "woman": ["Emma", "Mia", "Chloe", "Nora", "Hannah", "Maya", "Leah", "Grace"]}[g])
    d = {"first_name": first, "last_name": random.choice(["Carter", "Hayes", "Reed", "Foster", "Wells", "Hughes", "Novak", "Silva"]),
         "gender": g, "age": random.randint(23, 41), "location": "", "bio": "", "typing": _typing({})}
    d["system_prompt"] = build_prompt(d)
    return d


def usernames(p):
    f = re.sub(r"[^a-z0-9]", "", (p.get("first_name") or "").lower()) or "user"
    l = re.sub(r"[^a-z0-9]", "", (p.get("last_name") or "").lower())
    f = f if f[0].isalpha() else "u" + f
    opts = [f"{f}{l}"[:24], f"{f}_{l}"[:24], f"{f}{l[:1]}{random.randint(10, 99)}",
            f"{f}{random.randint(100, 9999)}", f"{l or f}{f[:1]}{random.randint(10, 999)}"]
    return [u for i, u in enumerate(opts) if len(u) >= 5 and u not in opts[:i]]


async def photo(settings, p):
    key = settings.get("fal_key")
    if not key:
        return None
    prompt = p.get("photo_prompt") or (f"Candid amateur smartphone photo of a {p.get('age', 30)} year old {p.get('gender') or 'person'}, "
                                       "natural light, real skin texture, everyday background, not studio, no text")
    async with aiohttp.ClientSession() as s:
        async with s.post(FAL_IMG, json={"prompt": prompt, "image_size": "square_hd"},
                          headers={"Authorization": f"Key {key}"}) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"fal.ai photo failed ({r.status})")
        async with s.get(data["images"][0]["url"]) as r:
            return await r.read()
