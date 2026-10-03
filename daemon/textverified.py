"""TextVerified API v2 client: rent a Telegram SMS number and read the code."""
import asyncio
import re
import time

import aiohttp

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
        async with s.post(BASE + "/api/pub/v2/auth", headers={"X-API-KEY": self.key, "X-API-USERNAME": self.user}) as r:
            if r.status != 200:
                raise TvError("TextVerified rejected the API key / username.")
            j = await r.json(content_type=None)
        self.token = j.get("token")
        self.exp = time.time() + float(j.get("expiresIn") or 1800)

    async def req(self, method, path, **kw):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            await self._auth(s)
            url = path if path.startswith("http") else BASE + path
            async with s.request(method, url, headers={"Authorization": "Bearer " + self.token}, **kw) as r:
                if r.status >= 400:
                    raise TvError(f"TextVerified error {r.status}: {(await r.text())[:200]}")
                if r.status == 204:
                    return {}
                try:
                    return await r.json(content_type=None)
                except Exception:  # noqa
                    return {}

    async def balance(self):
        me = await self.req("GET", "/api/pub/v2/account/me")
        return me.get("currentBalance")

    async def rent_telegram(self, max_price=None):
        body = {"serviceName": "telegram", "capability": "sms"}
        if max_price:
            body["maxPrice"] = float(max_price)
        res = await self.req("POST", "/api/pub/v2/verifications", json=body)
        href = res.get("href")
        v = await self.req("GET", href) if href else res
        vid, num = v.get("id"), v.get("number")
        if not (vid and num):
            raise TvError("TextVerified did not return a number.")
        return vid, "+1" + re.sub(r"\D", "", num)[-10:]

    async def wait_code(self, vid, timeout=300):
        end = time.time() + timeout
        while time.time() < end:
            res = await self.req("GET", "/api/pub/v2/sms", params={"reservationId": vid})
            for m in res.get("data") or []:
                code = m.get("parsedCode") or (re.search(r"\b(\d{5,6})\b", m.get("smsContent") or "") or [None, None])[1]
                if code:
                    return code
            await asyncio.sleep(5)
        raise TvError("No SMS code arrived in time.")

    async def cancel(self, vid):
        try:
            await self.req("POST", f"/api/pub/v2/verifications/{vid}/cancel")
        except Exception:  # noqa
            pass
