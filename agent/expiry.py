"""Warn people connected to the box as its expiry approaches (30, 10 and 5 minutes out).

The box reads its own `portal:expires-at` tag from IMDS (the launch template enables
instance metadata tags), so this needs no AWS credentials and no API call. `portal extend`
changes the tag; the next tick sees the new time and re-arms the warnings.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.imds import Imds

THRESHOLDS_MIN = (30, 10, 5)
TAG_PATH = "/latest/meta-data/tags/instance/portal:expires-at"
PTS_DIR = Path("/dev/pts")


def read_expiry(imds: Imds) -> datetime | None:
    raw = imds.get(TAG_PATH)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return None if parsed.tzinfo is None else parsed.astimezone(UTC)


def next_warning(remaining: timedelta, warned: frozenset[int]) -> tuple[str | None, frozenset[int]]:
    """Decide whether to warn now. Returns (message or None, thresholds to remember).

    Remembering exactly the thresholds that are currently crossed means an extension
    (which moves the expiry away from them) quietly re-arms the warnings. If the agent was
    late and several are crossed at once, one message goes out, not three.
    """
    minutes = remaining.total_seconds() / 60
    crossed = frozenset(t for t in THRESHOLDS_MIN if minutes <= t)
    if not crossed - warned:
        return None, crossed
    if minutes <= 0:
        return (
            "portal: this box has reached its expiry and may be terminated at any moment.",
            crossed,
        )
    return (
        f"portal: this box expires in about {max(1, round(minutes))} min. "
        "Run `portal extend 1h` from your laptop to keep it.",
        crossed,
    )


def broadcast(message: str, pts_dir: Path = PTS_DIR) -> int:
    """Write to every terminal. `wall` relies on login records that SSM sessions may not
    create, so write to the pseudo-terminals directly. Returns how many were reached."""
    delivered = 0
    for pts in pts_dir.iterdir():
        if not pts.name.isdigit():
            continue
        try:
            with pts.open("w") as terminal:
                terminal.write(f"\r\n*** {message} ***\r\n")
            delivered += 1
        except OSError:
            continue  # not ours to write to, or the session just closed
    return delivered


def load_warned(path: Path) -> frozenset[int]:
    try:
        return frozenset(int(x) for x in json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return frozenset()


def save_warned(path: Path, warned: frozenset[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(warned)))
    tmp.replace(path)


def check(
    imds: Imds,
    sessions: int,
    state_file: Path,
    now: datetime | None = None,
    pts_dir: Path = PTS_DIR,
) -> str | None:
    """One tick. Returns the message sent, if any. Nobody connected means nothing is
    sent and nothing is remembered, so someone who connects later still hears it."""
    expiry = read_expiry(imds)
    if expiry is None or sessions == 0:
        return None
    message, remember = next_warning(expiry - (now or datetime.now(UTC)), load_warned(state_file))
    if message is None:
        save_warned(state_file, remember)  # also re-arms after an extension
        return None
    broadcast(message, pts_dir)
    save_warned(state_file, remember)
    return message
