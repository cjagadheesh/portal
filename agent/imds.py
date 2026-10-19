"""Minimal IMDSv2 client (the box's own metadata service). Standard library only."""

import time
import urllib.error
import urllib.request

BASE = "http://169.254.169.254"
TOKEN_TTL_S = 300


class Imds:
    """IMDSv2 requires a session token: PUT for a token, then send it with every GET.

    A token is cheap, so we keep one only for its short lifetime and fetch a new one when it
    expires or the service says 401 (for example after the instance's metadata was reset).
    """

    def __init__(self, base: str = BASE, timeout_s: float = 2.0) -> None:
        self.base = base
        self.timeout_s = timeout_s
        self._token = ""
        self._token_expires = 0.0

    def _fetch_token(self) -> str:
        request = urllib.request.Request(  # noqa: S310 - fixed http URL on the link-local address
            f"{self.base}/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": str(TOKEN_TTL_S)},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
            self._token = response.read().decode()
        # Renew a little early so a request never races the expiry.
        self._token_expires = time.monotonic() + TOKEN_TTL_S - 30
        return self._token

    def get(self, path: str) -> str | None:
        """Return the body, or None if the path does not exist (HTTP 404)."""
        for attempt in (1, 2):
            token = self._token if time.monotonic() < self._token_expires else self._fetch_token()
            request = urllib.request.Request(  # noqa: S310
                f"{self.base}{path}", headers={"X-aws-ec2-metadata-token": token}
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                    return str(response.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code == 401 and attempt == 1:
                    self._token_expires = 0.0  # stale token: fetch a new one and retry once
                    continue
                raise
        return None
