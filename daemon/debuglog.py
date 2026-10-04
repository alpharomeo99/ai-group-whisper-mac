"""Detailed debug log for account generation.

Every worker (VMOS, TextVerified, Camoufox, Telethon, manager) calls dbg(). Entries are kept
in memory for the Accounts page and also appended to logs/provision.log on disk.
Never pass secrets (API keys, hashes in full, passwords) to dbg().
"""
import os
import threading
import time
import traceback

MAX = 4000
_lock = threading.Lock()
_entries = []
_seq = 0
LOG_DIR = os.path.expanduser("~/Library/Application Support/AI Group Whisper/data/logs")
LOG_FILE = os.path.join(LOG_DIR, "provision.log")


def _short(v, n=600):
    s = v if isinstance(v, str) else repr(v)
    s = s.replace("\r", " ")
    return s if len(s) <= n else s[:n] + f"... (+{len(s) - n} chars)"


def dbg(src, msg, level="info"):
    global _seq
    now = time.time()
    with _lock:
        _seq += 1
        e = {"id": _seq, "t": now, "src": src, "level": level, "text": _short(msg, 2000)}
        _entries.append(e)
        if len(_entries) > MAX:
            del _entries[: len(_entries) - MAX]
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{stamp} [{level.upper():5}] [{src}] {e['text']}\n")
    except Exception:  # noqa
        pass


def exc(src, e):
    dbg(src, f"{e.__class__.__name__}: {e}\n{traceback.format_exc()[-1500:]}", "error")


def mark_run():
    dbg("manager", "=" * 20 + " new generation run " + "=" * 20)


def since(after=0, limit=1500):
    with _lock:
        out = [e for e in _entries if e["id"] > after]
    return out[-limit:]


def clear():
    with _lock:
        _entries.clear()


short = _short
