#!/usr/bin/env python3
"""AI Group Whisper daemon: Telethon client + SQLite queue + local HTTP API for the Electron UI."""
import argparse
import asyncio
import logging
import os
import secrets
import sys
import time

from aiohttp import web
from telethon import TelegramClient, events, errors

import ai
import camoufox as cfx
import updater
from provisioner import Provisioner
from textverified import TextVerified
from vmos import Vmos
from store import Store

log = logging.getLogger("whisperd")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class Daemon:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.store = Store(os.path.join(data_dir, "whisper.db"))
        self.clients = {}        # account_id -> TelegramClient
        self.pending = {}        # login token -> {"client", "phone", "hash", "session"}
        self.sess_dir = os.path.join(data_dir, "sessions")
        os.makedirs(self.sess_dir, exist_ok=True)
        self.suspended = False
        self.paused_reason = self.store.get("paused_reason")  # persisted AI pause (402/403)
        self._adopt_legacy_session()
        self.wake_event = asyncio.Event()
        self.prov = Provisioner(self)
        self.autoapi = {}        # token -> {"future": code future, "task": camoufox task}
        self.cfx_install = {"running": False, "log": ""}

    # ---------- Telegram accounts ----------
    def _adopt_legacy_session(self):
        """v2.0 used one data/telegram.session; turn it into the first account."""
        old = os.path.join(self.data_dir, "telegram.session")
        if os.path.exists(old) and not self.store.rows("SELECT id FROM accounts"):
            os.replace(old, os.path.join(self.sess_dir, "legacy.session"))
            self.store.q("INSERT INTO accounts(session,name,created) VALUES(?,?,?)",
                         ("legacy", "Account", int(time.time())))

    def _api(self, acct=None):
        """Each account has its own Telethon api_id/api_hash. Old installs fall back to the global one."""
        if acct is not None and acct["api_id"] and acct["api_hash"]:
            return int(acct["api_id"]), acct["api_hash"]
        api_id, api_hash = self.store.get("tg_api_id"), self.store.get("tg_api_hash")
        return (int(api_id), api_hash) if api_id and api_hash else None

    def _tg_proxy(self, proxy_id):
        if not proxy_id:
            return None
        r = self.store.rows("SELECT url FROM proxies WHERE id=?", (proxy_id,))
        p = cfx.parse_proxy(r[0]["url"]) if r else None
        if not p:
            return None
        scheme, rest = p["server"].split("://", 1)
        host, port = rest.rsplit(":", 1)
        kind = "socks5" if scheme.startswith("socks5") else "socks4" if scheme.startswith("socks4") else "http"
        return {"proxy_type": kind, "addr": host, "port": int(port), "rdns": True,
                "username": p.get("username"), "password": p.get("password")}

    def _new_client(self, session, api, proxy_id=None):
        return TelegramClient(os.path.join(self.sess_dir, session), api[0], api[1], proxy=self._tg_proxy(proxy_id))

    async def adopt_number(self, phone, api_id, api_hash, code_getter, proxy_id=None):
        """Sign a number into Telethon with its own API keys; the code is read by code_getter()."""
        session = "acct_" + secrets.token_hex(8)
        c = self._new_client(session, (int(api_id), api_hash), proxy_id)
        await asyncio.wait_for(c.connect(), timeout=20)
        sent = await c.send_code_request(phone)
        await asyncio.sleep(5)
        code = await code_getter()
        if not code:
            await self._discard(c, session)
            raise RuntimeError("The Telegram login code did not arrive.")
        await c.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
        me = await c.get_me()
        await c.disconnect()
        cur = self.store.q("INSERT INTO accounts(session,api_id,api_hash,proxy_id,created) VALUES(?,?,?,?,?)",
                           (session, int(api_id), api_hash, proxy_id, int(time.time())))
        self._save_profile(cur.lastrowid, me, phone)
        await self.ensure_clients()
        return cur.lastrowid

    async def ensure_clients(self):
        """Connect every active account. Returns {account_id: authorized client}."""
        ready = {}
        for a in self.store.rows("SELECT * FROM accounts WHERE active=1"):
            c = self.clients.get(a["id"])
            if c is None:
                api = self._api(a)
                if not api:
                    continue
                c = self._new_client(a["session"], api, a.get("proxy_id"))
                c.add_event_handler(self._handler_for(a["id"]), events.NewMessage())
                self.clients[a["id"]] = c
            try:
                if not c.is_connected():
                    await asyncio.wait_for(c.connect(), timeout=10)
                if await c.is_user_authorized():
                    ready[a["id"]] = c
                    if not a["user_id"]:
                        me = await c.get_me()
                        self._save_profile(a["id"], me, me.phone and "+" + me.phone.lstrip("+"))
            except Exception as e:  # noqa
                log.warning("account %s connect failed: %s", a["id"], e)
        return ready

    def _save_profile(self, aid, me, phone):
        name = " ".join(x for x in (me.first_name, me.last_name) if x) or "Account"
        self.store.q("UPDATE accounts SET user_id=?, phone=?, name=?, username=? WHERE id=?",
                     (me.id, phone, name, me.username, aid))

    async def drop_clients(self):
        for c in list(self.clients.values()):
            try:
                await c.disconnect()
            except Exception:  # noqa
                pass
        self.clients = {}

    def _handler_for(self, aid):
        async def h(event):
            await self.on_message(event, aid)
        return h

    async def client_for(self, chat_id):
        ready = await self.ensure_clients()
        g = self.store.rows("SELECT account_id FROM groups WHERE chat_id=?", (chat_id,))
        aid = g[0]["account_id"] if g else None
        if aid in ready:
            return ready[aid]
        if ready:
            return next(iter(ready.values()))
        raise ConnectionError("No signed-in Telegram account")

    async def _discard(self, c, session, logout=False):
        try:
            if logout:
                if not c.is_connected():
                    await c.connect()
                await c.log_out()
            await c.disconnect()
        except Exception:  # noqa
            pass
        for ext in (".session", ".session-journal"):
            try:
                os.remove(os.path.join(self.sess_dir, session + ext))
            except FileNotFoundError:
                pass

    async def on_message(self, event, aid):
        if not event.is_group or not event.raw_text:
            return
        chat_id = event.chat_id
        g = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
        if g and g[0]["account_id"] not in (None, aid) and g[0]["account_id"] in self.clients:
            return  # another account owns this group; avoid duplicates
        if not g or not g[0]["watched"]:
            return
        sender = await event.get_sender()
        name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "unknown"
        self.store.add_message(chat_id, event.id, name, event.raw_text, int(event.date.timestamp()))
        if g[0]["auto_reply"] and not event.out:
            me = await event.client.get_me()
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
            if not await self.ensure_clients():
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
                await (await self.client_for(job["chat_id"])).send_message(job["chat_id"], p["text"], reply_to=p.get("reply_to"))
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
            await self.drop_clients()
        else:
            n = self.store.requeue_in_flight(delay=2)
            self.suspended = False
            log.info("resume (%s): reconnecting, requeued %d", reason, n)
            try:
                await self.catch_up()
            except Exception as e:  # network may lag after wake; worker retries
                log.warning("reconnect after wake failed: %s", e)
            self.wake_event.set()

    async def catch_up(self):
        """Fetch messages missed while the Mac slept."""
        ready = await self.ensure_clients()
        if not ready:
            return
        for g in self.store.rows("SELECT * FROM groups WHERE watched=1"):
            c = ready.get(g["account_id"]) or next(iter(ready.values()))
            last = self.store.q("SELECT MAX(msg_id) FROM messages WHERE chat_id=?", (g["chat_id"],)).fetchone()[0] or 0
            async for m in c.iter_messages(g["chat_id"], min_id=last, limit=200):
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
            ready = await self.ensure_clients()
            n_acc = len(self.store.rows("SELECT id FROM accounts WHERE active=1"))
            counts = {x["status"]: x["n"] for x in self.store.rows("SELECT status, COUNT(*) n FROM queue GROUP BY status")}
            return J({"configured": n_acc > 0, "authorized": bool(ready),
                      "accounts": n_acc, "connected": len(ready), "suspended": self.suspended,
                      "paused_reason": self.paused_reason, "queue": counts})

        SECRET_KEYS = ("fal_key", "vmos_sk", "tv_key")
        PLAIN_KEYS = ("ai_model", "vmos_ak", "vmos_pad", "tv_user", "tv_max_price", "cfx_headless", "cfx_app_title")

        @r.get("/settings")
        async def get_settings(_):
            out = {k: self.store.get(k) for k in PLAIN_KEYS}
            for k in SECRET_KEYS:
                out[k + "_set"] = bool(self.store.get(k))
            return J(out)

        @r.post("/settings")
        async def save_settings(req):
            body = await req.json()
            for k in PLAIN_KEYS + SECRET_KEYS:
                if k in body and body[k] not in (None, ""):
                    self.store.set(k, body[k])
            return J({"ok": True})

        # ----- integrations -----
        @r.post("/integrations/vmos/test")
        async def vmos_test(_):
            try:
                v = Vmos(self.store.get("vmos_ak"), self.store.get("vmos_sk"), self.store.get("vmos_pad"))
                pads = await v.pads()
            except Exception as e:  # noqa
                return J({"error": str(e)}, status=400)
            pad = self.store.get("vmos_pad")
            if not pad and pads:
                self.store.set("vmos_pad", pads[0]); pad = pads[0]
            return J({"ok": True, "pads": pads, "pad": pad})

        @r.post("/integrations/textverified/test")
        async def tv_test(_):
            try:
                bal = await TextVerified(self.store.get("tv_key"), self.store.get("tv_user")).balance()
            except Exception as e:  # noqa
                return J({"error": str(e)}, status=400)
            return J({"ok": True, "balance": bal})

        # ----- proxies -----
        @r.get("/proxies")
        async def proxies(_):
            rows = self.store.rows("SELECT * FROM proxies ORDER BY id")
            for p in rows:
                p["accounts"] = [a["name"] or a["phone"] for a in
                                 self.store.rows("SELECT name,phone FROM accounts WHERE proxy_id=?", (p["id"],))]
            return J(rows)

        @r.post("/proxies")
        async def add_proxies(req):
            lines = [l.strip() for l in str((await req.json()).get("text", "")).splitlines() if l.strip()]
            added, bad = 0, []
            for l in lines:
                if not cfx.parse_proxy(l):
                    bad.append(l); continue
                self.store.q("INSERT INTO proxies(label,url,created) VALUES(?,?,?)",
                             (cfx.parse_proxy(l)["server"].split("://")[1], l, int(time.time())))
                added += 1
            return J({"added": added, "bad": bad})

        @r.post("/proxies/{pid}/test")
        async def test_proxy(req):
            pid = int(req.match_info["pid"])
            r = self.store.rows("SELECT url FROM proxies WHERE id=?", (pid,))
            if not r:
                return J({"error": "Not found"}, status=404)
            try:
                ip = await cfx.check_proxy(r[0]["url"])
                self.store.q("UPDATE proxies SET ok=1,last_ip=?,last_check=? WHERE id=?", (ip, int(time.time()), pid))
                return J({"ok": True, "ip": ip})
            except Exception as e:  # noqa
                self.store.q("UPDATE proxies SET ok=0,last_check=? WHERE id=?", (int(time.time()), pid))
                return J({"ok": False, "error": str(e) or "Proxy did not respond"})

        @r.delete("/proxies/{pid}")
        async def del_proxy(req):
            pid = int(req.match_info["pid"])
            self.store.q("DELETE FROM proxies WHERE id=?", (pid,))
            self.store.q("UPDATE accounts SET proxy_id=NULL WHERE proxy_id=?", (pid,))
            return J({"ok": True})

        # ----- camoufox -----
        @r.get("/camoufox/status")
        async def cfx_status(_):
            import importlib.util
            installed = importlib.util.find_spec("camoufox") is not None
            ready = False
            if installed:
                p = await asyncio.create_subprocess_exec(sys.executable, "-m", "camoufox", "path",
                                                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                out = (await p.communicate())[0].decode(errors="ignore")
                ready = p.returncode == 0 and "/" in out
            return J({"installed": installed, "ready": ready, **self.cfx_install})

        @r.post("/camoufox/install")
        async def cfx_inst(_):
            if self.cfx_install["running"]:
                return J({"ok": True})

            async def go():
                self.cfx_install.update(running=True, log="Installing Camoufox…\n")
                for cmd in ([sys.executable, "-m", "pip", "install", "-U", "camoufox[geoip]"],
                            [sys.executable, "-m", "camoufox", "fetch"]):
                    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                             stderr=asyncio.subprocess.STDOUT)
                    async for line in p.stdout:
                        self.cfx_install["log"] = (self.cfx_install["log"] + line.decode(errors="ignore"))[-4000:]
                    await p.wait()
                    if p.returncode != 0:
                        self.cfx_install["log"] += "\nInstall failed."
                        break
                else:
                    self.cfx_install["log"] += "\nCamoufox is ready."
                self.cfx_install["running"] = False
            asyncio.create_task(go())
            return J({"ok": True})

        # Manual add: Camoufox creates the API keys, the user types the my.telegram.org code.
        @r.post("/autoapi/start")
        async def autoapi_start(req):
            b = await req.json()
            phone = "".join(ch for ch in str(b.get("phone", "")) if ch.isdigit() or ch == "+")
            if len(phone.lstrip("+")) < 7:
                return J({"error": "Enter the full phone number with country code first."}, status=400)
            pid = b.get("proxy_id")
            row = self.store.rows("SELECT url FROM proxies WHERE id=?", (pid,)) if pid else []
            token = secrets.token_hex(6)
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            entry = {"future": fut, "result": None, "error": None, "waiting_code": False}

            async def getter():
                entry["waiting_code"] = True
                return await fut

            async def run():
                try:
                    entry["result"] = await cfx.get_api_credentials(
                        phone, row[0]["url"] if row else None, getter,
                        app_title=self.store.get("cfx_app_title") or "Whisper",
                        headless=bool(self.store.get("cfx_headless", True)))
                except Exception as e:  # noqa
                    entry["error"] = str(e) or "Camoufox failed"
            entry["task"] = asyncio.create_task(run())
            self.autoapi[token] = entry
            return J({"token": token})

        @r.get("/autoapi/{token}")
        async def autoapi_status(req):
            e = self.autoapi.get(req.match_info["token"])
            if not e:
                return J({"error": "expired"}, status=404)
            res = e["result"]
            return J({"waiting_code": e["waiting_code"] and not e["future"].done(), "error": e["error"],
                      "api_id": res[0] if res else None, "api_hash": res[1] if res else None})

        @r.post("/autoapi/{token}/code")
        async def autoapi_code(req):
            e = self.autoapi.get(req.match_info["token"])
            if not e or e["future"].done():
                return J({"error": "Nothing is waiting for a code."}, status=400)
            e["future"].set_result(str((await req.json()).get("code", "")).strip())
            return J({"ok": True})

        # ----- autonomous provisioning -----
        @r.post("/provision/start")
        async def prov_start(req):
            try:
                self.prov.start(await req.json())
            except RuntimeError as e:
                return J({"error": str(e)}, status=400)
            return J({"ok": True})

        @r.get("/provision/status")
        async def prov_status(_):
            return J(self.prov.state)

        @r.post("/provision/cancel")
        async def prov_cancel(_):
            self.prov.cancel()
            return J({"ok": True})

        # ----- accounts -----
        @r.get("/accounts")
        async def accounts(_):
            ready = await self.ensure_clients()
            rows = self.store.rows("SELECT a.id,a.user_id,a.phone,a.name,a.username,a.active,a.api_id,a.proxy_id,p.label proxy_label "
                                   "FROM accounts a LEFT JOIN proxies p ON p.id=a.proxy_id ORDER BY a.id")
            for a in rows:
                a["connected"] = a["id"] in ready
                a["groups"] = self.store.q("SELECT COUNT(*) FROM groups WHERE account_id=?", (a["id"],)).fetchone()[0]
            return J(rows)

        @r.post("/accounts/login/code")
        async def acc_code(req):
            b = await req.json()
            phone = "".join(ch for ch in str(b.get("phone", "")) if ch.isdigit() or ch == "+")
            if len(phone.lstrip("+")) < 7:
                return J({"error": "Enter the full phone number with country code, like +212600000000."}, status=400)
            api_id, api_hash = str(b.get("api_id", "")).strip(), str(b.get("api_hash", "")).strip()
            if not api_id.isdigit() or len(api_hash) < 16:
                return J({"error": "Enter this account's API ID (numbers) and API hash from my.telegram.org."}, status=400)
            api = (int(api_id), api_hash)
            token = secrets.token_hex(8)
            session = "acct_" + token
            proxy_id = b.get("proxy_id") or None
            c = self._new_client(session, api, proxy_id)
            try:
                await asyncio.wait_for(c.connect(), timeout=15)
                sent = await c.send_code_request(phone)
            except errors.ApiIdInvalidError:
                await self._discard(c, session)
                return J({"error": "Telegram rejected that API ID / hash. Copy them again from my.telegram.org."}, status=400)
            except errors.PhoneNumberInvalidError:
                await self._discard(c, session)
                return J({"error": "Telegram says that phone number is invalid."}, status=400)
            except errors.FloodWaitError as e:
                await self._discard(c, session)
                return J({"error": f"Too many tries. Telegram asks you to wait {e.seconds} seconds."}, status=429)
            except Exception as e:  # noqa
                await self._discard(c, session)
                return J({"error": f"Could not reach Telegram: {e}"}, status=400)
            self.pending[token] = {"client": c, "phone": phone, "hash": sent.phone_code_hash, "session": session, "api": api, "proxy_id": proxy_id}
            return J({"token": token})

        @r.post("/accounts/login/verify")
        async def acc_verify(req):
            b = await req.json()
            p = self.pending.get(b.get("token"))
            if not p:
                return J({"error": "This sign-in expired. Start again."}, status=400)
            c = p["client"]
            try:
                if p.get("need_password"):
                    if not b.get("password"):
                        return J({"need_password": True})
                    await c.sign_in(password=b["password"])
                else:
                    await c.sign_in(p["phone"], str(b.get("code", "")).strip(), phone_code_hash=p["hash"])
            except errors.SessionPasswordNeededError:
                p["need_password"] = True
                return J({"need_password": True})
            except errors.PhoneCodeInvalidError:
                return J({"error": "That code is wrong. Check Telegram and try again."}, status=400)
            except errors.PhoneCodeExpiredError:
                return J({"error": "That code expired. Press Cancel and start again."}, status=400)
            except errors.PasswordHashInvalidError:
                return J({"error": "Wrong 2FA password."}, status=400)
            me = await c.get_me()
            self.pending.pop(b["token"], None)
            if self.store.rows("SELECT id FROM accounts WHERE user_id=?", (me.id,)):
                await self._discard(c, p["session"], logout=False)
                return J({"error": "This account is already added."}, status=400)
            await c.disconnect()  # ensure_clients reopens it with the message handler
            cur = self.store.q("INSERT INTO accounts(session,api_id,api_hash,proxy_id,created) VALUES(?,?,?,?,?)",
                             (p["session"], p["api"][0], p["api"][1], p.get("proxy_id"), int(time.time())))
            self._save_profile(cur.lastrowid, me, p["phone"])
            await self.ensure_clients()
            return J({"ok": True, "id": cur.lastrowid})

        @r.post("/accounts/login/cancel")
        async def acc_cancel(req):
            p = self.pending.pop((await req.json()).get("token"), None)
            if p:
                await self._discard(p["client"], p["session"], logout=False)
            return J({"ok": True})

        @r.post("/accounts/{aid}")
        async def acc_update(req):
            aid, b = int(req.match_info["aid"]), await req.json()
            if "active" in b:
                self.store.q("UPDATE accounts SET active=? WHERE id=?", (1 if b["active"] else 0, aid))
                c = self.clients.pop(aid, None)
                if c:
                    await c.disconnect()
            if "proxy_id" in b:
                self.store.q("UPDATE accounts SET proxy_id=? WHERE id=?", (b["proxy_id"] or None, aid))
                c = self.clients.pop(aid, None)
                if c:
                    await c.disconnect()
            return J({"ok": True})

        @r.delete("/accounts/{aid}")
        async def acc_delete(req):
            aid = int(req.match_info["aid"])
            a = self.store.rows("SELECT * FROM accounts WHERE id=?", (aid,))
            if not a:
                return J({"ok": True})
            api = self._api(a[0])
            c = self.clients.pop(aid, None) or (self._new_client(a[0]["session"], api) if api else None)
            if c:
                await self._discard(c, a[0]["session"], logout=True)
            self.store.q("DELETE FROM accounts WHERE id=?", (aid,))
            self.store.q("UPDATE groups SET account_id=NULL WHERE account_id=?", (aid,))
            return J({"ok": True})

        @r.get("/groups")
        async def groups(_):
            for aid, c in (await self.ensure_clients()).items():
                async for d in c.iter_dialogs():
                    if d.is_group:
                        self.store.q("INSERT INTO groups(chat_id,title,account_id) VALUES(?,?,?) ON CONFLICT(chat_id) "
                                     "DO UPDATE SET title=excluded.title, account_id=COALESCE(groups.account_id, excluded.account_id)",
                                     (d.id, d.name, aid))
            return J(self.store.rows("SELECT g.*, a.name account_name FROM groups g LEFT JOIN accounts a ON a.id=g.account_id "
                                     "ORDER BY g.watched DESC, g.title"))

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
    app.add_routes(updater.routes())
    renderer_dir = os.path.join(updater.APP_ROOT, "renderer")

    async def index_handler(_):
        return web.FileResponse(os.path.join(renderer_dir, "index.html"))
    app.router.add_get("/", index_handler)
    app.router.add_static("/", renderer_dir)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", a.port).start()
    log.info("listening on 127.0.0.1:%d", a.port)
    try:
        await d.ensure_clients()
    except Exception as e:  # noqa
        log.warning("telegram connect deferred: %s", e)
    await d.worker()


if __name__ == "__main__":
    asyncio.run(main())
