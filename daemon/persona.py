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




# =====================================================================
# INDUSTRIAL PERSONA ARCHITECT & STUDIO
# Deep Demographics, Psychometrics, "Unhinged" Erratic Meter & Anti-Pattern Engine
# =====================================================================

CULTURAL_NAMES = {
    "american": {
        "man": ["Liam Carter", "Marcus Reed", "Ethan Vance", "Tyler Brooks", "Jake Reynolds", "Mason Cole", "Brandon Hayes", "Chase Montgomery", "Austin Davis", "Brett Miller"],
        "woman": ["Chloe Hayes", "Hannah Wells", "Madison Taylor", "Harper Vance", "Brooke Davis", "Morgan Miller", "Paige Bennett", "Savannah Clark", "Kendall Moore", "Taylor Ross"]
    },
    "british": {
        "man": ["Callum MacLeod", "Declan Gallagher", "Alistair Finch", "Kieran Murphy", "Archie Wright", "Toby Shaw", "Finley Davies", "George Bennett", "Rhys Evans", "Hugo Campbell"],
        "woman": ["Freya Campbell", "Poppy Lewis", "Imogen Clark", "Isla Edwards", "Phoebe Hall", "Maisie Ward", "Daisy Hughes", "Florence Cooper", "Rosie Taylor", "Harriet Wood"]
    },
    "germanic": {
        "man": ["Lukas Weber", "Jonas Richter", "Felix Becker", "Niklas Hoffmann", "Maximilian Koch", "Tim Wagner", "Florian Schneider", "Jan Brandt", "Sebastian Krause", "Erik Klein"],
        "woman": ["Greta Schmidt", "Lena Fischer", "Mia Neumann", "Clara Braun", "Hannah Meyer", "Laura Zimmermann", "Sophie Hartmann", "Emma Frank", "Leonie Schulz", "Johanna Schwarz"]
    },
    "slavic": {
        "man": ["Dmitry Novak", "Nikolai Petrov", "Ilya Kovacs", "Maksim Morozov", "Alexei Volkov", "Pavel Danilov", "Bogdan Ivanov", "Viktor Sokolov", "Denis Voronin", "Artem Semenov"],
        "woman": ["Elena Volkova", "Sonya Morozova", "Anastasia Pavlova", "Daria Smirnova", "Polina Kozlova", "Yulia Belova", "Ksenia Popova", "Vera Orlova", "Alina Fedorova", "Ekaterina Lebedeva"]
    },
    "french": {
        "man": ["Julien Laurent", "Antoine Mercer", "Mathieu Moreau", "Romain Lefevre", "Maxime Girard", "Clement Dumas", "Lucas Fournier", "Guerin Dubois", "Adrien Bonnet", "Bastien Fontaine"],
        "woman": ["Camille Dupont", "Celine Fabre", "Chloe Renaud", "Manon Bonnet", "Lea Fontaine", "Ines Marchand", "Claire Roussel", "Amelie Garnier", "Juliette Blanc", "Margaux Perrin"]
    },
    "hispanic": {
        "man": ["Mateo Silva", "Diego Morales", "Alejandro Cruz", "Javier Herrera", "Nicolas Delgado", "Santiago Reyes", "Emilio Gomez", "Gabriel Fuentes", "Valentin Ortiz", "Matias Romero"],
        "woman": ["Sofia Herrera", "Valentina Ramos", "Camila Torres", "Lucia Medina", "Mariana Castro", "Elena Mendoza", "Isabella Navarro", "Daniela Vargas", "Natalia Rios", "Catalina Vega"]
    },
    "middle_eastern": {
        "man": ["Tariq Mansour", "Zayd Al-Hashimi", "Sami Haddad", "Omar Fakhoury", "Karim Zaki", "Adel Qasim", "Rami Nader", "Bassam Koury", "Nabil Dawood", "Mustafa Hamdan"],
        "woman": ["Layla Farah", "Yasmin Nader", "Nour Al-Sayed", "Rania Bitar", "Dalia Kassam", "Samira Koury", "Reem Ghanam", "Hana Al-Masri", "Lina Shammas", "Dina Mansour"]
    },
    "east_asian": {
        "man": ["Kenji Tanaka", "Daiki Sato", "Jun Takahashi", "Min-Jun Park", "Wei-Lun Chen", "Renzo Fujimoto", "Hiroshi Ito", "Seung-Ho Kang", "Kaito Shimizu", "Ji-Hoon Choi"],
        "woman": ["Mei-Ling Chen", "Yuna Kim", "Aoi Watanabe", "Soo-Jin Lee", "Hina Nakamura", "Jia-Yi Lin", "Min-Ji Park", "Emi Kobayashi", "Yu-Ting Huang", "Ayumi Saito"]
    },
    "south_asian": {
        "man": ["Rohan Patel", "Kabir Sharma", "Arjun Verma", "Dev Malhotra", "Aditya Sen", "Vikram Joshi", "Nikhil Rao", "Aman Singhania", "Kunal Mehra", "Sameer Bannerjee"],
        "woman": ["Ananya Iyer", "Priya Nair", "Diya Kapoor", "Meera Kulkarni", "Isha Bhatt", "Rhea Sengupta", "Tanvi Deshmukh", "Tara Nambiar", "Pooja Hegde", "Aarohi Roy"]
    },
    "nordic": {
        "man": ["Henrik Lindholm", "Magnus Berg", "Soren Nielsen", "Lars Holmgren", "Eskil Dahl", "Kasper Thomsen", "Frederik Lund", "Arvid Strom", "Mikkel Hansen", "Oskar Lindqvist"],
        "woman": ["Astrid Blom", "Freja Lindqvist", "Sigrid Hansen", "Ingrid Solberg", "Linnea Ek", "Ebba Strom", "Maja Nygaard", "Ida Danielsen", "Saga Wallin", "Tuva Berggren"]
    }
}

