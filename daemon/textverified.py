"""TextVerified API v2 client: rent a Telegram SMS number and read the code."""
import asyncio
import re
import time

import aiohttp

from debuglog import dbg, short

BASE = "https://www.textverified.com"


class TvError(Exception):
    pass


class TextVerified:
    def __init__(self, api_key, username):
        if not (api_key and username):
            raise TvError("Add your TextVerified API key and username (email) in Settings.")
        self.key, self.user = api_key.strip(), username.strip()
        self.token, self.exp = None, 0

    async def _auth(self, s):
        if self.token and time.time() < self.exp - 60:
            return
        dbg("textverified", f"Signing in as {self.user}")
        async with s.post(BASE + "/api/pub/v2/auth", headers={"X-API-KEY": self.key, "X-API-USERNAME": self.user}) as r:
            dbg("textverified", f"<- auth HTTP {r.status}", "debug" if r.status == 200 else "error")
            if r.status != 200:
                dbg("textverified", f"auth response: {short(await r.text(), 300)}", "error")
                raise TvError("TextVerified rejected the API key / username.")
            j = await r.json(content_type=None)
        self.token = j.get("token")
        self.exp = time.time() + float(j.get("expiresIn") or 1800)

    async def req(self, method, path, **kw):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            await self._auth(s)
            url = path if path.startswith("http") else BASE + path
            dbg("textverified", f"{method} {path} {short(kw.get('json') or kw.get('params') or '', 300)}", "debug")
            t0 = time.time()
            async with s.request(method, url, headers={"Authorization": "Bearer " + self.token}, **kw) as r:
                txt = await r.text()
                dbg("textverified", f"<- {method} {path} HTTP {r.status} in {time.time() - t0:.1f}s: {short(txt, 500)}",
                    "error" if r.status >= 400 else "debug")
                if r.status >= 400:
                    raise TvError(f"TextVerified error {r.status}: {txt[:200]}")
                if r.status == 204 or not txt.strip():
                    return {}
                try:
                    import json as _j
                    return _j.loads(txt)
                except Exception:  # noqa
                    return {}

    async def balance(self):
        me = await self.req("GET", "/api/pub/v2/account/me")
        return me.get("currentBalance")

    async def rent_telegram(self, max_price=None):
        body = {"serviceName": "telegram", "capability": "sms"}
        dbg("textverified", f"Renting a Telegram SMS number (max price: {max_price or 'none'})")
        if max_price:
            body["maxPrice"] = float(max_price)
        res = await self.req("POST", "/api/pub/v2/verifications", json=body)
        href = res.get("href")
        v = await self.req("GET", href) if href else res
        vid, num = v.get("id"), v.get("number")
        dbg("textverified", f"Verification id={vid} number={num} state={v.get('state')} cost={v.get('totalCost')}")
        if not (vid and num):
            raise TvError("TextVerified did not return a number.")
        return vid, "+1" + re.sub(r"\D", "", num)[-10:]

    async def wait_code(self, vid, timeout=300):
        end = time.time() + timeout
        n = 0
        dbg("textverified", f"Waiting for the SMS on {vid} (up to {timeout}s)")
        while time.time() < end:
            n += 1
            res = await self.req("GET", "/api/pub/v2/sms", params={"reservationId": vid})
            msgs = res.get("data") or []
            dbg("textverified", f"SMS check #{n}: {len(msgs)} message(s), {int(end - time.time())}s left", "debug")
            for m in msgs:
                dbg("textverified", f"SMS from {m.get('from')}: {short(m.get('smsContent') or '', 200)} parsedCode={m.get('parsedCode')}")
                code = m.get("parsedCode") or (re.search(r"\b(\d{5,6})\b", m.get("smsContent") or "") or [None, None])[1]
                if code:
                    dbg("textverified", f"SMS code found: {code}")
                    return code
            await asyncio.sleep(5)
        dbg("textverified", f"No SMS after {n} checks", "error")
        raise TvError("No SMS code arrived in time.")

    async def cancel(self, vid):
        dbg("textverified", f"Cancelling verification {vid}")
        try:
            await self.req("POST", f"/api/pub/v2/verifications/{vid}/cancel")
            dbg("textverified", f"Verification {vid} cancelled")
        except Exception as e:  # noqa
            dbg("textverified", f"Cancel failed: {e}", "error")
