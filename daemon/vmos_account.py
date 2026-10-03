"""VMOS worker: owns the cloud phone and the Telegram account that lives on it.

The manager (provisioner.py) asks it to: connect to the cloud phone, register a number,
and read any login code Telegram sends to the app (used by Camoufox and Telethon).
"""
import asyncio
import random
import re
import time

import vmos as vmos_mod

TG_PKG = "org.telegram.messenger"
FIRST = ["Alex", "Sam", "Jordan", "Riley", "Casey", "Taylor", "Jamie", "Morgan"]
LAST = ["Reed", "Hayes", "Brooks", "Shaw", "Ellis", "Quinn", "Barnes", "Flynn"]


class AccountExists(Exception):
    pass


class VmosAccountWorker:
    def __init__(self, ak, sk, pad, log):
        self.vm = vmos_mod.Vmos(ak, sk, pad)
        self.log = log
        self.seen_codes = set()

    async def prepare(self, country):
        self.log("Connecting to the cloud phone")
        await self.vm.wait_online()

    async def set_sim(self, country, phone):
        try:
            await self.vm.set_sim(country, phone)
        except Exception as e:  # noqa
            self.log(f"SIM step skipped ({e})")

    async def wipe(self):
        """Clear the Telegram app's data on the cloud phone. The Mac's Telethon session stays signed in."""
        self.log("Clearing the Telegram app on the cloud phone")
        await self.vm.sh(f"am force-stop {TG_PKG}; pm clear {TG_PKG}")

    async def _retry_tap(self, *labels, cls=None, tries=3):
        for _ in range(tries):
            n = await self.vm.tap(*labels, cls=cls, wait=20)
            if n:
                return n
            await asyncio.sleep(3)
        return None

    async def register(self, phone, sms_code):
        """Sign the number up inside Telegram on the phone. sms_code() -> awaitable code."""
        vm = self.vm
        self.log("Opening Telegram on the cloud phone")
        await vm.sh(f"am force-stop {TG_PKG}; pm clear {TG_PKG}; monkey -p {TG_PKG} -c android.intent.category.LAUNCHER 1")
        await asyncio.sleep(10)
        await self._retry_tap("start messaging", "continue", "start")
        await asyncio.sleep(3)

        self.log("Entering the phone number")
        if await self._retry_tap(cls="EditText"):
            await vm.sh("input keyevent KEYCODE_MOVE_END; for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do input keyevent KEYCODE_DEL; done")
        await vm.type(phone)
        await asyncio.sleep(1)
        await self._retry_tap("next", "continue", "done")
        await asyncio.sleep(2)
        await vm.tap("yes", "ok", "continue", wait=6)  # "Is this number correct?" dialog

        self.log("Waiting for the SMS code")
        code = await sms_code()
        self.seen_codes.add(code)
        self.log("SMS code received, signing in on the phone")
        await vm.type(code)
        await asyncio.sleep(8)

        screen = (await vm.screen_text()).lower()
        if "password" in screen and "your name" not in screen:
            raise AccountExists("That number already has a Telegram account with a password.")
        if "your name" in screen or "first name" in screen or "profile info" in screen:
            self.log("Setting a profile name")
            await vm.tap(cls="EditText")
            await vm.type(random.choice(FIRST))
            await self._retry_tap("next", "done", "continue")
            await asyncio.sleep(3)
            await vm.tap("accept", "agree", "ok", wait=6)  # terms of service
            await asyncio.sleep(5)
        self.log("Telegram account is live on the cloud phone")

    async def read_code(self, timeout=300):
        """Newest Telegram login code shown on the phone that we haven't used yet."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                txt = await self.vm.sh("dumpsys notification --noredact | grep -i -o 'code[^\"]\\{0,40\\}' | head -20", timeout=40)
                txt += " " + await self.vm.screen_text()
            except Exception:  # noqa
                txt = ""
            for c in re.findall(r"\b(\d{5,6})\b", txt):
                if c not in self.seen_codes:
                    self.seen_codes.add(c)
                    return c
            await asyncio.sleep(5)
        return None
