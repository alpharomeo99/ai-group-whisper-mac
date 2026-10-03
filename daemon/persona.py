"""Persona for new accounts: name, bio, username and AI photo, applied through Telethon."""
import json
import random
import re

import aiohttp

import ai

FIRST = ["Liam", "Noah", "Ethan", "Lucas", "Mason", "Emma", "Olivia", "Ava", "Mia", "Sophia", "Chloe", "Leo", "Nora", "Ella", "Ryan", "Zoe"]
LAST = ["Carter", "Brooks", "Hayes", "Reed", "Foster", "Bennett", "Parker", "Wells", "Morgan", "Hughes", "Cole", "Ward"]
BIOS = ["Coffee, code and good conversations.", "Always learning something new.", "Traveler. Reader. Night owl.", "Here for the good groups."]
FAL_IMG = "https://fal.run/fal-ai/flux/schnell"


def _fallback():
    f, l = random.choice(FIRST), random.choice(LAST)
    return {"first_name": f, "last_name": l, "bio": random.choice(BIOS), "gender": "person"}


async def generate(settings, style):
    """Return {first_name, last_name, bio, gender}. Falls back to a template without fal.ai."""
    if not settings.get("fal_key"):
        return _fallback()
    try:
        raw = await ai.chat(settings,
                            "You invent realistic everyday Telegram user personas. Reply with only JSON: "
                            '{"first_name":"","last_name":"","bio":"","gender":"man|woman"}. Bio under 70 characters, no emojis, no hashtags.',
                            f"Persona style: {style or 'friendly, ordinary adult, English speaking'}")
        d = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        if d.get("first_name"):
            d["bio"] = (d.get("bio") or "")[:70]
            return d
    except Exception:  # noqa
        pass
    return _fallback()


def usernames(p):
    base = re.sub(r"[^a-z0-9]", "", (p["first_name"] + (p.get("last_name") or "")).lower()) or "user"
    base = base if base[0].isalpha() else "u" + base
    return [f"{base[:20]}{random.randint(10, 9999)}" for _ in range(4)]


async def photo(settings, p):
    """AI headshot bytes via fal.ai, or None."""
    key = settings.get("fal_key")
    if not key:
        return None
    prompt = (f"Casual smartphone selfie photo of a {p.get('gender') or 'person'} named {p['first_name']}, "
              "natural light, realistic, everyday look, head and shoulders, no text")
    async with aiohttp.ClientSession() as s:
        async with s.post(FAL_IMG, json={"prompt": prompt, "image_size": "square"},
                          headers={"Authorization": f"Key {key}"}) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"fal.ai photo failed ({r.status})")
        async with s.get(data["images"][0]["url"]) as r:
            return await r.read()
