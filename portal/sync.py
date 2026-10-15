"""`portal sync`: push a local directory to /home/dev over rsync, through an SSM port-forward.

The box runs an rsync daemon bound to 127.0.0.1 (see bootstrap.py). We open an SSM
port-forward from a free local port to it, point rsync at that local port, and close the
tunnel afterwards. No inbound port, no SSH key.
"""

import json
import re
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from portal.bootstrap import RSYNC_PORT
from portal.errors import PortalError

# Both files use .gitignore syntax. `:-` is rsync's "per-directory merge file, as excludes",
# so nested ignore files work like they do in git. Negations (`!x`) are only approximated.
FILTERS = ["--filter=:- .gitignore", "--filter=:- .portalignore"]
# -r recurse, -l keep symlinks, -t keep mtimes (so unchanged files are skipped next time),
# -p keep the executable bit, -z compress. No -o/-g: the daemon already writes as `dev`.
# --no-inc-recursive makes rsync scan everything first, so progress2's percentage is real.
BASE_FLAGS = ["-rltpz", "--no-inc-recursive", "--info=progress2", *FILTERS]


def check_dest(dest: str) -> str:
    """The destination is a folder name under /home/dev; refuse anything that climbs out."""
    parts = Path(dest).parts
    if not dest or dest.startswith("/") or ".." in parts:
        raise PortalError(f"Invalid --dest '{dest}': use a relative folder name under /home/dev.")
    return dest


def check_rsync() -> str:
    """Return the rsync path, requiring 3.1+ (older ones, e.g. macOS's, lack --info=progress2)."""
    path = shutil.which("rsync")
    if path is None:
        raise PortalError("`portal sync` needs rsync 3.1+ on PATH.")
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [path, "--version"], capture_output=True, text=True, check=False
    ).stdout
    match = re.search(r"rsync\s+version (\d+)\.(\d+)", out)
    if match is None or (int(match[1]), int(match[2])) < (3, 1):
        raise PortalError(
            "`portal sync` needs rsync 3.1+ (found an older or different rsync). "
            "On macOS: `brew install rsync`."
        )
    return path


def push_args(rsync: str, src: Path, url: str) -> list[str]:
    # The trailing slash on src means "the contents of", so dest/ mirrors the folder itself.
    return [rsync, *BASE_FLAGS, f"{src}/", url]


def listing_args(rsync: str, src: Path) -> list[str]:
    """Local-only listing of exactly the files a push would consider, ignore rules applied."""
    return [rsync, "-r", "--list-only", *FILTERS, f"{src}/"]


def snapshot(rsync: str, src: Path) -> str:
    """A fingerprint of the tree (names, sizes, mtimes). It changes when a push is needed."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        listing_args(rsync, src), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise PortalError(f"rsync could not read {src}: {result.stderr.strip()}")
    return result.stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _can_connect(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


@contextmanager
def port_forward(
    aws_cli: str, instance_id: str, region: str, timeout_s: float = 30
) -> Iterator[int]:
    """Forward a free local port to the box's rsync daemon; always close the tunnel after."""
    port = free_port()
    params = {"portNumber": [str(RSYNC_PORT)], "localPortNumber": [str(port)]}
    # stderr goes to a file, not a pipe: a long --watch session must never block on a full pipe.
    with tempfile.TemporaryFile("w+") as errors:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [
                aws_cli, "ssm", "start-session",
                "--target", instance_id,
                "--region", region,
                "--document-name", "AWS-StartPortForwardingSession",
                "--parameters", json.dumps(params),
            ],
            stdout=subprocess.DEVNULL,
            stderr=errors,
        )  # fmt: skip
        try:
            deadline = time.monotonic() + timeout_s
            while not _can_connect(port):
                if proc.poll() is not None or time.monotonic() > deadline:
                    errors.seek(0)
                    detail = errors.read().strip() or "no output"
                    raise PortalError(f"Could not open the SSM port-forward to the box: {detail}")
                time.sleep(0.25)
            yield port
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


_PERCENT = re.compile(r"(\d+)%")


def run_rsync(args: list[str], on_percent: Callable[[int], None] | None = None) -> None:
    """Run one push. rsync redraws progress with carriage returns, so read raw chunks."""
    with tempfile.TemporaryFile("w+") as errors:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            args, stdout=subprocess.PIPE, stderr=errors, text=True
        )
        if proc.stdout is None:  # cannot happen: PIPE was requested above
            raise PortalError("rsync started without an output stream.")
        while chunk := proc.stdout.read(256):
            if on_percent is not None and (found := _PERCENT.findall(chunk)):
                on_percent(int(found[-1]))
        code = proc.wait()
        if code != 0:
            errors.seek(0)
            detail = errors.read().strip().splitlines()[-1:] or ["no output"]
            raise PortalError(
                f"rsync failed (exit {code}): {detail[0]}. The box may still be bootstrapping, "
                "or it was launched before `portal sync` existed; check "
                "/var/log/portal-bootstrap.log via `portal ssh`."
            )


def watch(
    take_snapshot: Callable[[], str],
    push: Callable[[], None],
    on_pushed: Callable[[], None],
    interval_s: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    max_ticks: int | None = None,
) -> None:
    """Push whenever the local tree changes. Polling a local listing costs no network."""
    last = take_snapshot()
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        sleep(interval_s)
        ticks += 1
        current = take_snapshot()
        if current != last:
            push()
            on_pushed()
            last = current
