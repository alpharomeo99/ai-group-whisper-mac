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

    def get_config(self):
        """Load orchestration parameters from settings or defaults."""
        cfg = self.store.get("orchestrator_config") or {}
        return {
            "enabled": cfg.get("enabled", True),
            "base_threshold": float(cfg.get("base_threshold", 0.52)),
            "circadian_enabled": cfg.get("circadian_enabled", True),
            "circadian_peak_hour": int(cfg.get("circadian_peak_hour", 15)),  # 3 PM peak
            "circadian_sigma": float(cfg.get("circadian_sigma", 5.5)),
            "poisson_lambda": float(cfg.get("poisson_lambda", 0.08)),       # mean ~12.5s deliberation
            "reading_cps": float(cfg.get("reading_cps", 32.0)),             # ~320 wpm reading speed
            "fatigue_decay_tau": float(cfg.get("fatigue_decay_tau", 240.0)),# 4 min fatigue half-life
            "fatigue_increment": float(cfg.get("fatigue_increment", 0.45)),
            "anti_dogpile_window": float(cfg.get("anti_dogpile_window", 45.0)), # sec between bot teammates
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
        # Clamp to minimum baseline activity 0.08 (some humans check phone at night)
        return max(0.08, curve)

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

        # Clamp deliberation to realistic ranges [1.5s, 35.0s]
        deliberation_time = max(1.5, min(deliberation_time, 35.0))
        total_delay = read_time + deliberation_time
        return max(2.5, min(total_delay, 45.0))

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
        key = (int(chat_id), int(persona_id))
        self.activity_log.setdefault(key, []).append(now)
        self.last_group_speakers[int(chat_id)] = (int(persona_id), now)

    # ---------------- 4. Topic Salience & Relevance Scoring ----------------
    def compute_relevance_score(self, message_text, persona, group_profile=None):
        """Scores how relevant the incoming message is to this persona's background and interests.
        Score range: [0.0, 1.0].
        """
        if not message_text:
            return 0.0

        text_lower = message_text.lower()
        score = 0.20  # baseline conversational presence

        # Check for interrogative / question cues
        is_question = "?" in text_lower or bool(re.search(r"\b(who|what|where|when|why|how|anyone|anybody|thoughts|opinion|does|is it|can we|recommend)\b", text_lower))
        if is_question:
            score += 0.25

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
    def arbiter_decision(self, chat_id, candidate_personas, message_event, is_direct_mention=False):
        """Evaluates all candidate personas in this group and selects the optimal speaker
        according to the mathematical cadence & game-theoretic model.
        Returns: (selected_persona_id, selected_account_id, calculated_delay, reason) or (None, None, 0, reason)
        """
        cfg = self.get_config()
        if not cfg["enabled"]:
            if is_direct_mention and candidate_personas:
                p = candidate_personas[0]
                return p["persona_id"], p["account_id"], 3.0, "Direct mention (cadence disabled)"
            return None, None, 0, "Orchestration disabled & not directly mentioned"

        now = time.time()
        chat_id = int(chat_id)

        # Multi-persona turn-taking & anti-dogpile control:
        if chat_id in self.last_group_speakers:
            last_pid, last_time = self.last_group_speakers[chat_id]
            time_since_last = now - last_time

            # 1. Self-suppression / Refractory period:
            # Prevent the SAME persona from monopolizing the conversation and talking to itself
            same_speaker_cooldown = float(cfg.get("same_speaker_cooldown", 28.0))
            if time_since_last < same_speaker_cooldown and not is_direct_mention:
                other_candidates = [cp for cp in candidate_personas if cp.get("persona_id") != last_pid]
                if other_candidates:
                    candidate_personas = other_candidates
                else:
                    return None, None, 0, f"Same-speaker refractory cooldown active for persona {last_pid} ({int(time_since_last)}s < {int(same_speaker_cooldown)}s)"

            # 2. Inter-bot team spacing:
            # Enforce a natural human pause between different personas so they don't blast replies at the same second
            inter_spacing = min(float(cfg.get("anti_dogpile_window", 8.0)), 12.0)
            if time_since_last < inter_spacing and not is_direct_mention:
                return None, None, 0, f"Inter-persona spacing buffer active ({int(time_since_last)}s < {int(inter_spacing)}s)"

        # Calculate time-of-day circadian activity factor
        circadian_mod = 1.0
        if cfg["circadian_enabled"]:
            circadian_mod = self.circadian_factor(
                peak_hour=cfg["circadian_peak_hour"],
                sigma=cfg["circadian_sigma"]
            )

        scored_candidates = []
        msg_text = message_event.get("text", "")

        for cp in candidate_personas:
            pid = cp["persona_id"]
            aid = cp["account_id"]
            if not pid or not aid:
                continue

            # 1. Relevance score
            rel_score = self.compute_relevance_score(msg_text, cp)

            # 2. Direct mention or reply boost
            salience_boost = 0.50 if is_direct_mention else 0.0

            # 3. Fatigue penalty (Hawkes process self-excitation decay)
            fatigue = self.compute_fatigue(chat_id, pid, tau=cfg["fatigue_decay_tau"])
            fatigue_penalty = fatigue * cfg["fatigue_increment"]

            # 4. Freshness / Novelty Boost:
            # If an account/persona hasn't spoken yet (e.g. newly added user or quiet member),
            # give it a strong priority bonus so it is elected next!
            freshness_boost = 0.30 if fatigue < 0.05 else (0.15 if fatigue < 0.20 else 0.0)

            # 5. Total probability utility score
            # Score = (Relevance + Salience) * Circadian - Fatigue + Freshness
            utility = ((rel_score + salience_boost) * circadian_mod) - fatigue_penalty + freshness_boost

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

        # Check threshold
        threshold = cfg["base_threshold"]
        if not is_direct_mention and not cfg["allow_spontaneous"]:
            return None, None, 0, "Spontaneous chatter disabled, no direct mention"

        # If direct mention, threshold is lenient
        effective_threshold = (threshold * 0.4) if is_direct_mention else threshold

        if best["utility"] < effective_threshold:
            return None, None, 0, f"Utility {best['utility']:.2f} below threshold {effective_threshold:.2f} (Fatigue: {best['fatigue']:.2f})"

        # Calculate stochastic Poisson delay for cognitive deliberation
        delay = self.calculate_deliberation_delay(
            msg_text,
            reading_cps=cfg["reading_cps"],
            poisson_lambda=cfg["poisson_lambda"]
        )

        return best["persona_id"], best["account_id"], delay, f"Elected (utility: {best['utility']:.2f}, fatigue: {best['fatigue']:.2f}, freshness: {best['freshness']:.2f}, delay: {delay:.1f}s)"
