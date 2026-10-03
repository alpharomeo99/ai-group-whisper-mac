"""Autonomous Telegram account creation: TextVerified number + VMOS cloud phone + Camoufox API keys."""
import asyncio
import random
import re
import time

import camoufox as cfx
import vmos as vmos_mod
from textverified import TextVerified, TvError

TG_PKG = "org.telegram.messenger"
FIRST = ["Alex", "Sam", "Jordan", "Riley", "Casey", "Taylor", "Jamie", "Morgan"]
LAST = ["Reed", "Hayes", "Brooks", "Shaw", "Ellis", "Quinn", "Barnes", "Flynn"]


class Provisioner:
    """One run at a time. Progress is readable from the UI."""

    def __init__(self, daemon):
        self.d = daemon
        self.task = None
        self.state = {"running": False, "step": "", "steps": [], "error": None, "done": False, "account_id": None}

    def log(self, step):
        self.state["step"] = step
        self.state["steps"].append({"t": int(time.time()), "text": step})

    def start(self, opts):
        if self.task and not self.task.done():
            raise RuntimeError("An account is already being created.")
        self.state = {"running": True, "step": "Starting", "steps": [], "error": None, "done": False, "account_id": None}
        self.task = asyncio.create_task(self._run(opts))

    def cancel(self):
        if self.task and not self.task.done():
            self.task.cancel()

    # ---------- helpers ----------
    def _vmos(self):
        s = self.d.store
        return vmos_mod.Vmos(s.get("vmos_ak"), s.get("vmos_sk"), s.get("vmos_pad"))

    def _tv(self):
        s = self.d.store
        return TextVerified(s.get("tv_key"), s.get("tv_user"))

    async def _phone_code(self, vm, since, patterns=("Telegram code", "login code", "code:")):
        """Read the newest Telegram login code shown on the cloud phone."""
        end = time.time() + 300
        while time.time() < end:
            try:
                txt = await vm.sh("dumpsys notification --noredact | grep -i -o 'code[^\"]\\{0,40\\}' | head -20", timeout=40)
                txt += " " + await vm.screen_text()
            except Exception:  # noqa
                txt = ""
            m = re.search(r"\b(\d{5,6})\b", txt)
            if m:
                return m.group(1)
            await asyncio.sleep(6)
        return None

    # ---------- main flow ----------
    async def _run(self, opts):
        st = self.d.store
        vid = None
        country = (opts.get("country") or "US").upper()
        proxy_row = None
        if opts.get("proxy_id"):
            r = st.rows("SELECT * FROM proxies WHERE id=?", (opts["proxy_id"],))
            proxy_row = r[0] if r else None
        elif st.rows("SELECT id FROM proxies WHERE ok=1"):
            proxy_row = random.choice(st.rows("SELECT * FROM proxies WHERE ok=1"))
        try:
            tv = self._tv()
            vm = self._vmos()

            self.log("Giving the cloud phone a fresh device identity")
            await vm.new_device(country)
            await vm.wait_online()

            self.log("Renting a phone number")
            vid, phone = await tv.rent_telegram(st.get("tv_max_price"))
            self.log(f"Number rented: {phone}")

            self.log("Writing the number onto the cloud phone SIM")
            try:
                await vm.set_sim(country, phone)
            except Exception as e:  # noqa
                self.log(f"SIM step skipped ({e})")

            self.log("Opening Telegram on the cloud phone")
            await vm.sh(f"am force-stop {TG_PKG}; monkey -p {TG_PKG} -c android.intent.category.LAUNCHER 1")
            await asyncio.sleep(8)
            await vm.tap("start messaging", "continue", "start")
            await asyncio.sleep(3)

            self.log("Entering the phone number")
            n = await vm.tap(cls="EditText")
            if n:
                await vm.sh("input keyevent KEYCODE_MOVE_END")
            await vm.type(phone)
            await asyncio.sleep(1)
            await vm.tap("next", "continue", "start messaging")

            self.log("Waiting for the SMS code")
            code = await tv.wait_code(vid, timeout=300)
            self.log("Code received, signing in")
            await vm.type(code)
            await asyncio.sleep(8)

            screen = (await vm.screen_text()).lower()
            if "your name" in screen or "first name" in screen:
                self.log("Setting a profile name")
                name = f"{random.choice(FIRST)} {random.choice(LAST)}"
                await vm.tap(cls="EditText")
                await vm.type(name.split()[0])
                await vm.tap("next", "done", "continue")
                await asyncio.sleep(5)
            elif "password" in screen:
                raise RuntimeError("That number already has a Telegram account with a password.")

            self.log("Creating this account's own Telegram API keys")
            proxy_line = proxy_row["url"] if proxy_row else None

            async def code_getter():
                return await self._phone_code(vm, time.time())

            api_id, api_hash = await cfx.get_api_credentials(
                phone, proxy_line, code_getter,
                app_title=st.get("cfx_app_title") or "Whisper",
                headless=bool(st.get("cfx_headless", True)))
            self.log("API keys created")

            self.log("Linking the account to this app")
            aid = await self.d.adopt_number(phone, api_id, api_hash,
                                            lambda: self._phone_code(vm, time.time()),
                                            proxy_id=proxy_row["id"] if proxy_row else None)
            self.state["account_id"] = aid
            self.log("Done. The account is connected.")
            self.state["done"] = True
        except asyncio.CancelledError:
            self.state["error"] = "Cancelled."
            if vid:
                await self._tv().cancel(vid)
            raise
        except Exception as e:  # noqa
            self.state["error"] = str(e)
            self.log("Stopped: " + str(e))
            if vid:
                try:
                    await self._tv().cancel(vid)
                except TvError:
                    pass
        finally:
            self.state["running"] = False
