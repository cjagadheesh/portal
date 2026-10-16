"""Idle detection for portal-agent. Runs ON the box, so it uses the standard library only.

THE RULE. The box is idle once nothing below has happened for `idle_timeout` (default 30
minutes, from config). Any one of these resets the idle clock:

  1. An SSM session is open. A person is connected, or `portal sync --watch` is running.
  2. The 5-minute load average is at least 0.5. Something is using CPU or waiting on disk.
     This is an absolute number, not per core: a single-threaded build on a 16-core box
     only reaches ~1.0, and dividing by the core count would call it idle.
  3. A `docker build` / `buildx build` / `compose build` client is running. Builds often
     wait on the network with almost no load, which is exactly the case 2 cannot see.
  4. ~/.portal-keepalive was touched recently. This is the escape hatch for the failure
     mode below: `touch ~/.portal-keepalive` restarts the idle clock from that moment.

WHAT WE DELIBERATELY DO NOT COUNT, and why:
  - Containers that are merely running. A forgotten database or redis would keep the box
    alive forever, which is the surprise bill this tool exists to prevent. If a container
    is doing work, it shows up as load (2). Quiet means idle.
  - A detached tmux/screen session or a `nohup` job. Whether a *process exists* says
    nothing about whether it is working; load (2) says that. Counting them would make a
    forgotten `tmux` keep the box up all night.
  - Network traffic, file changes and keystrokes. Noisy, easy to fake by accident (a
    background updater) and hard to measure reliably from a systemd timer.

THE FAILURE MODE WE ACCEPT. A job that is detached from every session, is not a docker
build, and mostly waits (a 40-minute test suite blocked on a slow API, load under 0.5) looks
idle, and the box shuts down `idle_timeout` after the last signal. We accept it because the
opposite error, never shutting down, costs money every hour and is invisible, while a
wrongly killed box costs a reboot: /home/dev lives on its own volume and survives. Use
`touch ~/.portal-keepalive` before walking away from such a job. The opposite failure,
an SSM session left open overnight, keeps the box alive until its TTL; the reaper is the
backstop for that.
"""

import os
import time
from dataclasses import dataclass
from pathlib import Path

LOAD_THRESHOLD = 0.5
BUILD_SUBCOMMANDS = {"build", "buildx", "builder"}  # `docker buildx build` has build at argv[2]
SESSION_WORKER = "ssm-session-worker"


@dataclass(frozen=True)
class Signals:
    """Everything the rule looks at, as plain numbers so the rule is testable off the box."""

    ssm_sessions: int
    load5: float
    docker_builds: int
    keepalive_mtime: float | None  # None if the file does not exist


@dataclass(frozen=True)
class Verdict:
    idle: bool
    reason: str
    last_active: float  # persist this and pass it back in on the next run


def _cmdlines(proc: Path) -> list[list[str]]:
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue  # the process exited while we were looking
        if raw:
            found.append(raw.decode(errors="replace").rstrip("\0").split("\0"))
    return found


def _is_docker_build(argv: list[str]) -> bool:
    if os.path.basename(argv[0]) not in ("docker", "docker-compose"):
        return False
    return any(arg in BUILD_SUBCOMMANDS for arg in argv[1:3])


def read_signals(
    proc: Path = Path("/proc"),
    keepalive: Path = Path("/home/dev/.portal-keepalive"),
) -> Signals:
    cmdlines = _cmdlines(proc)
    try:
        mtime: float | None = keepalive.stat().st_mtime
    except OSError:
        mtime = None
    return Signals(
        # The agent runs one ssm-session-worker per session (shells and port-forwards alike).
        ssm_sessions=sum(os.path.basename(c[0]) == SESSION_WORKER for c in cmdlines),
        load5=float((proc / "loadavg").read_text().split()[1]),
        docker_builds=sum(_is_docker_build(c) for c in cmdlines),
        keepalive_mtime=mtime,
    )


def evaluate(
    signals: Signals, last_active: float | None, now: float, idle_timeout_s: float
) -> Verdict:
    """Apply the rule. Pure: no clock, no files, so every branch can be unit tested."""
    reasons = []
    if signals.ssm_sessions:
        reasons.append(f"{signals.ssm_sessions} SSM session(s) open")
    if signals.load5 >= LOAD_THRESHOLD:
        reasons.append(f"load5 {signals.load5:.2f} >= {LOAD_THRESHOLD}")
    if signals.docker_builds:
        reasons.append(f"{signals.docker_builds} docker build(s) running")
    if reasons:
        return Verdict(False, "active: " + ", ".join(reasons), now)

    # Quiet right now. The clock restarts from the last time we saw activity, or from the
    # keepalive touch if that was later. First run ever: start the clock now, never shut
    # down a box we have only just met.
    started = now if last_active is None else last_active
    if signals.keepalive_mtime is not None:
        started = max(started, signals.keepalive_mtime)
    quiet_for = now - started
    if quiet_for >= idle_timeout_s:
        return Verdict(
            True, f"idle for {quiet_for / 60:.0f} min (limit {idle_timeout_s / 60:.0f})", started
        )
    return Verdict(
        False, f"quiet for {quiet_for / 60:.0f} of {idle_timeout_s / 60:.0f} min", started
    )


def load_last_active(path: Path) -> float | None:
    try:
        return float(path.read_text())
    except (OSError, ValueError):
        return None  # missing or corrupt: behave like a first run, which is the safe side


def save_last_active(path: Path, value: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(value))
    tmp.replace(path)  # rename is atomic, so a crash never leaves half a number


def check(state_file: Path, idle_timeout_s: float, now: float | None = None) -> Verdict:
    """One agent tick: read signals, decide, remember when we last saw activity."""
    moment = time.time() if now is None else now
    verdict = evaluate(read_signals(), load_last_active(state_file), moment, idle_timeout_s)
    save_last_active(state_file, verdict.last_active)
    return verdict
