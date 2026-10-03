"""VMOS Cloud OpenAPI client (V2 signature: SHA-256(SK + ts + path + body))."""
import asyncio
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET

import aiohttp

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
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
            async with s.post(BASE + path, data=raw, headers=headers) as r:
                try:
                    j = await r.json(content_type=None)
                except Exception:  # noqa
                    raise VmosError(f"VMOS answered HTTP {r.status}")
        if j.get("code") != 200:
            raise VmosError(f"VMOS error {j.get('code')}: {j.get('msg')}")
        return j.get("data")

    # ----- account / instances -----
    async def pads(self):
        data = await self.call("/vcpcloud/api/padApi/userPadList", {})
        items = data if isinstance(data, list) else (data or {}).get("records") or (data or {}).get("list") or []
        return [p.get("padCode") for p in items if isinstance(p, dict) and p.get("padCode")]

    async def new_device(self, country="US"):
        await self.call("/vcpcloud/api/padApi/replacePad", {"padCodes": [self.pad], "countryCode": country})

    async def set_sim(self, country, phone):
        await self.call("/vcpcloud/api/padApi/updateSIMByCountryAndPhone",
                        {"padCode": self.pad, "countryCode": country, "phoneNumber": phone})

    async def wait_online(self, timeout=240):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if "ok" in await self.sh("echo ok", timeout=20):
                    return
            except VmosError:
                pass
            await asyncio.sleep(8)
        raise VmosError("The cloud phone did not come back online.")

    # ----- shell -----
    async def sh(self, cmd, timeout=60):
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
                return t.get("taskResult") or ""
            if st.startswith("-"):
                raise VmosError(t.get("errorMsg") or "Command failed on the cloud phone.")
        raise VmosError("Cloud phone command timed out.")

    # ----- UI automation helpers (uiautomator) -----
    async def nodes(self):
        out = await self.sh("uiautomator dump /sdcard/u.xml >/dev/null 2>&1; cat /sdcard/u.xml", timeout=40)
        i = out.find("<?xml")
        if i < 0:
            return []
        try:
            root = ET.fromstring(out[i:])
        except ET.ParseError:
            return []
        res = []
        for n in root.iter("node"):
            m = re.findall(r"\d+", n.get("bounds", ""))
            if len(m) == 4:
                x1, y1, x2, y2 = map(int, m)
                res.append({"text": n.get("text", ""), "desc": n.get("content-desc", ""), "id": n.get("resource-id", ""),
                            "cls": n.get("class", ""), "x": (x1 + x2) // 2, "y": (y1 + y2) // 2})
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
        end = time.time() + wait
        while time.time() < end:
            n = await self.find(*labels, cls=cls)
            if n:
                await self.sh(f"input tap {n['x']} {n['y']}")
                return n
            await asyncio.sleep(2)
        return None

    async def type(self, text):
        safe = re.sub(r"[^0-9A-Za-z+]", "", text)
        await self.sh(f"input text '{safe}'")

    async def screen_text(self):
        return " ".join((n["text"] + " " + n["desc"]) for n in await self.nodes())
