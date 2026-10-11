"""Small filesystem helpers for private runtime state."""

from __future__ import annotations

import os
from pathlib import Path


def private_file(path: Path) -> Path:
    """Ensure ``path`` exists with owner-only (0600) permissions and a 0700 parent.

    SQLite derives the permissions of its -wal/-shm sidecars from the main file,
    so securing the database file before the first connect covers them too.
    Message text (outbox) and memory facts live in these files.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    return path
