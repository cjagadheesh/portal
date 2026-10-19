"""portal-watcher: a long-running service that waits for a spot interruption notice.

AWS gives a spot instance about two minutes' warning before reclaiming it, published at
/latest/meta-data/spot/instance-action (HTTP 404 until there is one). A systemd timer that
runs once a minute could miss most of that window, so this runs continuously and polls
every 5 seconds. On a notice it runs the drain (agent/drain.py) and exits.
"""

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent import drain
from agent.imds import Imds

ACTION_PATH = "/latest/meta-data/spot/instance-action"
POLL_S = 5
LOG_FILE = Path("/var/log/portal-agent.log")


def log(log_file: Path, message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a") as f:
        f.write(f"{stamp} watcher: {message}\n")


def poll(imds: Imds) -> dict[str, Any] | None:
    """The pending notice, or None. A body we cannot parse still counts as a notice."""
    raw = imds.get(ACTION_PATH)
    if raw is None:
        return None
    try:
        notice = json.loads(raw)
    except ValueError:
        return {"action": "unknown", "raw": raw}
    return dict(notice) if isinstance(notice, dict) else {"action": "unknown", "raw": raw}


def watch(
    imds: Imds,
    on_notice: Callable[[dict[str, Any]], None],
    log_file: Path,
    interval_s: float = POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    max_polls: int | None = None,
) -> dict[str, Any] | None:
    """Poll until a notice arrives (then run `on_notice` once and return it)."""
    polls = 0
    failing = False
    while max_polls is None or polls < max_polls:
        polls += 1
        try:
            notice = poll(imds)
            failing = False
        except OSError as e:
            # IMDS hiccups happen; keep trying, but log only the start of a bad streak.
            if not failing:
                log(log_file, f"WARN could not read metadata service: {e}")
            failing = True
            notice = None
        if notice is not None:
            log(log_file, f"SPOT INTERRUPTION NOTICE: {json.dumps(notice)}")
            on_notice(notice)
            return notice
        sleep(interval_s)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="portal-watcher")
    parser.add_argument("--log-file", type=Path, default=LOG_FILE)
    parser.add_argument("--dry-run", action="store_true", help="log the drain steps, run none")
    parser.add_argument(
        "--drain-now",
        action="store_true",
        help="skip waiting for a notice and drain immediately (for drills; unmounts /home/dev)",
    )
    args = parser.parse_args(argv)
    if args.drain_now:
        log(args.log_file, "drain requested by hand")
        drain.drain(drain.Context.real(args.log_file, dry_run=args.dry_run))
        return 0
    log(args.log_file, "started; polling every 5s")

    def on_notice(notice: dict[str, Any]) -> None:
        drain.drain(drain.Context.real(args.log_file, dry_run=args.dry_run))

    watch(Imds(), on_notice, args.log_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
