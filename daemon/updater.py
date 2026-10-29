"""In-app updates: check GitHub releases, download the new version, swap files, restart."""
import asyncio
import io
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import time
import urllib.request
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


def fetch_latest_release_sync():
    """Ultra-resilient release checker prioritizing macOS native curl (avoids getaddrinfo stalls) with urllib fallback."""
    # 1. Try macOS native curl with GitHub REST API and raw VERSION
    for endpoint in (
        f"https://api.github.com/repos/{REPO}/releases/latest",
        f"https://raw.githubusercontent.com/{REPO}/main/VERSION",
    ):
        try:
            r = subprocess.run([
                "curl", "-sS", "-f",
                "--connect-timeout", "3",
                "--max-time", "6",
                "-H", "User-Agent: AIGroupWhisper-Mac/2.x (macOS; in-app-updater)",
                "-H", "Accept: application/vnd.github+json",
                endpoint,
            ], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                out = r.stdout.strip()
                if endpoint.endswith("/VERSION"):
                    ver = out
                    tag = f"v{ver}" if not ver.startswith("v") else ver
                    return {
                        "tag_name": tag,
                        "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
                        "name": f"AI Group Whisper {tag}",
                    }
                else:
                    data = json.loads(out)
                    if data.get("tag_name"):
                        return data
        except Exception:
            pass

    # 2. Try web release page redirect via curl
    try:
        r = subprocess.run([
            "curl", "-sS", "-I",
            "--connect-timeout", "3",
            "--max-time", "6",
            f"https://github.com/{REPO}/releases/latest"
        ], capture_output=True, text=True)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.lower().startswith("location:"):
                    loc = line.split(":", 1)[1].strip()
                    if "/releases/tag/" in loc:
                        tag = loc.split("/releases/tag/")[-1].strip("/")
                        if tag:
                            return {
                                "tag_name": tag,
                                "html_url": loc,
                                "name": f"AI Group Whisper {tag}",
                            }
    except Exception:
        pass

    # 3. Urllib fallback with tight 3-second timeout
    headers = {
        "User-Agent": "AIGroupWhisper-Mac/2.x (macOS; in-app-updater)",
        "Accept": "application/vnd.github+json",
    }
    for url, is_raw in (
        (f"https://api.github.com/repos/{REPO}/releases/latest", False),
        (f"https://raw.githubusercontent.com/{REPO}/main/VERSION", True),
    ):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    out = resp.read().decode("utf-8").strip()
                    if is_raw and out:
                        tag = f"v{out}" if not out.startswith("v") else out
                        return {
                            "tag_name": tag,
                            "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
                            "name": f"AI Group Whisper {tag}",
                        }
                    elif not is_raw:
                        data = json.loads(out)
                        if data.get("tag_name"):
                            return data
        except Exception:
            pass

    raise RuntimeError("Could not reach GitHub via API or mirrors. Check internet/DNS.")


async def latest():
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(None, fetch_latest_release_sync), timeout=10.0)


async def check():
    cur = current_version()
    rel = await latest()
    tag = rel.get("tag_name", "")
    return {
        "current": cur,
        "latest": tag.lstrip("v"),
        "tag": tag,
        "url": rel.get("html_url"),
        "available": vtuple(tag) > vtuple(cur),
        "canInstall": True,
    }


def download_archive_sync(tag, progress_cb=None):
    """Download update archive using curl directly to temporary zip file, with urllib fallback."""
    urls = [
        f"https://codeload.github.com/{REPO}/zip/refs/tags/{tag}",
        f"https://github.com/{REPO}/archive/refs/tags/{tag}.zip",
        f"https://api.github.com/repos/{REPO}/zipball/{tag}",
    ]
    # Try curl first
    for url in urls:
        try:
            with tempfile.TemporaryDirectory() as td:
                p = os.path.join(td, "update.zip")
                r = subprocess.run([
                    "curl", "-fsSL",
                    "--connect-timeout", "6",
                   "--max-time", "90",
                    "-H", "User-Agent: AIGroupWhisper-Mac/2.x (macOS; in-app-updater)",
                    url, "-o", p
                ], capture_output=True)
                if r.returncode == 0 and os.path.exists(p) and os.path.getsize(p) > 1000:
                    with zipfile.ZipFile(p) as z:
                        if z.namelist():
                            data = open(p, "rb").read()
                            return io.BytesIO(data)
        except Exception:
            pass

    # Urllib fallback
    headers = {"User-Agent": "AIGroupWhisper-Mac/2.x (macOS; in-app-updater)", "Accept": "*/*"}
    last_err = None
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 200:
                    buf = io.BytesIO(resp.read())
                    with zipfile.ZipFile(buf) as z:
                        if z.namelist():
                            buf.seek(0)
                            return buf
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Download failed: {last_err}")


async def install():
    STATE.update(stage="Preparing", pct=None, error=None)
    info = await check()
    if not info["available"]:
        STATE.update(stage="Already up to date")
        return info
    STATE.update(stage="Downloading update", pct=None)

    def on_progress(pct):
        STATE["pct"] = pct

    loop = asyncio.get_running_loop()
    buf = await loop.run_in_executor(None, download_archive_sync, info["tag"], on_progress)
    STATE.update(stage="Installing", pct=None)
    with tempfile.TemporaryDirectory() as tmp:
        zipfile.ZipFile(buf).extractall(tmp)
        top = os.path.join(tmp, os.listdir(tmp)[0])
        for name in ("daemon", "renderer"):
            src, dst = os.path.join(top, name), os.path.join(APP_ROOT, name)
            if os.path.isdir(src):
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst)
        contents = os.path.abspath(os.path.join(APP_ROOT, "..", ".."))
        plist_path = os.path.join(contents, "Info.plist")
        icon_src = os.path.join(top, "assets", "logo.icns")
        if os.path.isfile(plist_path) and os.path.isfile(icon_src):
            icon_dst = os.path.join(contents, "Resources", "AIGroupWhisper.icns")
            shutil.copy2(icon_src, icon_dst)
            with open(plist_path, "rb") as f:
                bundle_info = plistlib.load(f)
            bundle_info["CFBundleIconFile"] = "AIGroupWhisper.icns"
            bundle_info["CFBundleIconName"] = "AIGroupWhisper"
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
            STATE.update(error=str(e)}
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
