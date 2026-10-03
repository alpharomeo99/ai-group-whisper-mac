"""Camoufox (stealth Firefox) automation: create Telegram API credentials on my.telegram.org through a proxy."""
import asyncio
import random
import re

AUTH = "https://my.telegram.org/auth"
APPS = "https://my.telegram.org/apps"


class CamoufoxError(Exception):
    pass


def parse_proxy(line):
    """Accepts host:port, host:port:user:pass, or scheme://user:pass@host:port."""
    line = (line or "").strip()
    if not line:
        return None
    m = re.match(r"^(?:(?P<scheme>\w+)://)?(?:(?P<user>[^:@/]+):(?P<pw>[^@/]*)@)?(?P<host>[^:@/]+):(?P<port>\d+)$", line)
    if m:
        d = m.groupdict()
    else:
        parts = line.split("://")[-1].split(":")
        if len(parts) == 4:
            d = {"scheme": None, "host": parts[0], "port": parts[1], "user": parts[2], "pw": parts[3]}
        else:
            return None
    p = {"server": f"{d.get('scheme') or 'http'}://{d['host']}:{d['port']}"}
    if d.get("user"):
        p["username"], p["password"] = d["user"], d.get("pw") or ""
    return p


async def check_proxy(proxy_line, timeout=30):
    """Returns the exit IP seen through the proxy."""
    import aiohttp
    p = parse_proxy(proxy_line)
    if not p:
        raise CamoufoxError("Could not read that proxy. Use host:port:user:pass or http://user:pass@host:port.")
    url, auth = p["server"], None
    if p.get("username"):
        auth = aiohttp.BasicAuth(p["username"], p.get("password") or "")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.get("https://api.ipify.org?format=json", proxy=url, proxy_auth=auth) as r:
            return (await r.json(content_type=None)).get("ip")


def _run(phone, proxy_line, code_cb, app_title, headless, password):
    """Blocking Camoufox run; executed in a worker thread."""
    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        raise CamoufoxError("Camoufox is not installed yet. Open the Camoufox section and press Install.")
    proxy = parse_proxy(proxy_line) if proxy_line else None
    opts = {"headless": headless, "humanize": True, "geoip": bool(proxy), "os": ["windows", "macos"]}
    if proxy:
        opts["proxy"] = proxy
    with Camoufox(**opts) as browser:
        page = browser.new_page()
        page.goto(AUTH, wait_until="domcontentloaded", timeout=90000)
        page.fill("#my_login_phone", phone)
        page.click("button:has-text('Next')")
        page.wait_for_selector("#my_password", timeout=60000)
        code = code_cb()
        if not code:
            raise CamoufoxError("No my.telegram.org login code arrived.")
        page.fill("#my_password", str(code))
        page.click("button:has-text('Sign In')")
        page.wait_for_timeout(4000)
        if "login" in (page.content() or "").lower() and page.query_selector("#my_password"):
            raise CamoufoxError("my.telegram.org did not accept the login code.")
        page.goto(APPS, wait_until="domcontentloaded", timeout=90000)
        if page.query_selector("#app_title"):
            page.fill("#app_title", app_title)
            page.fill("#app_shortname", re.sub(r"\W", "", app_title.lower())[:20] or "whisper%d" % random.randint(10, 99))
            if page.query_selector("#app_platform_other"):
                page.check("#app_platform_other")
            page.click("button:has-text('Create application')")
            page.wait_for_timeout(3000)
            if page.query_selector("button:has-text('Confirm')"):
                page.click("button:has-text('Confirm')")
                page.wait_for_timeout(3000)
            page.goto(APPS, wait_until="domcontentloaded", timeout=90000)
        html = page.content()
        api_id = re.search(r"App api_id:.*?<(?:strong|code|span)[^>]*>\s*(\d{4,10})", html, re.S)
        api_hash = re.search(r"App api_hash:.*?<(?:strong|code|span)[^>]*>\s*([0-9a-f]{32})", html, re.S)
        if not api_id:
            api_id = re.search(r"\b(\d{6,10})\b", html)
        if not api_hash:
            api_hash = re.search(r"\b([0-9a-f]{32})\b", html)
        if not (api_id and api_hash):
            raise CamoufoxError("Could not read the API ID and hash from my.telegram.org.")
        return int(api_id.group(1)), api_hash.group(1)


async def get_api_credentials(phone, proxy_line, code_getter, app_title="Whisper", headless=True, password=None):
    """code_getter: async callable returning the my.telegram.org login code."""
    loop = asyncio.get_running_loop()
    box = {}

    def code_cb():
        fut = asyncio.run_coroutine_threadsafe(code_getter(), loop)
        return fut.result(360)

    box["r"] = await loop.run_in_executor(None, _run, phone, proxy_line, code_cb, app_title, headless, password)
    return box["r"]
