"""Demographic profiling and knowledge boundary enforcement for personas.

Infers gender, age bracket, conversational interests, and realistic knowledge
boundaries from persona names, bios, profile details, and downloaded avatars.
Prevents immersion breaks (e.g. 20-something female personas talking like 1970s
bodybuilding historians or quoting Frank Zane).
"""
import json
import logging
import os
import re

log = logging.getLogger("whisperd.demographics")

FEMALE_FIRST_NAMES = {
    'kami', 'leila', 'sophie', 'chloe', 'emma', 'sarah', 'olivia', 'ava', 'mia', 'isabella',
    'charlotte', 'amelia', 'emily', 'ella', 'sofia', 'camila', 'aria', 'scarlett', 'victoria',
    'madison', 'luna', 'grace', 'penelope', 'layla', 'zoey', 'nora', 'lily', 'hannah', 'natalie',
    'zoe', 'leah', 'hazel', 'violet', 'aurora', 'savannah', 'audrey', 'claire', 'lucy', 'anna',
    'maya', 'elena', 'alice', 'ruby', 'eva', 'hailey', 'quinn', 'ivy', 'piper', 'alexa', 'julia',
    'kaylee', 'elizabeth', 'jessica', 'ashley', 'amanda', 'brittany', 'megan', 'stephanie', 'nicole',
    'samantha', 'katherine', 'christine', 'rachel', 'lauren', 'amber', 'danielle', 'courtney',
    'vanessa', 'rebecca', 'melissa', 'tiffany', 'chelsea', 'erica', 'kimberly', 'kelly', 'kristen',
    'heather', 'brooke', 'shannon', 'paige', 'katelyn', 'laura', 'monica', 'whitney', 'april',
    'cassie', 'alyssa', 'tori', 'jenna', 'kylie', 'sierra', 'bailey', 'morgan', 'taylor', 'jordan',
    'casey', 'jamie', 'alana', 'brianna', 'talia', 'selena', 'nadia', 'yasmin', 'farah', 'dina',
    'zara', 'amira', 'soraya', 'mona', 'rania', 'salma', 'mariam', 'nour'
}

MALE_FIRST_NAMES = {
    'dorian', 'marcus', 'viktor', 'dave', 'mike', 'chad', 'alex', 'john', 'dan', 'steve', 'matt',
    'chris', 'josh', 'ryan', 'nick', 'tyler', 'brandon', 'jake', 'kyle', 'ben', 'luke', 'sam',
    'tom', 'eric', 'adam', 'zack', 'sean', 'derek', 'greg', 'justin', 'khalid', 'tariq', 'omar',
    'firas', 'youssef', 'karim', 'hamza', 'ali', 'hassan', 'mustafa', 'ziad', 'nasser', 'walid'
}

FEMALE_BIO_MARKERS = [
    r'\b(she/her|she|her|girl|gal|girly|miss|queen|babe|bikini|pilates|glutes|model|barbie)\b'
]

MALE_BIO_MARKERS = [
    r'\b(he/him|he|his|him|guy|dude|bro|man|father|dad|powerlifter|bodybuilder|mass monster)\b'
]


