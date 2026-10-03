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
from textverified import TextVerified, TvError
from vmos_account import VmosAccountWorker

PHASES = [
    ("phone", "Cloud phone"),
    ("number", "Number & SMS"),
    ("signup", "Telegram sign-up"),
    ("api", "Camoufox API keys"),
    ("link", "Connect account"),
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

    def _phase(self, pid, status="active"):
        for p in self.state["phases"]:
            if p["id"] == pid:
                p["status"] = status
            elif status == "active" and p["status"] == "active":
                p["status"] = "done"
        if status == "active":
            self.state["phase"] = pid

    def start(self, opts):
        if self.task and not self.task.done():
            raise RuntimeError("An account is already being created.")
        self.state = self._blank(True)
        self.task = asyncio.create_task(self._run(opts or {}))

    def cancel(self):
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
        try:
            tv = TextVerified(st.get("tv_key"), st.get("tv_user"))
            phone_worker = VmosAccountWorker(st.get("vmos_ak"), st.get("vmos_sk"), st.get("vmos_pad"), self.log)
            self.log(f"Proxy: {self.state['proxy'] or 'none'}")

            self._phase("phone")
            await phone_worker.prepare(country)

            self._phase("number")
            self.log("Renting a Telegram number")
            vid, phone = await tv.rent_telegram(st.get("tv_max_price"))
            self.state["phone"] = phone
            self.log(f"Number rented: {phone}")
            await phone_worker.set_sim(country, phone)

            self._phase("signup")
            await phone_worker.register(phone, lambda: tv.wait_code(vid, timeout=300))
            vid = None  # SMS used: the rental is consumed, don't cancel it anymore

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

            aid = await self.d.adopt_number(phone, api_id, api_hash, tg_code,
                                            proxy_id=proxy["id"] if proxy else None)
            self._phase("link", "done")
            self.state["account_id"] = aid
            self.log("Done. The account is connected.")
            self.state["done"] = True
        except asyncio.CancelledError:
            self.state["error"] = "Cancelled."
            self._fail_phase()
            await self._release(tv, vid)
            raise
        except Exception as e:  # noqa
            self.state["error"] = str(e)
            self._fail_phase()
            self.log("Stopped: " + str(e))
            await self._release(tv, vid)
        finally:
            self.state["running"] = False

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
