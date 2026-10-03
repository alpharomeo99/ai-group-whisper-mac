"""In-app updates: check GitHub releases, download the new version, swap files, restart."""
import asyncio
import io
import os
import plistlib
import shutil
import subprocess
import tempfile
import zipfile

import aiohttp
from aiohttp import web

REPO = os.environ.get("AGW_GITHUB_REPO", "alpharomeo99/ai-group-whisper-mac")
APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RESTART_CODE = 42
STATE = {"stage": "", "pct": None, "error": None}


def current_version():
    try:
        return open(os.path.join(APP_ROOT, "VERSION")).read().strip()
    except OSError:
        return "0.0.0"


def vtuple(v):
    out = []
    for p in v.lstrip("v").split("."):
        try:
            out.append(int("".join(ch for ch in p if ch.isdigit()) or 0))
        except ValueError:
            out.append(0)
    return tuple(out + [0] * (3 - len(out)))


async def latest():
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers={"Accept": "application/vnd.github+json"}) as r:
            if r.status != 200:
                raise RuntimeError(f"GitHub answered {r.status}")
            return await r.json()


async def check():
    cur = current_version()
    rel = await latest()
    tag = rel.get("tag_name", "")
    return {"current": cur, "latest": tag.lstrip("v"), "tag": tag, "url": rel.get("html_url"),
            "available": vtuple(tag) > vtuple(cur), "canInstall": True}


async def install():
    STATE.update(stage="Checking", pct=None, error=None)
    info = await check()
    if not info["available"]:
        STATE.update(stage="Already up to date")
        return info
    STATE.update(stage="Downloading", pct=0)
    url = f"https://codeload.github.com/{REPO}/zip/refs/tags/{info['tag']}"
    buf = io.BytesIO()
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            if r.status != 200:
                raise RuntimeError(f"Download failed ({r.status})")
            total = int(r.headers.get("Content-Length") or 0)
            async for chunk in r.content.iter_chunked(65536):
                buf.write(chunk)
                if total:
                    STATE["pct"] = int(buf.tell() * 100 / total)
    STATE.update(stage="Installing", pct=None)
    with tempfile.TemporaryDirectory() as tmp:
        zipfile.ZipFile(buf).extractall(tmp)
        top = os.path.join(tmp, os.listdir(tmp)[0])
        for name in ("daemon", "renderer"):
            src, dst = os.path.join(top, name), os.path.join(APP_ROOT, name)
            if os.path.isdir(src):
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst)
        # The desktop icon lives beside the app bundle, not in the updateable daemon folder.
        # Updating its bundle metadata also upgrades installs made before the logo existed.
        contents = os.path.abspath(os.path.join(APP_ROOT, "..", ".."))
        plist_path = os.path.join(contents, "Info.plist")
        icon_src = os.path.join(top, "assets", "logo.icns")
        if os.path.isfile(plist_path) and os.path.isfile(icon_src):
            icon_dst = os.path.join(contents, "Resources", "AIGroupWhisper.icns")
            shutil.copy2(icon_src, icon_dst)
            with open(plist_path, "rb") as f:
                bundle_info = plistlib.load(f)
            bundle_info["CFBundleIconFile"] = "AIGroupWhisper.icns"
            with open(plist_path, "wb") as f:
                plistlib.dump(bundle_info, f)
            os.utime(os.path.dirname(contents), None)
        for name in ("run.py", "VERSION"):
            src = os.path.join(top, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(APP_ROOT, name))
    STATE.update(stage="Restarting")
    asyncio.get_running_loop().call_later(1.0, os._exit, RESTART_CODE)
    return info


def routes():
    r = web.RouteTableDef()
    J = web.json_response

    @r.get("/app/info")
    async def info(_):
        return J({"version": current_version(), "repo": REPO})

    @r.get("/app/check-updates")
    async def chk(_):
        try:
            return J(await check())
        except Exception as e:  # noqa
            return J({"error": str(e)})

    @r.post("/app/install-update")
    async def inst(_):
        try:
            return J(await install())
        except Exception as e:  # noqa
            STATE.update(error=str(e))
            return J({"error": str(e)})

    @r.get("/app/update-progress")
    async def prog(_):
        return J(STATE)

    @r.post("/app/open")
    async def open_url(req):
        url = (await req.json()).get("url", "")
        if url.startswith("https://"):
            subprocess.Popen(["open", url])
        return J({"ok": True})

    return r