def infer_demographics(name=None, bio=None, details=None, prompt=None, avatar_path=None):
    """Resolves grounded demographic attributes and knowledge boundaries for a persona."""
    dt = details or {}
    if isinstance(dt, str):
        try:
            dt = json.loads(dt)
        except Exception:
            dt = {}

    if dt.get("demographics") and isinstance(dt["demographics"], dict):
        d = dt["demographics"]
        if d.get("gender") and d.get("age_bracket"):
            return _format_demographic_card(d.get("gender"), d.get("age_bracket"), d.get("vibe"))

    full_name = str(name or "").strip()
    first_name = re.sub(r"[^a-zA-Z]", "", full_name.split()[0].lower()) if full_name else ""

    bio_text = str(bio or "").lower()
    prompt_text = str(prompt or "").lower()
    combined_text = f"{full_name} {bio_text} {prompt_text}"

    gender = "neutral"

    if first_name in FEMALE_FIRST_NAMES:
        gender = "female"
    elif first_name in MALE_FIRST_NAMES:
        gender = "male"
    else:
        for pat in FEMALE_BIO_MARKERS:
            if re.search(pat, combined_text, re.I):
                gender = "female"
                break
        if gender == "neutral":
            for pat in MALE_BIO_MARKERS:
                if re.search(pat, combined_text, re.I):
                    gender = "male"
                    break

    age_bracket = dt.get("age_bracket") or dt.get("age") or "20s"
    if str(age_bracket).isdigit():
        age_num = int(age_bracket)
        if age_num < 30:
            age_bracket = "20s"
        elif age_num < 40:
            age_bracket = "30s"
        else:
            age_bracket = "veteran"

    if gender == "female":
        if "20" in str(age_bracket) or "young" in combined_text or "bikini" in combined_text or "glute" in combined_text or first_name in ('kami', 'leila', 'sophie', 'chloe'):
            vibe = "20-something casual fitness & lifestyle regular (glutes/hypertrophy, wellness, recovery, everyday banter)"
        else:
            vibe = "fitness & wellness peer (active lifter, recovery, nutrition, everyday lifestyle)"
    elif gender == "male":
        if "veteran" in str(age_bracket) or "40" in str(age_bracket) or "heavy" in combined_text or "power" in combined_text:
            vibe = "veteran heavy lifter (compounds, strength, gym mechanics, direct banter)"
        else:
            vibe = "casual gym regular (hypertrophy, training consistency, supplements, everyday life)"
    else:
        vibe = "active group community regular"

    return _format_demographic_card(gender, age_bracket, vibe)


def _format_demographic_card(gender, age_bracket, vibe):
    is_young_female = (gender == "female" and ("20" in str(age_bracket) or "young" in str(vibe).lower()))

    if is_young_female:
        knowledge_rule = (
            "STRICT KNOWLEDGE & REALISM BOUNDARIES: Only speaks from what a 20-something woman lifter naturally knows. "
            "She is NOT an encyclopedia of vintage 1970s/1980s male bodybuilding history (she does NOT know Frank Zane, "
            "Serge Nubret, golden era vacuum contests, or heavy male androgen blast cycles). If vintage male bodybuilding lore "
            "is mentioned, she must react organically from her own demographic perspective: e.g. ask who that is ('who even is that lol', "
            "'the vacuum pose guy?'), make a casual remark about aesthetics vs bloat, joke about the guys overanalyzing vintage dudes, "
            "or stay quiet. Never speak like an old-school male bodybuilding coach."
        )
        instruction = (
            f"- DEMOGRAPHIC GROUNDING & KNOWLEDGE BOUNDARY (MANDATORY): You are a {age_bracket} female regular ({vibe}). "
            f"NEVER speak like an old male bodybuilding historian. You do NOT have encyclopedic knowledge of 1970s Mr. Olympia "
            f"contests or vintage bodybuilders like Frank Zane. React casually, ask questions, or focus on modern fitness/life."
        )
    elif gender == "female":
        knowledge_rule = (
            "Speaks authentically from female fitness perspective (lifting, recovery, nutrition, everyday humor). "
            "Does not lecture on heavy male-specific androgen cycles or vintage male Olympia trivia."
        )
        instruction = (
            f"- DEMOGRAPHIC GROUNDING: You are a {age_bracket} female regular ({vibe}). "
            f"Keep your perspective natural to your background; do not lecture on niche male-specific bodybuilding trivia."
        )
    else:
        knowledge_rule = f"Speaks authentically as a {age_bracket} {gender} member ({vibe})."
        instruction = f"- DEMOGRAPHIC GROUNDING: Authentic {age_bracket} {gender} group regular ({vibe})."

    return {
        "gender": gender,
        "age_bracket": str(age_bracket),
        "vibe": vibe,
        "is_young_female": is_young_female,
        "knowledge_rule": knowledge_rule,
        "instruction": instruction
    }
