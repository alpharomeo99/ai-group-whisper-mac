#!/usr/bin/env python3
"""AI Group Whisper daemon: Telethon client + SQLite queue + local HTTP API for the Electron UI."""
import argparse
import asyncio
import logging
import os
import secrets
import sys
import random
import json
import time

from aiohttp import web
from contextlib import asynccontextmanager
import daily_batch
import memory
import usage
from telethon import TelegramClient, events, errors

import ai
import cfx_browser as cfx
import updater
import debuglog
from debuglog import dbg
from provisioner import Provisioner
from textverified import TextVerified
from vmos import Vmos
from store import Store
from orchestrator import PersonaCadenceOrchestrator

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
        self.orchestrator = PersonaCadenceOrchestrator(self.store)
        self.daily_batch = daily_batch.DailyBatchEngine(self.store, ai)
        self.memory = memory.MemoryEngine(self.store, ai)
        self.usage = usage.UsageTracker(self.store)
        self.autoapi = {}        # token -> {"future": code future, "task": camoufox task}
        self.cfx_install = {"running": False, "log": ""}

    async def memory_pruner_loop(self):
        """Autonomous Agent that wakes up every 12 hours to prune expired, useless, and overextended memories."""
        while True:
            try:
                await asyncio.sleep(12 * 3600)
                res = self.memory.prune_memories()
                log.info("Autonomous memory cleanup completed: %s", res)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Autonomous memory cleanup loop error: %s", e)
                await asyncio.sleep(600)

    async def ensure_camoufox(self, force=False):
        """Camoufox ships with the app: install the package and its browser automatically in the background."""
        if self.cfx_install["running"]:
            return
        import importlib.util
        if not force and importlib.util.find_spec("camoufox") is not None:
            p = await asyncio.create_subprocess_exec(sys.executable, "-m", "camoufox", "path",
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=os.path.expanduser("~"))
            out = (await p.communicate())[0].decode(errors="ignore")
            if p.returncode == 0 and "/" in out and os.path.isdir(out.strip().splitlines()[-1].strip()):
                return
        self.cfx_install.update(running=True, log="Setting up Camoufox (open-source stealth browser)...\n")
        ok = True
        for cmd in ([sys.executable, "-m", "pip", "install", "-q", "-U", "camoufox[geoip]"],
                    [sys.executable, "-m", "camoufox", "fetch"]):
            p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT, cwd=os.path.expanduser("~"))
            async for line in p.stdout:
                self.cfx_install["log"] = (self.cfx_install["log"] + line.decode(errors="ignore"))[-4000:]
            await p.wait()
            if p.returncode != 0:
                ok = False
                break
        self.cfx_install["log"] += "\nCamoufox is ready.\n" if ok else "\nSetup failed - press Repair to retry.\n"
        self.cfx_install["running"] = False

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
        dbg("telethon", f"Connecting new client session={session} api_id={api_id} proxy_id={proxy_id}")
        await asyncio.wait_for(c.connect(), timeout=20)
        dbg("telethon", "Connected, requesting login code")
        sent = await c.send_code_request(phone)
        dbg("telethon", f"Code requested, delivery type: {type(sent.type).__name__}")
        await asyncio.sleep(5)
        code = await code_getter()
        if not code:
            dbg("telethon", "No login code arrived, discarding session", "error")
            await self._discard(c, session)
            raise RuntimeError("The Telegram login code did not arrive.")
        dbg("telethon", f"Signing in with code {code}")
        try:
            await c.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
        except Exception as e:  # noqa
            debuglog.exc("telethon", e)
            raise
        me = await c.get_me()
        dbg("telethon", f"Signed in as id={me.id} name={me.first_name!r} username={me.username}")
        await c.disconnect()
        cur = self.store.q("INSERT INTO accounts(session,api_id,api_hash,proxy_id,created) VALUES(?,?,?,?,?)",
                           (session, int(api_id), api_hash, proxy_id, int(time.time())))
        new_aid = cur.lastrowid
        self._save_profile(new_aid, me, phone)
        await self.ensure_clients()
        try:
            await self.sync_all_dialogs(account_ids=[new_aid])
        except Exception as e:
            log.warning("adopt_number initial dialog sync failed for account %s: %s", new_aid, e)
        return new_aid

    async def apply_persona(self, aid, style, want_photo, log):
        """Set name, bio, username and photo on a connected account via MTProto."""
        import persona
        from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest
        from telethon.tl.functions.photos import UploadProfilePhotoRequest
        settings = {k: self.store.get(k) for k in ("fal_key", "ai_model", "persona_model")}
        c = (await self.ensure_clients()).get(aid)
        if not c:
            raise RuntimeError("Account is not connected")
        p = await persona.generate(settings, style)
        try:
            import json as _j
            cur = self.store.q("INSERT INTO personas(name,prompt,color,created,bio,details) VALUES(?,?,?,?,?,?)",
                               (f"{p['first_name']} {p.get('last_name') or ''}".strip(), p["system_prompt"],
                                random.choice(["#2fc4b2", "#7c6cff", "#ff8a4c", "#e45fa6", "#4ca8ff", "#9bd14c"]),
                                int(time.time()), p.get("bio") or "", _j.dumps(p)))
            self.store.q("UPDATE accounts SET persona_id=? WHERE id=?", (cur.lastrowid, aid))
            log(f"Persona saved and wired to this account ({p.get('archetype')}, {p.get('age')}, {p.get('location')})")
        except Exception as e:  # noqa
            log(f"Persona not saved ({e})")
        await c(UpdateProfileRequest(first_name=p["first_name"], last_name=p.get("last_name") or "", about=p.get("bio") or ""))
        log(f"Profile set: {p['first_name']} {p.get('last_name') or ''}")
        for u in persona.usernames(p):
            try:
                await c(UpdateUsernameRequest(u))
                log(f"Username set: @{u}")
                break
            except errors.UsernameOccupiedError:
                continue
            except errors.RPCError as e:
                log(f"Username skipped ({e.__class__.__name__})")
                break
        if want_photo:
            try:
                img = await persona.photo(settings, p)
                if img:
                    f = await c.upload_file(img, file_name="avatar.jpg")
                    await c(UploadProfilePhotoRequest(file=f))
                    log("Profile photo set")
                else:
                    log("Photo skipped (add a fal.ai key in Settings)")
            except Exception as e:  # noqa
                log(f"Photo skipped ({e})")
        me = await c.get_me()
        self._save_profile(aid, me, me.phone and "+" + me.phone.lstrip("+"))

    def get_scout_account_id(self):
        """Returns designated scout account id or the first active account."""
        scout_aid = self.store.get("scout_account_id")
        if scout_aid:
            acc = self.store.rows("SELECT id FROM accounts WHERE id=? AND active=1", (int(scout_aid),))
            if acc:
                return acc[0]["id"]
        first_active = self.store.rows("SELECT id FROM accounts WHERE active=1 ORDER BY id ASC LIMIT 1")
        return first_active[0]["id"] if first_active else None

    async def ensure_clients(self):
        """Maintains exactly ONE persistent scout listener to catch incoming events in watched groups.
        All other accounts remain completely dormant (0 sockets, 0 idle RAM) and connect ephemerally on demand.
        Returns {scout_account_id: scout_client}.
        """
        scout_aid = self.get_scout_account_id()
        if not scout_aid:
            await self.drop_clients()
            return {}

        # Disconnect any non-scout accounts still open
        for aid in list(self.clients.keys()):
            if aid != scout_aid:
                c = self.clients.pop(aid)
                try:
                    await c.disconnect()
                except Exception:
                    pass

        c = self.clients.get(scout_aid)
        if c is None:
            acc = self.store.rows("SELECT * FROM accounts WHERE id=?", (scout_aid,))
            if not acc:
                return {}
            a = acc[0]
            api = self._api(a)
            if not api:
                return {}
            c = self._new_client(a["session"], api, a.get("proxy_id"))
            c.add_event_handler(self._handler_for(scout_aid), events.NewMessage())
            c.add_event_handler(self._chat_action_handler_for(scout_aid), events.ChatAction())
            self.clients[scout_aid] = c

        try:
            if not c.is_connected():
                await asyncio.wait_for(c.connect(), timeout=12)
            if await c.is_user_authorized():
                acc = self.store.rows("SELECT * FROM accounts WHERE id=?", (scout_aid,))
                if acc and not acc[0]["user_id"]:
                    me = await c.get_me()
                    self._save_profile(scout_aid, me, me.phone and "+" + me.phone.lstrip("+"))
                return {scout_aid: c}
        except Exception as e:
            log.warning("scout account %s connect failed: %s", scout_aid, e)
        return {}

    @asynccontextmanager
    async def account_session(self, account_id):
        """Context manager that provides an active Telegram client for any account.
        Reuses the persistent connection if it is the active scout; otherwise connects on demand
        and disconnects cleanly immediately after the action completes.
        """
        if not account_id:
            ready = await self.ensure_clients()
            if not ready:
                raise ConnectionError("No active accounts available")
            yield next(iter(ready.values()))
            return

        account_id = int(account_id)
        if account_id in self.clients and self.clients[account_id].is_connected():
            yield self.clients[account_id]
            return

        acc_rows = self.store.rows("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not acc_rows:
            raise ConnectionError(f"Account {account_id} not found in database")
        a = acc_rows[0]
        api = self._api(a)
        if not api:
            raise ConnectionError(f"Account {account_id} has missing API credentials")

        client = self._new_client(a["session"], api, a.get("proxy_id"))
        await asyncio.wait_for(client.connect(), timeout=15)
        if not await client.is_user_authorized():
            try:
                await client.disconnect()
            except Exception:
                pass
            raise ConnectionError(f"Account {account_id} ({a.get('name')}) is not authorized")

        try:
            yield client
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

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

    
    def _chat_action_handler_for(self, aid):
        async def h(event):
            try:
                if event.user_left or event.user_kicked or event.user_added or event.user_joined:
                    asyncio.create_task(self.sync_all_dialogs())
                    if event.user_added or event.user_joined:
                        cid = getattr(event, 'chat_id', None)
                        if cid:
                            self.ensure_group_personas_assigned(cid)
            except Exception:
                pass
        return h

    def _handler_for(self, aid):
        async def h(event):
            await self.on_message(event, aid)
        return h

    async def client_for(self, chat_id, account_id=None):
        ready = await self.ensure_clients()
        if account_id and account_id in ready:
            return ready[account_id]
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
        # 1. Handle 1-on-1 Direct Messages (DMs)
        if event.is_private:
            if not event.raw_text:
                return
            sender = await event.get_sender()
            peer_id = event.chat_id
            name = " ".join(x for x in (getattr(sender, 'first_name', None), getattr(sender, 'last_name', None)) if x) or getattr(sender, 'username', None) or "User"
            username = getattr(sender, 'username', '') or ''
            phone = getattr(sender, 'phone', '') or ''
            ts = int(event.date.timestamp()) if event.date else int(time.time())
            incoming = 0 if event.out else 1

            self.store.q(
                "INSERT INTO direct_chats(account_id, peer_id, peer_name, peer_username, peer_phone, last_msg, last_ts, unread_count) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id, peer_id) DO UPDATE SET "
                "peer_name=excluded.peer_name, peer_username=excluded.peer_username, "
                "peer_phone=COALESCE(excluded.peer_phone, direct_chats.peer_phone), "
                "last_msg=excluded.last_msg, last_ts=excluded.last_ts, "
                "unread_count = CASE WHEN ?=1 THEN direct_chats.unread_count + 1 ELSE direct_chats.unread_count END",
                (aid, peer_id, name, username, phone, event.raw_text, ts, 1 if incoming else 0, 1 if incoming else 0)
            )
            self.store.add_direct_message(aid, peer_id, event.id, name if incoming else "Me", incoming, event.raw_text, ts)

            system_enabled = bool(self.store.get("system_enabled", True))
            if incoming and not event.out and system_enabled:
                chat_info = self.store.rows("SELECT * FROM direct_chats WHERE account_id=? AND peer_id=?", (aid, peer_id))
                if chat_info and chat_info[0].get("auto_reply"):
                    pid = chat_info[0].get("persona_id")
                    if not pid:
                        acc = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
                        if acc:
                            pid = acc[0].get("persona_id")
                    self.store.enqueue("direct_reply", peer_id, {
                        "account_id": aid,
                        "persona_id": pid,
                        "trigger": event.id,
                        "auto_send": True
                    }, dedupe_key=f"dm_reply:{aid}:{peer_id}:{event.id}", delay=3)
            self.wake_event.set()
            return

        # 2. Handle Group Messages
        if not event.is_group or not event.raw_text:
            return
        chat_id = event.chat_id

        # Auto-discover group so it instantly appears in Network canvas
        try:
            chat = await event.get_chat()
            title = getattr(chat, 'title', None) or getattr(chat, 'name', None) or f"Group {chat_id}"
            acc_check = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
            has_persona = bool(acc_check and acc_check[0]["persona_id"])
            self.store.q(
                "INSERT INTO groups(chat_id,title,account_id) VALUES(?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
                (chat_id, title, aid if has_persona else None)
            )
            self.store.q("INSERT OR IGNORE INTO group_accounts(chat_id, account_id) VALUES(?,?)", (chat_id, aid))
        except Exception:
            pass
        chat_id = event.chat_id
        g = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
        if g and g[0]["account_id"] not in (None, aid) and g[0]["account_id"] in self.clients:
            return  # another account owns this group; avoid duplicates
        if not g or not g[0]["watched"]:
            return
        sender = await event.get_sender()
        name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "unknown"
        sender_id = getattr(sender, "id", None) or getattr(event, "sender_id", None)

        # 3. IDENTIFY SENDER: Internal Bot vs External Human
        all_accounts = self.store.rows("SELECT user_id FROM accounts")
        our_user_ids = {a["user_id"] for a in all_accounts if a.get("user_id")}
        is_internal_bot = bool(event.out or (sender_id and sender_id in our_user_ids))

        # Record message with is_bot flag
        self.store.add_message(
            chat_id, event.id, name, event.raw_text, int(event.date.timestamp()),
            is_bot=1 if is_internal_bot else 0, sender_id=sender_id
        )
        self.usage.log_event("msg_recv", account_id=client_acc_id, chat_id=chat_id)

        # STRICT RULE: Bot-to-bot dialogue is governed strictly by the Daily Batch Schedule.
        # Messages sent by any bot account in our system DO NOT trigger live AI replies!
        if is_internal_bot:
            self.wake_event.set()
            return

        # 4. LIVE AI ORCHESTRATION: Only triggered when an external HUMAN sends a message!
        system_enabled = bool(self.store.get("system_enabled", True))
        if g[0]["auto_reply"] and system_enabled:
            me = await event.client.get_me()
            is_direct = event.mentioned
            if not is_direct and event.is_reply:
                try:
                    rep = await event.get_reply_message()
                    # Check if replied to this client or any client we control
                    client_uids = {getattr(c, "_self_id", None) for c in self.clients.values() if hasattr(c, "_self_id")}
                    if rep and (rep.sender_id == me.id or rep.sender_id in client_uids):
                        is_direct = True
                except Exception:
                    pass

            # Ensure all member accounts in this group have personas assigned
            self.ensure_group_personas_assigned(chat_id)

            # Gather candidate personas for ALL accounts in this group:
            # 1. Group matrix assignments (group_personas) with valid accounts
            candidates = self.store.rows(
                "SELECT p.*, gp.account_id, gp.persona_id FROM group_personas gp "
                "JOIN personas p ON p.id=gp.persona_id WHERE gp.chat_id=? AND gp.account_id IS NOT NULL",
                (chat_id,)
            )

            # 2. Also include any accounts in group_accounts that have account-level personas
            all_group_accs = self.store.rows("SELECT account_id FROM group_accounts WHERE chat_id=?", (chat_id,))
            existing_aids = {c["account_id"] for c in candidates}
            for ga in all_group_accs:
                ga_aid = ga.get("account_id")
                if ga_aid and ga_aid not in existing_aids:
                    acc_p = self.store.rows(
                        "SELECT p.*, a.id account_id, p.id persona_id FROM accounts a "
                        "JOIN personas p ON p.id=a.persona_id WHERE a.id=?",
                        (ga_aid,)
                    )
                    if acc_p:
                        candidates.extend(acc_p)
                        existing_aids.add(ga_aid)

            # 3. Fallback: group owner account's persona
            if not candidates and g[0].get("account_id"):
                acc_p = self.store.rows(
                    "SELECT p.*, a.id account_id, p.id persona_id FROM accounts a "
                    "JOIN personas p ON p.id=a.persona_id WHERE a.id=?",
                    (g[0]["account_id"],)
                )
                if acc_p:
                    candidates = acc_p

            if candidates:
                pid, aid, delay, reason = self.orchestrator.arbiter_decision(
                    chat_id, candidates,
                    {"text": event.raw_text, "sender": name, "id": event.id},
                    is_direct_mention=is_direct
                )
                if pid and aid:
                    self.orchestrator.record_activity(chat_id, pid)
                    self.store.enqueue(
                        "draft_reply", chat_id,
                        {"trigger": event.id, "auto_send": True, "account_id": aid, "persona_id": pid, "reason": reason},
                        dedupe_key=f"reply:{chat_id}:{event.id}",
                        delay=int(delay)
                    )
            elif is_direct:
                # Direct mention fallback with no explicit persona assigned
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
            if self.suspended or self.paused_reason or not bool(self.store.get("system_enabled", True)):
                continue
            if not await self.ensure_clients():
                continue
            for job in self.store.claim(limit=3):
                await self.run_job(job)

    def persona_for(self, g, persona_id=None, account_id=None):
        """Group override text wins; then the chosen persona; then the persona designed for this group and account; then account fallback."""
        pr = ""
        if (g.get("persona") or "").strip():
            pr = g["persona"]
        elif persona_id:
            p = self.store.rows("SELECT name, prompt FROM personas WHERE id=?", (persona_id,))
            if p and p[0]["prompt"]:
                pr = p[0]["prompt"]
                pr = pr if pr.lstrip().startswith("You are") else f"You are {p[0]['name']}. {pr}"
        if not pr:
            aid = account_id or g.get("account_id")
            gp = self.store.rows("SELECT p.prompt FROM group_personas gp JOIN personas p ON p.id=gp.persona_id "
                                 "WHERE gp.chat_id=? AND gp.account_id=?", (g.get("chat_id"), aid))
            if gp and gp[0]["prompt"]:
                pr = gp[0]["prompt"]
            else:
                r = self.store.rows("SELECT p.name,p.prompt FROM accounts a JOIN personas p ON p.id=a.persona_id WHERE a.id=?", (aid,))
                if r:
                    raw_pr = r[0]["prompt"] or ""
                    pr = raw_pr if raw_pr.lstrip().startswith("You are") else f"You are {r[0]['name']}. {raw_pr}"

        if not pr:
            pr = "You are a regular member of this Telegram group."

        # Fetch complete group profile if about/domain_knowledge not present in g dict
        if isinstance(g, dict) and g.get("chat_id") and not g.get("domain_knowledge"):
            full_g = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (g["chat_id"],))
            if full_g:
                g = full_g[0]

        # Inject group context & what the group is about
        group_ctx = []
        if isinstance(g, dict):
            if g.get("title"):
                group_ctx.append(f"CURRENT TELEGRAM GROUP: {g['title']}")
            if g.get("about"):
                group_ctx.append(f"WHAT THIS GROUP IS ABOUT: {g['about']}")
            if g.get("domain_knowledge"):
                group_ctx.append(f"DOMAIN KNOWLEDGE & TOPICS DISCUSSED:\n{g['domain_knowledge']}")
            if g.get("rules"):
                group_ctx.append(f"GROUP RULES & PARTICIPATION GUIDELINES: {g['rules']}")
            if g.get("tags"):
                group_ctx.append(f"KEY TOPIC TAGS: {g['tags']}")

        if group_ctx:
            pr = pr + "\n\n--- GROUP CONTEXT & TOPIC MASTERY ---\n" + "\n".join(group_ctx)

        # Inject Episodic & Semantic Persona Memory Context
        effective_pid = persona_id
        if not effective_pid:
            aid = account_id or (g.get("account_id") if isinstance(g, dict) else None)
            if aid and isinstance(g, dict) and g.get("chat_id"):
                gp = self.store.rows("SELECT persona_id FROM group_personas WHERE chat_id=? AND account_id=?", (g["chat_id"], aid))
                if gp:
                    effective_pid = gp[0]["persona_id"]
            if not effective_pid and aid:
                ap = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
                if ap:
                    effective_pid = ap[0]["persona_id"]

        if effective_pid:
            cid = g.get("chat_id") if isinstance(g, dict) else None
            mem_ctx = self.memory.get_prompt_memory_context(effective_pid, chat_id=cid)
            if mem_ctx:
                pr = pr + "\n\n" + mem_ctx

        pr += (
            "\n\nCRITICAL VENDOR EXPERIENCE TRIGGER RULE:\n"
            "Never volunteer unprompted vendor reviews, order testimonials, or package praise. "
            "Only if an external human participant specifically asks whether anyone had a BAD experience, issues, complaints, or asks if the vendor is legit/scam, "
            "casually and briefly state that personally you have only had good, smooth experiences (orders arrived clean, zero issues on your end). "
            "Never sound defensive or like a corporate shill."
        )

        return pr

    def ensure_group_personas_assigned(self, chat_id):
        """Ensures accounts participating in this group that have an assigned persona are bound to group_personas."""
        try:
            acc_rows = self.store.rows("SELECT account_id FROM group_accounts WHERE chat_id=?", (chat_id,))
            acc_ids = {r["account_id"] for r in acc_rows if r.get("account_id")}
            g = self.store.rows("SELECT account_id FROM groups WHERE chat_id=?", (chat_id,))
            if g and g[0].get("account_id"):
                acc_ids.add(g[0]["account_id"])

            for aid in acc_ids:
                if not aid:
                    continue
                acc = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
                pid = acc[0].get("persona_id") if acc else None
                if pid:
                    gp = self.store.rows("SELECT persona_id FROM group_personas WHERE chat_id=? AND account_id=?", (chat_id, aid))
                    if not gp:
                        self.store.q("UPDATE group_personas SET account_id=NULL WHERE chat_id=? AND account_id=?", (chat_id, aid))
                        r = self.store.q("UPDATE group_personas SET account_id=? WHERE chat_id=? AND persona_id=?", (aid, chat_id, pid))
                        if r.rowcount == 0:
                            self.store.q("INSERT INTO group_personas(chat_id, persona_id, account_id, created) VALUES(?,?,?,?)",
                                         (chat_id, pid, aid, int(time.time())))
        except Exception as e:
            log.warning("ensure_group_personas_assigned error: %s", e)

    def ensure_group_personas_assigned_for_account(self, aid):
        """When an account has a persona assigned, auto-binds that persona to all groups the account is in."""
        try:
            acc = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
            pid = acc[0].get("persona_id") if acc else None
            if not pid:
                return
            groups = self.store.rows("SELECT chat_id FROM group_accounts WHERE account_id=?", (aid,))
            for gr in groups:
                cid = gr["chat_id"]
                gp = self.store.rows("SELECT persona_id FROM group_personas WHERE chat_id=? AND account_id=?", (cid, aid))
                if not gp:
                    self.store.q("UPDATE group_personas SET account_id=NULL WHERE chat_id=? AND account_id=?", (cid, aid))
                    r = self.store.q("UPDATE group_personas SET account_id=? WHERE chat_id=? AND persona_id=?", (aid, cid, pid))
                    if r.rowcount == 0:
                        self.store.q("INSERT INTO group_personas(chat_id, persona_id, account_id, created) VALUES(?,?,?,?)",
                                     (cid, pid, aid, int(time.time())))
        except Exception as e:
            log.warning("ensure_group_personas_assigned_for_account error: %s", e)

    def typing_secs(self, g, text):
        """Typing-indicator delay accurately scaled to outgoing message size and realistic human typing cadence.
        Features lognormal keystroke velocity (18-24 CPS), cognitive initiation hesitation,
        and micro-pauses for punctuation and length.
        """
        import json as _j
        import random, math
        msg_len = max(len(text or ""), 1)

        t = {"min_seconds": 1.5, "max_seconds": 35.0, "chars_per_second": 21.0}
        cid = g.get("chat_id") if isinstance(g, dict) else None
        aid = g.get("account_id") if isinstance(g, dict) else None
        r = []
        if cid and aid:
            r = self.store.rows("SELECT p.details FROM group_personas gp JOIN personas p ON p.id=gp.persona_id "
                                "WHERE gp.chat_id=? AND gp.account_id=?", (cid, aid))
        if not r and aid:
            r = self.store.rows("SELECT p.details FROM accounts a JOIN personas p ON p.id=a.persona_id WHERE a.id=?", (aid,))

        try:
            if r and r[0].get("details"):
                dt = _j.loads(r[0]["details"] or "{}").get("typing") or {}
                t.update(dt)
        except Exception:
            pass

        base_cps = max(float(t.get("chars_per_second") or 21.0), 5.0)

        # Human keystroke variance (lognormal distribution around base CPS)
        effective_cps = base_cps * random.lognormvariate(0.0, 0.12)

        # Initial cognitive hesitation (looking at screen / placing fingers before typing)
        hesitation = random.uniform(1.2, 2.4)

        # Typing duration proportional to text length
        typing_duration = msg_len / effective_cps

        # Micro-pauses for longer messages (phone autocorrect, punctuation pauses)
        # Add ~0.6s per 80 characters of text
        micro_pauses = (msg_len / 80.0) * random.uniform(0.4, 0.8)

        total_secs = hesitation + typing_duration + micro_pauses

        # Dynamic max_seconds: allow longer messages to take proportional time rather than arbitrary clipping
        dynamic_max = max(float(t.get("max_seconds", 35.0)), (msg_len / 12.0) + 4.0)
        dynamic_max = min(dynamic_max, 40.0)  # absolute safety ceiling

        min_secs = max(float(t.get("min_seconds", 1.5)), 1.2)
        return max(min_secs, min(total_secs, dynamic_max))

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
                chosen_persona = self.persona_for(g, persona_id=p.get("persona_id"), account_id=p.get("account_id"))
                text, meta = await ai.draft_reply_with_meta(settings, g.get("title", ""), chosen_persona,
                                                            self.store.recent(job["chat_id"], 30))
                # Log AI token & latency usage
                self.usage.log_ai("ai_chat", persona_id=p.get("persona_id"), account_id=p.get("account_id"),
                                  chat_id=job["chat_id"], tokens_prompt=meta.get("prompt_tokens"),
                                  tokens_completion=meta.get("completion_tokens"), latency_ms=meta.get("latency_ms"),
                                  model=meta.get("model"))
                if p.get("auto_send"):
                    self.store.enqueue("send", job["chat_id"],
                                       {"text": text, "reply_to": p.get("trigger"), "account_id": p.get("account_id")},
                                       dedupe_key=f"send:{job['id']}")
                else:
                    self.store.q("INSERT INTO summaries(chat_id,body,created) VALUES(?,?,?)",
                                 (job["chat_id"], "Draft reply:\n" + text, int(time.time())))
            elif job["kind"] == "send":
                aid = p.get("account_id") or g.get("account_id")
                async with self.account_session(aid) as cl:
                    secs = self.typing_secs(g, p["text"])
                    try:
                        async with cl.action(job["chat_id"], "typing"):
                            await asyncio.sleep(secs)
                    except Exception:  # noqa
                        pass
                    await cl.send_message(job["chat_id"], p["text"], reply_to=p.get("reply_to"))
                    # Record memory and message sent usage
                    eff_pid = p.get("persona_id")
                    if not eff_pid and aid:
                        acc_p = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
                        eff_pid = acc_p[0]["persona_id"] if acc_p else None
                    if eff_pid:
                        self.memory.auto_record_statement(eff_pid, aid, job["chat_id"], p.get("text"))
                    self.usage.log_event("msg_sent", persona_id=eff_pid, account_id=aid, chat_id=job["chat_id"])

            elif job["kind"] == "batch_scheduled_send":
                # Check if a human recently spoke in this group:
                last_human_ts = self.store.get_last_human_message_ts(job["chat_id"])
                now_ts = int(time.time())
                pause_secs = int(self.store.get("batch_human_pause_seconds", 900))
                if last_human_ts and (now_ts - last_human_ts) < pause_secs:
                    # Defer batch message by remaining pause time + jitter so bots do not talk over human!
                    defer_secs = (pause_secs - (now_ts - last_human_ts)) + random.randint(60, 240)
                    if p.get("batch_item_id"):
                        self.store.q("UPDATE daily_batch_items SET status='postponed' WHERE id=?", (p["batch_item_id"],))
                    self.store.finish(job["id"], False, "Postponed: human spoke recently in chat", retry_in=defer_secs)
                    return

                aid = p.get("account_id") or g.get("account_id")
                async with self.account_session(aid) as cl:
                    secs = self.typing_secs(g, p["text"])
                    try:
                        async with cl.action(job["chat_id"], "typing"):
                            await asyncio.sleep(secs)
                    except Exception:  # noqa
                        pass
                    await cl.send_message(job["chat_id"], p["text"], reply_to=p.get("reply_to"))
                    # Record memory and message sent usage
                    eff_pid = p.get("persona_id")
                    if not eff_pid and aid:
                        acc_p = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
                        eff_pid = acc_p[0]["persona_id"] if acc_p else None
                    if eff_pid:
                        self.memory.auto_record_statement(eff_pid, aid, job["chat_id"], p.get("text"))
                    self.usage.log_event("msg_sent", persona_id=eff_pid, account_id=aid, chat_id=job["chat_id"])

                if p.get("batch_item_id"):
                    self.store.mark_batch_item_sent(p["batch_item_id"], int(time.time()))

            elif job["kind"] == "direct_send":
                aid = p.get("account_id")
                peer_id = job["chat_id"]
                async with self.account_session(aid) as cl:
                    try:
                        async with cl.action(peer_id, "typing"):
                            secs = self.typing_secs({"chat_id": peer_id, "account_id": aid}, p.get("text", ""))
                            await asyncio.sleep(secs)
                    except Exception:
                        pass
                    sent = await cl.send_message(peer_id, p["text"], reply_to=p.get("reply_to"))
                    self.store.add_direct_message(aid, peer_id, sent.id, "Me", 0, p["text"], int(time.time()))
                    eff_pid = p.get("persona_id")
                    if not eff_pid and aid:
                        acc_p = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
                        eff_pid = acc_p[0]["persona_id"] if acc_p else None
                    if eff_pid:
                        self.memory.auto_record_statement(eff_pid, aid, peer_id, p.get("text"))
                    self.usage.log_event("msg_sent", persona_id=eff_pid, account_id=aid, chat_id=peer_id)
            elif job["kind"] == "direct_reply":
                aid = p.get("account_id")
                peer_id = job["chat_id"]
                pid = p.get("persona_id")
                persona_row = self.store.rows("SELECT * FROM personas WHERE id=?", (pid,)) if pid else []
                persona_prompt = persona_row[0]["prompt"] if persona_row else "You are chatting 1-on-1 on Telegram. Keep responses natural, human, and direct."
                recent_msgs = self.store.direct_messages_recent(aid, peer_id, 20)
                formatted = [{"sender": m["sender_name"], "text": m["text"]} for m in recent_msgs]
                chat_info = self.store.rows("SELECT * FROM direct_chats WHERE account_id=? AND peer_id=?", (aid, peer_id))
                peer_name = chat_info[0]["peer_name"] if chat_info else "User"
                text = await ai.draft_reply(settings, f"1-on-1 conversation with {peer_name}", persona_prompt, formatted)
                if p.get("auto_send"):
                    self.store.enqueue("direct_send", peer_id, {
                        "account_id": aid,
                        "text": text,
                        "reply_to": p.get("trigger")
                    }, dedupe_key=f"dmsend:{job['id']}")
                else:
                    self.store.add_direct_message(aid, peer_id, 0, "AI Draft", 0, "[Draft]: " + text, int(time.time()))
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

    async def sync_all_dialogs(self, account_ids=None, force_all=False):
        """Discover every group's membership for selected active accounts, including dormant accounts.

        Telegram dialogs require each account's own session. The persistent Scout only covers one
        account, so all other active accounts are opened ephemerally and closed after discovery.
        Membership reconciliation only runs after a complete successful dialog iteration.
        """
        scout_ready = await self.ensure_clients()
        scout_aid = self.get_scout_account_id()

        if account_ids is not None:
            target_ids = []
            for account_id in account_ids:
                try:
                    target_ids.append(int(account_id))
                except (TypeError, ValueError):
                    continue
        else:
            now = int(time.time())
            account_rows = self.store.rows("SELECT id, last_synced_at FROM accounts WHERE active=1 ORDER BY id")
            target_ids = []
            for row in account_rows:
                aid = int(row["id"])
                last_sync = row.get("last_synced_at") or 0
                # Always sync scout, sync any account never synced, sync if force_all, or sync if stale (> 300s)
                if force_all or aid == scout_aid or last_sync == 0 or (now - last_sync) >= 300:
                    target_ids.append(aid)

        for aid in target_ids:
            if aid in scout_ready and self.clients.get(aid) and self.clients[aid].is_connected():
                c = self.clients[aid]
                close_after = False
                manager = None
            else:
                manager = self.account_session(aid)
                try:
                    c = await manager.__aenter__()
                    close_after = True
                except Exception as e:
                    log.warning("dialog sync could not connect account %s: %s", aid, e)
                    continue
            seen_group_ids = set()
            sync_succeeded = False
            try:
                async for d in c.iter_dialogs(limit=None):
                    is_grp = (
                        d.is_group or
                        getattr(d.entity, 'megagroup', False) or
                        getattr(d.entity, 'gigagroup', False) or
                        (d.is_channel and not getattr(d.entity, 'broadcast', False))
                    )
                    if is_grp:
                        seen_group_ids.add(d.id)
                        title = d.name or getattr(d.entity, 'title', None) or f"Group {d.id}"
                        acc_check = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
                        has_persona = bool(acc_check and acc_check[0]["persona_id"])
                        self.store.q(
                            "INSERT INTO groups(chat_id,title,account_id) VALUES(?,?,?) "
                            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, "
                            "account_id=COALESCE(groups.account_id, ?)",
                            (d.id, title, aid if has_persona else None)
                        )
                        self.store.q(
                            "INSERT OR IGNORE INTO group_accounts(chat_id, account_id) VALUES(?,?)",
                            (d.id, aid)
                        )
                    elif d.is_user and not getattr(d.entity, 'is_self', False) and not getattr(d.entity, 'bot', False):
                        p_name = d.name or "User"
                        p_user = getattr(d.entity, 'username', '') or ''
                        p_phone = getattr(d.entity, 'phone', '') or ''
                        l_msg = (d.message.message if d.message else '') or ''
                        l_ts = int(d.message.date.timestamp()) if d.message and d.message.date else int(time.time())
                        unr = d.unread_count or 0
                        self.store.q(
                            "INSERT INTO direct_chats(account_id, peer_id, peer_name, peer_username, peer_phone, last_msg, last_ts, unread_count) "
                            "VALUES(?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(account_id, peer_id) DO UPDATE SET "
                            "peer_name=excluded.peer_name, peer_username=excluded.peer_username, "
                            "peer_phone=COALESCE(excluded.peer_phone, direct_chats.peer_phone), "
                            "last_msg=excluded.last_msg, last_ts=excluded.last_ts, "
                            "unread_count=excluded.unread_count",
                            (aid, d.id, p_name, p_user, p_phone, l_msg, l_ts, unr)
                        )
                sync_succeeded = True
                # Reconcile only after the provider returned a complete dialog list.
                prev_rows = self.store.rows("SELECT chat_id FROM group_accounts WHERE account_id=?", (aid,))
                prev_cids = {r["chat_id"] for r in prev_rows}
                left_cids = prev_cids - seen_group_ids
                for lcid in left_cids:
                    self.store.q("DELETE FROM group_accounts WHERE chat_id=? AND account_id=?", (lcid, aid))
                    self.store.q("UPDATE group_personas SET account_id=NULL WHERE chat_id=? AND account_id=?", (lcid, aid))
                    rem = self.store.rows("SELECT account_id FROM group_accounts WHERE chat_id=?", (lcid,))
                    if not rem:
                        self.store.q("UPDATE groups SET account_id=NULL WHERE chat_id=?", (lcid,))
                    else:
                        self.store.q("UPDATE groups SET account_id=? WHERE chat_id=?", (rem[0]["account_id"], lcid))
                    log.info("Detected account %s left or removed from group %s", aid, lcid)
                self.store.q("UPDATE accounts SET last_synced_at=? WHERE id=?", (int(time.time()), aid))
                self.ensure_group_personas_assigned_for_account(aid)
            except Exception as e:
                log.warning("dialog sync failed for account %s: %s", aid, e)
            finally:
                if close_after and manager:
                    try:
                        await manager.__aexit__(None, None, None)
                    except Exception as e:
                        log.debug("account %s disconnect after dialog sync failed: %s", aid, e)

    async def auto_sync_loop(self):
        """Continuous background dialog sync loop."""
        await asyncio.sleep(2)
        while True:
            try:
                await self.sync_all_dialogs()
            except Exception as e:
                log.warning("auto_sync_loop error: %s", e)
            await asyncio.sleep(45)

    async def daily_batch_loop(self):
        """Automatically checks and generates today's daily batch for watched groups with personas."""
        await asyncio.sleep(10)
        while True:
            try:
                today = self.daily_batch.today_str()
                watched_groups = self.store.rows("SELECT chat_id FROM groups WHERE watched=1")
                for w in watched_groups:
                    cid = w["chat_id"]
                    existing = self.store.rows("SELECT id FROM daily_batches WHERE chat_id=? AND date_str=?", (cid, today))
                    if not existing:
                        cand_count = self.store.rows("SELECT COUNT(*) as c FROM group_personas WHERE chat_id=? AND account_id IS NOT NULL", (cid,))
                        if cand_count and cand_count[0]["c"] >= 2:
                            log.info("Auto-generating daily batch for group %s (%s)", cid, today)
                            await self.daily_batch.generate_batch_for_group(cid, count=int(self.store.get("batch_messages_count", 6)))
                            self.wake_event.set()
            except Exception as e:
                log.warning("daily_batch_loop error: %s", e)
            await asyncio.sleep(1800)

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
        PLAIN_KEYS = ("ai_model", "vmos_ak", "vmos_pad", "tv_user", "tv_max_price", "cfx_headless", "cfx_app_title", "cfx_proxy_id")

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
                                                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=os.path.expanduser("~"))
                out = (await p.communicate())[0].decode(errors="ignore")
                ready = p.returncode == 0 and "/" in out
            return J({"installed": installed, "ready": ready, **self.cfx_install})

        @r.post("/camoufox/install")
        async def cfx_inst(_):
            asyncio.create_task(self.ensure_camoufox(force=True))
            return J({"ok": True})

        @r.post("/camoufox/test")
        async def cfx_test(req):
            b = await req.json()
            pid = b.get("proxy_id") or self.store.get("cfx_proxy_id")
            line = None
            if pid:
                rr = self.store.rows("SELECT url FROM proxies WHERE id=?", (int(pid),))
                line = rr[0]["url"] if rr else None
            try:
                ip = await cfx.browser_ip(line)
                return J({"ok": True, "ip": ip, "proxied": bool(line)})
            except Exception as e:  # noqa
                return J({"ok": False, "error": (str(e) or "Camoufox could not open").splitlines()[0]})

        # Manual add: Camoufox creates the API keys, the user types the my.telegram.org code.
        @r.post("/autoapi/start")
        async def autoapi_start(req):
            b = await req.json()
            phone = "".join(ch for ch in str(b.get("phone", "")) if ch.isdigit() or ch == "+")
            if len(phone.lstrip("+")) < 7:
                return J({"error": "Enter the full phone number with country code first."}, status=400)
            pid = b.get("proxy_id") or self.store.get("cfx_proxy_id")
            row = self.store.rows("SELECT url FROM proxies WHERE id=?", (int(pid),)) if pid else []
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

        @r.get("/provision/debug")
        async def prov_debug(req):
            after = int(req.query.get("after") or 0)
            return J({"entries": debuglog.since(after), "file": debuglog.LOG_FILE})

        @r.post("/provision/debug/clear")
        async def prov_debug_clear(_):
            debuglog.clear()
            return J({"ok": True})

        @r.post("/provision/cancel")
        async def prov_cancel(_):
            self.prov.cancel()
            return J({"ok": True})

        # ----- accounts -----
        @r.get("/accounts")
        async def accounts(_):
            ready = {aid for aid, c in self.clients.items() if c and getattr(c, "is_connected", lambda: False)()}
            scout_aid = self.get_scout_account_id()
            if scout_aid and scout_aid not in ready:
                asyncio.create_task(self.ensure_clients())
            rows = self.store.rows("SELECT a.id,a.user_id,a.phone,a.name,a.username,a.active,a.api_id,a.proxy_id,a.persona_id,a.last_synced_at,p.label proxy_label "
                                   "FROM accounts a LEFT JOIN proxies p ON p.id=a.proxy_id ORDER BY a.id")
            for a in rows:
                a["connected"] = a["id"] in ready
                a["is_scout"] = (a["id"] == scout_aid)
                try:
                    a["groups"] = self.store.q(
                        "SELECT COUNT(DISTINCT chat_id) FROM ("
                        "  SELECT chat_id FROM group_accounts WHERE account_id=? "
                        "  UNION "
                        "  SELECT chat_id FROM groups WHERE account_id=?"
                        ")",
                        (a["id"], a["id"])
                    ).fetchone()[0]
                except Exception:
                    a["groups"] = 0
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
            cur = self.store.q("INSERT INTO accounts(session,api_id,api_hash,proxy_id,created) VALUES(?,?,?,?,?)",
                             (p["session"], p["api"][0], p["api"][1], p.get("proxy_id"), int(time.time())))
            new_aid = cur.lastrowid
            self._save_profile(new_aid, me, p["phone"])
            await c.disconnect()
            await self.ensure_clients()
            try:
                await self.sync_all_dialogs(account_ids=[new_aid])
            except Exception as e:
                log.warning("acc_verify initial dialog sync failed for account %s: %s", new_aid, e)
            return J({"ok": True, "id": new_aid})

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
                is_active = 1 if b["active"] else 0
                self.store.q("UPDATE accounts SET active=? WHERE id=?", (is_active, aid))
                c = self.clients.pop(aid, None)
                if c:
                    await c.disconnect()
                if is_active:
                    asyncio.create_task(self.sync_all_dialogs(account_ids=[aid]))
            if "proxy_id" in b:
                self.store.q("UPDATE accounts SET proxy_id=? WHERE id=?", (b["proxy_id"] or None, aid))
                c = self.clients.pop(aid, None)
                if c:
                    await c.disconnect()
            return J({"ok": True})

        @r.post("/accounts/{aid}/sync")
        async def acc_sync(req):
            aid = int(req.match_info["aid"])
            try:
                await self.sync_all_dialogs(account_ids=[aid])
                grp_count = self.store.q(
                    "SELECT COUNT(DISTINCT chat_id) FROM ("
                    "  SELECT chat_id FROM group_accounts WHERE account_id=? "
                    "  UNION "
                    "  SELECT chat_id FROM groups WHERE account_id=?"
                    ")",
                    (aid, aid)
                ).fetchone()[0]
                return J({"ok": True, "groups": grp_count})
            except Exception as e:
                return J({"error": str(e)}, status=500)

        @r.delete("/accounts/{aid}")
        async def acc_delete(req):
            aid = int(req.match_info["aid"])
            a = self.store.rows("SELECT * FROM accounts WHERE id=?", (aid,))
            if not a:
                return J({"ok": True})
            acc_data = a[0]
            api_info = self._api(acc_data)
            session_name = acc_data.get("session")

            # 1. Disconnect and log out active client if present
            c = self.clients.pop(aid, None)
            if not c and api_info and session_name:
                try:
                    c = self._new_client(session_name, api_info, acc_data.get("proxy_id"))
                except Exception:
                    c = None
            if c:
                try:
                    await self._discard(c, session_name, logout=True)
                except Exception as e:
                    log.warning("error logging out deleted account %s: %s", aid, e)

            # 2. Delete session files on disk
            if session_name:
                for ext in (".session", ".session-journal"):
                    p = os.path.join(self.sess_dir, session_name + ext)
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except Exception:
                        pass

            # 3. Full cascade deletion in database
            self.store.q("DELETE FROM accounts WHERE id=?", (aid,))
            self.store.q("DELETE FROM group_accounts WHERE account_id=?", (aid,))
            self.store.q("DELETE FROM group_personas WHERE account_id=?", (aid,))
            self.store.q("UPDATE groups SET account_id=NULL WHERE account_id=?", (aid,))
            self.store.q("DELETE FROM daily_batch_items WHERE account_id=?", (aid,))
            self.store.q("DELETE FROM direct_chats WHERE account_id=?", (aid,))
            self.store.q("DELETE FROM direct_messages WHERE account_id=?", (aid,))
            self.store.q("DELETE FROM usage_logs WHERE account_id=?", (aid,))

            # 4. Clear scout setting if this account was scout
            try:
                scout_setting = self.store.get("scout_account_id")
                if str(scout_setting) == str(aid):
                    self.store.set("scout_account_id", "")
            except Exception:
                pass

            return J({"ok": True, "deleted": aid})

        @r.get("/groups")
        async def groups(_):
            await self.sync_all_dialogs()
            return J(self.store.rows("SELECT g.*, a.name account_name FROM groups g LEFT JOIN accounts a ON a.id=g.account_id "
                                     "ORDER BY g.watched DESC, g.title"))

        @r.post("/groups/sync")
        async def groups_sync(_):
            await self.sync_all_dialogs(force_all=True)
            return J({"ok": True})

                # ----- Test Group Conversation Engine (fal.ai cheap model) -----
        @r.post("/groups/test-chat")
        @r.post("/groups/{cid}/test-chat")
        async def group_test_chat(req):
            import persona as persona_mod
            b = {}
            if req.can_read_body:
                try:
                    b = await req.json()
                except Exception:
                    b = {}
            cid_param = req.match_info.get("cid") or b.get("chat_id")
            target_group = None

            if cid_param and str(cid_param).lower() != "auto":
                try:
                    cid_int = int(cid_param)
                    g_rows = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (cid_int,))
                    if g_rows:
                        target_group = g_rows[0]
                except Exception:
                    pass

            all_accs = self.store.rows(
                "SELECT a.*, p.name persona_name, p.prompt persona_prompt, p.color persona_color "
                "FROM accounts a LEFT JOIN personas p ON p.id=a.persona_id"
            )
            leila_candidates = [a for a in all_accs if "leila" in (a.get("name") or "").lower() or "leila" in (a.get("username") or "").lower()]
            dorian_candidates = [a for a in all_accs if "dorian" in (a.get("name") or "").lower() or "dorian" in (a.get("username") or "").lower()]

            if not target_group:
                if leila_candidates and dorian_candidates:
                    l_id = leila_candidates[0]["id"]
                    d_id = dorian_candidates[0]["id"]
                    shared = self.store.rows(
                        "SELECT g.* FROM groups g "
                        "JOIN group_accounts ga1 ON ga1.chat_id=g.chat_id AND ga1.account_id=? "
                        "JOIN group_accounts ga2 ON ga2.chat_id=g.chat_id AND ga2.account_id=? "
                        "ORDER BY g.watched DESC",
                        (l_id, d_id)
                    )
                    if shared:
                        target_group = shared[0]

            if not target_group:
                top_grp = self.store.rows(
                    "SELECT g.*, COUNT(ga.account_id) cnt FROM groups g "
                    "JOIN group_accounts ga ON ga.chat_id=g.chat_id "
                    "GROUP BY g.chat_id ORDER BY cnt DESC, g.watched DESC"
                )
                if top_grp:
                    target_group = top_grp[0]
                else:
                    any_grp = self.store.rows("SELECT * FROM groups ORDER BY watched DESC, chat_id DESC")
                    if any_grp:
                        target_group = any_grp[0]

            if not target_group:
                return J({"error": "No Telegram groups found yet. Connect accounts and sync groups first."}, status=404)

            chat_id = target_group["chat_id"]
            group_title = target_group.get("title") or f"Group {chat_id}"

            part_a = None
            part_b = None
            if leila_candidates and dorian_candidates:
                part_a = leila_candidates[0]
                part_b = dorian_candidates[0]
            else:
                grp_acc_ids = [r["account_id"] for r in self.store.rows("SELECT account_id FROM group_accounts WHERE chat_id=?", (chat_id,))]
                grp_accs = [a for a in all_accs if a["id"] in grp_acc_ids]
                if len(grp_accs) >= 2:
                    part_a, part_b = grp_accs[0], grp_accs[1]
                elif len(all_accs) >= 2:
                    part_a, part_b = all_accs[0], all_accs[1]
                else:
                    return J({"error": "Need at least 2 accounts to have a conversational test chat."}, status=400)

            # Ensure both participants have a persona
            for p_acc in (part_a, part_b):
                if not p_acc.get("persona_id") or not p_acc.get("persona_prompt"):
                    fallback_p = persona_mod.fallback_detailed_persona(direction=f"Human participant named {p_acc.get('name') or 'User'}")
                    cur = self.store.q(
                        "INSERT INTO personas(name, prompt, color, bio, details, created) VALUES(?,?,?,?,?,?)",
                        (p_acc.get("name") or fallback_p["name"], fallback_p["prompt"], fallback_p["color"], fallback_p["bio"], json.dumps(fallback_p["details"]), int(time.time()))
                    )
                    new_pid = cur.lastrowid
                    self.store.q("UPDATE accounts SET persona_id=? WHERE id=?", (new_pid, p_acc["id"]))
                    p_acc["persona_id"] = new_pid
                    p_acc["persona_name"] = p_acc.get("name") or fallback_p["name"]
                    p_acc["persona_prompt"] = fallback_p["prompt"]
                self.store.q("INSERT OR IGNORE INTO group_accounts(chat_id, account_id) VALUES(?,?)", (chat_id, p_acc["id"]))

            cheap_model = b.get("model") or "google/gemini-2.5-flash"
            settings = {
                "fal_key": self.store.get("fal_key"),
                "ai_model": cheap_model
            }
            topic = b.get("topic") or f"Quick natural chat and banter in {group_title}"
            turns_cnt = max(2, min(8, int(b.get("turns") or 4)))

            recent_msgs = self.store.recent(chat_id, 10)
            ctx_summary = "\n".join(f"[{m['sender']}]: {m['text']}" for m in recent_msgs) if recent_msgs else "No prior history."

            # Anti-Pattern & High-Entropy conversational simulation
            sys_prompt = (
                f"You are simulating an authentic, messy, highly realistic Telegram group conversation in \"{group_title}\".\n\n"
                f"Context of recent chat:\n{ctx_summary}\n\n"
                f"Participant 1: {part_a.get('name')}\n"
                f"Persona & Stance:\n{part_a.get('persona_prompt')}\n\n"
                f"Participant 2: {part_b.get('name')}\n"
                f"Persona & Stance:\n{part_b.get('persona_prompt')}\n\n"
                "CRITICAL ANTI-PATTERN CONVERSATION RULES (BREAK ALL PREDICTABLE BOT PATTERNS):\n"
                "1. NO RIGID PING-PONG: Do NOT alternate every single line in a neat ping-pong match. Allow multi-message bursts where one participant sends 2 short consecutive messages in a row (e.g. quick initial reaction + follow-up thought or clarification).\n"
                "2. ZERO ECHOING OR VALIDATION: Never have either person start by echoing the previous speaker ('Yeah I agree', 'That is a great point', 'You are right that...'). Jump straight into raw personal reactions, counter-questions, or unprompted tangents.\n"
                "3. EXTREME STRUCTURAL VARIATION: Wildly vary message lengths! Mix 1-3 word informal reactions ('lol nah', 'wait fr?', 'cap', 'bruh', 'rip') with quick colloquial statements and occasional raw opinions. Never make turns equal length.\n"
                "4. HUMAN IRREGULARITY & AUTHENTIC VOICE: Embrace real human flaws—casual typos, missing punctuation, all-lowercase where fitting, erratic mood shifts, and blunt sarcasm. Let participants show genuine edge and skepticism.\n"
                "5. BANNED AI CLICHES: Absolute ban on 'delve', 'crucial', 'testament', 'landscape', 'pivotal', 'navigate', 'solid', 'align', 'streamline', 'nuanced', 'furthermore', 'in conclusion'.\n"
                f"- Output between {turns_cnt} and {turns_cnt + 2} messages as a JSON array of objects with keys \"sender\" and \"text\". No markdown, no commentary."
            )
            user_prompt = f"Topic/direction: {topic}"

            turns = []
            if settings.get("fal_key"):
                try:
                    raw = await ai.chat(settings, sys_prompt, user_prompt, temperature=1.08, frequency_penalty=0.75)
                    clean = raw.strip()
                    if "```" in clean:
                        clean = clean.split("```")[1]
                        if clean.startswith("json"):
                            clean = clean[4:]
                        clean = clean.strip()
                    parsed = json.loads(clean)
                    if isinstance(parsed, list):
                        turns = parsed
                except Exception as ex:
                    log.warning("fal.ai test chat generation error: %s", ex)

            if not turns:
                turns = [
                    {"sender": part_a.get("name"), "text": f"hey @{(part_b.get('username') or part_b.get('name')).lower()}, did you catch the latest updates?"},
                    {"sender": part_b.get("name"), "text": "yeah was just reading through, looking good so far"},
                    {"sender": part_a.get("name"), "text": "nice, wanted to make sure we're on the same page"},
                    {"sender": part_b.get("name"), "text": "definitely, let's keep it moving "}
                ][:turns_cnt]

            for t in turns:
                snd = (t.get("sender") or "").lower()
                if (part_b.get("name") or "").lower() in snd:
                    t["account_id"] = part_b["id"]
                    t["sender"] = part_b.get("name")
                else:
                    t["account_id"] = part_a["id"]
                    t["sender"] = part_a.get("name")

            send_live = bool(b.get("send_live", False))
            if send_live:
                ready = await self.ensure_clients()
                prev_id = None
                for t in turns:
                    cl = ready.get(t["account_id"])
                    if cl:
                        try:
                            cps = 24.0
                            is_burst = (prev_id is not None and turns.index(t) > 0 and turns[turns.index(t)-1].get("sender") == t["sender"])
                            typing_delay = min(3.8, max(0.6, (len(t["text"]) / cps) + random.uniform(0.2, 0.6))) if not is_burst else min(2.0, max(0.4, (len(t["text"]) / cps)))
                            async with cl.action(chat_id, "typing"):
                                await asyncio.sleep(typing_delay)
                            sent_msg = await cl.send_message(chat_id, t["text"], reply_to=prev_id)
                            prev_id = sent_msg.id
                            t["sent"] = True
                            t["msg_id"] = sent_msg.id
                            self.store.add_message(chat_id, sent_msg.id, t["sender"], t["text"], int(time.time()))
                            await asyncio.sleep(random.uniform(0.8, 1.6) if is_burst else random.uniform(1.8, 3.5))
                        except Exception as e:
                            t["sent"] = False
                            t["error"] = str(e)
                    else:
                        t["sent"] = False
                        t["error"] = f"Account {t['sender']} client not connected"

            return J({
                "ok": True,
                "chat_id": chat_id,
                "group_title": group_title,
                "model": cheap_model,
                "send_live": send_live,
                "participants": [
                    {"id": part_a["id"], "name": part_a["name"], "persona": part_a.get("persona_name")},
                    {"id": part_b["id"], "name": part_b["name"], "persona": part_b.get("persona_name")}
                ],
                "turns": turns
            })


        @r.get("/groups/{cid}")
        async def get_group(req):
            cid = int(req.match_info["cid"])
            g = self.store.rows("SELECT g.*, a.name account_name FROM groups g LEFT JOIN accounts a ON a.id=g.account_id WHERE g.chat_id=?", (cid,))
            if not g:
                return J({"error": "Group not found"}, status=404)
            data = dict(g[0])
            # Include accounts associated with this group
            accs = self.store.rows(
                "SELECT a.id, a.name, a.username, a.phone, gp.persona_id, p.name persona_name, p.color persona_color "
                "FROM group_accounts ga "
                "JOIN accounts a ON a.id=ga.account_id "
                "LEFT JOIN group_personas gp ON gp.chat_id=ga.chat_id AND gp.account_id=a.id "
                "LEFT JOIN personas p ON p.id=gp.persona_id "
                "WHERE ga.chat_id=?", (cid,)
            )
            data["accounts"] = accs
            return J(data)

        @r.post("/groups/{cid}")
        async def update_group(req):
            b = await req.json()
            cid = int(req.match_info["cid"])
            for k in ("watched", "auto_reply", "persona", "title", "about", "domain_knowledge", "tags", "rules", "account_id"):
                if k in b:
                    self.store.q(f"UPDATE groups SET {k}=? WHERE chat_id=?", (b[k], cid))
            if b.get("watched"):
                asyncio.create_task(self.catch_up())
            return J({"ok": True})

        @r.post("/groups/{cid}/load-xbiolabs-preset")
        async def load_xbiolabs_preset(req):
            cid = int(req.match_info["cid"])
            about = "Official vendor and community group for xbiolabs. While it is a vendor group, the community actively discusses everything related to peptides, underground biohacking, and health optimization."
            domain_knowledge = (
                "Comprehensive domain mastery across all topics discussed in xbiolabs:\n"
                "1. PEPTIDES & RESEARCH CHEMICALS: In-depth familiarity with BPC-157 (gut repair, tendon healing), TB-500/Thymosin Beta-4 (tissue regeneration), Semaglutide/Ozempic, Tirzepatide/Mounjaro, Retatrutide (triple GIP/GLP-1/glucagon agonist), GHK-Cu (copper peptide for tissue/skin/collagen), CJC-1295 no DAC & Ipamorelin (GH secretagogues), Sermorelin, MOTS-c (mitochondrial optimization), Epitalon (telomere elongation), NAD+ injections, Kisspeptin, MT-2.\n"
                "2. RECONSTITUTION & DOSING PROTOCOLS: Proper mixing techniques with bacteriostatic water (BAC), calculating mcg per tick on 100-unit/30-unit insulin syringes, sterile needle hygiene, subQ vs IM administration sites, cold fridge storage, avoiding vigorous shaking.\n"
                "3. VENDOR OPERATIONS & BUYING: Sourcing, batch COA purity verification, HPLC/Janoshik lab test reports, ordering procedures, payment methods (crypto/Bitcoin/USDT, wire), tracking numbers, customs handling, stealth domestic shipping, discrete packaging, pricing per vial vs kit (10 vials), reship policies, customer service resolution.\n"
                "4. GEAR & ANABOLIC PROTOCOLS: TRT bases (test cypionate, test enanthate), Primobolan, Masteron, Anavar/Oxandrolone, Deca/NPP, estrogen management (Arimidex/anastrozole, Aromasin), prolactin control (Cabergoline), post cycle therapy (PCT - Enclomiphene, Nolvadex, HCG).\n"
                "5. PROGRESS & HEALTH MONITORING: Bloodwork interpretation (CBC, CMP, lipid panels, ApoB, hs-CRP, total/free test, sensitive estradiol, ALT/AST liver enzymes, eGFR kidney health), DEXA scans, body composition, fatigue markers.\n"
                "6. SEXUAL ENHANCEMENT: PT-141/Bremelanotide (melanocortin receptor agonist for libido), PDE5 inhibitors (Tadalafil/Cialis daily vs as-needed, Sildenafil/Viagra), Melanotan-2 (tanning + libido synergy).\n"
                "7. WELLNESS & LONGEVITY: Mitochondrial health, sleep optimization, metabolic flexibility, anti-inflammatory protocols.\n"
                "8. NOOTROPICS & NEUROCHEMISTRY: Semax (NA-Semax-Amidate for BDNF and focus), Selank (anxiolytic peptide), Bromantane (dopamine upregulation), Modafinil/Armodafinil, L-Tyrosine, Alpha-GPC.\n"
                "9. TONE & PARTICIPATION: Speak as experienced, grounded community insiders who share real personal experiences, troubleshoot reconstitutions, exchange batch updates, and talk shop casually without generic corporate disclaimers."
            )
            tags = "peptides, vendor, dosing, buying, shipping, pricing, coa, bpc157, retatrutide, tirzepatide, gear, trt, sexual-enhancement, pt141, nootropics, wellness, xbiolabs"
            rules = (
                "Be authentic, direct, and knowledgeable. Share real protocol experiences honestly. "
                "Never volunteer unprompted vendor praise; only if someone asks whether anyone had a bad experience or issues with xbiolabs, casually share that personally you have only had good experiences."
            )
            self.store.q(
                "UPDATE groups SET about=?, domain_knowledge=?, tags=?, rules=? WHERE chat_id=?",
                (about, domain_knowledge, tags, rules, cid)
            )
            return J({"ok": True, "about": about, "domain_knowledge": domain_knowledge, "tags": tags, "rules": rules})
        @r.get("/groups/{cid}/feed")
        async def feed(req):
            cid = int(req.match_info["cid"])
            return J({"messages": self.store.recent(cid, 100),
                      "summaries": self.store.rows("SELECT * FROM summaries WHERE chat_id=? ORDER BY created DESC LIMIT 20", (cid,))})

        @r.delete("/groups/{cid}")
        async def group_delete(req):
            cid_raw = req.match_info["cid"]
            try:
                cid = int(cid_raw)
            except ValueError:
                return J({"error": "Invalid group ID"}, status=400)

            # Cascade delete all group references
            self.store.q("DELETE FROM groups WHERE chat_id=?", (cid,))
            self.store.q("DELETE FROM group_accounts WHERE chat_id=?", (cid,))
            self.store.q("DELETE FROM group_personas WHERE chat_id=?", (cid,))
            self.store.q("DELETE FROM messages WHERE chat_id=?", (cid,))
            self.store.q("DELETE FROM summaries WHERE chat_id=?", (cid,))
            self.store.q("DELETE FROM queue WHERE chat_id=?", (cid,))
            self.store.q("DELETE FROM daily_batches WHERE chat_id=?", (cid,))
            self.store.q("DELETE FROM daily_batch_items WHERE chat_id=?", (cid,))
            self.store.q("DELETE FROM persona_memories WHERE chat_id=?", (cid,))
            self.store.q("DELETE FROM usage_logs WHERE chat_id=?", (cid,))
            return J({"ok": True, "deleted": cid})

        @r.post("/groups/{cid}/{action}")
        async def action(req):
            cid, act = int(req.match_info["cid"]), req.match_info["action"]
            if act not in ("summarize", "draft_reply"):
                return J({"error": "unknown action"}, status=400)
            self.store.enqueue(act, cid, {}, dedupe_key=f"{act}:{cid}:{int(time.time()) // 10}")
            self.wake_event.set()
            return J({"ok": True})

        # ----- Orchestrator / Cadence Engine -----
        @r.get("/orchestrator/config")
        async def orch_get_cfg(_):
            return J(self.orchestrator.get_config())

        @r.post("/orchestrator/config")
        async def orch_set_cfg(req):
            b = await req.json()
            curr = self.orchestrator.get_config()
            curr.update(b)
            self.store.set("orchestrator_config", curr)
            return J(self.orchestrator.get_config())

        # ----- network graph (personas -> accounts -> groups) -----
        @r.get("/graph")
        async def graph(_):
            # Groups and accounts graph query: only accounts with active persona are wired to groups

            if not self.store.q("SELECT COUNT(*) FROM groups").fetchone()[0] and self.clients:
                try:
                    await self.sync_all_dialogs()
                except Exception:
                    pass
            accs = self.store.rows("SELECT a.id,a.phone,a.name,a.username,a.active,a.persona_id,a.proxy_id,p.label proxy_label "
                                   "FROM accounts a LEFT JOIN proxies p ON p.id=a.proxy_id ORDER BY a.id")
            for a in accs:
                a["connected"] = a["id"] in self.clients
            grps = self.store.rows("SELECT chat_id,title,watched,auto_reply,persona,account_id FROM groups ORDER BY watched DESC, title")
            for g in grps:
                g["chat_id"] = str(g["chat_id"])
                g["messages"] = self.store.q("SELECT COUNT(*) FROM messages WHERE chat_id=?", (int(g["chat_id"]),)).fetchone()[0]
                links = self.store.rows(
                    "SELECT ga.account_id FROM group_accounts ga "
                    "JOIN accounts a ON a.id=ga.account_id "
                    "WHERE ga.chat_id=? AND a.persona_id IS NOT NULL",
                    (int(g["chat_id"]),)
                )
                valid_aids = [x["account_id"] for x in links]
                if not valid_aids and g.get("account_id"):
                    owner_check = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (g["account_id"],))
                    if owner_check and owner_check[0]["persona_id"]:
                        valid_aids = [g["account_id"]]
                g["account_ids"] = valid_aids
            return J({"accounts": accs, "groups": grps,
                      "personas": self.store.rows("SELECT * FROM personas ORDER BY id"),
                      "layout": self.store.get("graph_layout", {})})

        @r.post("/graph/layout")
        async def graph_layout(req):
            self.store.set("graph_layout", (await req.json()).get("layout", {}))
            return J({"ok": True})

        @r.post("/graph/sync")
        async def graph_sync(_):
            await self.sync_all_dialogs(force_all=True)
            return J({"ok": True})

        def _parse(ref):
            kind, _, val = str(ref).partition(":")
            return kind, val

        # ----- Daily Batch Dialogue & Scout Control -----
        @r.get("/daily-batch/status")
        async def daily_batch_status(_):
            return J(self.daily_batch.get_status())

        @r.post("/daily-batch/generate")
        async def daily_batch_generate(req):
            b = await req.json()
            chat_id = b.get("chat_id")
            if not chat_id:
                watched = self.store.rows("SELECT chat_id FROM groups WHERE watched=1 LIMIT 1")
                if not watched:
                    return J({"error": "No watched group available to generate batch for"}, status=400)
                chat_id = watched[0]["chat_id"]
            count = int(b.get("count", 6))
            topic = b.get("topic")
            try:
                res = await self.daily_batch.generate_batch_for_group(chat_id, count=count, topic_override=topic)
                self.wake_event.set()
                return J({"ok": True, "batch": res})
            except Exception as e:
                return J({"error": str(e)}, status=400)

        @r.post("/daily-batch/clear")
        async def daily_batch_clear(req):
            b = {}
            try:
                b = await req.json()
            except Exception:
                pass
            cleared = self.daily_batch.clear_today(b.get("chat_id"))
            return J({"ok": True, "cleared": cleared})

        @r.post("/daily-batch/send-next")
        async def daily_batch_send_next(_):
            it_id = self.daily_batch.trigger_next_now()
            if not it_id:
                return J({"error": "No pending scheduled batch items found"}, status=400)
            self.wake_event.set()
            return J({"ok": True, "item_id": it_id})

        @r.post("/daily-batch/scout")
        async def daily_batch_set_scout(req):
            b = await req.json()
            aid = b.get("account_id")
            if aid:
                self.store.set("scout_account_id", int(aid))
            else:
                self.store.set("scout_account_id", None)
            await self.ensure_clients()
            return J({"ok": True, "scout_account_id": self.get_scout_account_id()})

        @r.post("/daily-batch/settings")
        async def daily_batch_save_settings(req):
            b = await req.json()
            if "human_pause_minutes" in b:
                self.store.set("batch_human_pause_seconds", int(b["human_pause_minutes"]) * 60)
            if "batch_messages_count" in b:
                self.store.set("batch_messages_count", int(b["batch_messages_count"]))
            return J({"ok": True})

        # ----- System Master Control -----
        @r.get("/system/status")
        async def system_status(_):
            enabled = bool(self.store.get("system_enabled", True))
            scout_aid = self.get_scout_account_id()
            scout_acc = self.store.rows("SELECT id, name, phone, username FROM accounts WHERE id=?", (scout_aid,)) if scout_aid else []
            total_active_accs = self.store.q("SELECT COUNT(*) FROM accounts WHERE active=1").fetchone()[0]
            watched_groups = self.store.q("SELECT COUNT(*) FROM groups WHERE watched=1").fetchone()[0]
            queued_jobs = self.store.q("SELECT COUNT(*) FROM queue WHERE status='pending'").fetchone()[0]
            batch_status = self.daily_batch.get_status()
            return J({
                "enabled": enabled,
                "scout_account": scout_acc[0] if scout_acc else None,
                "active_listeners": len(self.clients),
                "dormant_senders": max(0, total_active_accs - len(self.clients)),
                "total_accounts": total_active_accs,
                "watched_groups": watched_groups,
                "queued_jobs": queued_jobs,
                "live_ai_mode": "Humans Only",
                "batch_stats": batch_status["stats"]
            })

        @r.post("/system/toggle")
        async def system_toggle(req):
            b = {}
            try:
                b = await req.json()
            except Exception:
                pass
            cur = bool(self.store.get("system_enabled", True))
            new_val = not cur if "enabled" not in b else bool(b["enabled"])
            self.store.set("system_enabled", new_val)
            log.info("System master enabled changed to: %s", new_val)
            return J({"enabled": new_val, "ok": True})

        # ----- Industrial Persona Architect & Account Binding -----
        @r.post("/personas/ai-generate")
        async def persona_ai_generate(req):
            import persona
            b = await req.json()
            save_now = bool(b.get("save", False))
            settings = {k: self.store.get(k) for k in ("fal_key", "ai_model", "persona_model")}
            group_id = b.get("group_id")
            if group_id:
                g = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (int(group_id),))
                if g:
                    prof = {}
                    try:
                        prof = json.loads(g[0].get("profile") or "{}")
                    except Exception:
                        pass
                    b["group_context"] = {"title": g[0].get("title"), "profile": prof}
            try:
                data = await persona.generate_industrial_persona(settings, params=b)
            except Exception as e:
                log.warning("Industrial AI generation failed, fallback used: %s", e)
                data = persona.fallback_industrial_persona(params=b)

            if save_now:
                details_json = json.dumps(data.get("details") or {})
                cur = self.store.q("INSERT INTO personas(name,prompt,color,bio,details,created) VALUES(?,?,?,?,?,?)",
                                   (data["name"][:60], data.get("prompt") or "", data.get("color") or "#2fc4b2",
                                    (data.get("bio") or "")[:120], details_json, int(time.time())))
                data["id"] = cur.lastrowid
                return J({"ok": True, "persona": data, "saved": True})
            return J({"ok": True, "persona": data, "saved": False})

        @r.post("/personas/match-name")
        async def persona_match_name(req):
            import persona
            b = await req.json()
            culture = b.get("culture") or "american"
            gender = b.get("gender")
            name, g, cult = persona.get_culture_name(culture, gender)
            return J({"ok": True, "name": name, "gender": g, "culture": cult})

        @r.post("/personas/compile-prompt")
        async def persona_compile_prompt(req):
            import persona
            b = await req.json()
            prompt = persona.compile_industrial_prompt(b)
            return J({"ok": True, "prompt": prompt})

        @r.post("/personas/account-bind")
        async def persona_account_bind(req):
            b = await req.json()
            aid = int(b["account_id"])
            pid = int(b["persona_id"]) if b.get("persona_id") else None
            self.store.q("UPDATE accounts SET persona_id=? WHERE id=?", (pid, aid))
            if pid:
                # Update groups default account_id if needed
                self.store.q("UPDATE groups SET account_id=COALESCE(account_id, ?) WHERE chat_id IN (SELECT chat_id FROM group_accounts WHERE account_id=?)", (aid, aid))
                self.ensure_group_personas_assigned_for_account(aid)
            return J({"ok": True})

        @r.post("/graph/link")
        async def graph_link(req):
            b = await req.json()
            (fk, fv), (tk, tv) = _parse(b.get("from")), _parse(b.get("to"))
            on = b.get("on", True)
            if fk == "per" and tk == "acc":
                self.store.q("UPDATE accounts SET persona_id=? WHERE id=?", (int(fv) if on else None, int(tv)))
            elif fk == "acc" and tk == "grp":
                aid, cid = int(fv), int(tv)
                if on:
                    acc = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
                    if not acc or not acc[0].get("persona_id"):
                        return J({"error": "Account must connect to a Persona first before connecting to a Group."}, status=400)
                    self.store.q("INSERT OR IGNORE INTO group_accounts(chat_id, account_id) VALUES(?,?)", (cid, aid))
                    self.store.q("UPDATE groups SET account_id=COALESCE(account_id, ?) WHERE chat_id=?", (aid, cid))
                else:
                    self.store.q("DELETE FROM group_accounts WHERE chat_id=? AND account_id=?", (cid, aid))
                    self.store.q("UPDATE group_personas SET account_id=NULL WHERE chat_id=? AND account_id=?", (cid, aid))
                    rem = self.store.rows("SELECT account_id FROM group_accounts WHERE chat_id=?", (cid,))
                    if rem:
                        self.store.q("UPDATE groups SET account_id=? WHERE chat_id=?", (rem[0]["account_id"], cid))
                    else:
                        self.store.q("UPDATE groups SET account_id=NULL WHERE chat_id=?", (cid,))
            elif fk == "per" and tk == "grp":
                return J({"error": "Connect the Persona to an Account first, then connect the Account to the Group."}, status=400)
            else:
                return J({"error": "Connect Persona to Account, or Account to Group."}, status=400)
            return J({"ok": True})

        # ----- Direct Messages / 1-on-1 Chats -----
        @r.get("/direct-chats")
        async def direct_chats_list(req):
            aid = req.query.get("account_id")
            sql = ("SELECT dc.*, a.name account_name, a.phone account_phone, p.name persona_name, p.color persona_color "
                   "FROM direct_chats dc LEFT JOIN accounts a ON a.id=dc.account_id "
                   "LEFT JOIN personas p ON p.id=dc.persona_id ")
            args = ()
            if aid:
                sql += "WHERE dc.account_id=? "
                args = (int(aid),)
            sql += "ORDER BY dc.last_ts DESC"
            return J({"chats": self.store.rows(sql, args)})

        @r.post("/direct-chats/sync")
        async def direct_chats_sync(_):
            await self.sync_all_dialogs()
            return J({"ok": True})

        @r.get("/direct-chats/{aid}/{peer_id}/messages")
        async def direct_chat_messages(req):
            aid, peer_id = int(req.match_info["aid"]), int(req.match_info["peer_id"])
            self.store.q("UPDATE direct_chats SET unread_count=0 WHERE account_id=? AND peer_id=?", (aid, peer_id))
            if aid in self.clients:
                try:
                    c = self.clients[aid]
                    me = await c.get_me()
                    async for m in c.iter_messages(peer_id, limit=30):
                        if not m.raw_text:
                            continue
                        incoming = 0 if (m.out or m.sender_id == me.id) else 1
                        sender_name = "Me" if not incoming else "User"
                        self.store.q("INSERT OR IGNORE INTO direct_messages(account_id,peer_id,msg_id,sender_name,incoming,text,ts) VALUES(?,?,?,?,?,?,?)",
                                     (aid, peer_id, m.id, sender_name, incoming, m.raw_text, int(m.date.timestamp()) if m.date else int(time.time())))
                except Exception as e:
                    log.warning("direct chat history fetch failed: %s", e)
            return J({"messages": self.store.direct_messages_recent(aid, peer_id, 60)})

        @r.post("/direct-chats/{aid}/{peer_id}/send")
        async def direct_chat_send(req):
            aid, peer_id = int(req.match_info["aid"]), int(req.match_info["peer_id"])
            b = await req.json()
            text = (b.get("text") or "").strip()
            if not text:
                return J({"error": "Message text required"}, status=400)
            try:
                async with self.account_session(aid) as c:
                    sent = await c.send_message(peer_id, text, reply_to=b.get("reply_to"))
                    self.store.add_direct_message(aid, peer_id, sent.id, "Me", 0, text, int(time.time()))
                    return J({"ok": True, "msg_id": sent.id})
            except Exception as e:
                return J({"error": str(e)}, status=500)

        @r.post("/direct-chats/{aid}/{peer_id}/draft")
        async def direct_chat_draft(req):
            aid, peer_id = int(req.match_info["aid"]), int(req.match_info["peer_id"])
            b = await req.json() if req.can_read_body else {}
            chat = self.store.rows("SELECT * FROM direct_chats WHERE account_id=? AND peer_id=?", (aid, peer_id))
            peer_name = chat[0]["peer_name"] if chat else "User"
            pid = b.get("persona_id") or (chat[0].get("persona_id") if chat else None)
            if not pid:
                acc = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
                pid = acc[0].get("persona_id") if acc else None
            persona_row = self.store.rows("SELECT * FROM personas WHERE id=?", (pid,)) if pid else []
            prompt = persona_row[0]["prompt"] if persona_row else "You are chatting 1-on-1 on Telegram. Keep responses natural and concise."
            msgs = self.store.direct_messages_recent(aid, peer_id, 20)
            settings = {k: self.store.get(k) for k in ("fal_key", "ai_model")}
            draft = await ai.draft_reply(settings, f"Private conversation with {peer_name}", prompt,
                                         [{"sender": m["sender_name"], "text": m["text"]} for m in msgs])
            return J({"ok": True, "draft": draft})

        @r.post("/direct-chats/{aid}/{peer_id}/settings")
        async def direct_chat_settings(req):
            aid, peer_id = int(req.match_info["aid"]), int(req.match_info["peer_id"])
            b = await req.json()
            if "auto_reply" in b:
                self.store.q("UPDATE direct_chats SET auto_reply=? WHERE account_id=? AND peer_id=?",
                             (1 if b["auto_reply"] else 0, aid, peer_id))
            if "persona_id" in b:
                pid = int(b["persona_id"]) if b["persona_id"] else None
                self.store.q("UPDATE direct_chats SET persona_id=? WHERE account_id=? AND peer_id=?", (pid, aid, peer_id))
            return J({"ok": True})

        @r.get("/personas")
        async def personas_list(_):
            rows = self.store.rows("SELECT * FROM personas ORDER BY id DESC")
            gps = self.store.rows("SELECT gp.persona_id, gp.chat_id, g.title group_title, gp.account_id, a.name account_name, a.phone "
                                  "FROM group_personas gp "
                                  "JOIN groups g ON g.chat_id=gp.chat_id "
                                  "LEFT JOIN accounts a ON a.id=gp.account_id")
            acc_fallback = self.store.rows("SELECT a.persona_id, a.id account_id, a.name account_name, a.phone "
                                           "FROM accounts a WHERE a.persona_id IS NOT NULL")
            out = []
            for r in rows:
                p = dict(r)
                try:
                    p["details"] = json.loads(p.get("details") or "{}")
                except Exception:
                    p["details"] = {}
                p["group_assignments"] = [g for g in gps if g["persona_id"] == p["id"]]
                p["account_defaults"] = [a for a in acc_fallback if a["persona_id"] == p["id"]]
                out.append(p)
            return J({"personas": out})

        @r.get("/personas/matrix")
        async def personas_matrix(_):
            groups = self.store.rows("SELECT chat_id, title, watched, auto_reply, persona, account_id, profile FROM groups ORDER BY watched DESC, title")
            accounts = self.store.rows("SELECT id, phone, name, username, active, persona_id FROM accounts ORDER BY id")
            personas = self.store.rows("SELECT * FROM personas ORDER BY id")
            for p in personas:
                try:
                    p["details"] = json.loads(p.get("details") or "{}")
                except Exception:
                    p["details"] = {}

            gps = self.store.rows("SELECT gp.chat_id, gp.persona_id, gp.account_id, p.name persona_name, p.color persona_color, p.bio persona_bio, p.details "
                                  "FROM group_personas gp "
                                  "JOIN personas p ON p.id=gp.persona_id")
            for g in gps:
                try:
                    g["details"] = json.loads(g.get("details") or "{}")
                except Exception:
                    g["details"] = {}

            matrix = []
            for g in groups:
                cid = g["chat_id"]
                grp_assigns = [x for x in gps if x["chat_id"] == cid]
                user_assignments = []
                for a in accounts:
                    assigned = next((x for x in grp_assigns if x["account_id"] == a["id"]), None)
                    fallback = next((p for p in personas if p["id"] == a["persona_id"]), None) if not assigned else None
                    user_assignments.append({
                        "account_id": a["id"],
                        "account_name": a["name"] or a["phone"] or f"Account #{a['id']}",
                        "account_phone": a["phone"],
                        "active": a["active"],
                        "assigned_persona": assigned,
                        "fallback_persona": fallback,
                        "is_fallback": bool(not assigned and fallback),
                    })
                prof = {}
                try:
                    prof = json.loads(g.get("profile") or "{}")
                except Exception:
                    pass
                matrix.append({
                    "chat_id": cid,
                    "title": g["title"],
                    "watched": bool(g["watched"]),
                    "auto_reply": bool(g["auto_reply"]),
                    "override_persona": g.get("persona") or "",
                    "profile": prof,
                    "available_personas": grp_assigns,
                    "user_assignments": user_assignments
                })
            return J({"matrix": matrix, "accounts": accounts, "personas": personas})

        @r.post("/personas/assign-matrix")
        async def assign_matrix(req):
            b = await req.json()
            cid = int(b["chat_id"])
            aid = int(b["account_id"]) if b.get("account_id") is not None else None
            pid = int(b["persona_id"]) if b.get("persona_id") else None
            bind_account = bool(b.get("set_as_account_persona", False))

            if not aid:
                return J({"error": "account_id is required"}, status=400)

            # Enforce account-first binding: if account has no persona, bind it
            acc = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
            if acc and (bind_account or not acc[0].get("persona_id")) and pid:
                self.store.q("UPDATE accounts SET persona_id=? WHERE id=?", (pid, aid))

            # Clear previous persona assignment for this user in this group
            self.store.q("UPDATE group_personas SET account_id=NULL WHERE chat_id=? AND account_id=?", (cid, aid))

            if pid:
                r = self.store.q("UPDATE group_personas SET account_id=? WHERE chat_id=? AND persona_id=?", (aid, cid, pid))
                if r.rowcount == 0:
                    self.store.q("INSERT INTO group_personas(chat_id,persona_id,account_id,created) VALUES(?,?,?,?)",
                                 (cid, pid, aid, int(time.time())))
                self.store.q("INSERT OR IGNORE INTO group_accounts(chat_id, account_id) VALUES(?,?)", (cid, aid))
                self.store.q("UPDATE groups SET account_id=COALESCE(account_id, ?) WHERE chat_id=?", (aid, cid))
            return J({"ok": True})

        @r.post("/personas")
        async def persona_save(req):
            b = await req.json()
            name = (b.get("name") or "New persona").strip()[:60]
            prompt, color = (b.get("prompt") or "")[:5000], (b.get("color") or "#2fc4b2")[:9]
            bio = (b.get("bio") or "")[:120]
            details = json.dumps(b.get("details") or {})
            if b.get("id"):
                self.store.q("UPDATE personas SET name=?,prompt=?,color=?,bio=?,details=? WHERE id=?",
                             (name, prompt, color, bio, details, int(b["id"])))
                return J({"ok": True, "id": int(b["id"])})
            cur = self.store.q("INSERT INTO personas(name,prompt,color,bio,details,created) VALUES(?,?,?,?,?,?)",
                               (name, prompt, color, bio, details, int(time.time())))
            return J({"ok": True, "id": cur.lastrowid})

        @r.post("/personas/{pid}/duplicate")
        async def persona_duplicate(req):
            pid = int(req.match_info["pid"])
            rows = self.store.rows("SELECT * FROM personas WHERE id=?", (pid,))
            if not rows:
                return J({"error": "Persona not found"}, status=404)
            p = rows[0]
            cur = self.store.q("INSERT INTO personas(name,prompt,color,bio,details,created) VALUES(?,?,?,?,?,?)",
                               (f"{p['name']} (Copy)"[:60], p["prompt"], p["color"], p.get("bio") or "", p.get("details") or "{}", int(time.time())))
            return J({"ok": True, "id": cur.lastrowid})

        @r.get("/group-personas/{cid}")
        async def gp_list(req):
            cid = int(req.match_info["cid"])
            rows = self.store.rows("SELECT p.*, gp.account_id FROM group_personas gp JOIN personas p ON p.id=gp.persona_id "
                                   "WHERE gp.chat_id=? ORDER BY p.id", (cid,))
            g = self.store.rows("SELECT profile FROM groups WHERE chat_id=?", (cid,))
            prof = {}
            try:
                prof = json.loads(g[0]["profile"] or "{}") if g else {}
            except Exception:  # noqa
                pass
            return J({"personas": rows, "profile": prof})

        @r.post("/group-personas/{cid}/generate")
        async def gp_generate(req):
            import persona
            cid, b = int(req.match_info["cid"]), await req.json()
            count = max(1, min(int(b.get("count") or 3), 8))
            settings = {k: self.store.get(k) for k in ("fal_key", "ai_model", "persona_model")}
            g = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (cid,))
            if not g:
                return J({"error": "Group not found. Sync groups first."}, status=404)
            # Read the real history straight from Telegram (up to 800 messages), fall back to what's saved.
            msgs, members = [], []
            for aid, c in (await self.ensure_clients()).items():
                try:
                    await c.get_input_entity(cid)
                    members.append(aid)
                except Exception:  # noqa
                    continue
                if not msgs:
                    try:
                        async for m in c.iter_messages(cid, limit=800):
                            if m.raw_text:
                                s_ = await m.get_sender()
                                msgs.append({"sender": getattr(s_, "first_name", None) or getattr(s_, "title", None) or "user",
                                             "text": m.raw_text[:600]})
                        msgs.reverse()
                    except Exception as e:  # noqa
                        debuglog.dbg("persona", f"History read failed: {e}", "warn")
            if len(msgs) < 30:
                msgs = msgs or self.store.recent(cid, 800)
            if len(msgs) < 15:
                return J({"error": "Not enough chat history to study this group yet (need ~15+ messages). Turn on Watch and wait a bit."}, status=400)
            have = [r["name"] for r in self.store.rows("SELECT p.name FROM group_personas gp JOIN personas p ON p.id=gp.persona_id WHERE gp.chat_id=?", (cid,))]
            try:
                prof, ps = await persona.for_group(settings, g[0]["title"] or "", msgs, count, have, b.get("direction") or "")
            except ai.AIError as e:
                return J({"error": f"AI failed: {e}"}, status=400)
            except Exception as e:  # noqa
                return J({"error": f"Persona design failed: {e}"}, status=400)
            self.store.q("UPDATE groups SET profile=? WHERE chat_id=?", (json.dumps(prof), cid))
            # Give each new persona an account that is in this group and doesn't have a persona here yet.
            taken = {r["account_id"] for r in self.store.rows("SELECT account_id FROM group_personas WHERE chat_id=?", (cid,))}
            free = [a for a in members if a not in taken]
            colors = ["#2fc4b2", "#7c6cff", "#ff8a4c", "#e45fa6", "#4ca8ff", "#9bd14c", "#f2c94c", "#56ccf2"]
            ids = []
            for p in ps:
                cur = self.store.q("INSERT INTO personas(name,prompt,color,created,bio,details) VALUES(?,?,?,?,?,?)",
                                   (f"{p['first_name']} {p.get('last_name') or ''}".strip(), p["system_prompt"], random.choice(colors),
                                    int(time.time()), p.get("bio") or "", json.dumps(p)))
                acc = free.pop(0) if free else None
                self.store.q("INSERT OR REPLACE INTO group_personas(chat_id,persona_id,account_id,created) VALUES(?,?,?,?)",
                             (cid, cur.lastrowid, acc, int(time.time())))
                ids.append(cur.lastrowid)
            debuglog.dbg("persona", f"Designed {len(ids)} personas for {g[0]['title']} from {len(msgs)} messages")
            return J({"ok": True, "ids": ids, "profile": prof, "studied": len(msgs)})

        @r.post("/group-personas/{cid}/assign")
        async def gp_assign(req):
            cid, b = int(req.match_info["cid"]), await req.json()
            pid, acc = int(b["persona_id"]), (int(b["account_id"]) if b.get("account_id") else None)
            if acc:
                self.store.q("UPDATE group_personas SET account_id=NULL WHERE chat_id=? AND account_id=?", (cid, acc))
            self.store.q("UPDATE group_personas SET account_id=? WHERE chat_id=? AND persona_id=?", (acc, cid, pid))
            return J({"ok": True})

        @r.delete("/group-personas/{cid}/{pid}")
        async def gp_del(req):
            cid, pid = int(req.match_info["cid"]), int(req.match_info["pid"])
            self.store.q("DELETE FROM group_personas WHERE chat_id=? AND persona_id=?", (cid, pid))
            self.store.q("DELETE FROM personas WHERE id=?", (pid,))
            return J({"ok": True})

        @r.delete("/personas/{pid}")
        async def persona_del(req):
            pid = int(req.match_info["pid"])
            self.store.q("DELETE FROM personas WHERE id=?", (pid,))
            self.store.q("UPDATE accounts SET persona_id=NULL WHERE persona_id=?", (pid,))
            return J({"ok": True})

        @r.post("/personas/{pid}/preview")
        async def persona_preview(req):
            pid, b = int(req.match_info["pid"]), await req.json()
            p = self.store.rows("SELECT * FROM personas WHERE id=?", (pid,))
            if not p:
                return J({"error": "Persona not found."}, status=404)
            settings = {k: self.store.get(k) for k in ("fal_key", "ai_model")}
            cid = b.get("chat_id")
            g = (self.store.rows("SELECT * FROM groups WHERE chat_id=?", (int(cid),)) if cid else []) or [{}]
            msgs = self.store.recent(int(cid), 30) if cid else []
            if not msgs:
                msgs = [{"sender": "Alex", "text": "hey everyone, what's new today?", "ts": int(time.time())}]
            try:
                text = await ai.draft_reply(settings, g[0].get("title", "Preview group"),
                                            f"You are {p[0]['name']}. {p[0]['prompt']}", msgs)
            except ai.AIError as e:
                return J({"error": f"AI preview failed: {e}"}, status=400)
            return J({"text": text, "context": msgs[-5:]})

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

                # ---------- Persona Memory Engine Routes ----------
        @r.get("/personas/{pid}/memory-tree")
        async def persona_mem_tree(req):
            pid = int(req.match_info["pid"])
            tree = self.memory.get_memory_tree(pid)
            return J(tree)

        @r.get("/personas/{pid}/memories")
        async def persona_mem_list(req):
            pid = int(req.match_info["pid"])
            tier = req.query.get("tier")
            mems = self.memory.get_memories(pid, retention_tier=tier)
            return J(mems)

        @r.post("/personas/{pid}/memories")
        async def persona_mem_add(req):
            pid = int(req.match_info["pid"])
            b = await req.json()
            mid = self.memory.add_memory(
                persona_id=pid,
                content=b.get("content"),
                kind=b.get("kind", "statement"),
                retention_tier=b.get("retention_tier", "medium"),
                salience=float(b.get("salience", 0.7)),
                expires_in_seconds=int(b["expires_in_seconds"]) if b.get("expires_in_seconds") else None
            )
            return J({"ok": True, "id": mid})

        @r.delete("/personas/memories/{mid}")
        async def persona_mem_del(req):
            mid = int(req.match_info["mid"])
            self.memory.delete_memory(mid)
            return J({"ok": True})

        @r.post("/personas/memories/{mid}/promote")
        async def persona_mem_promote(req):
            mid = int(req.match_info["mid"])
            self.memory.promote_to_anchor(mid)
            return J({"ok": True})

        @r.post("/personas/{pid}/memory/prune")
        async def persona_mem_prune(req):
            pid = int(req.match_info["pid"])
            res = self.memory.prune_memories(pid)
            return J(res)

        @r.post("/memory/prune-all")
        async def memory_prune_all(_):
            res = self.memory.prune_memories()
            return J(res)

        @r.get("/memory/overview")
        async def memory_overview(_):
            personas = self.store.rows("SELECT id, name, color FROM personas ORDER BY id ASC")
            items = []
            for p in personas:
                tree = self.memory.get_memory_tree(p["id"])
                items.append({
                    "id": p["id"],
                    "name": p["name"],
                    "color": p["color"],
                    "stats": tree["stats"]
                })
            return J({"personas": items})

        # ---------- Usage & Compute Analytics Routes ----------
        @r.get("/usage/summary")
        async def usage_summary(_):
            summary = self.usage.get_summary()
            return J(summary)

        @r.get("/usage/timeseries")
        async def usage_timeseries(req):
            days = int(req.query.get("days", 7))
            ts = self.usage.get_timeseries(days=days)
            return J(ts)

        @r.get("/usage/personas")
        async def usage_personas(_):
            pb = self.usage.get_persona_breakdown()
            return J(pb)

        @r.get("/usage/activity")
        async def usage_activity(req):
            limit = int(req.query.get("limit", 25))
            act = self.usage.get_recent_activity(limit=limit)
            return J(act)
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
    asyncio.create_task(d.ensure_camoufox())
    asyncio.create_task(d.auto_sync_loop())
    asyncio.create_task(d.daily_batch_loop())
    asyncio.create_task(d.memory_pruner_loop())
    try:
        await d.ensure_clients()
    except Exception as e:  # noqa
        log.warning("telegram connect deferred: %s", e)
    await d.worker()


if __name__ == "__main__":
    asyncio.run(main())
