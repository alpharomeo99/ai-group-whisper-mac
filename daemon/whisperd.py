#!/usr/bin/env python3
"""AI Group Whisper daemon: Telethon client + SQLite queue + local HTTP API for the Electron UI."""
import argparse
import asyncio
import logging
import os
import secrets
import sys
import random
import re
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
        self.avatars_dir = os.path.join(data_dir, "avatars")
        os.makedirs(self.avatars_dir, exist_ok=True)
        self.suspended = False
        # Clear stale paused_reason so transient AI limits from earlier never freeze daemon on restart
        self.paused_reason = None
        self.store.set("paused_reason", None)
        if self.store.get("system_enabled") is None:
            self.store.set("system_enabled", True)
        self._adopt_legacy_session()
        self.wake_event = asyncio.Event()
        self.prov = Provisioner(self)
        self.orchestrator = PersonaCadenceOrchestrator(self.store)
        self.memory = memory.MemoryEngine(self.store, ai)
        self.daily_batch = daily_batch.DailyBatchEngine(self.store, ai, memory_engine=self.memory)
        self.usage = usage.UsageTracker(self.store)
        self.autoapi = {}        # token -> {"future": code future, "task": camoufox task}
        self.cfx_install = {"running": False, "log": ""}
        self._account_locks = {}
        self._presence_abort_events = {}
        # Clear stale failed jobs and batch items on boot so queue remains clean and responsive
        try:
            self.store.q("DELETE FROM queue WHERE status='failed'")
            self.store.q("DELETE FROM daily_batch_items WHERE status='failed'")
        except Exception as e:
            log.debug("startup failed item purge: %s", e)

        # Proactively tune stored pacing defaults: 25-minute (1500s) cooling window during human conversations
        try:
            curr_pause = int(self.store.get("batch_human_pause_seconds", 1500))
            if curr_pause <= 45:
                self.store.set("batch_human_pause_seconds", 1500)
            curr_int = int(self.store.get("batch_session_interval_seconds", 90))
            if curr_int > 180:
                self.store.set("batch_session_interval_seconds", 90)
            curr_cnt = int(self.store.get("batch_messages_count", 48))
            if curr_cnt < 48:
                self.store.set("batch_messages_count", 48)
        except Exception as e:
            log.debug("startup pacing tuning notice: %s", e)

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
        try:
            await self.fetch_account_avatar(c, new_aid, me)
        except Exception:
            pass
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
        first_active = self.store.rows(
            "SELECT a.id FROM accounts a LEFT JOIN group_accounts ga ON ga.account_id=a.id "
            "WHERE a.active=1 AND LOWER(COALESCE(a.username,''))<>'xbiolabs' "
            "GROUP BY a.id ORDER BY COUNT(ga.chat_id) DESC, a.id ASC LIMIT 1")
        return first_active[0]["id"] if first_active else None

    async def ensure_clients(self):
        """Maintains persistent event listeners across all active accounts to guarantee 100% real-time
        event coverage across all watched groups, regardless of which account is a member.
        Returns {account_id: client} for all authorized connected accounts.
        """
        active_accs = self.store.rows("SELECT * FROM accounts WHERE active=1 ORDER BY id")
        active_ids = {a["id"] for a in active_accs}

        # Disconnect any accounts that are no longer active
        for aid in list(self.clients.keys()):
            if aid not in active_ids:
                c = self.clients.pop(aid)
                try:
                    await c.disconnect()
                except Exception:
                    pass

        ready = {}
        for a in active_accs:
            aid = a["id"]
            c = self.clients.get(aid)
            if c is None:
                api = self._api(a)
                if not api:
                    continue
                c = self._new_client(a["session"], api, a.get("proxy_id"))
                c.add_event_handler(self._handler_for(aid), events.NewMessage())
                c.add_event_handler(self._chat_action_handler_for(aid), events.ChatAction())
                self.clients[aid] = c

            try:
                if not c.is_connected():
                    await asyncio.wait_for(c.connect(), timeout=12)
                if await c.is_user_authorized():
                    if not a.get("user_id"):
                        me = await c.get_me()
                        self._save_profile(aid, me, me.phone and "+" + me.phone.lstrip("+"))
                    ready[aid] = c
            except Exception as e:
                log.warning("Account %s connect notice: %s", aid, e)

        return ready

    async def _safe_ensure_scout(self):
        if getattr(self, "_ensuring_scout", False):
            return
        self._ensuring_scout = True
        try:
            await self.ensure_clients()
        except Exception as e:
            log.warning("background scout ensure failed: %s", e)
        finally:
            self._ensuring_scout = False

    def _get_account_lock(self, account_id):
        aid = int(account_id)
        if aid not in self._account_locks:
            self._account_locks[aid] = asyncio.Lock()
        return self._account_locks[aid]

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

        # If account is lingering in casual presence check-in, interrupt linger immediately
        if account_id in self._presence_abort_events:
            self._presence_abort_events[account_id].set()

        lock = self._get_account_lock(account_id)
        async with lock:
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
            connected = False
            for c_attempt in range(2):
                try:
                    await asyncio.wait_for(client.connect(), timeout=25.0)
                    connected = True
                    break
                except Exception as ce:
                    if c_attempt == 0:
                        await asyncio.sleep(1.5)
                        continue
                    raise ConnectionError(f"Account {account_id} connection timed out/failed: {ce}") from ce
            if not await client.is_user_authorized():
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise ConnectionError(f"Account {account_id} ({a.get('name')}) is not authorized")

            if not a.get("user_id"):
                try:
                    me = await client.get_me()
                    if me:
                        self._save_profile(account_id, me, me.phone and "+" + me.phone.lstrip("+"))
                except Exception:
                    pass

            try:
                yield client
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    async def _casual_check_in(self, aid, account_info):
        """Ephemerally wakes up a dormant account to simulate a human user checking Telegram on their phone.
        Marks status online, browses recent dialogs, lingers for 15-45s, sets status offline, and disconnects."""
        lock = self._get_account_lock(aid)
        if lock.locked():
            return  # Account is actively sending or syncing, skip this check-in

        abort_ev = asyncio.Event()
        self._presence_abort_events[aid] = abort_ev

        try:
            async with lock:
                api = self._api(account_info)
                if not api:
                    return
                client = self._new_client(account_info["session"], api, account_info.get("proxy_id"))
                try:
                    await asyncio.wait_for(client.connect(), timeout=12)
                    if not await client.is_user_authorized():
                        return

                    # Update presence: Mark Online
                    try:
                        from telethon.tl.functions.account import UpdateStatusRequest
                        await client(UpdateStatusRequest(offline=False))
                    except Exception as e:
                        log.debug("Online status update error for account %s: %s", aid, e)

                    # Casual human browsing: refresh top dialogs / messages
                    try:
                        await client.get_dialogs(limit=5)
                    except Exception:
                        pass

                    # Snappy check-in: 5 to 12s so account locks are promptly released for sending
                    linger = random.uniform(5.0, 12.0)
                    log.info("Casual presence: Account %s (%s) online for quick check (~%ds)", aid, account_info.get("name"), int(linger))

                    try:
                        await asyncio.wait_for(abort_ev.wait(), timeout=linger)
                    except asyncio.TimeoutError:
                        pass

                    # Update presence: Mark Offline (records Last Seen)
                    try:
                        from telethon.tl.functions.account import UpdateStatusRequest
                        await client(UpdateStatusRequest(offline=True))
                    except Exception:
                        pass
                finally:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
        except Exception as e:
            log.warning("Casual check-in failed for account %s: %s", aid, e)
        finally:
            self._presence_abort_events.pop(aid, None)

    async def casual_presence_loop(self):
        """Autonomous background simulation: dormant accounts randomly wake up,
        log into Telegram briefly (set status online, check dialogs), and go back to sleep.
        Intervals follow an organic circadian wakefulness curve with Poisson intervals."""
        await asyncio.sleep(45)
        next_checkins = {}
        while True:
            try:
                system_enabled = bool(self.store.get("system_enabled", True))
                if not system_enabled or self.suspended or self.paused_reason:
                    await asyncio.sleep(30)
                    continue

                scout_aid = self.get_scout_account_id()
                accounts = self.store.rows("SELECT * FROM accounts WHERE active=1")
                now = time.time()
                active_ids = {a["id"] for a in accounts}

                for aid in list(next_checkins.keys()):
                    if aid not in active_ids:
                        del next_checkins[aid]

                for a in accounts:
                    aid = a["id"]
                    if aid == scout_aid:
                        continue  # Scout maintains the persistent incoming event socket

                    if aid not in next_checkins:
                        # Stagger initial check-in: 2 to 20 minutes from boot
                        next_checkins[aid] = now + random.uniform(120, 1200)
                        continue

                    if now >= next_checkins[aid]:
                        circ_factor = 1.0
                        try:
                            circ_factor = self.orchestrator.circadian_factor()
                        except Exception:
                            pass

                        # Daytime base interval: 25 to 55 minutes.
                        # Nighttime (circ_factor lower): stretches up to 90 to 180+ minutes.
                        base_mins = random.uniform(25.0, 55.0)
                        adjusted_mins = base_mins / max(circ_factor, 0.25)
                        jitter = random.uniform(-5.0, 10.0)
                        next_interval_secs = max(600.0, (adjusted_mins + jitter) * 60.0)
                        next_checkins[aid] = now + next_interval_secs

                        asyncio.create_task(self._casual_check_in(aid, a))
                        await asyncio.sleep(random.uniform(5.0, 15.0))

            except Exception as e:
                log.warning("casual_presence_loop error: %s", e)

            await asyncio.sleep(30)

    def _save_profile(self, aid, me, phone):
        name = " ".join(x for x in (me.first_name, me.last_name) if x) or "Account"
        self.store.q("UPDATE accounts SET user_id=?, phone=?, name=?, username=? WHERE id=?",
                     (me.id, phone, name, me.username, aid))

    async def fetch_account_avatar(self, client, aid, me=None):
        try:
            dest = os.path.join(self.avatars_dir, f"{aid}.jpg")
            temp_dest = os.path.join(self.avatars_dir, f"{aid}.tmp.jpg")

            if me is None:
                try:
                    me = await client.get_me()
                except Exception as e:
                    log.warning("get_me failed for account %s: %s", aid, e)
                    return False
            if not me:
                return False

            if os.path.exists(temp_dest):
                try:
                    os.remove(temp_dest)
                except Exception:
                    pass

            downloaded = False

            # Strategy 1: Official high-resolution download via download_profile_photo('me')
            try:
                res = await client.download_profile_photo('me', file=temp_dest, download_big=True)
                if res and os.path.exists(temp_dest) and os.path.getsize(temp_dest) > 500:
                    downloaded = True
                    log.info("Downloaded high-res profile photo for account %s (%d bytes)", aid, os.path.getsize(temp_dest))
            except Exception as e:
                log.debug("Strategy 1 (download_profile_photo 'me' big) failed for account %s: %s", aid, e)

            # Strategy 2: Query user photos directly via get_profile_photos
            if not downloaded:
                try:
                    photos = await client.get_profile_photos('me', limit=1)
                    if photos and len(photos) > 0:
                        res = await client.download_media(photos[0], file=temp_dest)
                        if res and os.path.exists(temp_dest) and os.path.getsize(temp_dest) > 500:
                            downloaded = True
                            log.info("Downloaded profile photo via get_profile_photos for account %s (%d bytes)", aid, os.path.getsize(temp_dest))
                except Exception as e:
                    log.debug("Strategy 2 (get_profile_photos) failed for account %s: %s", aid, e)

            # Strategy 3: Download using the full me entity
            if not downloaded:
                try:
                    res = await client.download_profile_photo(me, file=temp_dest, download_big=True)
                    if res and os.path.exists(temp_dest) and os.path.getsize(temp_dest) > 500:
                        downloaded = True
                        log.info("Downloaded profile photo using entity me for account %s (%d bytes)", aid, os.path.getsize(temp_dest))
                except Exception as e:
                    log.debug("Strategy 3 (download_profile_photo me) failed for account %s: %s", aid, e)

            # Strategy 4: Standard size profile photo
            if not downloaded:
                try:
                    res = await client.download_profile_photo('me', file=temp_dest, download_big=False)
                    if res and os.path.exists(temp_dest) and os.path.getsize(temp_dest) > 500:
                        downloaded = True
                        log.info("Downloaded standard profile photo for account %s (%d bytes)", aid, os.path.getsize(temp_dest))
                except Exception as e:
                    log.debug("Strategy 4 (download_profile_photo small) failed for account %s: %s", aid, e)

            if downloaded and os.path.exists(temp_dest) and os.path.getsize(temp_dest) > 500:
                os.replace(temp_dest, dest)
                return True

            # Clean stale or corrupted tiny file (< 500 bytes)
            if os.path.exists(dest) and os.path.getsize(dest) < 500:
                try:
                    os.remove(dest)
                except Exception:
                    pass

            # Strategy 5: Thumbnail fallback from stripped_thumb only if dest does not exist
            if not os.path.exists(dest):
                photo = getattr(me, 'photo', None)
                if photo and getattr(photo, 'stripped_thumb', None):
                    try:
                        from telethon import utils
                        thumb_data = utils.stripped_photo_to_jpg(photo.stripped_thumb)
                        if thumb_data and len(thumb_data) > 0:
                            with open(dest, "wb") as f:
                                f.write(thumb_data)
                            log.info("Saved fallback thumbnail avatar for account %s (%d bytes)", aid, len(thumb_data))
                            return True
                    except Exception as e:
                        log.debug("stripped_thumb decoding failed for account %s: %s", aid, e)

            return os.path.exists(dest) and os.path.getsize(dest) > 0
        except Exception as e:
            log.warning("Avatar fetch failed for account %s: %s", aid, e)
            return False

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
                "VALUES(?,?,?,?,?,?,?,0) "
                "ON CONFLICT(account_id, peer_id) DO UPDATE SET "
                "peer_name=excluded.peer_name, peer_username=excluded.peer_username, "
                "peer_phone=COALESCE(excluded.peer_phone, direct_chats.peer_phone), "
                "last_msg=excluded.last_msg, last_ts=excluded.last_ts",
                (aid, peer_id, name, username, phone, event.raw_text, ts)
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
                    }, dedupe_key=f"dm_reply:{aid}:{peer_id}:{event.id}", delay=max(4, int(self.orchestrator.calculate_deliberation_delay(event.raw_text))))
            self.wake_event.set()
            return

        # 2. Handle Group Messages
        is_group_chat = bool(
            getattr(event, 'is_group', False)
            or getattr(event, 'is_channel', False)
            or (getattr(event, 'chat_id', None) and event.chat_id < 0)
        )
        if not is_group_chat or not event.raw_text:
            return
        chat_id = event.chat_id

        # Auto-discover group & self-heal watched status
        try:
            chat = await event.get_chat()
            title = getattr(chat, 'title', None) or getattr(chat, 'name', None) or f"Group {chat_id}"
            acc_check = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
            has_persona = bool(acc_check and acc_check[0]["persona_id"])
            self.store.q(
                "INSERT INTO groups(chat_id,title,account_id,watched,auto_reply) VALUES(?,?,?,1,1) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, watched=1, auto_reply=1",
                (chat_id, title, aid if has_persona else None)
            )
            self.store.q("INSERT OR IGNORE INTO group_accounts(chat_id, account_id) VALUES(?,?)", (chat_id, aid))
        except Exception:
            pass

        # Deduplication: claim message; if already handled, skip
        if not self.store.claim_incoming(chat_id, event.id):
            return
        msg_age = int(time.time()) - int(event.date.timestamp()) if getattr(event, "date", None) else 0

        g = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
        if not g:
            self.store.q("INSERT OR IGNORE INTO groups(chat_id, title, watched, auto_reply) VALUES(?,?,1,1)", (chat_id, f"Group {chat_id}"))
            g = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
        elif not g[0].get("watched") or not g[0].get("auto_reply"):
            self.store.q("UPDATE groups SET watched=1, auto_reply=1 WHERE chat_id=?", (chat_id,))
            g = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (chat_id,))

        sender = None
        try:
            sender = await event.get_sender()
        except Exception:
            pass
        if not sender and getattr(event, "client", None) and getattr(event, "sender_id", None):
            try:
                sender = await event.client.get_entity(event.sender_id)
            except Exception:
                pass

        first_n = getattr(sender, 'first_name', None) or ""
        last_n = getattr(sender, 'last_name', None) or ""
        name = " ".join(x for x in (first_n, last_n) if x) or getattr(sender, 'title', None) or getattr(sender, 'username', None) or "Member"
        sender_username = (getattr(sender, "username", None) or "").lower().lstrip("@")
        sender_id = getattr(sender, "id", None) or getattr(event, "sender_id", None)
        raw_text = event.raw_text or ""

        # Fetch accounts and personas to identify sender role
        try:
            all_accounts = self.store.rows("SELECT id, user_id, name, username, phone, persona_id, is_admin FROM accounts")
        except Exception:
            all_accounts = self.store.rows("SELECT id, user_id, name, username, phone, persona_id FROM accounts")
            for a in all_accounts:
                a["is_admin"] = 0
        all_personas = self.store.rows("SELECT id, name, details FROM personas")

        # Comprehensive Admin identification (Telegram group permissions, anonymous admin bot/channel, configured handle, xbiolabs)
        is_chat_admin = False
        if sender_id and sender_id > 0 and getattr(event, "client", None):
            try:
                perms = await event.client.get_permissions(chat_id, sender_id)
                if perms and (getattr(perms, 'is_admin', False) or getattr(perms, 'is_creator', False)):
                    is_chat_admin = True
            except Exception:
                pass
        if sender_id == chat_id or sender_id == 1087968824 or getattr(event, "post", False) or sender_username == "groupanonymousbot":
            is_chat_admin = True

        configured_admin = (self.store.get("admin_handle") or "").lower().lstrip("@")
        admin_u_ids = {
            a["user_id"] for a in all_accounts
            if a.get("user_id") and (
                a.get("is_admin")
                or "xbiolabs" in (a.get("username") or "").lower()
                or (configured_admin and (a.get("username") or "").lower().lstrip("@") == configured_admin)
            )
        }
        is_admin_sender = bool(
            "xbiolabs" in sender_username
            or "xbiolabs" in name.lower()
            or (configured_admin and (sender_username == configured_admin or configured_admin in sender_username))
            or is_chat_admin
            or (sender_id and sender_id in admin_u_ids)
            or any(a.get("user_id") == sender_id and (a.get("is_admin") or "xbiolabs" in (a.get("username") or "").lower() or (configured_admin and (a.get("username") or "").lower().lstrip("@") == configured_admin)) for a in all_accounts)
        )

        # Check if sender is an internal bot persona (strictly excluding admin, xbiolabs, and chat admins)
        bot_user_ids = {
            a["user_id"] for a in all_accounts
            if a.get("user_id") and not a.get("is_admin") and "xbiolabs" not in (a.get("username") or "").lower().lstrip("@")
            and ((a.get("username") or "").lower().lstrip("@") != configured_admin if configured_admin else True)
        }
        is_internal_bot = bool(not is_admin_sender and (getattr(event, "out", False) or (sender_id and sender_id in bot_user_ids)))

        # Record message with is_bot flag
        self.store.add_message(
            chat_id, event.id, name, raw_text, int(event.date.timestamp() if event.date else time.time()),
            is_bot=1 if is_internal_bot else 0, sender_id=sender_id
        )
        self.usage.log_event("msg_recv", account_id=aid, chat_id=chat_id)

        # Active peer dynamic: Persona messages wake the queue, and can organically trigger
        # spontaneous chime-ins from awake teammates (controlled probability + anti-loop cooldown).
        if is_internal_bot:
            self.wake_event.set()
            try:
                await self.maybe_trigger_peer_chime_in(chat_id, event, raw_text, sender_id, all_accounts, all_personas)
            except Exception as e:
                log.debug("maybe_trigger_peer_chime_in notice: %s", e)
            return

        # 4. LIVE AI ORCHESTRATION: Triggered when an external HUMAN or @xbiolabs ADMIN sends a message!
        system_enabled = bool(self.store.get("system_enabled", True))
        if msg_age > 900:
            log.info("Skipping reply to stale message %s in %s (%ds old)", event.id, chat_id, msg_age)
            system_enabled = False

        msg_handled = False
        try:
            if system_enabled:
                g_title = g[0].get("title") if g else f"Group {chat_id}"
                sender_label = "ADMIN (@xbiolabs)" if is_admin_sender else f"HUMAN ({name})"
                self.store.set("last_human_event", {
                    "chat_id": chat_id,
                    "group_title": g_title,
                    "sender": name,
                    "sender_username": sender_username,
                    "text": raw_text,
                    "is_admin": is_admin_sender,
                    "ts": int(time.time())
                })
                log.info("%s DETECTED in %s: %s", sender_label, g_title, raw_text[:80])

                # Record meaningful human disclosures for conversational continuity
                if not is_admin_sender and raw_text and not raw_text.startswith("["):
                    try:
                        self.memory.record_human_interaction(chat_id, name, sender_id, sender_username, raw_text)
                    except Exception as mem_err:
                        log.warning("Memory recording notice: %s", mem_err)

                # Detect direct replies and mentions across ALL persona accounts & @xbiolabs
                is_direct = bool(getattr(event, 'mentioned', False)) or is_admin_sender
                target_persona_id = None
                target_account_id = None
                reply_parent_text = None
                reply_parent_sender = None
                reply_parent_id = None

                # A. Check reply target: was a persona message replied to?
                # 1. First, check infallible local SQLite message table (0ms, 100% reliable)
                if getattr(event, 'is_reply', False) or getattr(event, 'reply_to_msg_id', None):
                    r_id = getattr(event, 'reply_to_msg_id', None)
                    if r_id:
                        reply_parent_id = r_id
                        p_rows = self.store.rows("SELECT sender, text, persona_id, account_id, is_bot FROM messages WHERE chat_id=? AND msg_id=?", (chat_id, r_id))
                        if p_rows:
                            reply_parent_sender = p_rows[0].get("sender") or ""
                            reply_parent_text = p_rows[0].get("text") or ""
                            if p_rows[0].get("persona_id") and p_rows[0].get("account_id"):
                                is_direct = True
                                target_persona_id = p_rows[0]["persona_id"]
                                target_account_id = p_rows[0]["account_id"]

                # 2. Second, verify with Telethon get_reply_message if target not fully resolved
                if not target_persona_id and getattr(event, 'is_reply', False):
                    try:
                        rep = await event.get_reply_message()
                        if rep:
                            reply_parent_id = rep.id
                            reply_parent_text = reply_parent_text or rep.text or rep.raw_text or ""
                            if rep.sender:
                                reply_parent_sender = reply_parent_sender or " ".join(x for x in (getattr(rep.sender, 'first_name', None), getattr(rep.sender, 'last_name', None)) if x) or getattr(rep.sender, 'username', None) or ""
                            if rep.sender_id:
                                for a in all_accounts:
                                    if a.get("user_id") == rep.sender_id:
                                        is_direct = True
                                        target_account_id = a["id"]
                                        target_persona_id = a.get("persona_id")
                                        for p in all_personas:
                                            if p.get("id") == target_persona_id:
                                                reply_parent_sender = p.get("name") or reply_parent_sender
                                        break
                    except Exception:
                        pass

                # B. Check handle / username mention in text
                if not target_persona_id and raw_text:
                    text_lower = raw_text.lower()
                    for a in all_accounts:
                        uname = (a.get("username") or "").lower().lstrip("@")
                        if uname and uname != "xbiolabs" and (f"@{uname}" in text_lower or re.search(r"(?:|_)" + re.escape(uname) + r"(?:|_)", text_lower)):
                            is_direct = True
                            target_account_id = a["id"]
                            target_persona_id = a.get("persona_id")
                            break

                # C. Check persona name mention in text (e.g. "Leila", "Sam", "Dave")
                if not target_persona_id and raw_text:
                    text_lower = raw_text.lower()
                    for p in all_personas:
                        pname = (p.get("name") or "").lower()
                        if pname and len(pname) >= 2 and re.search(r"(?:|_)" + re.escape(pname) + r"(?:|_)", text_lower):
                            is_direct = True
                            target_persona_id = p["id"]
                            matching_accs = [a for a in all_accounts if a.get("persona_id") == p["id"] and a.get("active", 1)]
                            if matching_accs:
                                target_account_id = matching_accs[0]["id"]
                            break

                # D. If @xbiolabs mentioned or speaking, always mark direct
                if is_admin_sender or ("@xbiolabs" in raw_text.lower()):
                    is_direct = True

                if target_persona_id and not target_account_id:
                    matching_accs = [a for a in all_accounts if a.get("persona_id") == target_persona_id and a.get("active", 1)]
                    if not matching_accs:
                        matching_accs = [a for a in all_accounts if a.get("persona_id") == target_persona_id]
                    if matching_accs:
                        target_account_id = matching_accs[0]["id"]

                # Proactively postpone any pending scheduled batch messages so live human conversation has exclusive priority
                try:
                    now_ts_ev = int(time.time())
                    cooldown_secs = max(1200, int(self.store.get("batch_human_pause_seconds", 1500)))
                    self.store.q(
                        "UPDATE daily_batch_items SET status='postponed', scheduled_ts=scheduled_ts+? "
                        "WHERE chat_id=? AND status IN ('pending', 'postponed') AND scheduled_ts < ?",
                        (cooldown_secs, chat_id, now_ts_ev + cooldown_secs)
                    )
                except Exception as e:
                    log.debug("Proactive batch postponement: %s", e)

                # Hard budget cap guard (bash.20/day for human replies) - Admin messages always bypass cap!
                can_reply, r_spent, r_cap = self.store.check_reply_budget_available()
                if not is_admin_sender and not can_reply:
                    log.warning(
                        "[BUDGET CAP] Daily human reply budget cap ($%.2f) reached ($%.4f spent today). "
                        "Skipping reply to %s in group %s until midnight reset.",
                        r_cap, r_spent, sender_label, chat_id
                    )
                    msg_handled = True
                    return

                candidates = self.daily_batch.get_group_candidates(chat_id)
                if not candidates:
                    self.ensure_group_personas_assigned(chat_id)
                    candidates = self.daily_batch.get_group_candidates(chat_id)

                if not candidates:
                    all_active = self.store.rows("SELECT id, name, username FROM accounts WHERE active=1 AND (is_admin=0 OR is_admin IS NULL) AND LOWER(COALESCE(username,'')) NOT LIKE '%xbiolabs%'")
                    for i, acc in enumerate(all_active):
                        if all_personas:
                            p = all_personas[i % len(all_personas)]
                            candidates.append({
                                "persona_id": p["id"],
                                "persona_name": p["name"],
                                "account_id": acc["id"],
                                "account_name": acc["name"],
                                "details": p.get("details"),
                                "prompt": p.get("prompt")
                            })

                if candidates:
                    if target_persona_id and not target_account_id:
                        target_account_id = candidates[0].get("account_id")
                    if target_persona_id and target_account_id:
                        if not any(c.get("persona_id") == target_persona_id for c in candidates):
                            p_name = reply_parent_sender or "Target Persona"
                            for p in all_personas:
                                if p.get("id") == target_persona_id:
                                    p_name = p.get("name") or p_name
                                    break
                            candidates.append({
                                "persona_id": target_persona_id,
                                "account_id": target_account_id,
                                "persona_name": p_name
                            })

                    pid, aid_chosen, delay, reason = self.orchestrator.arbiter_decision(
                        chat_id, candidates,
                        {"text": raw_text, "sender": name, "id": event.id},
                        is_direct_mention=is_direct,
                        target_persona_id=target_persona_id,
                        target_account_id=target_account_id,
                        is_admin=is_admin_sender
                    )
                    if not (pid and aid_chosen):
                        for c in candidates:
                            if c.get("persona_id") and c.get("account_id"):
                                pid = c["persona_id"]
                                aid_chosen = c["account_id"]
                                delay = 3.5
                                reason = "Candidate election fallback for guaranteed responsiveness"
                                break
                    if pid and aid_chosen:
                        self.orchestrator.record_activity(chat_id, pid)
                        # Snappy primary reply delay (2 to 5s for admin, 2 to 10s for humans)
                        eff_reply_delay = random.randint(2, 5) if is_admin_sender else max(2, min(int(delay * 0.40), 10))
                        self.store.enqueue(
                            "draft_reply", chat_id,
                            {
                                "trigger": event.id,
                                "trigger_sender": name,
                                "trigger_text": raw_text,
                                "reply_parent_text": reply_parent_text,
                                "reply_parent_sender": reply_parent_sender,
                                "reply_parent_id": reply_parent_id,
                                "auto_send": True,
                                "account_id": aid_chosen,
                                "persona_id": pid,
                                "is_admin": is_admin_sender,
                                "reason": reason
                            },
                            dedupe_key=f"reply:{chat_id}:{event.id}",
                            delay=eff_reply_delay
                        )
                        log.info("Enqueued %s reply in %s (Acc %s / Persona %s, delay=%ds): %s",
                                 "ADMIN (@xbiolabs)" if is_admin_sender else "HUMAN", chat_id, aid_chosen, pid, eff_reply_delay, reason)
                        msg_handled = True

                        # Dynamic Teammate Chime-In: high engagement second opinion (65% humans, 80% admin)
                        chime_chance = 0.80 if is_admin_sender else 0.65
                        if len(candidates) >= 2 and random.random() < chime_chance and not target_persona_id:
                            teammates = [c for c in candidates if c["persona_id"] != pid and c["account_id"] != aid_chosen]
                            if teammates:
                                counts = self.orchestrator.persona_speaker_counts.get(chat_id, {})
                                teammates.sort(key=lambda t: counts.get(t["persona_id"], 0))
                                t_c = teammates[0]
                                second_delay = eff_reply_delay + random.randint(10, 24)
                                self.store.enqueue(
                                    "draft_reply", chat_id,
                                    {
                                        "trigger": event.id,
                                        "trigger_sender": name,
                                        "trigger_text": raw_text,
                                        "auto_send": True,
                                        "account_id": t_c["account_id"],
                                        "persona_id": t_c["persona_id"],
                                        "is_admin": is_admin_sender,
                                        "is_teammate_chime": True,
                                        "primary_persona_id": pid,
                                        "reason": "Teammate natural follow-up chime-in"
                                    },
                                    dedupe_key=f"teammate_reply:{chat_id}:{event.id}",
                                    delay=second_delay
                                )
                                log.info("Enqueued teammate follow-up in %s (Acc %s / Persona %s, delay=%ds)", chat_id, t_c["account_id"], t_c["persona_id"], second_delay)
                else:
                    # No candidates resolved: still answer from any persona account in this group or database
                    fallback = self.store.rows(
                        "SELECT a.id, a.persona_id FROM accounts a JOIN group_accounts ga ON ga.account_id=a.id "
                        "WHERE ga.chat_id=? AND a.active=1 AND a.persona_id IS NOT NULL "
                        "AND LOWER(COALESCE(a.username,''))<>'xbiolabs' ORDER BY RANDOM() LIMIT 1", (chat_id,))
                    if not fallback:
                        fallback = self.store.rows(
                            "SELECT a.id, a.persona_id FROM accounts a "
                            "WHERE a.active=1 AND a.persona_id IS NOT NULL "
                            "AND LOWER(COALESCE(a.username,''))<>'xbiolabs' ORDER BY RANDOM() LIMIT 1")
                    log.warning("No reply candidates for %s in %s; fallback account=%s", sender_label, chat_id,
                                fallback[0]["id"] if fallback else None)
                    if fallback:
                        eff_delay = random.randint(2, 5) if is_admin_sender else max(4, int(self.orchestrator.calculate_deliberation_delay(raw_text)))
                        self.store.enqueue("draft_reply", chat_id, {
                            "account_id": fallback[0]["id"],
                            "persona_id": fallback[0]["persona_id"],
                            "trigger": event.id,
                            "trigger_sender": name,
                            "trigger_text": raw_text,
                            "is_admin": is_admin_sender,
                            "auto_send": True
                        }, dedupe_key=f"reply:{chat_id}:{event.id}", delay=eff_delay)
                        msg_handled = True
            else:
                msg_handled = True
        except Exception as proc_err:
            log.error("Incoming group processing exception for msg %s in %s: %s", event.id, chat_id, proc_err)
        finally:
            if not msg_handled and not is_internal_bot:
                self.store.unclaim_incoming(chat_id, event.id)
                log.info("Unclaimed failed message %s in %s so backup sweeper can retry", event.id, chat_id)
        self.wake_event.set()

    # ---------- Queue worker ----------
    async def worker(self):
        while True:
            self.wake_event.clear()
            if self.suspended or self.paused_reason or not bool(self.store.get("system_enabled", True)):
                try:
                    await asyncio.wait_for(self.wake_event.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                jobs = self.store.claim(limit=4)
                if jobs:
                    for idx, job in enumerate(jobs):
                        await self.run_job(job)
                        if idx < len(jobs) - 1:
                            await asyncio.sleep(random.uniform(2.5, 6.0))
                    if len(jobs) == 4:
                        await asyncio.sleep(random.uniform(3.0, 7.0))
                        continue
            except Exception as e:
                log.warning("Worker run_job execution error: %s", e)

            try:
                await asyncio.wait_for(self.wake_event.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass

    def persona_for(self, g, persona_id=None, account_id=None, topic_context=None):
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
            mem_ctx = self.memory.get_prompt_memory_context(effective_pid, chat_id=cid, limit=10, query_text=topic_context)
            if mem_ctx:
                pr = pr + "\n\n" + mem_ctx

        pr += (
            "\n\nCRITICAL CONVERSATIONAL GUIDELINES:\n"
            "1. Grounding: Stay naturally grounded in what members are discussing (fitness, workouts, recovery protocols, daily health). Do not invent unprompted stories about placing orders, shipping packages, or website payment issues.\n"
            "2. Vendor Mentions: Do NOT bring up vendors, suppliers, or purchasing unprompted. If an external participant specifically asks if anyone had bad experiences or if ordering was smooth, keep it casual and positive (e.g. 'orders landed clean for me, no issues'). Never sound defensive or corporate.\n"
            "3. Voice: Chat like an authentic community member with natural length variation. Mix quick punchy reactions (3-10 words) with normal 1-2 sentence comments, and occasional 2-3 sentence explanations when discussing routines or experiences. Never write essays, multi-paragraph walls of text, or formal lists."
        )

        return pr

    def ensure_group_personas_assigned(self, chat_id):
        """Ensures accounts participating in this group that have an assigned persona are bound to group_personas."""
        try:
            acc_rows = self.store.rows("SELECT account_id FROM group_accounts WHERE chat_id=?", (chat_id,))
            acc_ids = {r["account_id"] for r in acc_rows if r.get("account_id")}
            g = self.store.rows("SELECT account_id, watched FROM groups WHERE chat_id=?", (chat_id,))
            if g and g[0].get("account_id"):
                acc_ids.add(g[0]["account_id"])
            if g and g[0].get("watched"):
                all_active = self.store.rows("SELECT id FROM accounts WHERE active=1 AND persona_id IS NOT NULL")
                for ar in all_active:
                    acc_ids.add(ar["id"])
                    self.store.q("INSERT OR IGNORE INTO group_accounts(chat_id, account_id) VALUES(?,?)", (chat_id, ar["id"]))

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

        base_cps = max(float(t.get("chars_per_second") or 7.0), 3.5)

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

    def burst_typing_secs(self, g, text):
        """Typing-indicator delay for rapid consecutive burst messages.
        Because fingers are already typing, initiation hesitation is brief (~0.4-0.9s)
        and keystroke velocity is high (9-14 CPS), creating authentic multi-bubble delivery (1.2s - 5.5s).
        """
        import random
        msg_len = max(len(text or ""), 1)
        effective_cps = random.uniform(9.0, 14.0)
        hesitation = random.uniform(0.4, 0.9)
        typing_dur = msg_len / effective_cps
        total = hesitation + typing_dur
        return max(1.2, min(total, 5.5))

    async def safe_send_message(self, cl, aid, chat_id, text, reply_to=None):
        """Sends message with automatic resilience against invalid reply targets, unprimed peers, and account restrictions."""
        try:
            return await cl.send_message(chat_id, text, reply_to=reply_to)
        except (errors.rpcerrorlist.ReplyToMsgIdInvalidError, errors.rpcerrorlist.MsgIdInvalidError):
            log.warning("Reply target invalid/deleted in %s; sending without quote", chat_id)
            return await cl.send_message(chat_id, text, reply_to=None)
        except (ValueError, errors.rpcerrorlist.PeerIdInvalidError, errors.rpcerrorlist.ChannelInvalidError) as peer_err:
            try:
                entity = await cl.get_entity(chat_id)
                return await cl.send_message(entity, text, reply_to=reply_to)
            except Exception:
                try:
                    dialogs = await cl.get_dialogs(limit=250)
                    target_entity = None
                    for d in dialogs:
                        if d.id == chat_id or getattr(d.entity, 'id', None) == chat_id:
                            target_entity = d.entity
                            break
                        if isinstance(chat_id, int) and chat_id < 0:
                            norm_cid = abs(chat_id)
                            if str(norm_cid).startswith('100') and len(str(norm_cid)) > 10:
                                norm_cid = int(str(norm_cid)[3:])
                            if getattr(d.entity, 'id', None) == norm_cid or d.id == norm_cid:
                                target_entity = d.entity
                                break
                    if target_entity:
                        return await cl.send_message(target_entity, text, reply_to=reply_to)
                except Exception as search_err:
                    log.debug("Dialog search in %s for account %s: %s", chat_id, aid, search_err)

                # Resilient fallback: Re-route to Scout account or any verified active group account
                scout_aid = self.get_scout_account_id()
                alt_aids = []
                if scout_aid and scout_aid != aid:
                    alt_aids.append(scout_aid)
                for r in self.store.rows("SELECT a.id FROM group_accounts ga JOIN accounts a ON a.id=ga.account_id WHERE ga.chat_id=? AND a.active=1 AND a.id != ?", (chat_id, aid)):
                    if r["id"] not in alt_aids:
                        alt_aids.append(r["id"])
                for alt_aid in alt_aids:
                    try:
                        log.info("Peer resolution fallback in %s: re-routing from account %s to verified account %s", chat_id, aid, alt_aid)
                        async with self.account_session(alt_aid) as alt_cl:
                            return await alt_cl.send_message(chat_id, text, reply_to=None)
                    except Exception as alt_err:
                        log.debug("Alternate account %s delivery fallback error in %s: %s", alt_aid, chat_id, alt_err)
                raise peer_err
        except (errors.rpcerrorlist.ChatWriteForbiddenError, errors.rpcerrorlist.UserBannedInChannelError, errors.rpcerrorlist.ChannelPrivateError) as perm_err:
            scout_aid = self.get_scout_account_id()
            alt_aids = []
            if scout_aid and scout_aid != aid:
                alt_aids.append(scout_aid)
            for r in self.store.rows("SELECT a.id FROM group_accounts ga JOIN accounts a ON a.id=ga.account_id WHERE ga.chat_id=? AND a.active=1 AND a.id != ?", (chat_id, aid)):
                if r["id"] not in alt_aids:
                    alt_aids.append(r["id"])
            for alt_aid in alt_aids:
                try:
                    log.warning("Account %s cannot send in %s (%s). Re-routing to verified account %s", aid, chat_id, perm_err, alt_aid)
                    async with self.account_session(alt_aid) as alt_cl:
                        return await alt_cl.send_message(chat_id, text, reply_to=None)
                except Exception as alt_err:
                    log.debug("Alternate account %s permission fallback error in %s: %s", alt_aid, chat_id, alt_err)
            raise perm_err

    async def auto_generate_daily_if_needed(self):
        """Helper to ensure batches are generated automatically in the background if none exist."""
        try:
            can_batch, b_spent, b_cap = self.store.check_batch_budget_available()
            if not can_batch:
                log.debug("auto_generate_daily_if_needed: Daily batch budget cap reached ($%.4f >= $%.2f)", b_spent, b_cap)
                return
            today = self.daily_batch.today_str()
            all_cids = [r["chat_id"] for r in self.store.rows(
                "SELECT chat_id FROM groups WHERE watched=1 "
                "UNION SELECT chat_id FROM group_personas "
                "UNION SELECT chat_id FROM group_accounts "
                "UNION SELECT chat_id FROM groups"
            )]
            for cid in all_cids:
                existing = self.store.rows("SELECT id FROM daily_batches WHERE chat_id=? AND date_str=?", (cid, today))
                if not existing:
                    cands = self.daily_batch.get_group_candidates(cid)
                    if len(cands) >= 1:
                        count = int(self.store.get("batch_messages_count", 24))
                        log.info("Auto-Pilot: Auto-generating missing batch for group %s (%d candidates)", cid, len(cands))
                        await self.daily_batch.generate_batch_for_group(cid, count=count)
                        self.wake_event.set()
        except Exception as e:
            log.warning("auto_generate_daily_if_needed error: %s", e)

    async def run_job(self, job):
        import json
        p = json.loads(job["payload"])
        settings = {k: self.store.get(k) for k in ("fal_key", "ai_model", "admin_handle")}
        g = (self.store.rows("SELECT * FROM groups WHERE chat_id=?", (job["chat_id"],)) or [{}])[0]
        try:
            if self.suspended:
                raise asyncio.CancelledError()
            if job["kind"] == "summarize":
                body = await ai.summarize(settings, g.get("title", ""), self.store.recent(job["chat_id"]))
                self.store.q("INSERT INTO summaries(chat_id,body,created) VALUES(?,?,?)",
                             (job["chat_id"], body, int(time.time())))
            elif job["kind"] == "draft_reply":
                is_admin_job = bool(p.get("is_admin"))
                can_reply, r_spent, r_cap = self.store.check_reply_budget_available()
                if not is_admin_job and not can_reply:
                    log.warning(
                        "[BUDGET CAP] Daily reply budget cap ($%.2f) reached ($%.4f spent). Cancelling reply job %s.",
                        r_cap, r_spent, job["id"]
                    )
                    self.store.finish(job["id"], True, f"Daily reply budget cap ($0.20) reached ($ {r_spent:.4f} spent)")
                    return
                trigger_sender = p.get("trigger_sender") or ""
                trigger_text = p.get("trigger_text") or ""
                reply_parent_text = p.get("reply_parent_text") or ""
                reply_parent_sender = p.get("reply_parent_sender") or ""
                if not trigger_text and p.get("trigger"):
                    trig_rows = self.store.rows("SELECT sender, text FROM messages WHERE chat_id=? AND msg_id=?", (job["chat_id"], p["trigger"]))
                    if trig_rows:
                        trigger_sender = trig_rows[0].get("sender") or ""
                        trigger_text = trig_rows[0].get("text") or ""

                all_accs = self.store.rows("SELECT username, name FROM accounts")
                all_pers = self.store.rows("SELECT name FROM personas")
                internal_persona_tags = set()
                for a in all_accs:
                    uname = (a.get("username") or "").lower().lstrip("@")
                    if uname and uname != "xbiolabs":
                        internal_persona_tags.add(uname)
                for pr in all_pers:
                    pname = (pr.get("name") or "").lower()
                    if pname and len(pname) >= 3:
                        internal_persona_tags.add(pname)

                chosen_persona = self.persona_for(
                    g,
                    persona_id=p.get("persona_id"),
                    account_id=p.get("account_id"),
                    topic_context=trigger_text
                )
                eff_pid = p.get("persona_id")
                if eff_pid:
                    proto_card = self.store.get_persona_protocol_card(eff_pid)
                    if proto_card and proto_card not in chosen_persona:
                        chosen_persona = chosen_persona + chr(10) + chr(10) + proto_card

                cand_names = [r["name"] for r in self.store.rows("SELECT name FROM personas UNION SELECT name FROM accounts") if r.get("name")]
                if trigger_sender and trigger_sender not in cand_names:
                    cand_names.append(trigger_sender)

                # Fetch relational memory context about this specific human member
                human_ctx = self.memory.get_human_context(job["chat_id"], trigger_sender)

                # Resolve persona details & burstiness prior to drafting
                burstiness = 35
                eff_pid = p.get("persona_id")
                dt = None
                p_disp_name = p.get("persona_name") or p.get("name")
                if eff_pid:
                    p_row = self.store.rows("SELECT name, details FROM personas WHERE id=?", (eff_pid,))
                    if p_row:
                        if not p_disp_name and p_row[0].get("name"):
                            p_disp_name = p_row[0]["name"]
                        if p_row[0].get("details"):
                            try:
                                dt = json.loads(p_row[0]["details"] or "{}")
                                burstiness = int(dt.get("burstiness") or dt.get("burstiness_percent") or 35)
                            except Exception:
                                dt = None

                primary_reply_text = p.get("primary_reply_text")
                if not primary_reply_text and p.get("is_teammate_chime") and p.get("trigger"):
                    trig_id = p.get("trigger")
                    rep_rows = self.store.rows(
                        "SELECT text FROM messages WHERE chat_id=? AND reply_to_msg_id=? AND is_bot=1 ORDER BY id DESC LIMIT 1",
                        (job["chat_id"], trig_id)
                    )
                    if rep_rows and rep_rows[0].get("text"):
                        primary_reply_text = rep_rows[0]["text"]
                    else:
                        q_sends = self.store.rows(
                            "SELECT payload FROM queue WHERE chat_id=? AND kind='send' AND status IN ('pending', 'completed') "
                            "ORDER BY id DESC LIMIT 5",
                            (job["chat_id"],)
                        )
                        for qs in q_sends:
                            try:
                                q_p = json.loads(qs["payload"]) if isinstance(qs["payload"], str) else qs["payload"]
                                if q_p.get("reply_to") == trig_id and q_p.get("text"):
                                    primary_reply_text = q_p.get("text")
                                    break
                            except Exception:
                                pass

                text, meta = await ai.draft_reply_with_meta(
                    settings,
                    g.get("title", ""),
                    chosen_persona,
                    self.store.recent(job["chat_id"], 25),
                    trigger_sender=trigger_sender,
                    trigger_text=trigger_text,
                    reply_parent_text=reply_parent_text,
                    reply_parent_sender=reply_parent_sender,
                    known_names=cand_names,
                    internal_persona_tags=list(internal_persona_tags),
                    is_admin=bool(p.get("is_admin")),
                    human_context=human_ctx,
                    is_teammate_chime=bool(p.get("is_teammate_chime")),
                    temperature=0.78,
                    persona_name=p_disp_name,
                    persona_details=dt,
                    primary_reply_text=primary_reply_text
                )
                # Log AI token & latency usage
                self.usage.log_ai("ai_chat", persona_id=p.get("persona_id"), account_id=p.get("account_id"),
                                  chat_id=job["chat_id"], tokens_prompt=meta.get("prompt_tokens"),
                                  tokens_completion=meta.get("completion_tokens"), latency_ms=meta.get("latency_ms"),
                                  model=meta.get("model"))

                if not (text or "").strip():
                    log.warning("Empty draft for job %s; redrafting once", job["id"])
                    text, meta = await ai.draft_reply_with_meta(
                        settings, g.get("title", ""), chosen_persona, self.store.recent(job["chat_id"], 12),
                        trigger_sender=trigger_sender, trigger_text=trigger_text,
                        reply_parent_text=reply_parent_text, reply_parent_sender=reply_parent_sender,
                        known_names=cand_names, internal_persona_tags=list(internal_persona_tags),
                        is_admin=bool(p.get("is_admin")), human_context=human_ctx,
                        is_teammate_chime=bool(p.get("is_teammate_chime")), temperature=0.92,
                        persona_name=p_disp_name, persona_details=dt,
                        primary_reply_text=primary_reply_text
                    )
                if not (text or "").strip():
                    raise RuntimeError("AI returned an empty reply twice")

                bubbles = ai.split_burst_bubbles(text, names=cand_names, burstiness_pct=burstiness)

                if p.get("auto_send"):
                    self.store.enqueue("send", job["chat_id"],
                                       {
                                           "text": text,
                                           "bubbles": bubbles,
                                           "reply_to": p.get("trigger"),
                                           "account_id": p.get("account_id"),
                                           "persona_id": p.get("persona_id")
                                       },
                                       dedupe_key=f"send:{job['id']}")
                    self.wake_event.set()
                else:
                    preview = "\n\n".join(bubbles) if len(bubbles) > 1 else text
                    self.store.q("INSERT INTO summaries(chat_id,body,created) VALUES(?,?,?)",
                                 (job["chat_id"], "Draft reply:\n" + preview, int(time.time())))
            elif job["kind"] == "send":
                aid = p.get("account_id") or g.get("account_id")
                cand_names = [r["name"] for r in self.store.rows("SELECT name FROM personas UNION SELECT name FROM accounts") if r.get("name")]

                eff_pid = p.get("persona_id")
                if not eff_pid and aid:
                    acc_p = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
                    eff_pid = acc_p[0]["persona_id"] if acc_p else None

                burstiness = 35
                if eff_pid:
                    p_row = self.store.rows("SELECT details FROM personas WHERE id=?", (eff_pid,))
                    if p_row and p_row[0].get("details"):
                        try:
                            dt = json.loads(p_row[0]["details"] or "{}")
                            burstiness = int(dt.get("burstiness") or dt.get("burstiness_percent") or 35)
                        except Exception:
                            pass

                bubbles = p.get("bubbles")
                if not bubbles:
                    clean_txt = ai.format_telegram_message(p.get("text", ""), names=cand_names, max_chars=360)
                    bubbles = ai.split_burst_bubbles(clean_txt or p.get("text", ""), names=cand_names, burstiness_pct=burstiness)
                if not bubbles:
                    bubbles = [p.get("text", "")]
                bubbles = [b.strip() for b in bubbles if b and b.strip()]

                reply_to_msg_id = p.get("reply_to")
                if not reply_to_msg_id and p.get("reply_to_item_id"):
                    rep_rows = self.store.rows("SELECT telegram_msg_id FROM daily_batch_items WHERE id=?", (p["reply_to_item_id"],))
                    if rep_rows and rep_rows[0].get("telegram_msg_id"):
                        reply_to_msg_id = rep_rows[0]["telegram_msg_id"]

                # Native Telegram Emoji Reaction Execution
                react_emoji = p.get("reaction")
                if react_emoji and reply_to_msg_id:
                    async with self.account_session(aid) as cl:
                        try:
                            from telethon.tl.functions.messages import SendReactionRequest
                            from telethon.tl.types import ReactionEmoji
                            await asyncio.sleep(random.uniform(1.2, 3.2))
                            await cl(SendReactionRequest(
                                peer=job["chat_id"],
                                msg_id=int(reply_to_msg_id),
                                reaction=[ReactionEmoji(emoticon=str(react_emoji).strip())]
                            ))
                            log.info("Sent native Telegram reaction '%s' to msg %s in %s (account %s)", react_emoji, reply_to_msg_id, job["chat_id"], aid)
                            if p.get("batch_item_id"):
                                self.store.mark_batch_item_sent(p["batch_item_id"], int(time.time()), telegram_msg_id=reply_to_msg_id)
                            self.usage.log_event("reaction_sent", persona_id=eff_pid, account_id=aid, chat_id=job["chat_id"])
                            if eff_pid:
                                self.orchestrator.record_activity(job["chat_id"], eff_pid)
                            self.store.finish(job["id"], True)
                            return
                        except Exception as react_err:
                            log.warning("Safe reaction fallback (%s): %s. Proceeding to text bubbles.", react_emoji, react_err)

                async with self.account_session(aid) as cl:
                    sent_msg = None
                    for b_idx, bubble_text in enumerate(bubbles):
                        if b_idx == 0:
                            secs = self.typing_secs(g, bubble_text)
                        else:
                            await asyncio.sleep(random.uniform(0.8, 1.8))
                            secs = self.burst_typing_secs(g, bubble_text)

                        try:
                            async with cl.action(job["chat_id"], "typing"):
                                await asyncio.sleep(secs)
                        except Exception:  # noqa
                            pass

                        curr_reply = reply_to_msg_id if b_idx == 0 else None
                        sent_msg = await self.safe_send_message(cl, aid, job["chat_id"], bubble_text, reply_to=curr_reply)
                        sender_id = getattr(sent_msg, "sender_id", None)
                        if sender_id and aid and b_idx == 0:
                            acc_check = self.store.rows("SELECT user_id FROM accounts WHERE id=?", (aid,))
                            if acc_check and not acc_check[0]["user_id"]:
                                self.store.q("UPDATE accounts SET user_id=? WHERE id=?", (sender_id, aid))
                        self.store.add_message(
                            job["chat_id"], sent_msg.id, p.get("sender_name") or "Me",
                            bubble_text, int(time.time()), is_bot=1, sender_id=sender_id,
                            persona_id=eff_pid, account_id=aid
                        )
                        if eff_pid:
                            self.memory.auto_record_statement(eff_pid, aid, job["chat_id"], bubble_text)
                        self.usage.log_event("msg_sent", persona_id=eff_pid, account_id=aid, chat_id=job["chat_id"])

                        if b_idx == 0 and p.get("batch_item_id"):
                            self.store.mark_batch_item_sent(p["batch_item_id"], int(time.time()), telegram_msg_id=sent_msg.id)

                    if eff_pid:
                        self.orchestrator.record_activity(job["chat_id"], eff_pid)

            elif job["kind"] == "batch_scheduled_send":
                now_ts = int(time.time())
                # Human conversation cooling window: default 25 minutes (1500s), minimum 20 minutes (1200s)
                configured_pause = max(1200, int(self.store.get("batch_human_pause_seconds", 1500)))

                # Check recent human activity across the last 45 minutes
                human_count, last_human_ts, avg_gap = self.store.get_recent_human_message_stats(job["chat_id"], window_secs=2700)
                if not last_human_ts:
                    last_human_ts = self.store.get_last_human_message_ts(job["chat_id"])

                # Also check recent stored human event
                last_event = self.store.get("last_human_event")
                if isinstance(last_event, dict) and last_event.get("chat_id") == job["chat_id"]:
                    ev_ts = int(last_event.get("ts") or 0)
                    if ev_ts > (last_human_ts or 0):
                        last_human_ts = ev_ts

                postpone_count = int(p.get("_postpone_count") or 0)
                first_attempt_ts = int(p.get("_first_attempt_ts") or now_ts)
                starvation_age = max(0, now_ts - first_attempt_ts)

                # Check if batch item is excessively stale (older than 18 hours of human conversation)
                if starvation_age > 64800:
                    if p.get("batch_item_id"):
                        self.store.q("UPDATE daily_batch_items SET status='expired' WHERE id=?", (p["batch_item_id"],))
                    self.store.finish(job["id"], True, "Batch item expired after active day of human conversations")
                    return

                # Clock skew guard: if Telegram server ts is ahead of Mac system clock
                if last_human_ts and last_human_ts > now_ts:
                    last_human_ts = now_ts - 10

                elapsed_since_human = max(0, now_ts - last_human_ts) if last_human_ts else 999999

                # If a human has spoken within the cooling window, smoothly YIELD
                if last_human_ts and elapsed_since_human < configured_pause:
                    remaining = max(20, configured_pause - elapsed_since_human)
                    defer_secs = remaining + random.randint(15, 60)

                    p["_postpone_count"] = postpone_count + 1
                    p["_first_attempt_ts"] = first_attempt_ts
                    self.store.q("UPDATE queue SET payload=? WHERE id=?", (json.dumps(p), job["id"]))

                    if p.get("batch_item_id"):
                        self.store.q("UPDATE daily_batch_items SET status='postponed', scheduled_ts=? WHERE id=?",
                                     (now_ts + defer_secs, p["batch_item_id"]))
                    mins_left = round((remaining + 30) / 60.0, 1)
                    reason = (
                        f"Paused for live human conversation: last human spoke {elapsed_since_human}s ago. "
                        f"Holding pre-scheduled chatter until {configured_pause // 60}m quiet window (resuming in ~{mins_left}m)."
                    )
                    self.store.finish(job["id"], False, reason, retry_in=defer_secs, count_attempt=False)
                    return

                aid = p.get("account_id") or g.get("account_id")
                cand_names = [r["name"] for r in self.store.rows("SELECT name FROM personas UNION SELECT name FROM accounts") if r.get("name")]

                eff_pid = p.get("persona_id")
                if not eff_pid and aid:
                    acc_p = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
                    eff_pid = acc_p[0]["persona_id"] if acc_p else None

                burstiness = 35
                if eff_pid:
                    p_row = self.store.rows("SELECT details FROM personas WHERE id=?", (eff_pid,))
                    if p_row and p_row[0].get("details"):
                        try:
                            dt = json.loads(p_row[0]["details"] or "{}")
                            burstiness = int(dt.get("burstiness") or dt.get("burstiness_percent") or 35)
                        except Exception:
                            pass

                bubbles = p.get("bubbles")
                if not bubbles:
                    clean_txt = ai.format_telegram_message(p.get("text", ""), names=cand_names, max_chars=360)
                    bubbles = ai.split_burst_bubbles(clean_txt or p.get("text", ""), names=cand_names, burstiness_pct=burstiness)
                if not bubbles:
                    bubbles = [p.get("text", "")]
                bubbles = [b.strip() for b in bubbles if b and b.strip()]

                reply_to_msg_id = p.get("reply_to")
                if not reply_to_msg_id and p.get("reply_to_item_id"):
                    rep_rows = self.store.rows("SELECT telegram_msg_id FROM daily_batch_items WHERE id=?", (p["reply_to_item_id"],))
                    if rep_rows and rep_rows[0].get("telegram_msg_id"):
                        reply_to_msg_id = rep_rows[0]["telegram_msg_id"]

                # Native Telegram Emoji Reaction Execution
                react_emoji = p.get("reaction")
                if react_emoji and reply_to_msg_id:
                    async with self.account_session(aid) as cl:
                        try:
                            from telethon.tl.functions.messages import SendReactionRequest
                            from telethon.tl.types import ReactionEmoji
                            await asyncio.sleep(random.uniform(1.2, 3.2))
                            await cl(SendReactionRequest(
                                peer=job["chat_id"],
                                msg_id=int(reply_to_msg_id),
                                reaction=[ReactionEmoji(emoticon=str(react_emoji).strip())]
                            ))
                            log.info("Sent native Telegram reaction '%s' to msg %s in %s (account %s)", react_emoji, reply_to_msg_id, job["chat_id"], aid)
                            if p.get("batch_item_id"):
                                self.store.mark_batch_item_sent(p["batch_item_id"], int(time.time()), telegram_msg_id=reply_to_msg_id)
                            self.usage.log_event("reaction_sent", persona_id=eff_pid, account_id=aid, chat_id=job["chat_id"])
                            if eff_pid:
                                self.orchestrator.record_activity(job["chat_id"], eff_pid)
                            self.store.finish(job["id"], True)
                            return
                        except Exception as react_err:
                            log.warning("Safe reaction fallback (%s): %s. Proceeding to text bubbles.", react_emoji, react_err)

                async with self.account_session(aid) as cl:
                    sent_msg = None
                    for b_idx, bubble_text in enumerate(bubbles):
                        if b_idx == 0:
                            secs = self.typing_secs(g, bubble_text)
                        else:
                            await asyncio.sleep(random.uniform(0.8, 1.8))
                            secs = self.burst_typing_secs(g, bubble_text)

                        try:
                            async with cl.action(job["chat_id"], "typing"):
                                await asyncio.sleep(secs)
                        except Exception:  # noqa
                            pass

                        curr_reply = reply_to_msg_id if b_idx == 0 else None
                        sent_msg = await self.safe_send_message(cl, aid, job["chat_id"], bubble_text, reply_to=curr_reply)
                        sender_id = getattr(sent_msg, "sender_id", None)
                        if sender_id and aid and b_idx == 0:
                            acc_check = self.store.rows("SELECT user_id FROM accounts WHERE id=?", (aid,))
                            if acc_check and not acc_check[0]["user_id"]:
                                self.store.q("UPDATE accounts SET user_id=? WHERE id=?", (sender_id, aid))
                        self.store.add_message(
                            job["chat_id"], sent_msg.id, p.get("sender_name") or "Me",
                            bubble_text, int(time.time()), is_bot=1, sender_id=sender_id,
                            persona_id=eff_pid, account_id=aid
                        )
                        if eff_pid:
                            self.memory.auto_record_statement(eff_pid, aid, job["chat_id"], bubble_text)
                        self.usage.log_event("msg_sent", persona_id=eff_pid, account_id=aid, chat_id=job["chat_id"])

                        if b_idx == 0 and p.get("batch_item_id"):
                            self.store.mark_batch_item_sent(p["batch_item_id"], int(time.time()), telegram_msg_id=sent_msg.id)

                    if eff_pid:
                        self.orchestrator.record_activity(job["chat_id"], eff_pid)

            elif job["kind"] == "direct_send":
                aid = p.get("account_id")
                peer_id = job["chat_id"]
                eff_pid = p.get("persona_id")
                if not eff_pid and aid:
                    acc_p = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (aid,))
                    eff_pid = acc_p[0]["persona_id"] if acc_p else None

                burstiness = 35
                if eff_pid:
                    p_row = self.store.rows("SELECT details FROM personas WHERE id=?", (eff_pid,))
                    if p_row and p_row[0].get("details"):
                        try:
                            dt = json.loads(p_row[0]["details"] or "{}")
                            burstiness = int(dt.get("burstiness") or dt.get("burstiness_percent") or 35)
                        except Exception:
                            pass

                bubbles = p.get("bubbles")
                if not bubbles:
                    bubbles = ai.split_burst_bubbles(p.get("text", ""), burstiness_pct=burstiness)
                if not bubbles:
                    bubbles = [p.get("text", "")]
                bubbles = [b.strip() for b in bubbles if b and b.strip()]

                async with self.account_session(aid) as cl:
                    for b_idx, bubble_text in enumerate(bubbles):
                        if b_idx == 0:
                            secs = self.typing_secs({"chat_id": peer_id, "account_id": aid}, bubble_text)
                        else:
                            await asyncio.sleep(random.uniform(0.8, 1.8))
                            secs = self.burst_typing_secs({"chat_id": peer_id, "account_id": aid}, bubble_text)
                        try:
                            async with cl.action(peer_id, "typing"):
                                await asyncio.sleep(secs)
                        except Exception:
                            pass
                        curr_reply = p.get("reply_to") if b_idx == 0 else None
                        sent = await self.safe_send_message(cl, aid, peer_id, bubble_text, reply_to=curr_reply)
                        self.store.add_direct_message(aid, peer_id, sent.id, "Me", 0, bubble_text, int(time.time()))
                        if eff_pid:
                            self.memory.auto_record_statement(eff_pid, aid, peer_id, bubble_text)
                        self.usage.log_event("msg_sent", persona_id=eff_pid, account_id=aid, chat_id=peer_id)

            elif job["kind"] == "direct_reply":
                aid = p.get("account_id")
                peer_id = job["chat_id"]
                pid = p.get("persona_id")
                burstiness = 35
                persona_row = self.store.rows("SELECT * FROM personas WHERE id=?", (pid,)) if pid else []
                if persona_row and persona_row[0].get("details"):
                    try:
                        dt = json.loads(persona_row[0]["details"] or "{}")
                        burstiness = int(dt.get("burstiness") or dt.get("burstiness_percent") or 35)
                    except Exception:
                        pass
                persona_prompt = persona_row[0]["prompt"] if persona_row else "You are chatting 1-on-1 on Telegram. Keep responses natural, human, and direct."
                recent_msgs = self.store.direct_messages_recent(aid, peer_id, 20)
                formatted = [{"sender": m["sender_name"], "text": m["text"]} for m in recent_msgs]
                chat_info = self.store.rows("SELECT * FROM direct_chats WHERE account_id=? AND peer_id=?", (aid, peer_id))
                peer_name = chat_info[0]["peer_name"] if chat_info else "User"
                text = await ai.draft_reply(settings, f"1-on-1 conversation with {peer_name}", persona_prompt, formatted)
                bubbles = ai.split_burst_bubbles(text, burstiness_pct=burstiness)
                if p.get("auto_send"):
                    self.store.enqueue("direct_send", peer_id, {
                        "account_id": aid,
                        "persona_id": pid,
                        "text": text,
                        "bubbles": bubbles,
                        "reply_to": p.get("trigger")
                    }, dedupe_key=f"dmsend:{job['id']}")
                else:
                    preview = "\n\n".join(bubbles) if len(bubbles) > 1 else text
                    self.store.add_direct_message(aid, peer_id, 0, "AI Draft", 0, "[Draft]: " + preview, int(time.time()))
            self.store.finish(job["id"], True)
        except asyncio.CancelledError:
            self.store.finish(job["id"], False, "interrupted by sleep", retry_in=5)
        except ai.AIError as e:
            if e.status in (402, 403):
                self.paused_reason = f"AI provider blocked requests ({e.status}): {e}"
                self.store.set("paused_reason", self.paused_reason)
                self.store.finish(job["id"], False, str(e), retry_in=0)
                if job.get("kind") == "batch_scheduled_send" and p.get("batch_item_id"):
                    self.store.mark_batch_item_failed(p["batch_item_id"], str(e))
            elif e.retryable and job["attempts"] < 5:
                retry_in = min(600, 15 * 2 ** job["attempts"])
                self.store.finish(job["id"], False, str(e), retry_in=retry_in)
                if job.get("kind") == "batch_scheduled_send" and p.get("batch_item_id"):
                    self.store.postpone_batch_item(p["batch_item_id"], int(time.time()) + retry_in, f"AI retry: {e}")
            else:
                self.store.finish(job["id"], False, str(e))
                if job.get("kind") == "batch_scheduled_send" and p.get("batch_item_id"):
                    self.store.mark_batch_item_failed(p["batch_item_id"], str(e))
        except (errors.FloodWaitError, errors.rpcerrorlist.SlowmodeWaitError) as e:
            wait_sec = getattr(e, "seconds", 30) + 2
            self.store.finish(job["id"], False, f"Telegram rate wait {wait_sec}s", retry_in=wait_sec)
            if job.get("kind") == "batch_scheduled_send" and p.get("batch_item_id"):
                self.store.postpone_batch_item(p["batch_item_id"], int(time.time()) + wait_sec, f"Rate wait {wait_sec}s")
        except (ConnectionError, OSError, asyncio.TimeoutError, TimeoutError) as e:
            attempts = job.get("attempts", 0)
            if attempts < 5:
                retry_in = min(300, 15 * (2 ** attempts))
                self.store.finish(job["id"], False, str(e), retry_in=retry_in)
                if job.get("kind") == "batch_scheduled_send" and p.get("batch_item_id"):
                    self.store.postpone_batch_item(p["batch_item_id"], int(time.time()) + retry_in, f"Network retry: {e}")
            else:
                self.store.finish(job["id"], False, str(e))
                if job.get("kind") == "batch_scheduled_send" and p.get("batch_item_id"):
                    self.store.mark_batch_item_failed(p["batch_item_id"], str(e))
        except Exception as e:  # noqa
            attempts = job.get("attempts", 0)
            if attempts < 3:
                retry_in = min(180, 15 * (2 ** attempts))
                log.warning("Transient error on job %s (attempt %d/3), will retry in %ds: %s", job["id"], attempts + 1, retry_in, e)
                self.store.finish(job["id"], False, str(e), retry_in=retry_in)
                if job.get("kind") == "batch_scheduled_send" and p.get("batch_item_id"):
                    self.store.postpone_batch_item(p["batch_item_id"], int(time.time()) + retry_in, f"Auto-retry: {e}")
            else:
                log.exception("job %s failed after %d attempts: %s", job["id"], attempts + 1, e)
                self.store.finish(job["id"], False, str(e))
                if job.get("kind") == "batch_scheduled_send" and p.get("batch_item_id"):
                    self.store.mark_batch_item_failed(p["batch_item_id"], str(e))

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
            rearranged = self.store.stagger_overdue_batch_jobs(min_spacing_secs=90)
            log.info("resume (%s): reconnecting, requeued %d, restaggered %d overdue batch items", reason, n, rearranged)
            try:
                await self.catch_up()
            except Exception as e:  # network may lag after wake; worker retries
                log.warning("reconnect after wake failed: %s", e)
            self.wake_event.set()

    async def sync_all_dialogs(self, account_ids=None, force_all=False):
        """Discover every group's membership for selected active accounts, including dormant accounts.
        Guarded against overlapping runs so VPN and network connections are not overwhelmed."""
        if getattr(self, "_sync_in_progress", False):
            log.info("sync_all_dialogs is already in progress; skipping concurrent request")
            return
        self._sync_in_progress = True
        try:
            return await self._do_sync_all_dialogs(account_ids=account_ids, force_all=force_all)
        finally:
            self._sync_in_progress = False

    async def _do_sync_all_dialogs(self, account_ids=None, force_all=False):
        """Telegram dialogs require each account's own session. The persistent Scout only covers one
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
                try:
                    me = await c.get_me()
                    if me:
                        self._save_profile(aid, me, me.phone and "+" + me.phone.lstrip("+"))
                        await self.fetch_account_avatar(c, aid, me)
                except Exception:
                    pass
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
                            "INSERT INTO groups(chat_id,title,account_id,watched,auto_reply) VALUES(?,?,?,1,1) "
                            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, watched=1, auto_reply=1, "
                            "account_id=COALESCE(groups.account_id, excluded.account_id)",
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

    async def maybe_trigger_peer_chime_in(self, chat_id, event, raw_text, sender_id, all_accounts, all_personas):
        """Allows awake peer personas to spontaneously react or follow up on a teammate's message
        with strict anti-loop, anti-dogpile, and pacing controls. Creates lively natural peer banter."""
        if not raw_text or len(raw_text.strip()) < 8:
            return

        now_ts = int(time.time())
        # Cooldown guard: at least 25 seconds between spontaneous peer chime-ins in the same group
        last_chime_ts = int(self.store.get(f"last_peer_chime_{chat_id}") or 0)
        if (now_ts - last_chime_ts) < 25:
            return

        # Identify sender persona/account
        sender_acc = [a for a in all_accounts if a.get("user_id") == sender_id]
        sender_pid = sender_acc[0].get("persona_id") if sender_acc else None
        sender_aid = sender_acc[0]["id"] if sender_acc else None

        candidates = self.daily_batch.get_group_candidates(chat_id)
        # Filter for other personas that are NOT the sender
        peer_cands = [c for c in candidates if c.get("persona_id") != sender_pid and c.get("account_id") != sender_aid]
        if not peer_cands:
            return

        # Filter for candidates who are awake in their personal timezone right now
        awake_peers = [c for c in peer_cands if self.orchestrator.is_persona_awake(c, ts=now_ts)]
        eligible = awake_peers if awake_peers else peer_cands

        # Probability roll: high engagement on questions (88%) and banter (65%)
        has_question = ("?" in raw_text)
        prob = 0.88 if has_question else 0.65
        if random.random() > prob:
            return

        # Pick responder using arbiter decision
        pid, aid_chosen, delay, reason = self.orchestrator.arbiter_decision(
            chat_id, eligible,
            {"text": raw_text, "sender": getattr(event, "sender_id", sender_id), "id": event.id},
            is_direct_mention=has_question,
            is_admin=False
        )
        if not pid or not aid_chosen:
            return

        # Fast, snappy deliberation & reading delay (3.5 to 14 seconds)
        effective_delay = max(3.5, min((delay * 0.40) + random.uniform(1.5, 4.0), 14.0))

        # Record chime-in timestamp to prevent looping
        self.store.set(f"last_peer_chime_{chat_id}", now_ts + int(effective_delay))
        self.orchestrator.record_activity(chat_id, pid)

        # Enqueue reply as draft_reply with auto_send=True so it executes cleanly
        self.store.enqueue(
            "draft_reply", chat_id,
            {
                "trigger": event.id,
                "trigger_sender": getattr(event, "sender_name", None) or "Teammate",
                "trigger_text": raw_text,
                "reply_parent_text": raw_text,
                "reply_parent_sender": "Teammate",
                "reply_parent_id": event.id,
                "auto_send": True,
                "account_id": aid_chosen,
                "persona_id": pid,
                "is_admin": False,
                "is_teammate_chime": True,
                "reason": "Spontaneous peer banter"
            },
            dedupe_key=f"peer_chime:{chat_id}:{event.id}",
            delay=int(effective_delay)
        )
        log.info("Spontaneous peer chime-in scheduled in group %s (persona %s in %.1fs)", chat_id, pid, effective_delay)

    async def human_reply_sweeper_loop(self):
        """Every ~20s, reads the newest messages of each watched group and hands any human/@xbiolabs
        message that the live listener missed to on_message (claim_incoming prevents double replies)."""
        await asyncio.sleep(20)
        last_prune = 0
        while True:
            try:
                if not self.suspended and bool(self.store.get("system_enabled", True)):
                    await self._sweep_groups_once()
                if time.time() - last_prune > 3600:
                    self.store.prune_processed_incoming()
                    last_prune = time.time()
            except Exception as e:
                log.warning("human_reply_sweeper error: %s", e)
            await asyncio.sleep(random.uniform(16, 24))

    async def _sweep_groups_once(self):
        ready = await self.ensure_clients()
        if not ready:
            return
        configured_admin = (self.store.get("admin_handle") or "").lower().lstrip("@")
        accs = self.store.rows("SELECT id, user_id, username, is_admin FROM accounts")
        own_ids = {
            a["user_id"] for a in accs
            if a.get("user_id") and not a.get("is_admin") and "xbiolabs" not in (a.get("username") or "").lower()
            and ((a.get("username") or "").lower().lstrip("@") != configured_admin if configured_admin else True)
        }
        now = int(time.time())
        target_cids = [r["chat_id"] for r in self.store.rows(
            "SELECT chat_id FROM groups WHERE watched=1 OR auto_reply=1 "
            "UNION SELECT chat_id FROM group_accounts "
            "UNION SELECT chat_id FROM group_personas "
            "UNION SELECT chat_id FROM groups"
        )]
        for cid in target_cids:
            members = [r["account_id"] for r in self.store.rows(
                "SELECT ga.account_id FROM group_accounts ga JOIN accounts a ON a.id=ga.account_id "
                "WHERE ga.chat_id=? AND a.active=1", (cid,))]
            if not members:
                members = list(ready.keys())
            if not members:
                continue
            reader = next((m for m in members if m in ready), members[0])
            last = int(self.store.get(f"last_sweep_{cid}") or 0)
            if now - last < 25:
                continue
            self.store.set(f"last_sweep_{cid}", now)
            try:
                async with self.account_session(reader) as cl:
                    async for m in cl.iter_messages(cid, limit=25):
                        if not m.date or now - int(m.date.timestamp()) > 900:
                            break
                        if m.out or not m.raw_text or (m.sender_id and m.sender_id in own_ids):
                            continue
                        done = self.store.rows("SELECT 1 FROM processed_incoming WHERE chat_id=? AND msg_id=?", (cid, m.id))
                        if done:
                            continue
                        log.info("Sweeper caught missed message %s in %s; handling now", m.id, cid)
                        await self.on_message(m, reader)
            except Exception as e:
                log.warning("sweeper could not read group %s via account %s: %s", cid, reader, e)

    async def auto_sync_loop(self):
        """Continuous background dialog sync loop with gentle cadence for VPN stability."""
        await asyncio.sleep(15)
        while True:
            try:
                await self.sync_all_dialogs()
            except Exception as e:
                log.warning("auto_sync_loop error: %s", e)
            await asyncio.sleep(600)

    async def daily_batch_loop(self):
        """Automatically checks and generates daily batches for active groups with personas."""
        await asyncio.sleep(2)
        while True:
            try:
                system_enabled = bool(self.store.get("system_enabled", True))
                auto_pilot = bool(self.store.get("daily_batch_auto_pilot", True))
                if system_enabled and auto_pilot:
                    today = self.daily_batch.today_str()
                    # Target watched groups or any group configured with active personas/accounts
                    all_cids = [r["chat_id"] for r in self.store.rows(
                        "SELECT chat_id FROM groups WHERE watched=1 "
                        "UNION SELECT chat_id FROM group_personas "
                        "UNION SELECT chat_id FROM group_accounts "
                        "UNION SELECT chat_id FROM groups"
                    )]

                    for cid in all_cids:
                        try:
                            # Check if today has pending/scheduled batch items
                            now_ts = int(time.time())
                            pending_items = self.store.rows(
                                "SELECT id, scheduled_ts FROM daily_batch_items WHERE chat_id=? AND status IN ('pending', 'postponed') AND created_at >= ?",
                                (cid, now_ts - 86400)
                            )
                            existing_batches = self.store.rows("SELECT id FROM daily_batches WHERE chat_id=? AND date_str=?", (cid, today))

                            upcoming_items = [it for it in pending_items if (it.get("scheduled_ts") or 0) > now_ts]

                            should_generate = False
                            can_batch, b_spent, b_cap = self.store.check_batch_budget_available()
                            if not can_batch:
                                log.debug("daily_batch_loop: Daily batch spending cap ($%.2f) reached ($%.4f spent).", b_cap, b_spent)
                            elif not existing_batches:
                                # Strictly ONE meticulous batch per group per day
                                should_generate = True

                            if should_generate:
                                cands = self.daily_batch.get_group_candidates(cid)
                                if len(cands) >= 1:
                                    count = int(self.store.get("batch_messages_count", 48))
                                    log.info("Auto-Pilot: Generating batch for group %s (%d candidates, %d messages)", cid, len(cands), count)
                                    await self.daily_batch.generate_batch_for_group(cid, count=count)
                                    self.wake_event.set()
                        except Exception as group_err:
                            log.warning("daily_batch_loop error for group %s: %s", cid, group_err)
            except Exception as e:
                log.warning("daily_batch_loop error: %s", e)
            await asyncio.sleep(20)

    async def catch_up(self):
        """Fetch messages missed while the Mac slept."""
        ready = await self.ensure_clients()
        if not ready:
            return
        all_accs = {a["user_id"]: a for a in self.store.rows("SELECT id, user_id, persona_id, name, username FROM accounts") if a.get("user_id")}
        acc_unames = {(a.get("username") or "").lower().lstrip("@"): a for a in all_accs.values() if a.get("username")}
        acc_names = {(a.get("name") or "").lower(): a for a in all_accs.values() if a.get("name")}

        for g in self.store.rows("SELECT * FROM groups WHERE watched=1"):
            c = ready.get(g["account_id"]) or next(iter(ready.values()))
            last = self.store.q("SELECT MAX(msg_id) FROM messages WHERE chat_id=?", (g["chat_id"],)).fetchone()[0] or 0
            async for m in c.iter_messages(g["chat_id"], min_id=last, limit=200):
                if m.raw_text:
                    s = await m.get_sender()
                    sender_id = getattr(s, "id", None)
                    s_uname = (getattr(s, "username", "") or "").lower().lstrip("@")
                    s_first = (getattr(s, "first_name", "") or "").lower()
                    matched = None
                    if s_uname == "xbiolabs":
                        matched = None
                    elif sender_id and sender_id in all_accs and (all_accs[sender_id].get("username") or "").lower().lstrip("@") != "xbiolabs":
                        matched = all_accs[sender_id]
                    elif s_uname and s_uname in acc_unames:
                        matched = acc_unames[s_uname]

                    is_b = 1 if matched else 0
                    m_aid = matched["id"] if matched else None
                    m_pid = matched["persona_id"] if matched else None

                    self.store.add_message(
                        g["chat_id"], m.id, getattr(s, "first_name", None) or "unknown",
                        m.raw_text, int(m.date.timestamp()),
                        is_bot=is_b, sender_id=sender_id,
                        persona_id=m_pid, account_id=m_aid
                    )

    # ---------- HTTP API ----------
    def routes(self):
        r = web.RouteTableDef()
        J = web.json_response

        @r.get("/status")
        async def status(_):
            scout_aid = self.get_scout_account_id()
            ready = {}
            if scout_aid and scout_aid in self.clients:
                c = self.clients[scout_aid]
                if c and getattr(c, "is_connected", lambda: False)():
                    ready[scout_aid] = c
            if scout_aid and scout_aid not in ready:
                if not getattr(self, "_ensuring_scout", False):
                    asyncio.create_task(self._safe_ensure_scout())
            n_acc = len(self.store.rows("SELECT id FROM accounts WHERE active=1"))
            counts = {x["status"]: x["n"] for x in self.store.rows("SELECT status, COUNT(*) n FROM queue GROUP BY status")}
            return J({"configured": n_acc > 0, "authorized": bool(ready),
                      "accounts": n_acc, "connected": len(ready), "suspended": self.suspended,
                      "paused_reason": self.paused_reason, "queue": counts})

        @r.post("/window/mode")
        async def set_window_mode(req):
            try:
                b = await req.json()
            except Exception:
                b = {}
            mode = b.get("mode") or "full"
            cmd_file = os.path.join(self.data_dir, "window_cmd.json")
            try:
                with open(cmd_file, "w") as f:
                    json.dump({"mode": mode, "t": time.time()}, f)
            except Exception as e:
                return J({"ok": False, "error": str(e)}, status=500)
            return J({"ok": True, "mode": mode})

        @r.get("/window/mode")
        async def get_window_mode(_):
            cmd_file = os.path.join(self.data_dir, "window_cmd.json")
            mode = "full"
            if os.path.isfile(cmd_file):
                try:
                    with open(cmd_file, "r") as f:
                        mode = json.load(f).get("mode", "full")
                except Exception:
                    pass
            return J({"mode": mode})

        SECRET_KEYS = ("fal_key", "vmos_sk", "tv_key")
        PLAIN_KEYS = ("ai_model", "admin_handle", "daily_reply_budget_cap_usd", "daily_batch_budget_cap_usd", "vmos_ak", "vmos_pad", "tv_user", "tv_max_price", "cfx_headless", "cfx_app_title", "cfx_proxy_id")

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

        @r.post("/accounts/{aid}/toggle-admin")
        async def toggle_admin(req):
            aid = int(req.match_info["aid"])
            row = self.store.rows("SELECT is_admin FROM accounts WHERE id=?", (aid,))
            if not row:
                return J({"error": "Account not found"}, status=404)
            new_val = 0 if row[0].get("is_admin") else 1
            self.store.q("UPDATE accounts SET is_admin=? WHERE id=?", (new_val, aid))
            return J({"ok": True, "is_admin": new_val})

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
                avatar_path = os.path.join(self.avatars_dir, f"{a['id']}.jpg")
                a["has_avatar"] = os.path.exists(avatar_path)
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

        @r.get("/accounts/{aid}/avatar")
        async def acc_avatar(req):
            try:
                aid = int(req.match_info["aid"])
                avatar_path = os.path.join(self.avatars_dir, f"{aid}.jpg")
                if os.path.exists(avatar_path) and os.path.getsize(avatar_path) > 0:
                    return web.FileResponse(avatar_path, headers={
                        "Content-Type": "image/jpeg",
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0"
                    })
            except Exception:
                pass
            return web.Response(status=404)

        @r.put("/accounts/{aid}/avatar")
        async def acc_avatar_upload(req):
            try:
                aid = int(req.match_info["aid"])
                acc = self.store.rows("SELECT id FROM accounts WHERE id=?", (aid,))
                if not acc:
                    return J({"error": "Account not found"}, status=404)
                body = await req.read()
                if not body or len(body) > 5 * 1024 * 1024:
                    return J({"error": "Avatar must be a non-empty image under 5 MB"}, status=400)
                from PIL import Image
                import io
                try:
                    with Image.open(io.BytesIO(body)) as image:
                        image.verify()
                    with Image.open(io.BytesIO(body)) as image:
                        image = image.convert("RGB")
                        image.thumbnail((512, 512))
                        temp_path = os.path.join(self.avatars_dir, f"{aid}.upload.tmp.jpg")
                        image.save(temp_path, format="JPEG", quality=88, optimize=True)
                    os.replace(temp_path, os.path.join(self.avatars_dir, f"{aid}.jpg"))
                    return J({"ok": True})
                except Exception:
                    return J({"error": "File is not a supported image"}, status=400)
            except Exception as e:
                log.warning("Avatar upload failed: %s", e)
                return J({"error": str(e)}, status=500)

        @r.get("/personas/{pid}/avatar")
        async def per_avatar(req):
            try:
                pid = int(req.match_info["pid"])
                # Look up bound account in accounts table
                acc = self.store.rows("SELECT id FROM accounts WHERE persona_id=? ORDER BY id LIMIT 1", (pid,))
                aid = None
                if acc:
                    aid = acc[0]["id"]
                else:
                    gp = self.store.rows("SELECT account_id FROM group_personas WHERE persona_id=? AND account_id IS NOT NULL ORDER BY created DESC LIMIT 1", (pid,))
                    if gp and gp[0]["account_id"]:
                        aid = gp[0]["account_id"]

                if aid is not None:
                    avatar_path = os.path.join(self.avatars_dir, f"{aid}.jpg")
                    if os.path.exists(avatar_path) and os.path.getsize(avatar_path) > 0:
                        return web.FileResponse(avatar_path, headers={
                            "Content-Type": "image/jpeg",
                            "Cache-Control": "no-cache, no-store, must-revalidate",
                            "Pragma": "no-cache",
                            "Expires": "0"
                        })
            except Exception:
                pass
            return web.Response(status=404)

        @r.post("/accounts/sync-avatars")
        async def acc_sync_avatars(_):
            accs = self.store.rows("SELECT id, name, phone FROM accounts ORDER BY id")
            synced = 0
            no_photo = 0
            errors_list = []
            sem = asyncio.Semaphore(3)

            async def sync_one(a):
                nonlocal synced, no_photo
                aid = a["id"]
                label = a.get("name") or a.get("phone") or f"Account {aid}"
                async with sem:
                    try:
                        if aid in self.clients and getattr(self.clients[aid], "is_connected", lambda: False)():
                            c = self.clients[aid]
                            ok = await self.fetch_account_avatar(c, aid)
                        else:
                            async with self.account_session(aid) as c:
                                ok = await self.fetch_account_avatar(c, aid)
                        if ok:
                            synced += 1
                        else:
                            no_photo += 1
                    except Exception as e:
                        log.warning("Avatar sync failed for account %s (%s): %s", aid, label, e)
                        errors_list.append({"id": aid, "name": label, "error": str(e)})

            await asyncio.gather(*(sync_one(a) for a in accs))

            return J({
                "ok": True,
                "synced": synced,
                "no_photo": no_photo,
                "total": len(accs),
                "errors": errors_list
            })

        @r.post("/personas/reconcile")
        async def personas_reconcile(_):
            res = self.store.reconcile_personas_and_accounts()
            try:
                acc_rows = self.store.rows("SELECT id FROM accounts WHERE active=1")
                for ar in acc_rows:
                    self.ensure_group_personas_assigned_for_account(ar["id"])
            except Exception as e:
                log.warning("error auto-assigning group personas during reconcile: %s", e)
            asyncio.create_task(self._do_sync_all_dialogs(force_all=True))
            return J(res or {"ok": True})

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
            try:
                await self.fetch_account_avatar(c, new_aid, me)
            except Exception:
                pass
            await c.disconnect()
            await self.ensure_clients()
            try:
                await self.sync_all_dialogs(account_ids=[new_aid])
            except Exception as e:
                log.warning("acc_verify initial dialog sync failed for account %s: %s", new_aid, e)
            # Instantly create/link persona and bind to every group this account is in (no click needed)
            try:
                self.store.reconcile_personas_and_accounts()
                self.ensure_group_personas_assigned_for_account(new_aid)
            except Exception as e:
                log.warning("acc_verify auto-reconcile failed for account %s: %s", new_aid, e)
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
                    async def _sync_and_link(a=aid):
                        try:
                            await self.sync_all_dialogs(account_ids=[a])
                            acc = self.store.rows("SELECT persona_id FROM accounts WHERE id=?", (a,))
                            if not (acc and acc[0].get("persona_id")):
                                self.store.reconcile_personas_and_accounts()
                            self.ensure_group_personas_assigned_for_account(a)
                        except Exception as e:
                            log.warning("auto-link on activate failed for %s: %s", a, e)
                    asyncio.create_task(_sync_and_link())
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
                # Sync avatar as well on individual account sync
                avatar_ok = False
                try:
                    if aid in self.clients and getattr(self.clients[aid], "is_connected", lambda: False)():
                        avatar_ok = await self.fetch_account_avatar(self.clients[aid], aid)
                    else:
                        async with self.account_session(aid) as c:
                            avatar_ok = await self.fetch_account_avatar(c, aid)
                except Exception as av_err:
                    log.warning("Individual avatar sync failed for %s: %s", aid, av_err)

                await self.sync_all_dialogs(account_ids=[aid])
                grp_count = self.store.q(
                    "SELECT COUNT(DISTINCT chat_id) FROM ("
                    "  SELECT chat_id FROM group_accounts WHERE account_id=? "
                    "  UNION "
                    "  SELECT chat_id FROM groups WHERE account_id=?"
                    ")",
                    (aid, aid)
                ).fetchone()[0]
                return J({"ok": True, "groups": grp_count, "avatar": avatar_ok})
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

            # 2b. Delete cached avatar files
            for av_ext in (".jpg", ".tmp.jpg"):
                av_p = os.path.join(self.avatars_dir, f"{aid}{av_ext}")
                try:
                    if os.path.exists(av_p):
                        os.remove(av_p)
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
            # Cancel any pending queue jobs belonging to this account
            self.store.q("DELETE FROM queue WHERE status='pending' AND (payload LIKE ? OR payload LIKE ?)",
                         (f'%"account_id": {aid}%', f'%"account_id":{aid}%'))

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
            rows = self.store.rows("SELECT g.*, a.name account_name FROM groups g LEFT JOIN accounts a ON a.id=g.account_id "
                                   "ORDER BY g.watched DESC, g.title")
            if not rows and self.store.q("SELECT COUNT(*) FROM accounts WHERE active=1").fetchone()[0] > 0:
                asyncio.create_task(self.sync_all_dialogs())
            return J(rows)

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
            grp_acc_ids = [r["account_id"] for r in self.store.rows("SELECT account_id FROM group_accounts WHERE chat_id=?", (chat_id,))]
            grp_accs = [a for a in all_accs if a["id"] in grp_acc_ids]
            eligible = grp_accs if len(grp_accs) >= 2 else all_accs
            if len(eligible) >= 2:
                # Rotate dynamically instead of hardcoding any persona
                shuffled = list(eligible)
                random.shuffle(shuffled)
                part_a, part_b = shuffled[0], shuffled[1]
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
                    {"sender": part_a.get("name"), "text": "hey, did you catch the latest updates?"},
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
                            sent_msg = await cl.send_message(chat_id, t["text"], reply_to=prev_id if not is_burst else None)
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
            about = "Official community and vendor group for xbiolabs. While it is a vendor group, the community actively discusses peptides, nootropics, general fitness, recovery, longevity, wellness, helping out beginners, and casual everyday banter."
            domain_knowledge = (
                "Comprehensive domain mastery across all topics discussed in xbiolabs:\n"
                "1. PEPTIDES & RESEARCH CHEMICALS: In-depth familiarity with BPC-157 (gut repair, tendon healing), TB-500/Thymosin Beta-4 (tissue regeneration), Semaglutide/Ozempic, Tirzepatide/Mounjaro, Retatrutide (triple GIP/GLP-1/glucagon agonist), GHK-Cu (copper peptide for tissue/skin/collagen), CJC-1295 no DAC & Ipamorelin (GH secretagogues), Sermorelin, MOTS-c (mitochondrial optimization), Epitalon (telomere elongation), NAD+ injections, Kisspeptin, MT-2.\n"
                "2. RECONSTITUTION & DOSING PROTOCOLS: Proper mixing techniques with bacteriostatic water (BAC), calculating mcg per tick on 100-unit/50-unit/30-unit insulin syringes, sterile needle hygiene, subQ vs IM administration sites, cold fridge storage, gentle swirling without vigorous shaking.\n"
                "3. NOOTROPICS & COGNITIVE OPTIMIZATION: In-depth understanding of Semax (NA-Semax-Amidate for BDNF, neurogenesis, and mental clarity), Selank (anxiolytic neuropeptide), Bromantane (dopamine synthesis upregulation and clean stamina), Modafinil/Armodafinil, L-Tyrosine, Alpha-GPC, Citicoline, caffeine + L-Theanine synergy, preventing brain fog, and managing stimulant tolerance for deep work.\n"
                "4. GENERAL FITNESS & TRAINING: Real lifter culture and training splits (Push/Pull/Legs, Upper/Lower, full body), hypertrophy, progressive overload, volume management, powerlifting lifts (squat, bench, deadlift form tweaks), deloads, recovery between sessions, body recomposition, cutting vs bulking protocols, protein and macronutrient balance.\n"
                "5. WELLNESS, LONGEVITY & RECOVERY: Deep sleep optimization, sleep architecture (REM vs deep), magnesium glycinate/threonate, sauna protocols, cold exposure, joint and tendon health, gut lining repair, hydration and electrolyte balance, metabolic health, interpreting bloodwork panels (CBC, CMP, lipids, ApoB, hs-CRP, hormones, liver/kidney markers).\n"
                "6. HELPING COMMUNITY MEMBERS: When peers or newcomers ask for help, dosing calculations, reconstitution ratios, syringe tick math, or gym routine guidance, jump in with clear, friendly, and practical assistance. Share direct tips as an experienced buddy rather than a formal medical lecturer.\n"
                "7. BANTER, HUMOR & UNSERIOUS FUN: Active community camaraderie, humorous gym observations, playful banter, memes, joking about tired mornings or skipping cardio, laughing off funny gym moments, and never taking everything too seriously.\n"
                "8. GEAR & ANABOLIC PROTOCOLS: TRT bases (test cypionate, test enanthate), Primobolan, Masteron, Anavar/Oxandrolone, Deca/NPP, estrogen management (Arimidex/anastrozole, Aromasin), prolactin control (Cabergoline), post cycle therapy (PCT - Enclomiphene, Nolvadex, HCG).\n"
                "9. VENDOR OPERATIONS, ADMIN & ORDER PROCESSING (ZERO @TAGS): The group owner/admin processes all orders. Sourcing, batch COA purity verification, lab test reports, ordering procedures, payment methods, tracking, stealth domestic shipping. When newcomers ask where or how to order, or when members ask about order status, tracking, payments, or processing, point them to admin (e.g. 'dm admin', 'ask admin', 'the owner handles processing'). NEVER type @tags or usernames in messages.\n"
                "10. TONE & PARTICIPATION: Speak as experienced, grounded community insiders who share real personal experiences, troubleshoot reconstitutions, exchange batch updates, help out beginners, joke around, and talk shop casually without generic corporate disclaimers."
            )
            tags = "peptides, vendor, dosing, buying, shipping, pricing, coa, bpc157, retatrutide, tirzepatide, gear, trt, nootropics, semax, selank, bromantane, wellness, sleep, recovery, fitness, gym, training, banter, humor, xbiolabs"
            rules = (
                "Be authentic, direct, and knowledgeable. Share real protocol and gym experiences, help out members with dosing math or questions, joke around casually, and discuss nootropics, fitness, and wellness. Strictly never criticize or complain about the vendor or products."
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
            if not self.store.q("SELECT COUNT(*) FROM groups").fetchone()[0] and self.clients:
                asyncio.create_task(self.sync_all_dialogs())
            accs = self.store.rows("SELECT a.id,a.phone,a.name,a.username,a.active,a.persona_id,a.proxy_id,p.label proxy_label "
                                   "FROM accounts a LEFT JOIN proxies p ON p.id=a.proxy_id ORDER BY a.id")
            for a in accs:
                a["connected"] = a["id"] in self.clients
                avatar_path = os.path.join(self.avatars_dir, f"{a['id']}.jpg")
                a["has_avatar"] = os.path.exists(avatar_path)
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
            st = self.daily_batch.get_status()
            can_b, b_spent, b_cap = self.store.check_batch_budget_available()
            can_r, r_spent, r_cap = self.store.check_reply_budget_available()
            st["budget"] = {
                "batch_budget_cap_usd": b_cap,
                "batch_spend_today_usd": b_spent,
                "batch_budget_remaining_usd": max(0.0, round(b_cap - b_spent, 6)),
                "batch_budget_capped": not can_b,
                "reply_budget_cap_usd": r_cap,
                "reply_spend_today_usd": r_spent,
                "reply_budget_remaining_usd": max(0.0, round(r_cap - r_spent, 6)),
                "reply_budget_capped": not can_r,
            }
            if bool(self.store.get("daily_batch_auto_pilot", True)) and bool(self.store.get("system_enabled", True)) and can_b:
                if not st.get("items"):
                    asyncio.create_task(self.auto_generate_daily_if_needed())
            return J(st)

        @r.post("/daily-batch/retry-failed")
        async def daily_batch_retry_failed(_):
            failed_items = self.store.rows("SELECT * FROM daily_batch_items WHERE status='failed'")
            now = int(time.time())
            count = 0
            for it in failed_items:
                self.store.q(
                    "UPDATE daily_batch_items SET status='pending', scheduled_ts=?, error=NULL WHERE id=?",
                    (now + count * 15, it["id"])
                )
                q_rows = self.store.rows(
                    "SELECT id FROM queue WHERE kind='batch_scheduled_send' AND payload LIKE ?",
                    (f'%"batch_item_id": {it["id"]}%',)
                )
                if q_rows:
                    self.store.q(
                        "UPDATE queue SET status='pending', attempts=0, last_error=NULL, not_before=?, updated=? WHERE id=?",
                        (now + count * 15, now, q_rows[0]["id"])
                    )
                else:
                    self.store.enqueue("batch_scheduled_send", it["chat_id"], {
                        "batch_item_id": it["id"],
                        "account_id": it["account_id"],
                        "persona_id": it["persona_id"],
                        "text": it["text"],
                        "reply_to_item_id": it.get("reply_to_item_id")
                    }, dedupe_key=f"batch_send:{it['id']}", delay=count * 15)
                count += 1
            self.store.q("UPDATE queue SET status='pending', attempts=0, last_error=NULL, not_before=? WHERE status='failed'", (now,))
            self.wake_event.set()
            return J({"ok": True, "retried": count})

        @r.post("/queue/clear-failed")
        async def queue_clear_failed(_):
            res = self.store.q("DELETE FROM queue WHERE status='failed'")
            self.store.q("DELETE FROM daily_batch_items WHERE status='failed'")
            return J({"ok": True, "cleared": res.rowcount})

        @r.post("/daily-batch/generate")
        async def daily_batch_generate(req):
            b = await req.json()
            chat_id = b.get("chat_id")
            if not chat_id:
                watched = self.store.rows("SELECT chat_id FROM groups WHERE watched=1 LIMIT 1")
                if not watched:
                    return J({"error": "No watched group available to generate batch for"}, status=400)
                chat_id = watched[0]["chat_id"]
            count = int(b.get("count", self.store.get("batch_messages_count", 24)))
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

        @r.post("/daily-batch/clear-failed")
        async def daily_batch_clear_failed(req):
            b = {}
            try:
                b = await req.json()
            except Exception:
                pass
            cleared = self.daily_batch.clear_failed(b.get("chat_id"))
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

        @r.post("/daily-batch/generate-all")
        async def daily_batch_generate_all(req):
            b = {}
            try:
                b = await req.json()
            except Exception:
                pass
            count = b.get("count")
            if count is not None:
                count = int(count)
            res = await self.daily_batch.generate_all_groups(count=count)
            self.wake_event.set()
            return J({"ok": True, "generated": res})

        @r.post("/budget/reset")
        async def budget_reset(_):
            self.store.reset_daily_spend()
            self.wake_event.set()
            return J({"ok": True})

        @r.post("/daily-batch/settings")
        async def daily_batch_save_settings(req):
            b = await req.json()
            if "auto_pilot" in b:
                self.store.set("daily_batch_auto_pilot", bool(b["auto_pilot"]))
            if "human_pause_minutes" in b:
                self.store.set("batch_human_pause_seconds", int(float(b["human_pause_minutes"]) * 60))
            if "batch_messages_count" in b:
                self.store.set("batch_messages_count", int(b["batch_messages_count"]))
            if "session_interval_seconds" in b:
                self.store.set("batch_session_interval_seconds", int(b["session_interval_seconds"]))
            if "batch_budget_cap_usd" in b:
                self.store.set("daily_batch_budget_cap_usd", float(b["batch_budget_cap_usd"]))
            if "reply_budget_cap_usd" in b:
                self.store.set("daily_reply_budget_cap_usd", float(b["reply_budget_cap_usd"]))
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
                retention_tier=b.get("retention_tier", "long"),
                salience=float(b.get("salience", 0.9)),
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
    # Camoufox auto-download disabled on startup for lean, low-overhead system operation
    asyncio.create_task(d.auto_sync_loop())
    asyncio.create_task(d.human_reply_sweeper_loop())
    asyncio.create_task(d.daily_batch_loop())
    asyncio.create_task(d.memory_pruner_loop())
    asyncio.create_task(d.casual_presence_loop())
    asyncio.create_task(d.auto_generate_daily_if_needed())
    try:
        await d.ensure_clients()
    except Exception as e:  # noqa
        log.warning("telegram connect deferred: %s", e)
    await d.worker()


if __name__ == "__main__":
    asyncio.run(main())
