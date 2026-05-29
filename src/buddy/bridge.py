"""
buddy-bridge — persistent BLE daemon.

Maintains a connection to the M5StickC Plus S3, sends heartbeats so the
display stays live, and handles approval requests forwarded by buddy-hook.

Run once in the background before starting a Claude Code session:
    buddy-bridge &
"""

import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("bleak not found — run: uv tool install ./tools/buddy", file=sys.stderr)
    sys.exit(1)

DEVICE_PREFIX  = os.environ.get("BUDDY_DEVICE", "Claude")
SOCKET_PATH    = f"/tmp/buddy-bridge-{os.getuid()}.sock"
HEARTBEAT_SECS = 5
MAX_APPROVAL_SECS = 180

NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


class BuddyBridge:
    def __init__(self, on_status_change=None):
        self.client: BleakClient | None = None
        self._connected = False
        self._disconnect_evt = asyncio.Event()
        self._rx_buf = b""
        self._pending: dict | None = None
        self._device_name: str | None = None
        self.on_status_change = on_status_change
        self.state: dict = {
            "total": 1, "running": 0, "waiting": 0,
            "msg": "Claude Code", "entries": [],
            "tokens": 0, "tokens_today": 0,
        }

    def _emit(self):
        if self.on_status_change:
            self.on_status_change({
                **self.state,
                "connected": self._connected,
                "device_name": self._device_name,
            })

    # ── BLE ──────────────────────────────────────────────────────────────────

    def _on_notify(self, _sender, data: bytes):
        self._rx_buf += data
        while b"\n" in self._rx_buf:
            line, self._rx_buf = self._rx_buf.split(b"\n", 1)
            if line.strip():
                try:
                    self._on_device_msg(json.loads(line.strip()))
                except (json.JSONDecodeError, ValueError):
                    pass

    def _on_device_msg(self, msg: dict):
        if msg.get("cmd") == "permission" and self._pending:
            if msg.get("id") == self._pending["id"]:
                self._pending["decision"] = msg.get("decision", "once")
                self._pending["event"].set()

    def _on_disconnect(self, _client):
        print("Device disconnected.", flush=True)
        self._connected = False
        self._disconnect_evt.set()
        if self._pending:
            self._pending["decision"] = "once"
            self._pending["event"].set()
        self._emit()

    async def _write(self, obj: dict):
        if not self._connected or not self.client:
            return
        payload = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        for i in range(0, len(payload), 200):
            try:
                await self.client.write_gatt_char(NUS_RX, payload[i:i + 200],
                                                  response=False)
            except Exception:
                self._connected = False
                return

    async def _time_sync(self):
        ts = int(time.time())
        isdst = time.daylight and bool(time.localtime(ts).tm_isdst)
        tz = -(time.altzone if isdst else time.timezone)
        await self._write({"time": [ts, tz]})

    async def _heartbeat_loop(self):
        while True:
            if self._connected:
                try:
                    await self._write(self.state)
                except Exception:
                    pass
            await asyncio.sleep(HEARTBEAT_SECS)

    async def _connect_loop(self):
        while True:
            try:
                print(f"Scanning for '{DEVICE_PREFIX}*'…", flush=True)
                devices = await BleakScanner.discover(timeout=8.0)
                target = next(
                    (d for d in devices
                     if d.name and d.name.startswith(DEVICE_PREFIX)),
                    None,
                )
                if target is None:
                    print("No device found — retrying in 10 s…", flush=True)
                    await asyncio.sleep(10)
                    continue

                print(f"Connecting to {target.name} ({target.address})…",
                      flush=True)
                self._device_name = target.name
                self._disconnect_evt.clear()
                async with BleakClient(
                    target.address,
                    disconnected_callback=self._on_disconnect,
                ) as client:
                    self.client = client
                    self._connected = True
                    await client.start_notify(NUS_TX, self._on_notify)
                    await self._time_sync()
                    print("Connected.", flush=True)
                    self._emit()
                    await self._disconnect_evt.wait()

            except Exception as exc:
                print(f"BLE error: {exc} — retrying in 5 s…", flush=True)
                self._connected = False

            await asyncio.sleep(5)

    # ── Unix socket ───────────────────────────────────────────────────────────

    async def _handle_hook(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter):
        try:
            data = await asyncio.wait_for(reader.read(8192), timeout=5)
            msg  = json.loads(data)
            kind = msg.get("type")

            if kind == "pre_tool":
                reply = await self._handle_pre_tool(msg)
                writer.write(json.dumps(reply).encode())
            elif kind == "tool_start":
                self.state["running"] = 1
                tool, hint = msg.get("tool", ""), msg.get("hint", "")
                self.state["msg"] = (f"{tool}: {hint}" if hint else tool)[:40]
                _log(self.state["entries"], self.state["msg"])
                await self._write(self.state)
                self._emit()
                writer.write(b"ok")
            elif kind == "post_tool":
                self.state.update({"running": 0, "waiting": 0,
                                   "msg": msg.get("msg", "Claude Code")})
                self.state.pop("prompt", None)
                if entry := msg.get("entry"):
                    _log(self.state["entries"], entry)
                await self._write(self.state)
                self._emit()
                writer.write(b"ok")
            elif kind == "stop":
                self.state.update({"running": 0, "waiting": 0,
                                   "msg": "Claude Code done"})
                self.state.pop("prompt", None)
                await self._write(self.state)
                self._emit()
                writer.write(b"ok")
            else:
                writer.write(b"unknown")

        except Exception as exc:
            print(f"Hook error: {exc}", file=sys.stderr)
            writer.write(b"error")
        finally:
            try:
                await writer.drain()
                writer.close()
            except Exception:
                pass

    async def _handle_pre_tool(self, msg: dict) -> dict:
        tool    = msg.get("tool", "unknown")
        hint    = msg.get("hint", "")
        req_id  = msg.get("id", f"r{int(time.time() * 1000) % 10 ** 9}")
        timeout = min(float(msg.get("timeout", 0)), MAX_APPROVAL_SECS)

        self.state["running"] = 1
        self.state["msg"] = (f"approve: {tool}" if timeout > 0
                             else f"{tool}: {hint}")[:40]
        _log(self.state["entries"], self.state["msg"])

        if timeout > 0:
            self.state["waiting"] = 1
            self.state["prompt"] = {"id": req_id, "tool": tool,
                                    "hint": hint[:43]}
            event = asyncio.Event()
            self._pending = {"id": req_id, "event": event, "decision": "once"}
            await self._write(self.state)
            self._emit()

            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                print(f"Approval timeout ({timeout:.0f} s) for {tool}"
                      " — auto-approving", flush=True)

            decision = self._pending["decision"]
            self._pending = None
            self.state.update({"waiting": 0,
                               "msg": ("denied" if decision == "deny"
                                       else "approved") + f": {tool}"})
            self.state.pop("prompt", None)
            await self._write(self.state)
            self._emit()
            return {"decision": decision}

        await self._write(self.state)
        self._emit()
        return {"decision": "once"}

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(self):
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

        server = await asyncio.start_unix_server(self._handle_hook, SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        print(f"Socket: {SOCKET_PATH}", flush=True)

        async with server:
            await asyncio.gather(
                server.serve_forever(),
                self._connect_loop(),
                self._heartbeat_loop(),
            )

    def cleanup(self):
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass


def _log(entries: list, msg: str):
    entries.insert(0, f"{datetime.now().strftime('%H:%M')} {msg}")
    del entries[8:]


def main():
    bridge = BuddyBridge()

    async def _run():
        task = asyncio.ensure_future(bridge.run())

        def _stop(_sig, _frame):
            print("\nShutting down…", flush=True)
            bridge.cleanup()
            task.cancel()

        signal.signal(signal.SIGINT,  _stop)
        signal.signal(signal.SIGTERM, _stop)

        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
