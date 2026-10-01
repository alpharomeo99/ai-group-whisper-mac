#!/usr/bin/env python3
"""AI Group Whisper daemon: Telethon client + SQLite queue + local HTTP API for the Electron UI."""
import argparse
import asyncio
import logging
import os
import time

from aiohttp import web
from telethon import TelegramClient, events, errors

import ai
from store import Store

log = logging.getLogger("whisperd")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class Daemon:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.store = Store(os.path.join(data_dir, "whisper.db"))
        self.client = None
        self.suspended = False
        self.paused_reason = self.store.get("paused_reason")  # persisted AI pause (402/403)
        self.pending_login = {}
        self.wake_event = asyncio.Event()

    # ---------- Telegram ----------
    async def ensure_client(self):
        api_id, api_hash = self.store.get("tg_api_id"), self.store.get("tg_api_hash")
        if not api_id or not api_hash:
            return None
        if self.client is None:
            self.client = TelegramClient(os.path.join(self.data_dir, "telegram"), int(api_id), api_hash)
            self.client.add_event_handler(self.on_message, events.NewMessage())
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def on_message(self, event):
        if not event.is_group or not event.raw_text:
            return
        chat_id = event.chat_id
        g = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
        if not g or not g[0]["watched"]:
            return
        sender = await event.get_sender()
        name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "unknown"
        self.store.add_message(chat_id, event.id, name, event.raw_text, int(event.date.timestamp()))
        if g[0]["auto_reply"] and not event.out:
            me = await self.client.get_me()
            if event.mentioned or (event.is_reply and (await event.get_reply_message()).sender_id == me.id):
                # dedupe per triggering message so restarts/wakes never double-send
                self.store.enqueue("draft_reply", chat_id, {"trigger": event.id, "auto_send": True},
                                   dedupe_key=f"reply:{chat_id}:{event.id}", delay=3)
        self.wake_event.set()

    # ---------- Queue worker ----------
    async def worker(self):
        while True:
            try:
                await asyncio.wait_for(self.wake_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            self.wake_event.clear()
            if self.suspended or self.paused_reason:
                continue
            client = await self.ensure_client()
            if not client or not await client.is_user_authorized():
                continue
            for job in self.store.claim(limit=3):
                await self.run_job(job)

    async def run_job(self, job):
        import json
        p = json.loads(job["payload"])
        settings = {k: self.store.get(k) for k in ("fal_key", "ai_model")}
        g = (self.store.rows("SELECT * FROM groups WHERE chat_id=?", (job["chat_id"],)) or [{}])[0]
        try:
            if self.suspended:
                raise asyncio.CancelledError()
            if job["kind"] == "summarize":
                body = await ai.summarize(settings, g.get("title", ""), self.store.recent(job["chat_id"]))
                self.store.q("INSERT INTO summaries(chat_id,body,created) VALUES(?,?,?)",
                             (job["chat_id"], body, int(time.time())))
            elif job["kind"] == "draft_reply":
                text = await ai.draft_reply(settings, g.get("title", ""), g.get("persona", ""),
                                            self.store.recent(job["chat_id"], 30))
                if p.get("auto_send"):
                    self.store.enqueue("send", job["chat_id"], {"text": text, "reply_to": p.get("trigger")},
                                       dedupe_key=f"send:{job['id']}")
                else:
                    self.store.q("INSERT INTO summaries(chat_id,body,created) VALUES(?,?,?)",
                                 (job["chat_id"], "Draft reply:\n" + text, int(time.time())))
            elif job["kind"] == "send":
                await self.client.send_message(job["chat_id"], p["text"], reply_to=p.get("reply_to"))
            self.store.finish(job["id"], True)
        except asyncio.CancelledError:
            self.store.finish(job["id"], False, "interrupted by sleep", retry_in=5)
        except ai.AIError as e:
            if e.status in (402, 403):
                self.paused_reason = f"AI provider blocked requests ({e.status}): {e}"
                self.store.set("paused_reason", self.paused_reason)
                self.store.finish(job["id"], False, str(e), retry_in=0)
            elif e.retryable and job["attempts"] < 5:
                self.store.finish(job["id"], False, str(e), retry_in=min(600, 15 * 2 ** job["attempts"]))
            else:
                self.store.finish(job["id"], False, str(e))
        except errors.FloodWaitError as e:
            self.store.finish(job["id"], False, f"Telegram flood wait {e.seconds}s", retry_in=e.seconds + 1)
        except (ConnectionError, OSError) as e:
            self.store.finish(job["id"], False, str(e), retry_in=30)
        except Exception as e:  # noqa
            log.exception("job failed")
            self.store.finish(job["id"], False, str(e))

    # ---------- Sleep / wake ----------
    async def set_power(self, state, reason):
        if state == "suspend":
            self.suspended = True
            n = self.store.requeue_in_flight()
            log.info("suspend (%s): paused queue, requeued %d", reason, n)
            if self.client:
                await self.client.disconnect()
        else:
            n = self.store.requeue_in_flight(delay=2)
            self.suspended = False
            log.info("resume (%s): reconnecting, requeued %d", reason, n)
            try:
                await self.ensure_client()
                await self.catch_up()
            except Exception as e:  # network may lag after wake; worker retries
                log.warning("reconnect after wake failed: %s", e)
            self.wake_event.set()

    async def catch_up(self):
        """Fetch messages missed while the Mac slept."""
        if not self.client or not await self.client.is_user_authorized():
            return
        for g in self.store.rows("SELECT * FROM groups WHERE watched=1"):
            last = self.store.q("SELECT MAX(msg_id) FROM messages WHERE chat_id=?", (g["chat_id"],)).fetchone()[0] or 0
            async for m in self.client.iter_messages(g["chat_id"], min_id=last, limit=200):
                if m.raw_text:
                    s = await m.get_sender()
                    self.store.add_message(g["chat_id"], m.id, getattr(s, "first_name", None) or "unknown",
                                           m.raw_text, int(m.date.timestamp()))

    # ---------- HTTP API ----------
    def routes(self):
        r = web.RouteTableDef()
        J = web.json_response

        @r.get("/status")
        async def status(_):
            c = await self.ensure_client() if self.store.get("tg_api_id") else None
            authed = bool(c and await c.is_user_authorized())
            counts = {x["status"]: x["n"] for x in self.store.rows("SELECT status, COUNT(*) n FROM queue GROUP BY status")}
            return J({"configured": c is not None, "authorized": authed, "suspended": self.suspended,
                      "paused_reason": self.paused_reason, "queue": counts})

        @r.get("/settings")
        async def get_settings(_):
            keys = ("tg_api_id", "tg_api_hash", "ai_model")
            out = {k: self.store.get(k) for k in keys}
            out["fal_key_set"] = bool(self.store.get("fal_key"))
            return J(out)

        @r.post("/settings")
        async def save_settings(req):
            body = await req.json()
            for k in ("tg_api_id", "tg_api_hash", "ai_model", "fal_key"):
                if k in body and body[k] not in (None, ""):
                    self.store.set(k, body[k])
            self.client = None
            return J({"ok": True})

        @r.post("/login/code")
        async def login_code(req):
            phone = (await req.json())["phone"]
            c = await self.ensure_client()
            if not c:
                return J({"error": "Save your Telegram API ID and hash first."}, status=400)
            sent = await c.send_code_request(phone)
            self.pending_login = {"phone": phone, "hash": sent.phone_code_hash}
            return J({"ok": True})

        @r.post("/login/verify")
        async def login_verify(req):
            b = await req.json()
            try:
                await self.client.sign_in(self.pending_login["phone"], b["code"],
                                          phone_code_hash=self.pending_login["hash"])
            except errors.SessionPasswordNeededError:
                if not b.get("password"):
                    return J({"need_password": True})
                await self.client.sign_in(password=b["password"])
            return J({"ok": True})

        @r.get("/groups")
        async def groups(_):
            c = await self.ensure_client()
            if c and await c.is_user_authorized():
                async for d in c.iter_dialogs():
                    if d.is_group:
                        self.store.q("INSERT INTO groups(chat_id,title) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
                                     (d.id, d.name))
            return J(self.store.rows("SELECT * FROM groups ORDER BY watched DESC, title"))

        @r.post("/groups/{cid}")
        async def update_group(req):
            b = await req.json()
            cid = int(req.match_info["cid"])
            for k in ("watched", "auto_reply", "persona"):
                if k in b:
                    self.store.q(f"UPDATE groups SET {k}=? WHERE chat_id=?", (b[k], cid))
            if b.get("watched"):
                asyncio.create_task(self.catch_up())
            return J({"ok": True})

        @r.get("/groups/{cid}/feed")
        async def feed(req):
            cid = int(req.match_info["cid"])
            return J({"messages": self.store.recent(cid, 100),
                      "summaries": self.store.rows("SELECT * FROM summaries WHERE chat_id=? ORDER BY created DESC LIMIT 20", (cid,))})

        @r.post("/groups/{cid}/{action}")
        async def action(req):
            cid, act = int(req.match_info["cid"]), req.match_info["action"]
            if act not in ("summarize", "draft_reply"):
                return J({"error": "unknown action"}, status=400)
            self.store.enqueue(act, cid, {}, dedupe_key=f"{act}:{cid}:{int(time.time()) // 10}")
            self.wake_event.set()
            return J({"ok": True})

        @r.get("/queue")
        async def queue(_):
            return J(self.store.rows("SELECT * FROM queue ORDER BY id DESC LIMIT 100"))

        @r.post("/queue/resume")
        async def resume_ai(_):
            self.paused_reason = None
            self.store.set("paused_reason", None)
            self.wake_event.set()
            return J({"ok": True})

        @r.post("/power")
        async def power(req):
            b = await req.json()
            await self.set_power(b.get("state"), b.get("reason", ""))
            return J({"ok": True})

        return r


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", default=os.path.expanduser("~/Library/Application Support/AI Group Whisper/data"))
    a = ap.parse_args()
    os.makedirs(a.data, exist_ok=True)
    d = Daemon(a.data)
    n = d.store.requeue_in_flight(delay=0)  # recover from crash / hard power-off
    log.info("startup: recovered %d interrupted jobs", n)

    @web.middleware
    async def errors_mw(req, handler):
        try:
            return await handler(req)
        except web.HTTPException:
            raise
        except Exception as e:  # noqa
            log.exception("api error")
            return web.json_response({"error": str(e)}, status=500)

    app = web.Application(middlewares=[errors_mw])
    app.add_routes(d.routes())
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", a.port).start()
    log.info("listening on 127.0.0.1:%d", a.port)
    try:
        await d.ensure_client()
    except Exception as e:  # noqa
        log.warning("telegram connect deferred: %s", e)
    await d.worker()


if __name__ == "__main__":
    asyncio.run(main())
