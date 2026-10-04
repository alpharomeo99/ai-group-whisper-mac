"""VMOS worker: owns the cloud phone and the Telegram account that lives on it.

The manager (provisioner.py) asks it to: connect to the cloud phone, register a number,
and read any login code Telegram sends to the app (used by Camoufox and Telethon).
"""
import asyncio
import random
import re
import time

import vmos as vmos_mod
from debuglog import dbg, short

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
        dbg("vmos", f"Using cloud phone {self.vm.pad} (device identity is never changed)")
        await self.vm.wait_online()


    async def clear_telegram_app(self):
        """Clear the Telegram app's data on the cloud phone. The Mac's Telethon session stays signed in."""
        self.log("Clearing the Telegram app on the cloud phone")
        await self.vm.sh(f"am force-stop {TG_PKG}; pm clear {TG_PKG}")

    async def _retry_tap(self, *labels, cls=None, tries=3):
        for i in range(tries):
            n = await self.vm.tap(*labels, cls=cls, wait=20)
            if n:
                return n
            dbg("vmos", f"Tap attempt {i + 1}/{tries} for {list(labels)} found nothing", "warn")
            await asyncio.sleep(3)
        dbg("vmos", f"Gave up tapping {list(labels)}{' class=' + cls if cls else ''}", "error")
        return None

    async def _clear_field(self, n):
        # Delete exactly what's in the box. Extra deletes in an empty number box make Telegram
        # jump back to the country code box and erase it, so never press more than needed.
        cnt = len(n["text"] or "")
        cmd = f"input tap {n['x']} {n['y']}; sleep 0.5; input keyevent KEYCODE_MOVE_END"
        if cnt:
            cmd += f"; for i in $(seq 1 {cnt}); do input keyevent KEYCODE_DEL; done"
        await self.vm.sh(cmd)

    async def _enter_phone(self, phone):
        """Telegram has two boxes: country code (+1) and the number. Fill each one, cleared first."""
        vm = self.vm
        digits = re.sub(r"\D", "", phone)
        fields = []
        for _ in range(5):
            fields = [n for n in await vm.nodes() if "EditText" in n["cls"]]
            if fields:
                break
            await asyncio.sleep(3)
        fields.sort(key=lambda n: n["x"])
        dbg("vmos", f"Phone screen boxes: {[(f['text'], f['x'], f['y']) for f in fields]}")
        if len(fields) >= 2:
            cc_box, num_box = fields[0], fields[-1]
            cc = re.sub(r"\D", "", cc_box["text"]) or ("1" if len(digits) == 11 and digits.startswith("1") else "")
            if not digits.startswith(cc) or not cc:
                cc = "1" if digits.startswith("1") else digits[:2]
            national = digits[len(cc):]
            if cc_box["text"].strip() != cc:
                await self._clear_field(cc_box)
                await vm.type(cc)
                await asyncio.sleep(1)
            await self._clear_field(num_box)
            await asyncio.sleep(0.5)
            await vm.sh(f"input tap {num_box['x']} {num_box['y']}")
            await vm.type(national)
            dbg("vmos", f"Entered country code {cc} and number {national}")
        elif fields:
            await self._clear_field(fields[0])
            await vm.type(digits[1:] if len(digits) == 11 and digits.startswith("1") else digits)
        else:
            dbg("vmos", "No number box found on screen", "error")
            await vm.type(digits)
        await asyncio.sleep(1)
        shown = [n["text"] for n in await vm.nodes() if "EditText" in n["cls"]]
        dbg("vmos", f"Number boxes now show: {shown}")
        if len(shown) >= 2 and re.sub(r"\D", "", "".join(shown)) != digits and not getattr(self, "_phone_retry", False):
            dbg("vmos", "Number boxes don't match the rented number, entering it again", "warn")
            self._phone_retry = True
            try:
                await self._enter_phone(phone)
            finally:
                self._phone_retry = False

    async def register(self, phone, sms_code):
        """Sign the number up inside Telegram on the phone. sms_code() -> awaitable code."""
        vm = self.vm
        self.log("Opening Telegram on the cloud phone")
        perms = " ".join(f"pm grant {TG_PKG} android.permission.{p} 2>/dev/null;" for p in (
            "POST_NOTIFICATIONS", "READ_PHONE_STATE", "CALL_PHONE", "READ_CALL_LOG", "READ_CONTACTS",
            "WRITE_CONTACTS", "RECEIVE_SMS", "READ_SMS", "READ_PHONE_NUMBERS", "ANSWER_PHONE_CALLS",
            "CAMERA", "RECORD_AUDIO", "ACCESS_FINE_LOCATION", "READ_MEDIA_IMAGES"))
        await vm.sh(f"am force-stop {TG_PKG}; pm clear {TG_PKG}; {perms} monkey -p {TG_PKG} -c android.intent.category.LAUNCHER 1")
        dbg("vmos", "Granted Telegram's Android permissions up front so no permission popups appear")
        await asyncio.sleep(10)
        await vm.current_app()
        await vm.dismiss_popups()
        # Telegram's welcome screen is animated, so the screen reader often can't see it.
        # Try by text once, then tap the "Start Messaging" button by its position.
        if not await vm.tap("start messaging", "continue", "start", wait=15):
            w, h = await vm.screen_size()
            for frac in (0.88, 0.84, 0.92):
                await vm.tap_xy(w // 2, int(h * frac), "(Start Messaging button)")
                await asyncio.sleep(4)
                if await vm.find(cls="EditText") or "phone" in (await vm.screen_text()).lower():
                    dbg("vmos", "Phone number screen is open")
                    break
            else:
                dbg("vmos", "Still on the welcome screen after position taps", "error")
        await asyncio.sleep(3)
        await vm.dismiss_popups()

        self.log("Entering the phone number")
        await self._enter_phone(phone)
        await asyncio.sleep(1)
        await self._retry_tap("next", "continue", "done")
        await asyncio.sleep(2)
        await vm.tap("yes", "ok", "continue", wait=6)  # "Is this number correct?" dialog
        await vm.dismiss_popups()

        self.log("Waiting for the SMS code")
        code = await sms_code()
        self.seen_codes.add(code)
        self.log("SMS code received, signing in on the phone")
        await vm.type(code)
        await asyncio.sleep(8)

        screen = (await vm.screen_text()).lower()
        dbg("vmos", f"Screen after entering the code: {short(screen, 500)}")
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
        n = 0
        dbg("vmos", f"Reading Telegram login code from the phone (up to {timeout}s, ignoring {sorted(self.seen_codes)})")
        while time.time() < end:
            n += 1
            try:
                txt = await self.vm.sh("dumpsys notification --noredact | grep -i -o 'code[^\"]\\{0,40\\}' | head -20", timeout=40)
                txt += " " + await self.vm.screen_text()
            except Exception as e:  # noqa
                dbg("vmos", f"Code check #{n} failed: {e}", "warn")
                txt = ""
            found = re.findall(r"\b(\d{5,6})\b", txt)
            dbg("vmos", f"Code check #{n}: numbers seen {found or 'none'}, {int(end - time.time())}s left", "debug")
            for c in found:
                if c not in self.seen_codes:
                    self.seen_codes.add(c)
                    dbg("vmos", f"New login code found: {c}")
                    return c
            await asyncio.sleep(5)
        dbg("vmos", f"No new code after {n} checks", "error")
        return None
