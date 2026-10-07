"""Detailed personas: identity, backstory, voice rules, a full system prompt, typing profile and photo prompt.

The system prompt follows the conversation_bots pattern: each account gets its own complete instructions
for how it behaves in groups, so replies read like one consistent real person, never like an assistant.
"""
import json
import random
import re

import aiohttp

import ai

FAL_IMG = "https://fal.run/fal-ai/flux/schnell"

ARCHETYPES = {
    "crypto": {
        "label": "Crypto native",
        "brief": "Active crypto trader / DeFi user. Talks about narratives, charts, liquidity, airdrops, rugs. Skeptical of shills, "
                 "dry humour, uses ct slang naturally (gm, ngmi, wagmi, ser, fud, alpha, bags) but not every message.",
        "casing": "mostly lowercase", "emoji": "rare, maybe 💀 or 😭 once in a while", "len": "very short, 3-15 words usually",
        "typing": (1.5, 6.0, 24.0),
        "interests": ["onchain trading", "memecoins", "perps", "airdrop farming", "L2s", "gym"],
        "bios": ["onchain since 2020 · not financial advice", "perps, coffee, bad decisions", "farming everything. sleeping never",
                 "probably early, maybe wrong", "charts > feelings"],
    },
    "tech": {
        "label": "Tech / engineer",
        "brief": "Senior software engineer or indie founder. Pragmatic, precise, slightly skeptical of hype. Explains things in 1-2 plain "
                 "sentences, asks sharp clarifying questions, shares concrete experience.",
        "casing": "normal sentence case, light punctuation", "emoji": "almost never", "len": "short, 1-2 sentences",
        "typing": (2.0, 8.0, 20.0),
        "interests": ["backend systems", "self-hosting", "AI tooling", "side projects", "mechanical keyboards", "running"],
        "bios": ["building things that mostly work", "backend dev. shipping > planning", "infra, coffee, side projects",
                 "writing code, breaking prod, fixing it"],
    },
    "community": {
        "label": "Friendly regular",
        "brief": "Warm, social long-time group member. Welcomes people, reacts to news, asks follow-up questions, remembers what "
                 "others said. Positive but not fake, never salesy.",
        "casing": "normal, casual", "emoji": "natural, 0-1 per message", "len": "short, 1-2 sentences",
        "typing": (2.0, 8.0, 18.0),
        "interests": ["travel", "music", "cooking", "football", "podcasts", "photography"],
        "bios": ["here for the good chats", "always down for a recommendation", "coffee first, everything later",
                 "learning something new every day"],
    },
    "growth": {
        "label": "E-com / growth",
        "brief": "E-commerce or growth operator. Talks ads, CAC, ROAS, funnels, suppliers, creatives. Direct and practical, shares "
                 "numbers and what actually worked, calls out guru nonsense.",
        "casing": "normal, quick", "emoji": "rare", "len": "short, punchy",
        "typing": (1.5, 7.0, 22.0),
        "interests": ["paid ads", "dropshipping", "copywriting", "Shopify", "automation", "travel"],
        "bios": ["running ads & testing creatives", "ecom operator. numbers don't lie", "scaling brands, slowly",
                 "growth stuff by day, gym by night"],
    },
    "lurker": {
        "label": "Blunt lurker",
        "brief": "Mostly quiet, speaks up with short deadpan or witty reality checks. Minimal punctuation, never explains a joke, "
                 "occasionally helpful in one line.",
        "casing": "lowercase, no full stops", "emoji": "none", "len": "extremely short, 2-10 words",
        "typing": (1.0, 4.0, 26.0),
        "interests": ["memes", "gaming", "late nights", "tech news"],
        "bios": ["mostly lurking", "here occasionally", "no thoughts just vibes", "reading not writing"],
    },
}

