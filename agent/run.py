"""portal-agent entry point. systemd runs one tick every 60 seconds (see portal/bootstrap.py).

    python3 -m agent.run --idle-timeout-minutes 30 [--dry-run]

--dry-run does everything except power off, which is the safe way to check the agent on a
real box: `sudo python3 -m agent.run --dry-run` then read /var/log/portal-agent.log.
"""

import argparse
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from agent import expiry, idle
from agent.imds import Imds

LOG_FILE = Path("/var/log/portal-agent.log")
STATE_FILE = Path("/var/lib/portal/last-active")
WARNED_FILE = Path("/var/lib/portal/expiry-warned")


def power_off(reason: str) -> None:
    # On a portal box the launch template sets InstanceInitiatedShutdownBehavior=terminate,
    # so this ends the instance (and its root disk) instead of leaving a stopped one.
    subprocess.run(["shutdown", "-h", "now", f"portal-agent: {reason}"], check=False)  # noqa: S603, S607


def log(log_file: Path, message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a") as f:
        f.write(f"{stamp} {message}\n")


def tick(
    idle_timeout_s: float,
    state_file: Path,
    log_file: Path,
    dry_run: bool = False,
    shutdown: Callable[[str], None] = power_off,
) -> bool:
    """One check. Returns True if it decided to shut the box down."""
    try:
        verdict = idle.check(state_file, idle_timeout_s)
    except Exception:  # log every failure; the next tick retries
        log(log_file, "ERROR " + traceback.format_exc().strip().replace("\n", " | "))
        raise
    if not verdict.idle:
        log(log_file, f"ok: {verdict.reason}")
        return False
    if dry_run:
        log(log_file, f"DRY RUN would shut down: {verdict.reason}")
        return False
    log(log_file, f"SHUTTING DOWN: {verdict.reason}")  # logged first: the box is about to vanish
    shutdown(verdict.reason)
    return True


def warn_expiry(log_file: Path, warned_file: Path = WARNED_FILE) -> None:
    """Tell attached sessions the box is about to expire. Never allowed to break the agent."""
    try:
        sessions = idle.read_signals().ssm_sessions
        sent = expiry.check(Imds(), sessions, warned_file)
        if sent:
            log(log_file, f"warned sessions: {sent}")
    except OSError as e:  # includes URLError: IMDS unreachable (not on EC2, or blocked)
        log(log_file, f"WARN expiry warnings skipped: {e}")
    except Exception:  # log and carry on: a failed warning must not stop idle shutdown
        log(
            log_file, "ERROR expiry warning: " + traceback.format_exc().strip().replace("\n", " | ")
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="portal-agent")
    parser.add_argument("--idle-timeout-minutes", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state-file", type=Path, default=STATE_FILE)
    parser.add_argument("--log-file", type=Path, default=LOG_FILE)
    args = parser.parse_args(argv)
    shutting_down = tick(
        args.idle_timeout_minutes * 60, args.state_file, args.log_file, args.dry_run
    )
    if not shutting_down:
        warn_expiry(args.log_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
