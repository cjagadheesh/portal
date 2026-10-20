"""Safety rails for the whole suite (hard rule 5: tests never touch real AWS).

Every test runs with: no AWS credentials or config from the developer's machine, a private
empty ~/.portal and working directory (so a real config.yaml or .portal.yaml cannot leak in),
and outbound network connections blocked. Loopback and Unix sockets stay open because some
tests run a fake metadata service and a real rsync daemon on 127.0.0.1.
"""

import ipaddress
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex


def _is_local(address: Any) -> bool:
    if isinstance(address, (str, bytes)):  # Unix socket path
        return True
    host = address[0]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _guard(real: Any) -> Any:
    def connect(self: socket.socket, address: Any) -> Any:
        if self.family != socket.AF_UNIX and not _is_local(address):
            raise RuntimeError(f"test tried to reach the network: {address!r}")
        return real(self, address)

    return connect


@pytest.fixture(autouse=True)
def isolated_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    sandbox: Path = tmp_path_factory.mktemp("sandbox")
    for var in (
        "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
        "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "PORTAL_REGION", "PORTAL_TIMEZONE", "PORTAL_CONTROL_PLANE_DIR",
    ):  # fmt: skip
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(sandbox / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(sandbox / "no-aws-credentials"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")  # obviously fake; moto needs something
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")  # never probe 169.254.169.254
    monkeypatch.setenv("PORTAL_HOME", str(sandbox / "portal-home"))
    monkeypatch.chdir(sandbox)
    monkeypatch.setattr(socket.socket, "connect", _guard(_REAL_CONNECT))
    monkeypatch.setattr(socket.socket, "connect_ex", _guard(_REAL_CONNECT_EX))
    yield