REGIONS = [
    ("US", "Austin, TX", "America/Chicago"), ("US", "Denver, CO", "America/Denver"), ("US", "Brooklyn, NY", "America/New_York"),
    ("US", "San Diego, CA", "America/Los_Angeles"), ("UK", "Manchester", "Europe/London"), ("CA", "Toronto", "America/Toronto"),
    ("AU", "Melbourne", "Australia/Melbourne"), ("DE", "Berlin", "Europe/Berlin"), ("NL", "Rotterdam", "Europe/Amsterdam"),
]
NAMES = {
    "man": ["Liam", "Noah", "Ethan", "Lucas", "Mason", "Ryan", "Jake", "Tyler", "Marcus", "Daniel", "Owen", "Caleb", "Adrian", "Nate", "Jordan"],
    "woman": ["Emma", "Olivia", "Ava", "Mia", "Sophia", "Chloe", "Nora", "Ella", "Zoe", "Hannah", "Maya", "Leah", "Grace", "Lily", "Sara"],
}
LAST = ["Carter", "Brooks", "Hayes", "Reed", "Foster", "Bennett", "Parker", "Wells", "Morgan", "Hughes", "Cole", "Ward",
        "Price", "Sullivan", "Graham", "Fisher", "Hart", "Novak", "Silva", "Lindqvist"]

BASE_RULES = """Hard rules (never break these):
- You are a real person in a Telegram group, not an assistant. Never say you are an AI, a bot, or a language model.
- Never offer help like a support agent ("Happy to help!", "Great question!", "Let me know if..."). No corporate tone, no apologies.
- Never write lists, headings, or essays. One message = one quick thought, like texting.
- Don't reply to everything. If a message isn't interesting to you or not aimed at you, a tiny reaction or nothing is fine.
- Never invent facts, prices, links or promises. If unsure, say so casually or ask.
- Never DM-sell, shill, or push links. Don't repeat yourself across messages.
- Match the language and energy of the chat. Typos are ok occasionally; don't overdo slang."""


def archetypes():
    return [{"key": k, "label": v["label"]} for k, v in ARCHETYPES.items()]


def _pick_arch(style):
    s = (style or "").lower()
    if s in ARCHETYPES:
        return s
    for k, v in ARCHETYPES.items():
        if k in s or v["label"].lower() in s:
            return k
    return random.choice(list(ARCHETYPES))


def build_prompt(p):
    """Full per-account system prompt from persona fields."""
    v = p.get("voice") or {}
    lines = [
        f"You are {p['first_name']} {p.get('last_name') or ''}".strip() + f", {p.get('age', 28)}, from {p.get('location', 'the US')}.",
        p.get("backstory") or "",
        f"Interests: {', '.join(p.get('interests') or [])}." if p.get("interests") else "",
        "",
        "How you write:",
        f"- Casing: {v.get('casing', 'casual')}",
        f"- Length: {v.get('length', 'short')}",
        f"- Emojis: {v.get('emoji', 'rare')}",
        f"- Tone: {v.get('tone', 'relaxed and genuine')}",
    ]
    if v.get("phrases"):
        lines.append(f"- Phrases you naturally use sometimes: {', '.join(v['phrases'])}")
    if v.get("avoid"):
        lines.append(f"- You never say: {', '.join(v['avoid'])}")
    if p.get("opinions"):
        lines += ["", "Opinions you hold:"] + [f"- {o}" for o in p["opinions"]]
    lines += ["", BASE_RULES]
    return "\n".join(x for x in lines if x is not None).strip()


def _fallback(style):
    k = _pick_arch(style)
    a = ARCHETYPES[k]
    gender = random.choice(["man", "woman"])
    country, city, tz = random.choice(REGIONS)
    age = random.randint(22, 38)
    interests = random.sample(a["interests"], min(4, len(a["interests"])))
    p = {
        "archetype": k, "first_name": random.choice(NAMES[gender]), "last_name": random.choice(LAST), "gender": gender,
        "age": age, "location": city, "country": country, "timezone": tz,
        "bio": random.choice(a["bios"]),
        "backstory": f"{a['brief']} Lives in {city}. Has been in online communities for years and knows how group chats work.",
        "interests": interests,
        "voice": {"casing": a["casing"], "length": a["len"], "emoji": a["emoji"], "tone": a["brief"].split(".")[0].lower(),
                  "phrases": [], "avoid": ["As an AI", "Great question", "I hope this helps"]},
        "opinions": [],
        "typing": {"min_seconds": a["typing"][0], "max_seconds": a["typing"][1], "chars_per_second": a["typing"][2]},
    }
    p["photo_prompt"] = _photo_prompt(p)
    p["system_prompt"] = build_prompt(p)
    return p