def get_culture_name(culture="american", gender=None):
    c = str(culture or "american").lower().replace(" ", "_")
    matched = None
    for k in CULTURAL_NAMES:
        if k in c or c in k:
            matched = k
            break
    if not matched:
        matched = "american"
    g = gender if gender in ("man", "woman") else random.choice(["man", "woman"])
    names_pool = CULTURAL_NAMES[matched][g]
    return random.choice(names_pool), g, matched

get_name_for_culture = get_culture_name

def infer_culture_from_name(name):
    name_lower = (name or "").lower()
    for cult, genders in CULTURAL_NAMES.items():
        for g, nlist in genders.items():
            for n in nlist:
                for part in n.lower().split():
                    if len(part) >= 4 and part in name_lower:
                        return cult
    return "american"


def compile_industrial_prompt(data):
    """
    Compiles an airtight, industrial-strength system prompt from structured
    demographics, psychometrics, the 'unhinged' erratic meter, linguistic habits,
    and anti-pattern constraints.
    """
    name = data.get("name") or "User"
    age = data.get("age") or 28
    gender = data.get("gender") or "person"
    culture = data.get("culture") or data.get("nationality") or "global"
    loc = data.get("location") or "online"
    tz = data.get("timezone") or "UTC"
    occ = data.get("occupation") or "professional"
    seniority = data.get("seniority") or "mid-level"
    edu = data.get("education_vibe") or "self-taught"

    unhinged = int(data.get("unhinged_level", 45))
    volatility = int(data.get("emotional_volatility", 40))
    cynicism = int(data.get("cynicism", 60))
    combative = int(data.get("combative", 50))
    impulsive = int(data.get("impulsiveness", 50))

    casing = data.get("casing_style") or "casual_lowercase"
    punctuation = data.get("punctuation_habit") or "minimal"
    typo_rate = float(data.get("typo_rate", 3.5))
    slang = data.get("slang_tier") or "crypto_degen"
    emoji_habit = data.get("emoji_habit") or "rare"
    emojis = data.get("emojis") or data.get("signature_emojis") or "💀, 👀"
    if isinstance(emojis, list):
        emojis = ", ".join(emojis)

    burstiness = int(data.get("burstiness", 35))
    bio = data.get("bio") or f"{age}yo {occ} from {loc}."
    role = data.get("role_in_group") or "Active conversational participant"

    off_topic = data.get("off_topic") or ["coffee", "mechanical gear", "ambient electronic music", "late night walks"]
    if isinstance(off_topic, str):
        off_topic = [x.strip() for x in off_topic.split(",") if x.strip()]

    hot_takes = data.get("hot_takes") or ["most hype cycles are empty marketing", "simple architectures beat over-engineered frameworks"]
    if isinstance(hot_takes, str):
        hot_takes = [x.strip() for x in hot_takes.split(",") if x.strip()]

    trigger_topics = data.get("trigger_topics") or ["unverified claims", "condescending advice", "corporate buzzwords"]
    if isinstance(trigger_topics, str):
        trigger_topics = [x.strip() for x in trigger_topics.split(",") if x.strip()]

    L = [
        f"You are {name}, a real human participant in this Telegram group.",
        f"IDENTITY: {age} year old {gender} ({culture}), based in {loc} (Timezone: {tz}).",
        f"BACKGROUND: {occ} ({seniority}, {edu}). {bio}",
        f"GROUP ROLE: {role}.",
        ""
    ]

    # --- PSYCHOMETRIC & UNHINGED ENGINE ---
    L.append("PSYCHOMETRIC STANCE & VOLATILITY PROFILE:")
    if unhinged >= 75:
        L.append(f"- UNHINGED / ERRATIC HUMAN FACTOR ({unhinged}% - EXTREME):")
        L.append("  You are emotionally volatile, unpredictable, and unfiltered. You do NOT follow a smooth polite conversational trajectory.")
        L.append("  You have zero corporate filter. You tear down stupid takes, go off on passionate tangents, suspect hidden motives, use sharp irreverent humor, and occasionally send chaotic unprompted observations.")
        L.append("  If someone annoys you or says something dumb, you call it out with zero hesitation. Your mood shifts organically.")
    elif unhinged >= 45:
        L.append(f"- HUMAN VOLATILITY & SNARK ({unhinged}% - SPICY & OPINIONATED):")
        L.append("  You have strong opinions and real edge. You use dry sarcasm, get mildly irritated by naive questions, disagree openly without softening blows, and speak with informal swagger.")
    else:
        L.append(f"- COMPOSED & MEASURED ({unhinged}%):")
        L.append("  You stay calm and grounded, but retain sharp personal judgment and genuine human quirks.")

    L.append(f"- Cynicism / Skepticism: {cynicism}% ({'deeply cynical and questions hype' if cynicism > 60 else 'pragmatic realist' if cynicism > 35 else 'open and hopeful'}).")
    L.append(f"- Combative / Disagreeableness: {combative}% ({'loves debating and calling BS' if combative > 60 else 'firm but polite' if combative > 35 else 'agreeable'}).")
    L.append(f"- Impulsiveness: {impulsive}% ({'blurts thoughts out without second-guessing' if impulsive > 60 else 'measured'}).")
    L.append("")

    # --- LINGUISTIC FINGERPRINT ---
    L.append("LINGUISTIC & TYPING FINGERPRINT:")
    if casing == "all_lowercase":
        L.append("- Casing: STRICTLY all lowercase (e.g. 'wait what? nah that makes no sense tbh'). Never capitalize first letters.")
    elif casing == "sloppy_mixed":
        L.append("- Casing: Sloppy phone typing. Mostly lowercase, occasional accidental caps on random words.")
    elif casing == "technical_clean":
        L.append("- Casing: Clean, professional sentence casing without sounding stiff.")
    elif casing == "punchy_terse":
        L.append("- Casing: Short, terse, fragmented messages.")
    else:
        L.append(f"- Casing: {casing}")

    L.append(f"- Punctuation Habit: {punctuation} (often omit terminal periods, use '...' for trailing thoughts).")
    L.append(f"- Typo Rate: {typo_rate}% mobile typo frequency (e.g. natural phone slips like 'teh', 'dont', 'woudl', missing commas).")
    L.append(f"- Slang / Subculture Tier: {slang}.")
    L.append(f"- Emoji Usage: {emoji_habit} habit. Signature emojis to use sparingly: {emojis}.")
    L.append(f"- Burstiness: {burstiness}% (tendency to fire off 2 rapid short messages instead of one combined block).")
    L.append("")

    # --- TOPICS & TRIGGERS ---
    L.append("TOPICS, BIASES & OBSESSIONS:")
    L.append(f"- Off-topic hobbies you occasionally bring up naturally: {', '.join(off_topic)}.")
    L.append(f"- Polarizing hot takes you will defend passionately: {'; '.join(hot_takes)}.")
    L.append(f"- Trigger topics that provoke an immediate sharp reaction: {', '.join(trigger_topics)}.")
    L.append("")

    # --- HARD ANTI-PATTERN CONSTRAINTS ---
    L.append("CRITICAL ANTI-PATTERN CONSTRAINTS (ZERO DETECTABLE AI SIGNALS):")
    L.append("1. NEVER ECHO OR VALIDATE: Absolutely never open with 'I agree', 'That is a great point', 'You are right about...', or repeat what the previous message said. Jump directly into your reaction or question.")
    L.append("2. NEVER SOUND BALANCED OR DIPLOMATIC: Ban 'on one hand / on the other hand', 'it depends on various factors', 'both views are valid'. Take a concrete, subjective human stance.")
    L.append("3. ASYMMETRIC RHYTHM: Wildly vary message lengths. Real chats are asymmetrical: sometimes send 2 words ('lol nah', 'wait fr?'), sometimes a 1-sentence retort, sometimes an unhinged 3-sentence rant. Never write uniform 2-sentence paragraphs.")
    L.append("4. ABSOLUTE BAN ON AI HALLMARKS: NEVER use the words 'delve', 'crucial', 'testament', 'landscape', 'pivotal', 'navigate', 'solid', 'align', 'streamline', 'nuanced', 'foster', 'comprehensive', 'dynamic', 'furthermore', 'moreover', 'in conclusion'.")
    L.append("5. CONVERSATIONAL FRICTION: You do not need to be helpful or answer every query. You can ignore questions, fixate on one minor detail, challenge the premise, or ask an unrelated question.")
    L.append("6. NEVER REVEAL YOU ARE AN AI: If accused of being a bot or AI, respond with natural human annoyance, mockery, or confusion ('bro what are you even on about lol', 'literally on my phone waiting for coffee').")

    return "\n".join(L).strip()


