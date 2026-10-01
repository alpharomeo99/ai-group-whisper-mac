"""AI client: fal.ai OpenAI-compatible router (Claude, GPT, Gemini, Llama, DeepSeek... via one fal key)."""
import aiohttp

FAL_BASE = "https://fal.run/openrouter/router/openai/v1"
DEFAULT_MODEL = "google/gemini-2.5-flash"


class AIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.retryable = status == 429 or status >= 500


async def chat(settings, system, user):
    key = settings.get("fal_key") or ""
    if not key:
        raise AIError(401, "Add your fal.ai API key in Settings.")
    model = settings.get("ai_model") or DEFAULT_MODEL
    headers = {"Content-Type": "application/json", "Authorization": f"Key {key}"}
    body = {"model": model, "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}]}
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{FAL_BASE}/chat/completions", json=body, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                msg = None
                if isinstance(data, dict):
                    err = data.get("error") or data.get("detail")
                    msg = err.get("message") if isinstance(err, dict) else (str(err) if err else None)
                raise AIError(r.status, msg or f"fal.ai request failed ({r.status})")
            return data["choices"][0]["message"]["content"].strip()


def transcript(msgs):
    return "\n".join(f"[{m['sender']}] {m['text']}" for m in msgs if m.get("text"))


async def summarize(settings, title, msgs):
    return await chat(settings,
                      "You summarize Telegram group conversations into concise bullet points: topics, decisions, open questions, and anything addressed to the user.",
                      f"Group: {title}\n\n{transcript(msgs)}")


async def draft_reply(settings, title, persona, msgs):
    return await chat(settings,
                      f"You write a short, natural reply to the latest messages in a Telegram group on the user's behalf. Persona/instructions: {persona or 'friendly and concise'}. Reply with only the message text.",
                      f"Group: {title}\n\n{transcript(msgs)}")
