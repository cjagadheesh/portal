"""A per-box lock, so two `portal` commands on one machine cannot race on the same box.

Without it, two `portal up --name dev` run at once can both see "no box named dev" and launch
two (the second one's expiry and home volume would then fight the first). The lock is an
advisory flock on a file under ~/.portal, released by the OS if the process dies, so a crash
can never leave a stale lock behind.

It protects one machine. Two laptops racing is still possible; the window is the few seconds
between checking for a box and tagging the new one, and the reaper bounds the damage.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from portal.errors import PortalError


def lock_dir() -> Path:
    return Path(os.environ.get("PORTAL_HOME", Path.home() / ".portal")) / "locks"


@contextmanager
def box_lock(name: str) -> Iterator[None]:
    directory = lock_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = (directory / f"{name}.lock").open("a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PortalError(
                f"Another portal command is already working on box '{name}'. "
                "Wait for it to finish (or press Ctrl+C in that terminal)."
            ) from None
        yield
    finally:
        handle.close()  # closing releases the lock