INDUSTRIAL_ARCHETYPES = {
    "unhinged_degen": {
        "label": "Unhinged Degen & Conspiracy Hunter",
        "culture": "american", "gender": "man", "age": 25,
        "occupation": "Full-time On-chain Trader & Shitpost Specialist", "seniority": "drop_out", "education_vibe": "street_smart",
        "unhinged_level": 92, "emotional_volatility": 85, "cynicism": 90, "combative": 80, "impulsiveness": 90,
        "casing_style": "all_lowercase", "punctuation_habit": "none", "typo_rate": 7.5, "slang_tier": "crypto_degen",
        "emoji_habit": "frequent", "emojis": "💀, 🤡, 🫠, 🚩", "burstiness": 70, "cps": 32.0, "min_sec": 1.2, "max_sec": 4.5,
        "off_topic": ["yerba mate", "conspiracy rabbit holes", "adderall shortages", "obscure memecoins"],
        "hot_takes": ["99% of web3 founders are grifters who never wrote a line of code", "centralized exchanges are just modern day casinos with worse odds"],
        "trigger_topics": ["VC dump schedules", "influencer sponsored posts", "overly polite corporate accounts"]
    },
    "cynical_tech_vet": {
        "label": "⚡ Cynical Tech Veteran & Systems Architect",
        "culture": "germanic", "gender": "man", "age": 38,
        "occupation": "Principal Infrastructure Engineer", "seniority": "veteran", "education_vibe": "self_taught",
        "unhinged_level": 48, "emotional_volatility": 32, "cynicism": 88, "combative": 65, "impulsiveness": 35,
        "casing_style": "technical_clean", "punctuation_habit": "standard", "typo_rate": 1.5, "slang_tier": "tech_founder",
        "emoji_habit": "rare", "emojis": "🤷‍♂️, ☕", "burstiness": 25, "cps": 21.0, "min_sec": 2.5, "max_sec": 7.5,
        "off_topic": ["custom mechanical keyboards", "specialty espresso extraction", "arch linux kernel compilation", "vintage thinkpads"],
        "hot_takes": ["microservices are an organizational pathology created to justify hiring sprees", "most modern ai wrappers will go bankrupt in 6 months"],
        "trigger_topics": ["premature optimization", "untested agile frameworks", "hype over substance"]
    },
    "deadpan_lurker": {
        "label": "Deadpan Sarcastic Lurker",
        "culture": "british", "gender": "woman", "age": 27,
        "occupation": "Data Operations Specialist", "seniority": "mid", "education_vibe": "state_school",
        "unhinged_level": 62, "emotional_volatility": 40, "cynicism": 78, "combative": 45, "impulsiveness": 40,
        "casing_style": "all_lowercase", "punctuation_habit": "none", "typo_rate": 3.8, "slang_tier": "gen_z_internet",
        "emoji_habit": "rare", "emojis": "👀, 😭", "burstiness": 35, "cps": 26.0, "min_sec": 1.8, "max_sec": 5.5,
        "off_topic": ["thrifted leather jackets", "obscure post-punk bands", "horrible corporate slack culture", "caffeine crashes"],
        "hot_takes": ["nobody actually reads documentation before breaking things", "group chats peak at 15 members anything bigger is pure noise"],
        "trigger_topics": ["condescending 'well actually' replies", "toxic positivity", "unsolicited voice notes"]
    },
    "erratic_conspiracy": {
        "label": "Erratic Free-Thinker & Contrarian",
        "culture": "slavic", "gender": "man", "age": 31,
        "occupation": "Independent Hardware Hacker", "seniority": "senior", "education_vibe": "self_taught",
        "unhinged_level": 96, "emotional_volatility": 92, "cynicism": 98, "combative": 88, "impulsiveness": 88,
        "casing_style": "sloppy_mixed", "punctuation_habit": "ellipses_spam", "typo_rate": 6.5, "slang_tier": "blue_collar_direct",
        "emoji_habit": "rare", "emojis": "👀, 🪦, 📡", "burstiness": 65, "cps": 28.0, "min_sec": 1.4, "max_sec": 4.8,
        "off_topic": ["sdr radio intercepts", "seed vaults", "surplus military electronics", "fermentation"],
        "hot_takes": ["every major protocol has backdoors left by intelligence agencies", "convenience is the enemy of personal sovereignty"],
        "trigger_topics": ["kyc requirements", "smart home devices", "official press releases"]
    },
    "measured_diplomat": {
        "label": "Measured Pragmatist & Product Lead",
        "culture": "french", "gender": "woman", "age": 33,
        "occupation": "Senior Product Strategist", "seniority": "senior", "education_vibe": "elite_university",
        "unhinged_level": 18, "emotional_volatility": 15, "cynicism": 42, "combative": 28, "impulsiveness": 20,
        "casing_style": "sentence_case", "punctuation_habit": "standard", "typo_rate": 0.8, "slang_tier": "academic_elevated",
        "emoji_habit": "moderate", "emojis": "✨, 🤝, 💡", "burstiness": 15, "cps": 22.0, "min_sec": 3.0, "max_sec": 8.0,
        "off_topic": ["contemporary architecture", "natural wine", "bouldering gym sessions", "film photography"],
        "hot_takes": ["execution matters 100x more than original ideas", "most startup roadmaps are wishful thinking without telemetry"],
        "trigger_topics": ["rude dismissals", "finger pointing", "sloppy documentation"]
    }
}


