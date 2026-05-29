"""buddy-app — macOS menu bar app for the Claude buddy bridge."""

import asyncio
import os
import plistlib
import queue
import subprocess
import threading

import rumps

from .bridge import BuddyBridge

_ICON = {"scanning": "○", "connected": "●", "waiting": "⏸"}

_PLIST_PATH  = os.path.expanduser(
    "~/Library/LaunchAgents/com.sander.buddy-app.plist"
)
_PLIST_LABEL = "com.sander.buddy-app"
_BUDDY_BIN   = os.path.expanduser("~/.local/bin/buddy-app")


def _login_enabled() -> bool:
    return os.path.exists(_PLIST_PATH)


def _set_login(enabled: bool):
    if enabled:
        plist = {
            "Label": _PLIST_LABEL,
            "ProgramArguments": [_BUDDY_BIN],
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
        }
        os.makedirs(os.path.dirname(_PLIST_PATH), exist_ok=True)
        with open(_PLIST_PATH, "wb") as f:
            plistlib.dump(plist, f)
        subprocess.run(["launchctl", "load", _PLIST_PATH], check=False)
    else:
        subprocess.run(["launchctl", "unload", _PLIST_PATH], check=False)
        try:
            os.unlink(_PLIST_PATH)
        except OSError:
            pass


class BuddyApp(rumps.App):
    def __init__(self):
        super().__init__("Buddy", title=_ICON["scanning"], quit_button=None)

        self._q: queue.SimpleQueue = queue.SimpleQueue()

        self._device_item = rumps.MenuItem("Scanning…")
        self._msg_item    = rumps.MenuItem("Ready")
        self._entry_items = [rumps.MenuItem(" ") for _ in range(3)]
        self._login_item  = rumps.MenuItem("Start at Login",
                                           callback=self._toggle_login)
        self._quit_item   = rumps.MenuItem("Quit", callback=self._on_quit)

        self._login_item.state = _login_enabled()

        self.menu = [
            self._device_item,
            self._msg_item,
            None,
            *self._entry_items,
            None,
            self._login_item,
            self._quit_item,
        ]

        self._bridge = BuddyBridge(on_status_change=self._q.put)
        threading.Thread(target=self._run_bridge, daemon=True).start()

    # ── bridge thread ────────────────────────────────────────────────────────

    def _run_bridge(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._bridge.run())

    # ── UI update (main thread via timer) ────────────────────────────────────

    @rumps.timer(0.5)
    def _poll(self, _):
        status = None
        try:
            while True:
                status = self._q.get_nowait()
        except queue.Empty:
            pass
        if status:
            self._apply(status)

    def _apply(self, s: dict):
        connected = s.get("connected", False)
        waiting   = bool(s.get("waiting", 0))
        device    = s.get("device_name") or ""
        msg       = s.get("msg", "")
        entries   = s.get("entries", [])

        if waiting:
            self.title = _ICON["waiting"]
        elif connected:
            self.title = _ICON["connected"]
        else:
            self.title = _ICON["scanning"]

        if connected:
            self._device_item.title = f"● {device}" if device else "● Connected"
        else:
            self._device_item.title = "○ Scanning…"

        self._msg_item.title = msg or "Ready"

        for i, item in enumerate(self._entry_items):
            item.title = entries[i] if i < len(entries) else " "

    # ── login item ───────────────────────────────────────────────────────────

    def _toggle_login(self, sender):
        enabled = not bool(sender.state)
        _set_login(enabled)
        sender.state = enabled

    # ── quit ─────────────────────────────────────────────────────────────────

    def _on_quit(self, _):
        self._bridge.cleanup()
        rumps.quit_application()


def main():
    BuddyApp().run()
