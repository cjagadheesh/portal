"""Pure helpers for `portal status`: TTL and cost maths, free of AWS so they test easily."""

from datetime import UTC, datetime, timedelta

AMBER_UNDER = timedelta(minutes=30)
RED_UNDER = timedelta(minutes=10)


def parse_rfc3339(text: str) -> datetime | None:
    """Tags are plain strings anyone can edit; a bad value must not crash `status`."""
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def ttl_remaining(expires_at: str, now: datetime) -> timedelta | None:
    expiry = parse_rfc3339(expires_at)
    return None if expiry is None else expiry - now


def ttl_style(remaining: timedelta | None) -> str:
    # Red below 10 minutes (including already expired), amber below 30, otherwise green.
    if remaining is None or remaining < RED_UNDER:
        return "red"
    return "yellow" if remaining < AMBER_UNDER else "green"


def format_duration(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 0:
        return "expired"
    hours, rest = divmod(total, 3600)
    return f"{hours}h{rest // 60:02d}m"


def uptime(launched_at: str, now: datetime) -> timedelta | None:
    launched = parse_rfc3339(launched_at)
    return None if launched is None else max(now - launched, timedelta(0))


def cost_so_far(up: timedelta | None, hourly_price: float | None) -> float | None:
    """Estimate: current spot price times uptime. Spot prices drift, so it is approximate."""
    if up is None or hourly_price is None:
        return None
    return up.total_seconds() / 3600 * hourly_price
