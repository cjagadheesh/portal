"""Parse TTL strings like "4h", "90m", "1h30m" into a timedelta."""

import re
from datetime import timedelta

from portal.errors import PortalError

_TTL = re.compile(r"(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?")


def parse_ttl(text: str) -> timedelta:
    match = _TTL.fullmatch(text.strip().lower())
    if not match or not text.strip():
        raise PortalError(f"Invalid TTL '{text}'. Use hours and/or minutes, e.g. 4h, 90m, 1h30m.")
    ttl = timedelta(hours=int(match["h"] or 0), minutes=int(match["m"] or 0))
    if ttl <= timedelta(0):
        raise PortalError("TTL must be longer than zero.")
    return ttl
