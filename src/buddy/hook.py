"""
buddy-hook — Claude Code hook side-car.

Called by Claude Code for each hook event; forwards it to buddy-bridge via a
Unix socket. Exits 0 (allow) if the daemon is unreachable so Claude Code is
never blocked by a missing bridge.

Configured in ~/.claude/settings.json — see hooks.json.

Environment variables:
  BUDDY_TIMEOUT   Seconds to wait for a deny on the device before
                  auto-approving.  0 = display only.  Default: 30.
  BUDDY_DEVICE    BLE device name prefix to scan for (default "Claude").
"""

import json
import os
import socket
import sys
import time

SOCKET_PATH      = f"/tmp/buddy-bridge-{os.getuid()}.sock"
CONNECT_TIMEOUT  = 2.0
APPROVAL_TIMEOUT = float(os.environ.get("BUDDY_TIMEOUT", "30"))


def _hint(tool: str, tool_input: dict) -> str:
    if tool == "Bash":
        return tool_input.get("command", "")[:80]
    if tool in ("Read", "Write", "Edit", "MultiEdit"):
        return tool_input.get("file_path", "")
    if tool == "WebFetch":
        return tool_input.get("url", "")
    if tool == "WebSearch":
        return tool_input.get("query", "")
    if tool == "Agent":
        return tool_input.get("description", "subagent")
    if tool == "TodoWrite":
        return "update todos"
    for v in tool_input.values():
        if isinstance(v, str) and v.strip():
            return v[:80]
    return ""


def _talk(payload: dict, *, want_response: bool) -> dict | None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect(SOCKET_PATH)
            sock.sendall(json.dumps(payload).encode())
            if not want_response:
                return None
            sock.settimeout(APPROVAL_TIMEOUT + 10)
            chunks: list[bytes] = []
            while chunk := sock.recv(4096):
                chunks.append(chunk)
            data = b"".join(chunks)
            return json.loads(data) if data else None
    except (OSError, ConnectionRefusedError):
        return None
    except json.JSONDecodeError:
        return None


def main():
    if len(sys.argv) < 2:
        sys.exit(0)

    mode = sys.argv[1]

    try:
        ctx = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        ctx = {}

    if mode == "pre-tool":
        tool       = ctx.get("tool_name", ctx.get("tool", "unknown"))
        tool_input = ctx.get("tool_input", {})
        use_id     = ctx.get("tool_use_id",
                              f"r{int(time.time() * 1000) % 10 ** 9}")
        result = _talk({
            "type":    "pre_tool",
            "id":      use_id,
            "tool":    tool,
            "hint":    _hint(tool, tool_input),
            "timeout": APPROVAL_TIMEOUT,
        }, want_response=True)

        if result and result.get("decision") == "deny":
            print(f"Denied on hardware buddy ({tool})", file=sys.stderr)
            sys.exit(2)

    elif mode == "post-tool":
        tool  = ctx.get("tool_name", ctx.get("tool", "unknown"))
        hint  = _hint(tool, ctx.get("tool_input", {}))
        entry = f"{tool}: {hint}"[:60]
        _talk({"type": "post_tool", "tool": tool, "entry": entry,
               "msg": "Claude Code"}, want_response=False)

    elif mode == "stop":
        _talk({"type": "stop"}, want_response=False)

    sys.exit(0)
