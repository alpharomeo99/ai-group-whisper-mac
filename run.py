#!/usr/bin/env python3
"""AI Group Whisper desktop app: native macOS window (Apple WebKit) + background Telegram engine."""
import os
import socket
import subprocess
import sys
import threading
import time
import json
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
        self._window = window

    def show_mini(self):
        def resize():
            state["mini"] = True
            try:
                win = self._window or state.get("window")
                if not win:
                    return
                try:
                    win.set_title("Live")
                except Exception:
                    pass
                native = getattr(win, "native", None)
                if native:
                    try:
                        from AppKit import NSSize, NSMakeRect, NSFloatingWindowLevel
                        from PyObjCTools import AppHelper

                        def _cocoa_mini():
                            try:
                                target = native.window() if hasattr(native, "window") and callable(native.window) and native.window() else native
                                target.setMaxSize_(NSSize(10000, 10000))
                                target.setMinSize_(NSSize(100, 28))
                                try:
                                    target.setContentMinSize_(NSSize(100, 28))
                                except Exception:
                                    pass
                                cur = target.frame()
                                target_w, target_h = 220, 56
                                new_x = cur.origin.x
                                new_y = cur.origin.y + (cur.size.height - target_h)
                                target.setFrame_display_animate_(NSMakeRect(new_x, new_y, target_w, target_h), True, False)
                                target.setMinSize_(NSSize(140, 36))
                                target.setMaxSize_(NSSize(360, 72))
                                target.setLevel_(NSFloatingWindowLevel)
                            except Exception as ce:
                                print("Cocoa mini error:", ce)

                        AppHelper.callAfter(_cocoa_mini)
                    except Exception as e:
                        print("AppKit callAfter failed:", e)
                        try:
                            win.on_top = True
                        except Exception:
                            pass
                        win.resize(220, 56)
                else:
                    try:
                        win.on_top = True
                    except Exception:
                        pass
                    win.resize(220, 56)
            except Exception as e:
                print("Could not resize to Live widget:", e)
        threading.Thread(target=resize, daemon=True).start()
        return True

    def show_full(self):
        def resize():
            state["mini"] = False
            try:
                win = self._window or state.get("window")
                if not win:
                    return
                try:
                    win.set_title("AI Group Whisper")
                except Exception:
                    pass
                native = getattr(win, "native", None)
                if native:
                    try:
                        from AppKit import NSSize, NSMakeRect, NSNormalWindowLevel
                        from PyObjCTools import AppHelper

                        def _cocoa_full():
                            try:
                                target = native.window() if hasattr(native, "window") and callable(native.window) and native.window() else native
                                target.setMaxSize_(NSSize(10000, 10000))
                                target.setMinSize_(NSSize(640, 480))
                                try:
                                    target.setContentMinSize_(NSSize(640, 480))
                                except Exception:
                                    pass
                                cur = target.frame()
                                target_w, target_h = 1180, 780
                                new_x = cur.origin.x
                                new_y = cur.origin.y + (cur.size.height - target_h)
                                target.setFrame_display_animate_(NSMakeRect(new_x, new_y, target_w, target_h), True, False)
                                target.setLevel_(NSNormalWindowLevel)
                            except Exception as ce:
                                print("Cocoa full error:", ce)

                        AppHelper.callAfter(_cocoa_full)
                    except Exception as e:
                        print("AppKit callAfter failed:", e)
                        try:
                            win.on_top = False
                        except Exception:
                            pass
                        win.resize(1180, 780)
                else:
                    try:
                        win.on_top = False
                    except Exception:
                        pass
                    win.resize(1180, 780)
            except Exception as e:
                print("Could not restore dashboard size:", e)
        threading.Thread(target=resize, daemon=True).start()
        return True

    def get_live_status(self):
        proc = state.get("proc")
        alive = bool(proc and proc.poll() is None)
        return {"live": alive, "mini": state.get("mini", False)}


def watch_window_cmd(bridge):
    cmd_file = os.path.join(DATA, "window_cmd.json")
    last_t = time.time()
    while not state["quitting"]:
        try:
            if os.path.isfile(cmd_file):
                with open(cmd_file, "r") as f:
                    data = json.load(f)
                t = data.get("t", 0)
                if t > last_t:
                    last_t = t
                    mode = data.get("mode")
                    if mode == "mini":
                        bridge.show_mini()
                    elif mode == "full":
                        bridge.show_full()
        except Exception:
            pass
        time.sleep(0.3)


def main():
    start_engine()
    threading.Thread(target=watch_engine, daemon=True).start()
    wait_ready()
    hook_sleep_wake()
    set_dock_icon()
    bridge = WindowBridge()
    window = webview.create_window("AI Group Whisper", BASE + "/", width=1180, height=780, min_size=(100, 28), js_api=bridge)
    bridge._window = window
    state["window"] = window
    threading.Thread(target=watch_window_cmd, args=(bridge,), daemon=True).start()
    webview.start()
    state["quitting"] = True
    try:
        state["proc"].terminate()
    except Exception:  # noqa
        pass


if __name__ == "__main__":
    main()