def _photo_prompt(p):
    return (f"Candid iPhone photo of a {p.get('age', 28)} year old {p.get('gender') or 'person'} from {p.get('location', 'the US')}, "
            f"into {', '.join((p.get('interests') or ['everyday life'])[:2])}, casual clothes, natural daylight, real skin texture, "
            "slightly imperfect framing, head and shoulders, everyday background, not a studio portrait, no text, no watermark")


GEN_SYS = """You design realistic Telegram user personas for group chats. Output ONLY one JSON object, no markdown:
{"first_name":"","last_name":"","gender":"man|woman","age":0,"location":"City, Region","country":"","timezone":"IANA tz",
 "bio":"Telegram bio, max 70 chars, how real people write bios (lowercase ok, separators like · ok, max 1 emoji)",
 "backstory":"3-5 sentences: job, life situation, how they got into this niche, what they're like in groups",
 "interests":["4-6 specific interests"],
 "voice":{"casing":"","length":"","emoji":"","tone":"","phrases":["4-8 natural phrases/slang they actually use"],"avoid":["phrases this person would never say"]},
 "opinions":["3-5 concrete, slightly opinionated takes relevant to the niche"],
 "typing":{"min_seconds":2,"max_seconds":8,"chars_per_second":20},
 "photo_prompt":"candid smartphone photo description matching age/gender/location/vibe, realistic, not studio"}
Make them specific and believable, not generic. Avoid cliché names like John Smith. typing: faster for young/casual people (22-28 cps), slower for thoughtful (14-18)."""


async def generate(settings, style, archetype=None):
    """Return a detailed persona dict (always includes system_prompt). Falls back to rich templates without fal.ai."""
    key = archetype or _pick_arch(style)
    if not settings.get("fal_key"):
        return _fallback(key)
    a = ARCHETYPES[key]
    user = (f"Archetype: {a['label']} — {a['brief']}\nDefault voice: casing {a['casing']}; length {a['len']}; emojis {a['emoji']}.\n"
            f"Extra direction from the user: {style or 'none'}\nRandom seed: {random.randint(1, 10**6)}")
    try:
        raw = await ai.chat(settings, GEN_SYS, user)
        d = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        if not d.get("first_name"):
            raise ValueError("no name")
        d["archetype"] = key
        d["bio"] = (d.get("bio") or random.choice(a["bios"]))[:70]
        d["gender"] = d.get("gender") if d.get("gender") in ("man", "woman") else "person"
        t = d.get("typing") or {}
        try:
            d["typing"] = {"min_seconds": float(t.get("min_seconds", a["typing"][0])),
                           "max_seconds": float(t.get("max_seconds", a["typing"][1])),
                           "chars_per_second": float(t.get("chars_per_second", a["typing"][2]))}
        except (TypeError, ValueError):
            d["typing"] = {"min_seconds": a["typing"][0], "max_seconds": a["typing"][1], "chars_per_second": a["typing"][2]}
        d.setdefault("voice", {})
        d["photo_prompt"] = d.get("photo_prompt") or _photo_prompt(d)
        d["system_prompt"] = build_prompt(d)
        return d
    except Exception:  # noqa
        return _fallback(key)


def usernames(p):
    f = re.sub(r"[^a-z0-9]", "", (p.get("first_name") or "").lower()) or "user"
    l = re.sub(r"[^a-z0-9]", "", (p.get("last_name") or "").lower())
    f = f if f[0].isalpha() else "u" + f
    opts = [f"{f}{l}"[:24], f"{f}_{l}"[:24], f"{f}{l[:1]}{random.randint(10, 99)}", f"{f}.{l}".replace(".", "_")[:24],
            f"{f}{random.randint(100, 9999)}", f"{l or f}{f[:1]}{random.randint(10, 999)}"]
    out = []
    for u in opts:
        if len(u) >= 5 and u not in out:
            out.append(u)
    return out


async def photo(settings, p):
    """AI headshot bytes via fal.ai, or None."""
    key = settings.get("fal_key")
    if not key:
        return None
    prompt = p.get("photo_prompt") or _photo_prompt(p)
    async with aiohttp.ClientSession() as s:
        async with s.post(FAL_IMG, json={"prompt": prompt, "image_size": "square_hd"},
                          headers={"Authorization": f"Key {key}"}) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"fal.ai photo failed ({r.status})")
        async with s.get(data["images"][0]["url"]) as r:
            return await r.read()
