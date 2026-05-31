import os
from pathlib import Path


def socket_path() -> Path:
    # Use XDG_RUNTIME_DIR when available; fall back to ~/.local/run/.
    # Deliberately avoids tempfile.gettempdir() / $TMPDIR because Claude Code
    # overrides $TMPDIR for hook processes, causing the hook and daemon to
    # resolve different paths and never find each other.
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "buddy-bridge.sock"
    base = Path.home() / ".local" / "run"
    base.mkdir(parents=True, exist_ok=True)
    return base / "buddy-bridge.sock"
