"""VMOS Cloud OpenAPI client (V2 signature: SHA-256(SK + ts + path + body))."""
import asyncio
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET

import aiohttp

from debuglog import dbg, short

BASE = "https://api.vmoscloud.com"
UNSIGNED_BODY = ("/uploadFile", "/uploadFileV3", "/asyncCmd", "/syncCmd")


class VmosError(Exception):
    pass


class Vmos:
    def __init__(self, ak, sk, pad):
        if not (ak and sk):
            raise VmosError("Add your VMOS Access Key and Secret Key in Settings.")
        self.ak, self.sk, self.pad = ak.strip(), sk.strip(), (pad or "").strip()

    async def call(self, path, body):
        raw = json.dumps(body, separators=(",", ":"))
        ts = str(int(time.time()))
        signed = "" if path.endswith(UNSIGNED_BODY) else raw
        sign = hashlib.sha256((self.sk + ts + path + signed).encode()).hexdigest()
        headers = {"X-Access-Key": self.ak, "X-Timestamp": ts, "X-Sign": sign, "Content-Type": "application/json"}
        t0 = time.time()
        dbg("vmos", f"POST {path} body={short(raw, 400)}", "debug")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
                async with s.post(BASE + path, data=raw, headers=headers) as r:
                    txt = await r.text()
                    status = r.status
        except Exception as e:  # noqa
            dbg("vmos", f"{path} network error after {time.time() - t0:.1f}s: {e.__class__.__name__}: {e}", "error")
            raise VmosError(f"Could not reach VMOS ({e.__class__.__name__}).")
        dbg("vmos", f"<- {path} HTTP {status} in {time.time() - t0:.1f}s: {short(txt, 500)}", "debug")
        try:
            j = json.loads(txt)
        except Exception:  # noqa
            dbg("vmos", f"{path} returned non-JSON (HTTP {status})", "error")
            raise VmosError(f"VMOS answered HTTP {status}")
        if j.get("code") != 200:
            dbg("vmos", f"{path} error code={j.get('code')} msg={j.get('msg')}", "error")
            raise VmosError(f"VMOS error {j.get('code')}: {j.get('msg')}")
        return j.get("data")

    # ----- account / instances -----
    async def pads(self):
        data = await self.call("/vcpcloud/api/padApi/userPadList", {})
        items = data if isinstance(data, list) else (data or {}).get("records") or (data or {}).get("list") or []
        return [p.get("padCode") for p in items if isinstance(p, dict) and p.get("padCode")]

    async def set_sim(self, country, phone):
        dbg("vmos", f"Setting SIM: country={country} phone={phone}")
        await self.call("/vcpcloud/api/padApi/updateSIMByCountryAndPhone",
                        {"padCode": self.pad, "countryCode": country, "phoneNumber": phone})

    async def wait_online(self, timeout=240):
        end = time.time() + timeout
        n = 0
        while time.time() < end:
            n += 1
            try:
                if "ok" in await self.sh("echo ok", timeout=20):
                    dbg("vmos", f"Cloud phone {self.pad} is online (check #{n})")
                    return
                dbg("vmos", f"Online check #{n}: no reply yet", "warn")
            except VmosError as e:
                dbg("vmos", f"Online check #{n} failed: {e}", "warn")
            await asyncio.sleep(8)
        raise VmosError("The cloud phone did not come back online.")

    # ----- shell -----
    async def sh(self, cmd, timeout=60):
        dbg("vmos", f"shell $ {short(cmd, 300)}")
        t0 = time.time()
        data = await self.call("/vcpcloud/api/padApi/asyncCmd", {"padCodes": [self.pad], "scriptContent": cmd})
        item = data[0] if isinstance(data, list) else data
        if item.get("vmStatus") == 0:
            raise VmosError("The cloud phone is offline.")
        tid = item["taskId"]
        end = time.time() + timeout
        while time.time() < end:
            await asyncio.sleep(1.5)
            res = await self.call("/vcpcloud/api/padApi/padTaskDetail", {"taskIds": [tid]})
            t = res[0] if isinstance(res, list) and res else {}
            st = str(t.get("taskStatus"))
            if st == "3":
                out = t.get("taskResult") or ""
                dbg("vmos", f"shell done in {time.time() - t0:.1f}s, {len(out)} chars: {short(out.strip(), 300)}", "debug")
                return out
            if st.startswith("-"):
                dbg("vmos", f"shell failed (status {st}): {t.get('errorMsg')}", "error")
                raise VmosError(t.get("errorMsg") or "Command failed on the cloud phone.")
        raise VmosError("Cloud phone command timed out.")

    # ----- UI automation helpers (uiautomator) -----
    async def nodes(self):
        out = await self.sh("uiautomator dump /sdcard/u.xml >/dev/null 2>&1; cat /sdcard/u.xml", timeout=40)
        i = out.find("<?xml")
        if i < 0:
            dbg("vmos", "Screen dump empty (no UI XML)", "warn")
            return []
        try:
            root = ET.fromstring(out[i:])
        except ET.ParseError as e:
            dbg("vmos", f"Screen dump could not be parsed: {e}", "warn")
            return []
        res = []
        for n in root.iter("node"):
            m = re.findall(r"\d+", n.get("bounds", ""))
            if len(m) == 4:
                x1, y1, x2, y2 = map(int, m)
                res.append({"text": n.get("text", ""), "desc": n.get("content-desc", ""), "id": n.get("resource-id", ""),
                            "cls": n.get("class", ""), "x": (x1 + x2) // 2, "y": (y1 + y2) // 2})
        vis = [((r["text"] or r["desc"]).strip()) for r in res if (r["text"] or r["desc"]).strip()]
        dbg("vmos", f"Screen has {len(res)} elements. Visible text: {short(' | '.join(vis), 700)}", "debug")
        return res

    async def find(self, *labels, cls=None):
        want = [l.lower() for l in labels]
        for n in await self.nodes():
            hay = (n["text"] + " " + n["desc"]).lower()
            if cls and cls not in n["cls"]:
                continue
            if any(w in hay for w in want) or (cls and not want):
                return n
        return None

    async def tap(self, *labels, cls=None, wait=25):
        dbg("vmos", f"Looking for {list(labels) or ''}{' class=' + cls if cls else ''} (up to {wait}s)")
        end = time.time() + wait
        while time.time() < end:
            n = await self.find(*labels, cls=cls)
            if n:
                dbg("vmos", f"Tapping '{n['text'] or n['desc'] or n['cls']}' at {n['x']},{n['y']}")
                await self.sh(f"input tap {n['x']} {n['y']}")
                return n
            await asyncio.sleep(2)
        dbg("vmos", f"Not found on screen: {list(labels)}{' class=' + cls if cls else ''}", "warn")
        return None

    async def type(self, text):
        safe = re.sub(r"[^0-9A-Za-z+]", "", text)
        dbg("vmos", f"Typing '{safe}'")
        await self.sh(f"input text '{safe}'")

    async def screen_text(self):
        return " ".join((n["text"] + " " + n["desc"]) for n in await self.nodes())