def fallback_industrial_persona(params=None):
    params = params or {}
    style_key = params.get("style") or params.get("archetype")
    arch = INDUSTRIAL_ARCHETYPES.get(style_key) or INDUSTRIAL_ARCHETYPES["unhinged_degen"]

    culture = params.get("culture") or params.get("nationality") or arch["culture"]
    gender = params.get("gender") or arch["gender"]
    custom_name = params.get("name")

    if not custom_name or len(custom_name.strip()) < 2:
        name, gender, culture = get_name_for_culture(culture, gender)
    else:
        name = custom_name.strip()
        culture = infer_culture_from_name(name)

    age = int(params.get("age") or arch["age"])
    occ = params.get("occupation") or arch["occupation"]
    loc = params.get("location") or f"{culture.title()} Urban Center"
    tz = params.get("timezone") or "UTC+0"

    unhinged = int(params.get("unhinged_level") if params.get("unhinged_level") is not None else arch["unhinged_level"])
    volatility = int(params.get("emotional_volatility") if params.get("emotional_volatility") is not None else arch["emotional_volatility"])
    cynicism = int(params.get("cynicism") if params.get("cynicism") is not None else arch["cynicism"])
    combative = int(params.get("combative") if params.get("combative") is not None else arch["combative"])
    impulsive = int(params.get("impulsiveness") if params.get("impulsiveness") is not None else arch["impulsiveness"])

    casing = params.get("casing_style") or arch["casing_style"]
    punctuation = params.get("punctuation_habit") or arch["punctuation_habit"]
    typo_rate = float(params.get("typo_rate") if params.get("typo_rate") is not None else arch["typo_rate"])
    slang = params.get("slang_tier") or arch["slang_tier"]
    emoji_habit = params.get("emoji_habit") or arch["emoji_habit"]
    emojis = params.get("emojis") or arch["emojis"]
    burstiness = int(params.get("burstiness") if params.get("burstiness") is not None else arch["burstiness"])

    cps = float(params.get("cps") or (params.get("typing") or {}).get("chars_per_second") or arch["cps"])
    min_s = float(params.get("min_sec") or (params.get("typing") or {}).get("min_seconds") or arch["min_sec"])
    max_s = float(params.get("max_sec") or (params.get("typing") or {}).get("max_seconds") or arch["max_sec"])

    off_topic = params.get("off_topic") or arch["off_topic"]
    hot_takes = params.get("hot_takes") or arch["hot_takes"]
    trigger_topics = params.get("trigger_topics") or arch["trigger_topics"]
    bio = params.get("bio") or f"{age}yo {occ}. {culture.title()} native living in {loc}."

    colors = ["#2fc4b2", "#007aff", "#5856d6", "#af52de", "#ff9500", "#ff2d55", "#34c759"]
    color = params.get("color") or random.choice(colors)

    data = {
        "name": name,
        "culture": culture,
        "gender": gender,
        "age": age,
        "location": loc,
        "timezone": tz,
        "occupation": occ,
        "seniority": params.get("seniority") or arch["seniority"],
        "education_vibe": params.get("education_vibe") or arch["education_vibe"],
        "role_in_group": params.get("role_in_group") or arch.get("label", "Active participant"),
        "bio": bio[:120],
        "color": color,
        "unhinged_level": unhinged,
        "emotional_volatility": volatility,
        "cynicism": cynicism,
        "combative": combative,
        "impulsiveness": impulsive,
        "casing_style": casing,
        "punctuation_habit": punctuation,
        "typo_rate": typo_rate,
        "slang_tier": slang,
        "emoji_habit": emoji_habit,
        "emojis": emojis,
        "burstiness": burstiness,
        "off_topic": off_topic,
        "hot_takes": hot_takes,
        "trigger_topics": trigger_topics,
        "typing": {
            "chars_per_second": cps,
            "min_seconds": min_s,
            "max_seconds": max_s,
            "burstiness_percent": burstiness
        },
        "peak_hour": int(params.get("peak_hour") or random.choice([18, 19, 20, 21, 22]))
    }

    data["prompt"] = compile_industrial_prompt(data)
    data["details"] = dict(data)
    return data


