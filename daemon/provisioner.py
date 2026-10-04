"""Account manager: orchestrates the workers that build a new Telegram account.

  TextVerified  -> rents the number and receives the SMS
  VMOS worker   -> owns the cloud phone and the Telegram account on it (vmos_account.py)
  Camoufox      -> creates this account's own API ID/hash on my.telegram.org (cfx_browser.py)
  Telethon      -> signs in with those keys and saves the account (whisperd.adopt_number)

The manager only routes data between them (number, codes, proxy) and cleans up on failure.
"""
import asyncio
import random
import time

import cfx_browser as cfx
import debuglog
from debuglog import dbg, exc
from textverified import TextVerified, TvError
from vmos_account import VmosAccountWorker, NumberBanned

MAX_NUMBERS = 3  # how many numbers to try when Telegram bans one

PHASES = [
    ("phone", "Cloud phone"),
    ("number", "Number & SMS"),
    ("signup", "Telegram sign-up"),
    ("api", "Camoufox API keys"),
    ("link", "Connect account"),
    ("persona", "Profile & photo"),
    ("clearapp", "Clear Telegram app"),
]


class Provisioner:
    """One run at a time. Progress is readable from the UI."""

    def __init__(self, daemon):
        self.d = daemon
        self.task = None
        self.state = self._blank(False)

    @staticmethod
    def _blank(running):
        return {"running": running, "step": "", "steps": [], "error": None, "done": False,
                "account_id": None, "phase": None, "phases": [{"id": p, "label": l, "status": "todo"} for p, l in PHASES],
                "phone": None, "proxy": None}

    def log(self, step):
        self.state["step"] = step
        self.state["steps"].append({"t": int(time.time()), "text": step})
        dbg("step", step)

    def _phase(self, pid, status="active"):
        for p in self.state["phases"]:
            if p["id"] == pid:
                p["status"] = status
            elif status == "active" and p["status"] == "active":
                p["status"] = "done"
        if status == "active":
            self.state["phase"] = pid
        dbg("manager", f"Phase '{pid}' -> {status}")

    def start(self, opts):
        if self.task and not self.task.done():
            raise RuntimeError("An account is already being created.")
        self.state = self._blank(True)
        debuglog.mark_run()
        dbg("manager", f"Start requested with options: {opts}")
        self.task = asyncio.create_task(self._run(opts or {}))

    def cancel(self):
        dbg("manager", "Stop requested by user", "warn")
        if self.task and not self.task.done():
            self.task.cancel()

    def _pick_proxy(self, opts):
        st = self.d.store
        if opts.get("proxy_id"):
            r = st.rows("SELECT * FROM proxies WHERE id=?", (opts["proxy_id"],))
            return r[0] if r else None
        ok = st.rows("SELECT * FROM proxies WHERE ok=1")
        return random.choice(ok) if ok else None

    async def _run(self, opts):
        st = self.d.store
        vid = None
        tv = None
        country = (opts.get("country") or "US").upper()
        proxy = self._pick_proxy(opts)
        self.state["proxy"] = proxy["url"].split("@")[-1] if proxy else None
        dbg("manager", f"Country={country} proxy_id={proxy['id'] if proxy else None} proxy={self.state['proxy']}")
        dbg("manager", "Settings present: " + ", ".join(f"{k}={'yes' if st.get(k) else 'NO'}" for k in
            ("vmos_ak", "vmos_sk", "vmos_pad", "tv_key", "tv_user", "fal_key")) + f", tv_max_price={st.get('tv_max_price')}, cfx_headless={st.get('cfx_headless', True)}")
        try:
            tv = TextVerified(st.get("tv_key"), st.get("tv_user"))
            phone_worker = VmosAccountWorker(st.get("vmos_ak"), st.get("vmos_sk"), st.get("vmos_pad"), self.log)
            self.log(f"Proxy: {self.state['proxy'] or 'none'}")

            self._phase("phone")
            await phone_worker.prepare(country)

            # Telegram bans some rented numbers. When that happens, cancel the
            # TextVerified rental (credit comes back) and try a fresh number.
            phone = None
            for attempt in range(1, MAX_NUMBERS + 1):
                self._phase("number")
                self.log("Renting a Telegram number" if attempt == 1
                         else f"Renting another Telegram number (try {attempt} of {MAX_NUMBERS})")
                vid, phone = await tv.rent_telegram(st.get("tv_max_price"))
                self.state["phone"] = phone
                self.log(f"Number rented: {phone}")

                self._phase("signup")
                try:
                    await phone_worker.register(phone, lambda: tv.wait_code(vid, timeout=300))
                    vid = None  # SMS used: the rental is consumed, don't cancel it anymore
                    break
                except NumberBanned as e:
                    self.log(f"{e} Cancelling it on TextVerified and getting a new one.")
                    try:
                        await tv.cancel(vid)
                    except Exception as ce:  # noqa
                        dbg("manager", f"Cancel of banned number failed: {ce}", "warn")
                    vid = None
            else:
                raise RuntimeError(f"Telegram banned {MAX_NUMBERS} numbers in a row; giving up.")

            self._phase("api")
            self.log("Camoufox is opening my.telegram.org through the proxy")

            async def web_code():
                self.log("Waiting for Telegram to send the web login code to the phone")
                c = await phone_worker.read_code()
                self.log("Code found on the phone, handing it to Camoufox" if c else "No code arrived on the phone")
                return c

            api_id, api_hash = await cfx.get_api_credentials(
                phone, proxy["url"] if proxy else None, web_code,
                app_title=st.get("cfx_app_title") or "Whisper",
                headless=bool(st.get("cfx_headless", True)))
            self.log(f"API keys created (API ID {api_id})")

            self._phase("link")
            self.log("Signing in with Telethon using this account's own keys")

            async def tg_code():
                self.log("Waiting for the Telethon login code on the phone")
                return await phone_worker.read_code()

            dbg("telethon", f"adopt_number phone={phone} api_id={api_id} proxy_id={proxy['id'] if proxy else None}")
            aid = await self.d.adopt_number(phone, api_id, api_hash, tg_code,
                                            proxy_id=proxy["id"] if proxy else None)
            self.state["account_id"] = aid
            dbg("telethon", f"Account saved with id {aid}")

            self._phase("persona")
            try:
                await self.d.apply_persona(aid, opts.get("persona_style") or st.get("persona_style"),
                                           opts.get("photo", True), self.log)
            except Exception as e:  # noqa
                exc("telethon", e)
                self.log(f"Profile step skipped ({e})")

            self._phase("clearapp")
            if opts.get("clearapp", True):
                try:
                    await phone_worker.clear_telegram_app()
                    self.log("Telegram app cleared, ready for the next account")
                except Exception as e:  # noqa
                    exc("vmos", e)
                    self.log(f"Clearing skipped ({e})")
            else:
                self.log("Clearing turned off: Telegram left signed in on the cloud phone")
            self._phase("clearapp", "done")
            self.log("Done. The account is connected.")
            self.state["done"] = True
        except asyncio.CancelledError:
            self.state["error"] = "Cancelled."
            self._fail_phase()
            await self._release(tv, vid)
            raise
        except Exception as e:  # noqa
            self.state["error"] = str(e)
            exc("manager", e)
            self._fail_phase()
            self.log("Stopped: " + str(e))
            await self._release(tv, vid)
        finally:
            self.state["running"] = False
            dbg("manager", f"Run finished: done={self.state['done']} error={self.state['error']}")

    def _fail_phase(self):
        if self.state["phase"]:
            self._phase(self.state["phase"], "error")

    async def _release(self, tv, vid):
        if tv and vid:
            try:
                await tv.cancel(vid)
                self.log("Number rental cancelled (credit protected)")
            except (TvError, Exception):  # noqa
                pass
