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
        self.ak, self.sk, self.pad = "".join(ak.split()), "".join(sk.split()), "".join((pad or "").split())

    def _headers(self, path, raw, sign_body):
        ts = str(int(time.time()))
        sign = hashlib.sha256((self.sk + ts + path + (raw if sign_body else "")).encode("utf-8")).hexdigest()
        return {"X-Access-Key": self.ak, "X-Timestamp": ts, "X-Sign": sign, "Content-Type": "application/json"}

    async def _post(self, path, raw, sign_body):
        t0 = time.time()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
                async with s.post(BASE + path, data=raw.encode("utf-8"), headers=self._headers(path, raw, sign_body)) as r:
                    txt = await r.text()
                    status = r.status
        except Exception as e:  # noqa
            dbg("vmos", f"{path} network error after {time.time() - t0:.1f}s: {e.__class__.__name__}: {e}", "error")
            raise VmosError(f"Could not reach VMOS ({e.__class__.__name__}).")
        dbg("vmos", f"<- {path} HTTP {status} in {time.time() - t0:.1f}s (body signed={sign_body}): {short(txt, 500)}", "debug")
        try:
            return json.loads(txt)
        except Exception:  # noqa
            dbg("vmos", f"{path} returned non-JSON (HTTP {status})", "error")
            raise VmosError(f"VMOS answered HTTP {status}")

    async def call(self, path, body):
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        dbg("vmos", f"POST {path} body={short(raw, 400)}", "debug")
        sign_body = True  # this VMOS account accepts signed bodies on every endpoint
        j = await self._post(path, raw, sign_body)
        if j.get("code") == 2019:
            # Some VMOS servers sign command bodies, some don't: try the other way once.
            dbg("vmos", f"Signature rejected; retrying {path} with body signed={not sign_body}", "warn")
            j = await self._post(path, raw, not sign_body)
        if j.get("code") == 2019:
            dbg("vmos", f"Signature still rejected. Key check: access key {len(self.ak)} chars "
                f"(starts '{self.ak[:4]}'), secret key {len(self.sk)} chars. Mac clock unix={int(time.time())}. "
                "Most likely the Secret Key in Settings -> VMOS is wrong, swapped with the Access Key, or truncated.", "error")
            raise VmosError("VMOS rejected the keys (signature failed). Re-copy the Access Key and Secret Key from VMOS -> Developer -> API into Settings -> VMOS.")
        if j.get("code") in (2031, 2033):
            hint = {2031: "Access Key not found - re-copy it from VMOS -> Developer -> API.",
                    2033: "Your Mac's clock is off - turn on automatic date & time."}[j["code"]]
            dbg("vmos", hint, "error")
            raise VmosError(hint)
        if j.get("code") != 200:
            dbg("vmos", f"{path} error code={j.get('code')} msg={j.get('msg')}", "error")
            raise VmosError(f"VMOS error {j.get('code')}: {j.get('msg')}")
        return j.get("data")

    # ----- account / instances -----
    async def pads(self):
        data = await self.call("/vcpcloud/api/padApi/userPadList", {})
        items = data if isinstance(data, list) else (data or {}).get("records") or (data or {}).get("list") or []
        return [p.get("padCode") for p in items if isinstance(p, dict) and p.get("padCode")]


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
    # VMOS cuts shell output at ~2000 chars, so the full UI XML can never come back whole.
    # Compact it on the phone to one short line per useful element, then read it in chunks.
    _COMPACT = (
        "tr '>' '\\n' < /sdcard/u.xml | grep '<node' | "
        "sed -n 's/.* text=\"\\([^\"]*\\)\" resource-id=\"\\([^\"]*\\)\" class=\"\\([^\"]*\\)\".* content-desc=\"\\([^\"]*\\)\".* clickable=\"\\([^\"]*\\)\".* bounds=\"\\([^\"]*\\)\".*/\\6~\\3~\\5~\\2~\\1~\\4/p' | "
        "sed 's/android\\.widget\\.//; s/~[a-z.]*:id\\//~/' | "
        "grep -v '~false~[^~]*~~$' > /sdcard/u.txt; wc -c < /sdcard/u.txt"
    )

    async def nodes(self):
        out = await self.sh("rm -f /sdcard/u.xml /sdcard/u.txt; uiautomator dump /sdcard/u.xml >/dev/null 2>&1; "
                            "test -s /sdcard/u.xml && echo DUMPED || echo NODUMP", timeout=40)
        if "DUMPED" not in out:
            dbg("vmos", f"Screen dump failed: {short(out.strip(), 300)}", "warn")
            return []
        size_out = await self.sh(self._COMPACT, timeout=40)
        m = re.findall(r"\d+", size_out)
        size = int(m[-1]) if m else 0
        text = ""
        pos = 1
        while pos <= size and pos < 40000:
            text += await self.sh(f"tail -c +{pos} /sdcard/u.txt | head -c 1500", timeout=40)
            pos += 1500
        res = []
        for line in text.splitlines():
            parts = line.split("~")
            if len(parts) < 6:
                continue
            b = re.findall(r"\d+", parts[0])
            if len(b) != 4:
                continue
            x1, y1, x2, y2 = map(int, b)
            res.append({"text": parts[4], "desc": "~".join(parts[5:]), "id": parts[3], "cls": parts[1],
                        "click": parts[2] == "true", "x": (x1 + x2) // 2, "y": (y1 + y2) // 2})
        vis = [((r["text"] or r["desc"]).strip()) for r in res if (r["text"] or r["desc"]).strip()]
        dbg("vmos", f"Screen has {len(res)} elements ({size} bytes). Visible text: {short(' | '.join(vis), 700)}", "debug")
        return res

    async def dismiss_popups(self):
        """Tap away Android permission prompts and simple OK dialogs."""
        for _ in range(4):
            ns = await self.nodes()
            hit = None
            for n in ns:
                t = (n["text"] or n["desc"]).strip().lower()
                if n["id"].startswith("permission_allow") or t in ("allow", "while using the app", "only this time", "ok", "got it"):
                    hit = n
                    break
            if not hit:
                return
            dbg("vmos", f"Dismissing popup: tapping '{hit['text'] or hit['desc']}' at {hit['x']},{hit['y']}")
            await self.sh(f"input tap {hit['x']} {hit['y']}")
            await asyncio.sleep(2)

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

    async def screen_size(self):
        out = await self.sh("wm size")
        m = re.findall(r"(\d+)x(\d+)", out)
        w, h = map(int, m[-1]) if m else (720, 1280)
        dbg("vmos", f"Screen size {w}x{h}", "debug")
        return w, h

    async def tap_xy(self, x, y, why=""):
        dbg("vmos", f"Tapping by position {x},{y} {why}")
        await self.sh(f"input tap {x} {y}")

    async def current_app(self):
        out = await self.sh("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -2")
        dbg("vmos", f"In front: {short(out.strip(), 300)}", "debug")
        return out

    async def screen_text(self):
        return " ".join((n["text"] + " " + n["desc"]) for n in await self.nodes())