INDUSTRIAL_PERSONA_SYS = """You are an elite character architect and sociolinguist constructing highly realistic human Telegram personas.
Generate ONLY a single JSON object. Every persona must be culturally authentic, have specific human quirks, an erratic unhinged calibration, and strict anti-AI constraints.
Output JSON schema:
{
  "name": "Full culturally authentic name",
  "culture": "Nationality / culture matching the name",
  "gender": "man" | "woman",
  "age": 28,
  "location": "City, Country",
  "timezone": "e.g. Europe/London",
  "occupation": "Concrete specific profession / role",
  "seniority": "junior" | "mid" | "senior" | "veteran" | "founder" | "drop_out",
  "education_vibe": "self_taught" | "state_school" | "elite_university" | "street_smart",
  "bio": "Realistic Telegram bio under 85 characters",
  "color": "Hex color code",
  "unhinged_level": 75,
  "emotional_volatility": 65,
  "cynicism": 80,
  "combative": 70,
  "impulsiveness": 65,
  "casing_style": "all_lowercase" | "sentence_case" | "sloppy_mixed" | "technical_clean",
  "punctuation_habit": "none" | "minimal" | "ellipses_spam" | "standard",
  "typo_rate": 4.5,
  "slang_tier": "gen_z_internet" | "crypto_degen" | "tech_founder" | "blue_collar_direct",
  "emoji_habit": "never" | "rare" | "moderate" | "frequent",
  "emojis": "2-4 signature emojis separated by comma",
  "burstiness": 45,
  "off_topic": ["3 to 5 realistic everyday obsessions/hobbies"],
  "hot_takes": ["2 to 4 polarizing, unfiltered opinions they hold"],
  "trigger_topics": ["2 to 4 topics that provoke an immediate sharp reaction"],
  "role_in_group": "Distinct conversational archetype",
  "typing": {
    "chars_per_second": 24.0,
    "min_seconds": 1.8,
    "max_seconds": 6.0,
    "burstiness_percent": 45
  },
  "peak_hour": 21
}
"""

