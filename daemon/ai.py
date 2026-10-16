import re
"""AI client: fal.ai OpenAI-compatible router (Claude, GPT, Gemini, Llama, DeepSeek... via one fal key)."""
import time
import aiohttp

FAL_BASE = "https://fal.run/openrouter/router/openai/v1"
DEFAULT_MODEL = "google/gemini-2.5-flash"


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


def format_short_telegram_text(text, max_chars=140):
    """Enforces authentic, punchy Telegram message length (3 to 15 words, capped cleanly)."""
    text = (text or "").strip().strip("'\"")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if lines:
        text = lines[0]
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
        if combined and len(combined) >= 10:
            text = combined
        else:
            truncated = text[:max_chars]
            last_space = truncated.rfind(' ')
            text = truncated[:last_space] if last_space > 20 else truncated
    return text.strip()


async def draft_reply_with_meta(settings, title, persona, msgs, trigger_sender=None, trigger_text=None, temperature=0.55):
    sep = chr(10)
    persona_prompt = persona or "You are a regular member of this Telegram group."
    rules = [
        "CRITICAL RULES FOR YOUR REPLY:",
        "- SHORT PUNCHY TELEGRAM MESSAGES: Real users text quickly on phones. Write 3 to 15 words per message MAX (under 120 characters). NO paragraphs. NO essays. NO compound multi-sentence explanations. 1 punchy sentence or short reaction is ideal.",
        "- Stay strictly grounded in the ongoing conversation and context. Do NOT invent fake scenarios, unrequested vendor stories, fake orders, tracking numbers, payment issues, or imaginary drama.",
        "- Do NOT bring up vendors, suppliers, batch numbers, or purchasing unless the person explicitly asked about it in their message.",
        "- Write naturally in your persona's voice: casual lowercase or light casing, authentic gym/telegram slang, zero corporate fluff.",
        "- Avoid robotic structure: NO bullet lists, NO greeting headers, NO medical disclaimers, NO quotes around your text.",
        "- Output ONLY the raw message text to send to the group."
    ]
    system = persona_prompt + sep + sep + sep.join(rules)

    user_parts = [f"Group: {title}"]
    if msgs:
        user_parts.append(f"Recent Chat History:{sep}{transcript(msgs)}")
    if trigger_text and trigger_sender:
        user_parts.append(f'You are directly responding or reacting to this latest message from {trigger_sender}:{sep}"{trigger_text}"')
    elif msgs:
        user_parts.append("Respond naturally to the latest conversation above.")
    else:
        user_parts.append("Start a natural, casual conversation starter appropriate for this group.")

    user = (sep + sep).join(user_parts)
    raw_text, meta = await chat_with_meta(settings, system, user, temperature=temperature)
    return format_short_telegram_text(raw_text, max_chars=140), meta


async def draft_reply(settings, title, persona, msgs, trigger_sender=None, trigger_text=None, temperature=0.55):
    content, _ = await draft_reply_with_meta(settings, title, persona, msgs, trigger_sender=trigger_sender, trigger_text=trigger_text, temperature=temperature)
    return content
