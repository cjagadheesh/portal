"""SSM calls: AMI lookup (public parameters) and waiting for a box to register for sessions."""

import time
from collections.abc import Callable
from typing import Any

from portal.errors import PortalError

# Canonical publishes the current AMI id for each release/arch under this public parameter,
# so we look the id up per region instead of hardcoding one (Day 26 swaps in our own AMI).
_UBUNTU_AMI_PARAM = (
    "/aws/service/canonical/ubuntu/server/24.04/stable/current/{arch}/hvm/ebs-gp3/ami-id"
)


def latest_ubuntu_ami(ssm: Any, arch: str) -> str:
    """arch is Canonical's naming: "arm64" or "amd64"."""
    resp = ssm.get_parameter(Name=_UBUNTU_AMI_PARAM.format(arch=arch))
    return str(resp["Parameter"]["Value"])


def is_online(ssm: Any, instance_id: str) -> bool:
    """True once the SSM agent on the box has registered and is answering pings."""
    pages = ssm.get_paginator("describe_instance_information").paginate(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
    )
    return any(
        info["PingStatus"] == "Online" for page in pages for info in page["InstanceInformationList"]
    )


def wait_until_online(
    ssm: Any,
    instance_id: str,
    timeout_s: int,
    poll_s: float = 5,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    # sleep/clock are parameters only so tests can run the loop without really waiting.
    deadline = clock() + timeout_s
    while not is_online(ssm, instance_id):
        if clock() >= deadline:
            raise PortalError(
                f"{instance_id} did not register with SSM within {timeout_s}s. Usual causes: "
                "the box is still booting (retry in a minute), it has no outbound internet, or "
                "its instance profile lacks AmazonSSMManagedInstanceCore (re-run `portal init`)."
            )
        sleep(poll_s)