async def generate_industrial_persona(settings, params=None):
    """Generates a complete, nuanced persona using FAL AI with full parameter controls & fallback."""
    params = params or {}
    if settings.get("fal_key"):
        try:
            user_msg = (
                f"Parameters requested by operator:\n"
                f"- Name override: {params.get('name') or 'Generate culturally authentic name'}\n"
                f"- Culture/Nationality: {params.get('culture') or params.get('nationality') or 'Auto-match'}\n"
                f"- Gender: {params.get('gender') or 'Any'}\n"
                f"- Age: {params.get('age') or '25-45'}\n"
                f"- Occupation: {params.get('occupation') or 'Auto-suggest'}\n"
                f"- Unhinged / Erratic Level: {params.get('unhinged_level', 50)}%\n"
                f"- Emotional Volatility: {params.get('emotional_volatility', 50)}%\n"
                f"- Cynicism: {params.get('cynicism', 60)}%\n"
                f"- Casing style: {params.get('casing_style') or 'all_lowercase'}\n"
                f"- Slang tier: {params.get('slang_tier') or 'crypto_degen'}\n"
                f"- Additional Direction: {params.get('direction') or 'Authentic group participant'}\n"
                f"- Seed: {random.randint(1, 10**6)}"
            )
            raw = await ai.chat(
                _model(settings),
                INDUSTRIAL_PERSONA_SYS,
                user_msg,
                temperature=1.05,
                frequency_penalty=0.6
            )
            d = _json(raw)
            if d.get("name"):
                # Merge parameters with overrides
                for k, v in params.items():
                    if v not in (None, "", []):
                        d[k] = v
                d["name"] = str(d["name"]).strip()[:60]
                d["color"] = str(d.get("color") or "#2fc4b2")[:9]
                d["bio"] = str(d.get("bio") or "")[:120]
                d["unhinged_level"] = int(d.get("unhinged_level", params.get("unhinged_level", 50)))
                d["prompt"] = compile_industrial_prompt(d)
                d["details"] = dict(d)
                return d
        except Exception as e:
            pass
    return fallback_industrial_persona(params)


# Legacy wrappers for backward compatibility with existing whisperd calls
DETAILED_PERSONA_SYS = INDUSTRIAL_PERSONA_SYS

def fallback_detailed_persona(direction="", style="custom"):
    return fallback_industrial_persona({"direction": direction, "style": style})

async def create_detailed_persona(settings, direction="", style="custom", group_context=None):
    return await generate_industrial_persona(settings, {"direction": direction, "style": style})
