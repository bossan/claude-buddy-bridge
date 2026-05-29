import os
import tempfile
from pathlib import Path


def socket_path() -> Path:
    # tempfile.gettempdir() honours $TMPDIR. On macOS that resolves to a
    # per-user directory under /var/folders/ with 0700 perms, which is
    # stronger than relying on chmod 600 inside a shared /tmp.
    return Path(tempfile.gettempdir()) / f"buddy-bridge-{os.getuid()}.sock"
