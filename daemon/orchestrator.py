"""Mathematical decision engine and cadence orchestration for AI personas in Telegram groups.

Implements:
1. Non-homogeneous Poisson Process & Exponential Delays (reading latency + stochastic reaction time)
2. Circadian / Diurnal Wakefulness Curves (time-of-day activity modulation)
3. Topic Relevance & Salience Scoring (semantic relevance, question detection, mention boost)
4. Fatigue Accumulation & Exponential Cooldown Decay (Hawkes-style self-excitation and refractory suppression)
5. Multi-Persona Game-Theoretic Turn-Taking & Anti-Dogpiling Arbiter
"""
import json
import math
import random
import re
import time
from datetime import datetime

class PersonaCadenceOrchestrator:
    def __init__(self, store):
        self.store = store
        # In-memory tracking of recent persona events: { (chat_id, persona_id): [timestamps] }
        self.activity_log = {}
        # Multi-persona group lock / suppression: { chat_id: (last_speaker_persona_id, timestamp) }
        self.last_group_speakers = {}
        # Per-group speaker counts to enforce equal participation: { chat_id: { persona_id: count } }
        self.persona_speaker_counts = {}

    def get_config(self):
        """Load orchestration parameters from settings or defaults."""
        cfg = self.store.get("orchestrator_config") or {}
        return {
            "enabled": cfg.get("enabled", True),
            "base_threshold": float(cfg.get("base_threshold", 0.10)),
            "circadian_enabled": cfg.get("circadian_enabled", True),
            "circadian_peak_hour": int(cfg.get("circadian_peak_hour", 15)),  # 3 PM peak
            "circadian_sigma": float(cfg.get("circadian_sigma", 6.5)),
            "poisson_lambda": float(cfg.get("poisson_lambda", 0.08)),       # mean ~12.5s deliberation
            "reading_cps": float(cfg.get("reading_cps", 32.0)),             # ~320 wpm reading speed
            "fatigue_decay_tau": float(cfg.get("fatigue_decay_tau", 150.0)),# 4 min fatigue half-life
            "fatigue_increment": float(cfg.get("fatigue_increment", 0.45)),
            "anti_dogpile_window": float(cfg.get("anti_dogpile_window", 3.5)), # sec between bot teammates
            "allow_spontaneous": cfg.get("allow_spontaneous", True),        # can speak without explicit @mention
        }

    # ---------------- 1. Circadian / Diurnal Activity Curve ----------------
    def circadian_factor(self, hour=None, peak_hour=15, sigma=5.5):
        """Gaussian diurnal curve normalized between [0.08, 1.0].
        Peaks in mid-afternoon/evening, drops during late night/early morning sleeping hours.
        """
        if hour is None:
            # Use local time of host machine
            hour = datetime.now().hour + (datetime.now().minute / 60.0)

        # Distance on a 24-hour circular clock
        diff = abs(hour - peak_hour)
        if diff > 12:
            diff = 24 - diff

        # Gaussian density
        curve = math.exp(-0.5 * ((diff / sigma) ** 2))
        # Deep night activity drops to realistic baseline [0.06] rather than 24/7 chatter
        return max(0.06, min(1.0, curve))

    def get_persona_circadian_profile(self, persona):
        """Computes or extracts the persona's individual circadian schedule and sleeping window.
        Every persona has distinct waking and sleeping hours so night-time is personalized
        and groups remain actively populated 24/7 with who is awake.
        """
        details = {}
        if isinstance(persona, dict):
            raw_dt = persona.get("details")
            if isinstance(raw_dt, str):
                try:
                    details = json.loads(raw_dt or "{}")
                except Exception:
                    details = {}
            elif isinstance(raw_dt, dict):
                details = raw_dt

        # Deterministic seed based on persona id or name so it's consistent across reboots
        p_seed_str = str(persona.get("persona_id") or persona.get("id") or persona.get("name") or persona.get("persona_name") or "default")
        seed_hash = sum(ord(c) for c in p_seed_str)

        # Archetype sleep/circadian categories
        archetype_idx = seed_hash % 6
        SCHEDULES = [
            # (sleep_start, sleep_end, peak_hour, sigma, label)
            (22.5, 6.0, 13.0, 4.5, "early_bird"),
            (23.5, 7.5, 15.0, 5.0, "standard"),
            (1.0, 8.5, 17.0, 5.0, "moderate_night_owl"),
            (3.0, 10.5, 20.0, 4.5, "late_night_owl"),
            (5.0, 12.5, 22.0, 4.5, "graveyard_shift"),
            (21.5, 5.0, 12.0, 4.0, "morning_athlete"),
        ]
        base = SCHEDULES[archetype_idx]
        sleep_start = float(details.get("sleep_start", base[0]))
        sleep_end = float(details.get("sleep_end", base[1]))
        peak_hour = float(details.get("peak_hour", base[2]))
        sigma = float(details.get("sigma", base[3]))
        tz_offset = float(details.get("tz_offset", ((seed_hash % 7) - 3) * 1.0))

        return {
            "sleep_start": sleep_start,
            "sleep_end": sleep_end,
            "peak_hour": peak_hour,
            "sigma": sigma,
            "tz_offset": tz_offset,
            "profile_type": base[4]
        }

    def is_persona_awake(self, persona, ts=None):
        """Returns True if this persona is currently awake according to their personal night schedule."""
        if ts is None:
            ts = time.time()
        local_dt = datetime.fromtimestamp(ts)
        local_hour = local_dt.hour + (local_dt.minute / 60.0)

        # Daytime hours (08:00 to 23:30) are 100% active and awake for all personas
        if 8.0 <= local_hour < 23.5:
            return True

        prof = self.get_persona_circadian_profile(persona)
        s_start = prof["sleep_start"]
        s_end = prof["sleep_end"]
        if s_start <= s_end:
            asleep = (s_start <= local_hour < s_end)
        else:
            asleep = (local_hour >= s_start or local_hour < s_end)
        return not asleep

    def persona_circadian_factor(self, persona, ts=None):
        """Calculates normalized activity factor [0.15, 1.0] for this specific persona
        based on their individual sleep cycle and peak active hours."""
        if ts is None:
            ts = time.time()
        local_dt = datetime.fromtimestamp(ts)
        local_hour = local_dt.hour + (local_dt.minute / 60.0)

        if not self.is_persona_awake(persona, ts=ts):
            return 0.15

        prof = self.get_persona_circadian_profile(persona)
        diff = abs(local_hour - prof["peak_hour"])
        if diff > 12:
            diff = 24 - diff
        curve = math.exp(-0.5 * ((diff / max(prof["sigma"], 1.0)) ** 2))
        return max(0.50, min(1.0, curve))


    # ---------------- 2. Non-homogeneous Poisson Process & Delays ----------------
    def calculate_deliberation_delay(self, text, reading_cps=32.0, poisson_lambda=0.08):
        """Calculates human-realistic response delay:
        Total Delay = Reading Time (log-normal on incoming text size) + Deliberation Reaction (Poisson/exponential).
        """
        text_len = max(len(text or ""), 1)
        # Reading time with log-normal cognitive variability (humans read ~250-350 WPM -> ~25-35 CPS)
        mean_read = text_len / max(reading_cps, 5.0)
        read_time = mean_read * random.lognormvariate(0.0, 0.22)

        # Exponential reaction latency: -ln(U) / lambda (Poisson process)
        u = max(random.random(), 1e-6)
        deliberation_time = -math.log(u) / max(poisson_lambda, 0.01)

        # Fast, snappy deliberation for active group flow [1.5s, 8.0s]
        deliberation_time = max(1.5, min(deliberation_time, 8.0))
        total_delay = (read_time * 0.35) + deliberation_time
        total_delay += random.uniform(0.5, 1.8)
        return max(2.5, min(total_delay, 14.0))

    # ---------------- 3. Fatigue & Cooldown Dynamics (Hawkes Decay) ----------------
    def compute_fatigue(self, chat_id, persona_id, tau=240.0):
        """Computes current fatigue using exponential decay of previous message events."""
        now = time.time()
        key = (int(chat_id), int(persona_id))
        timestamps = self.activity_log.get(key, [])
        # Prune events older than 4 * tau
        cutoff = now - (4 * tau)
        timestamps = [t for t in timestamps if t > cutoff]
        self.activity_log[key] = timestamps

        # Sum of decaying exponentials: F = sum(exp(-(now - t_i) / tau))
        fatigue = sum(math.exp(-(now - t) / tau) for t in timestamps)
        return fatigue

    def record_activity(self, chat_id, persona_id):
        now = time.time()
        cid = int(chat_id)
        pid = int(persona_id)
        key = (cid, pid)
        self.activity_log.setdefault(key, []).append(now)
        self.last_group_speakers[cid] = (pid, now)
        if cid not in self.persona_speaker_counts:
            self.persona_speaker_counts[cid] = {}
        self.persona_speaker_counts[cid][pid] = self.persona_speaker_counts[cid].get(pid, 0) + 1

    # ---------------- 4. Topic Salience & Relevance Scoring ----------------
    def compute_relevance_score(self, message_text, persona, group_profile=None):
        """Scores how relevant the incoming message is to this persona's background and interests.
        Score range: [0.0, 1.0].
        """
        if not message_text:
            return 0.0

        text_lower = message_text.lower()
        score = 0.25  # baseline conversational presence
        # Conversational cues: greetings, shared reactions, casual chatter, humor and banter
        if bool(re.search(r"\b(hey|hi|hello|morning|evening|yo|sup|thanks|thx|appreciate|lol|haha|lmao|rofl|dead|bruh|bro|clown|joke|joking|funny|unserious|damn|nice|good|great|awesome|crazy|true|facts|fr|for real|today|yesterday|weekend)\b", text_lower)):
            score += 0.15

        # Check for interrogative / question cues
        is_question = "?" in text_lower or bool(re.search(r"\b(who|what|where|when|why|how|anyone|anybody|thoughts|opinion|does|is it|can we|recommend)\b", text_lower))
        if is_question:
            score += 0.25

        # Check for explicit help / guidance requests
        is_help = bool(re.search(r"\b(help|assist|advice|advise|confused|stuck|newbie|beginner|wondering|guide|how do i|how to|can someone|could someone|question|anyone know|check my)\b", text_lower))
        if is_help:
            score += 0.25

        # Broad domain relevance boost: nootropics, wellness, fitness, peptides
        if bool(re.search(r"\b(nootropic|nootropics|semax|selank|bromantane|modafinil|tyrosine|alpha-gpc|focus|wellness|sleep|sauna|plunge|magnesium|electrolytes|fitness|gym|split|ppl|hypertrophy|workout|bench|squat|deadlift|bpc|tb500|tirz|reta|peptides|dosing|recon|bac|test|trt|tren|deca|anavar|var|dbol|primo|cycle|blast|cruise|pct|gear|sarm|sarms|rad|lgd|ostarine|mk677|bloodwork|labs|bulk|cut|diet|protein|meal|cardio|pr|news|ufc|fight|football|soccer|nba|f1|game|work|job|boss|money|crypto|bitcoin|inflation|stocks|girlfriend|wife|kids|weekend|movie|show|car|trip|rogan|huberman|sulek|jackman|reacher|rock|efron|celeb|celebrity|podcast|mpmd|creami|costco|pereira|boxing|dating|crush|sauna|tripod|meme)\b", text_lower)):
            score += 0.15

        # Check persona details: interests, role, backstory, topics
        details = {}
        try:
            if isinstance(persona.get("details"), str):
                details = json.loads(persona.get("details") or "{}")
            elif isinstance(persona.get("details"), dict):
                details = persona.get("details")
        except Exception:
            details = {}

        # Scan persona interests & keywords
        role = (details.get("role_in_group") or "").lower()
        backstory = (details.get("backstory") or "").lower()
        off_topic = [str(x).lower() for x in (details.get("off_topic_interests") or details.get("off_topic") or [])]
        opinions = [str(x).lower() for x in (details.get("opinions_and_habits") or details.get("hot_takes") or [])]

        keyword_hits = 0
        tokens = set(re.findall(r"\b\w{3,}\b", text_lower))
        for item in [role, backstory] + off_topic + opinions:
            item_tokens = set(re.findall(r"\b\w{3,}\b", item))
            intersection = tokens.intersection(item_tokens)
            keyword_hits += len(intersection)

        if keyword_hits > 0:
            score += min(0.40, keyword_hits * 0.12)

        # Taboo suppression
        taboos = [str(t).lower() for t in (details.get("taboos_and_dislikes") or details.get("trigger_topics") or [])]
        for t in taboos:
            if t and t in text_lower:
                score -= 0.30

        return max(0.0, min(1.0, score))

    # ---------------- 5. Multi-Persona Turn-Taking & Arbiter ----------------
    def arbiter_decision(self, chat_id, candidate_personas, message_event, is_direct_mention=False, target_persona_id=None, target_account_id=None, is_admin=False):
        """Evaluates all candidate personas in this group and selects the optimal speaker
        according to the mathematical cadence & game-theoretic model.
        Returns: (selected_persona_id, selected_account_id, calculated_delay, reason) or (None, None, 0, reason)
        """
        cfg = self.get_config()
        if not cfg["enabled"]:
            if (is_direct_mention or is_admin) and candidate_personas:
                p = candidate_personas[0]
                return p["persona_id"], p["account_id"], 3.0, "Direct mention (cadence disabled)"
            return None, None, 0, "Orchestration disabled & not directly mentioned"

        now = time.time()
        chat_id = int(chat_id)
        msg_text = message_event.get("text", "")

        # 0. TARGET PERSONA OVERRIDE: If human or admin addressed a specific persona directly
        if target_persona_id:
            for cp in candidate_personas:
                if cp.get("persona_id") == target_persona_id:
                    delay = self.calculate_deliberation_delay(
                        msg_text,
                        reading_cps=cfg.get("reading_cps", 32.0),
                        poisson_lambda=cfg.get("poisson_lambda", 0.08)
                    )
                    return target_persona_id, target_account_id or cp["account_id"], delay, f"Directly addressed persona {target_persona_id}"
            # Direct reply or mention guarantee: if target account is known, honor it even if missing in filtered pool
            if target_account_id:
                delay = self.calculate_deliberation_delay(
                    msg_text,
                    reading_cps=cfg.get("reading_cps", 32.0),
                    poisson_lambda=cfg.get("poisson_lambda", 0.08)
                )
                return target_persona_id, target_account_id, delay, f"Target persona {target_persona_id} override (direct reply)"

        # Multi-persona turn-taking & anti-dogpile control:
        inter_spacing_offset = 0.0
        if chat_id in self.last_group_speakers:
            last_pid, last_time = self.last_group_speakers[chat_id]
            time_since_last = now - last_time

            # 1. Self-suppression / Refractory period:
            # Prevent the SAME persona from monopolizing the conversation and talking to itself
            same_speaker_cooldown = float(cfg.get("same_speaker_cooldown", 12.0))
            if time_since_last < same_speaker_cooldown and not is_direct_mention and not is_admin:
                other_candidates = [cp for cp in candidate_personas if cp.get("persona_id") != last_pid]
                if other_candidates:
                    candidate_personas = other_candidates
                else:
                    # For human/admin events, never discard: apply an offset instead of dropping!
                    inter_spacing_offset += max(2.0, same_speaker_cooldown - time_since_last)

            # 2. Inter-bot team spacing:
            # Enforce natural human pause between bot messages without dropping human/admin events
            inter_spacing = min(float(cfg.get("anti_dogpile_window", 8.0)), 12.0)
            if time_since_last < inter_spacing and not is_direct_mention and not is_admin:
                inter_spacing_offset += max(2.0, inter_spacing - time_since_last)

        # Individualized circadian modeling: each persona has their own sleep hours & timezone.
        # Personas awake in their timezone maintain full active presence.
        pass

        scored_candidates = []

        for cp in candidate_personas:
            pid = cp["persona_id"]
            aid = cp["account_id"]
            if not pid or not aid:
                continue

            # 0. Individualized Persona Circadian & Sleep Factor
            is_awake = self.is_persona_awake(cp, ts=now)
            circadian_mod = 1.0
            if cfg.get("circadian_enabled", True):
                circadian_mod = self.persona_circadian_factor(cp, ts=now)
            if is_direct_mention or is_admin:
                circadian_mod = max(0.85, circadian_mod)

            # 1. Relevance score
            rel_score = self.compute_relevance_score(msg_text, cp)

            # 2. Direct mention or admin priority boost
            salience_boost = 0.65 if is_admin else (0.50 if is_direct_mention else 0.25)

            # 3. Fatigue penalty (Hawkes process self-excitation decay)
            fatigue = self.compute_fatigue(chat_id, pid, tau=cfg["fatigue_decay_tau"])
            fatigue_penalty = fatigue * (cfg["fatigue_increment"] * 0.4 if (is_direct_mention or is_admin) else cfg["fatigue_increment"])

            # 4. Equal Participation / Anti-Monopoly Balancer:
            counts = self.persona_speaker_counts.get(chat_id, {})
            total_speeches = sum(counts.values()) or 1
            speech_share = counts.get(pid, 0) / total_speeches

            if speech_share > 0.40 and len(candidate_personas) >= 2:
                balance_mod = -0.25  # Lightly suppress over-represented persona
            elif counts.get(pid, 0) == 0 or speech_share < 0.20:
                balance_mod = 0.45   # Strong boost for quiet / under-represented personas
            else:
                balance_mod = 0.10

            # 5. Freshness / Novelty Boost
            freshness_boost = 0.30 if fatigue < 0.05 else (0.15 if fatigue < 0.20 else 0.0)

            # 6. Total probability utility score with individual sleep awareness
            sleep_mod = 0.0 if (is_awake or is_direct_mention or is_admin) else -0.45
            utility = ((rel_score + salience_boost) * circadian_mod) - fatigue_penalty + freshness_boost + balance_mod + sleep_mod

            # Add stochastic perturbation to break ties
            perturbed_utility = utility + random.uniform(-0.03, 0.03)

            scored_candidates.append({
                "persona_id": pid,
                "account_id": aid,
                "utility": perturbed_utility,
                "fatigue": fatigue,
                "rel_score": rel_score,
                "freshness": freshness_boost
            })

        if not scored_candidates:
            return None, None, 0, "No active personas assigned to accounts in this group"

        # Sort candidate personas by utility score descending
        scored_candidates.sort(key=lambda x: x["utility"], reverse=True)
        best = scored_candidates[0]

        # Calculate deliberation delay with natural human reading & hesitation cadence
        delay = self.calculate_deliberation_delay(
            msg_text,
            reading_cps=cfg.get("reading_cps", 32.0),
            poisson_lambda=cfg.get("poisson_lambda", 0.08)
        ) + inter_spacing_offset

        label = "direct target" if (target_persona_id or is_direct_mention) else "best affinity"
        return best["persona_id"], best["account_id"], delay, (
            f"Elected ({label}, utility: {best['utility']:.2f}, "
            f"relevance: {best['rel_score']:.2f}, fatigue: {best['fatigue']:.2f}, delay: {delay:.1f}s)"
        )
