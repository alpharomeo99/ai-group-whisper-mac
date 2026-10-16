#!/usr/bin/env python3
"""AI Group Whisper desktop app: native macOS window (Apple WebKit) + background Telegram engine."""
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import webview

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.expanduser("~/Library/Application Support/AI Group Whisper/data")
RESTART_CODE = 42
os.makedirs(DATA, exist_ok=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
state = {"proc": None, "quitting": False, "restarts": 0, "window": None, "mini": False}


def start_engine():
    log = open(os.path.join(DATA, "daemon.log"), "a")
    state["proc"] = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "daemon", "whisperd.py"), "--port", str(PORT), "--data", DATA],
        stdout=log, stderr=log)


def relaunch_app():
    bundle = os.environ.get("AGW_BUNDLE")
    if bundle:
        subprocess.Popen(["/bin/sh", "-c", f'sleep 1; open -n "{bundle}"'])
    else:
        subprocess.Popen([sys.executable] + sys.argv)
    os._exit(0)


def watch_engine():
    while not state["quitting"]:
        code = state["proc"].wait()
        if state["quitting"]:
            return
        if code == RESTART_CODE:  # update installed
            relaunch_app()
        if state["restarts"] >= 5:
            return
        state["restarts"] += 1
        time.sleep(2)
        start_engine()


def wait_ready(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(BASE + "/app/info", timeout=1)
            return True
        except Exception:  # noqa
            time.sleep(0.3)
    return False


def post_power(st):
    def go():
        try:
            req = urllib.request.Request(BASE + "/power", data=f'{{"state":"{st}","reason":"macos"}}'.encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:  # noqa
            pass
    threading.Thread(target=go, daemon=True).start()


def hook_sleep_wake():
    """Pause the queue before the Mac sleeps, resume and catch up after it wakes."""
    try:
        from AppKit import NSWorkspace
        from Foundation import NSObject

        class Observer(NSObject):
            def sleep_(self, _n):
                post_power("suspend")

            def wake_(self, _n):
                post_power("resume")

        obs = Observer.alloc().init()
        nc = NSWorkspace.sharedWorkspace().notificationCenter()
        nc.addObserver_selector_name_object_(obs, "sleep:", "NSWorkspaceWillSleepNotification", None)
        nc.addObserver_selector_name_object_(obs, "wake:", "NSWorkspaceDidWakeNotification", None)
        state["observer"] = obs
    except Exception as e:  # noqa
        print("sleep/wake hook unavailable:", e)


def set_dock_icon():
    """Set the running Cocoa application's Dock icon, including after in-app updates."""
    icon = os.path.abspath(os.path.join(ROOT, "..", "AIGroupWhisper.icns"))
    if not os.path.isfile(icon):
        return
    try:
        from AppKit import NSApplication, NSImage
        image = NSImage.alloc().initWithContentsOfFile_(icon)
        if image:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception as e:  # noqa
        print("Dock icon unavailable:", e)


class WindowBridge:
    """Small UI bridge for switching between the full dashboard and a compact Live widget."""
    def __init__(self, window=None):
        self.window = window

    def show_mini(self):
        def resize():
            state["mini"] = True
            try:
                win = self.window or state.get("window")
                if win:
                    win.resize(220, 72)
                    win.min_size = (220, 72)
                    win.max_size = (220, 72)
            except Exception as e:
                print("Could not resize to Live widget:", e)
        threading.Thread(target=resize, daemon=True).start()
        return True

    def show_full(self):
        def resize():
            state["mini"] = False
            try:
                win = self.window or state.get("window")
                if win:
                    win.max_size = (9999, 9999)
                    win.resize(1180, 780)
                    win.min_size = (820, 560)
            except Exception as e:
                print("Could not restore dashboard size:", e)
        threading.Thread(target=resize, daemon=True).start()
        return True

    def get_live_status(self):
        proc = state.get("proc")
        alive = bool(proc and proc.poll() is None)
        return {"live": alive, "mini": state.get("mini", False)}


def main():
    start_engine()
    threading.Thread(target=watch_engine, daemon=True).start()
    wait_ready()
    hook_sleep_wake()
    set_dock_icon()
    bridge = WindowBridge()
    window = webview.create_window("AI Group Whisper", BASE + "/", width=1180, height=780, min_size=(820, 560), js_api=bridge)
    bridge.window = window
    state["window"] = window
    try:
        window.expose(bridge.show_mini, bridge.show_full, bridge.get_live_status)
    except Exception:
        pass
    webview.start()
    state["quitting"] = True
    try:
        state["proc"].terminate()
    except Exception:  # noqa
        pass


if __name__ == "__main__":
    main()
