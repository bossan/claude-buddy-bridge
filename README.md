# claude-buddy-bridge

Python host-side bridge for the **Claude desk buddy** — a small BLE device
that shows live Claude Code activity on its display and can optionally gate
tool calls with a hardware button press.

Two CLI programs ship here:

**`buddy-bridge`** — persistent daemon that maintains a BLE connection to the
device, sends heartbeats every 5 seconds so the display stays live, syncs the
device clock on connect, and listens on a Unix socket for events from
`buddy-hook`.

**`buddy-hook`** — called by Claude Code for every hook event
(`PreToolUse`, `PostToolUse`, `Stop`). It forwards each event to
`buddy-bridge` over the Unix socket and exits immediately. If the bridge is
not running, `buddy-hook` exits 0 so Claude Code is never blocked by a
missing daemon.

## Requirements

- macOS (BLE via CoreBluetooth / `bleak`)
- Python 3.11+
- [uv](https://github.com/astral-sh/uv)

## Installation

From this repository root:

```bash
uv tool install .
```

This installs both `buddy-bridge` and `buddy-hook` into your uv tool bin
directory (usually `~/.local/bin`).

## Quick start

**1. Start the bridge** (once per login session, before starting Claude Code):

```bash
buddy-bridge &
```

The bridge scans for a BLE device whose name starts with `Claude`, connects,
and starts sending heartbeats. It reconnects automatically on disconnect.

**2. Wire up Claude Code hooks** — merge the block below into
`~/.claude/settings.json` (or merge from [`hooks.json`](hooks.json)):

```json
{
  "permissions": {
    "allow": ["Bash(*)", "Read(*)", "Edit(*)"]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": ".*",
        "hooks": [{ "type": "command", "command": "buddy-hook pre-tool" }]
      }
    ],
    "PostToolUse": [
      {
        "matcher": ".*",
        "hooks": [{ "type": "command", "command": "buddy-hook post-tool" }]
      }
    ],
    "Stop": [
      {
        "hooks": [{ "type": "command", "command": "buddy-hook stop" }]
      }
    ]
  }
}
```

## Environment variables

| Variable        | Default   | Description |
| --------------- | --------- | ----------- |
| `BUDDY_TIMEOUT` | `0`       | Seconds to wait for a deny on the device before auto-approving a `PreToolUse`. `0` = display-only mode; the hook never blocks Claude Code. |
| `BUDDY_DEVICE`  | `Claude`  | BLE device name prefix to scan for. |

Example — enable hardware approval with a 30-second timeout:

```bash
BUDDY_TIMEOUT=30 buddy-bridge &
```

## Approval flow

When `BUDDY_TIMEOUT > 0`, `buddy-hook pre-tool` waits up to that many seconds
for a button press on the device before returning. Press **A** (front button)
to approve or **B** (right button) to deny. On timeout the tool is
auto-approved. If the bridge is unreachable, tools are always auto-approved.

## How it works

```
Claude Code
  │  PreToolUse / PostToolUse / Stop
  ▼
buddy-hook          (short-lived process, one per event)
  │  Unix socket  /tmp/buddy-bridge-<uid>.sock
  ▼
buddy-bridge        (long-lived daemon)
  │  BLE / Nordic UART Service
  ▼
Claude desk buddy
```

The socket is created by `buddy-bridge` and is readable only by the owning
user (`chmod 600`). Messages are newline-terminated JSON.

## Development

```bash
uv sync
uv run buddy-bridge   # run without installing
```
