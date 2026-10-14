"""The home volume: an EBS volume mounted at /home/dev that survives `portal down`.

Spot boxes are cattle; your work is not. Keeping /home/dev on its own volume means an
interruption or a `portal down` costs you a reboot, not your files.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from portal.aws.ec2 import box_tags, rfc3339
from portal.errors import PortalError
from portal.state import Box

# Root volumes carry the same box tags, so this extra tag is what tells the two apart.
ROLE_TAG = {"Key": "portal:role", "Value": "home"}
# /dev/sdf is the conventional first data-disk slot. On Nitro it shows up as an NVMe
# device instead; bootstrap finds it by its EBS by-id link, not by this name.
DEVICE = "/dev/sdf"


@dataclass(frozen=True)
class HomeVolume:
    box_name: str
    volume_id: str
    az: str
    size_gb: int
    state: str  # available | in-use | creating | ...
    attached_to: str | None
    created_at: datetime
    expires_at: str = ""  # of the box that last used it; `up --resume` keeps counting from here
    last_instance_type: str = ""  # so a resume can try a different type first
    resumable: str = (
        ""  # "clean" / "dirty" as written by the drain handler, "in-use" while attached
    )


RESUMABLE_TAG = "portal:resumable"


def _tag(volume: dict[str, Any], key: str) -> str:
    return next((t["Value"] for t in volume.get("Tags", []) if t["Key"] == key), "")


def list_home_volumes(ec2: Any, owner: str, name: str | None = None) -> list[HomeVolume]:
    filters = [
        {"Name": "tag:portal:managed", "Values": ["true"]},
        {"Name": "tag:portal:owner", "Values": [owner]},
        {"Name": "tag:portal:role", "Values": ["home"]},
    ]
    if name is not None:
        filters.append({"Name": "tag:portal:box", "Values": [name]})
    return [
        HomeVolume(
            box_name=_tag(v, "portal:box"),
            volume_id=v["VolumeId"],
            az=v["AvailabilityZone"],
            size_gb=v["Size"],
            state=v["State"],
            attached_to=next((a["InstanceId"] for a in v.get("Attachments", [])), None),
            created_at=v["CreateTime"],
            expires_at=_tag(v, "portal:expires-at"),
            last_instance_type=_tag(v, "portal:last-instance-type"),
            resumable=_tag(v, RESUMABLE_TAG),
        )
        for page in ec2.get_paginator("describe_volumes").paginate(Filters=filters)
        for v in page["Volumes"]
    ]


def find_home_volume(ec2: Any, owner: str, name: str) -> HomeVolume | None:
    found = list_home_volumes(ec2, owner, name)
    if len(found) > 1:
        ids = ", ".join(v.volume_id for v in found)
        # Guessing which one holds your work is how work gets lost; make a human decide.
        raise PortalError(f"Box '{name}' has more than one home volume ({ids}). Delete the extras.")
    return found[0] if found else None


def check_reusable(volume: HomeVolume) -> None:
    """Fail before launching anything if the volume cannot be attached right now."""
    if volume.state == "in-use":
        raise PortalError(
            f"Home volume {volume.volume_id} is still attached to {volume.attached_to}. "
            "Wait for that box to finish terminating, then retry."
        )
    if volume.state != "available":
        raise PortalError(f"Home volume {volume.volume_id} is '{volume.state}'; retry shortly.")


def create_home_volume(
    ec2: Any, owner: str, name: str, az: str, size_gb: int, expires_at: datetime
) -> str:
    tags = [*box_tags(owner, name, expires_at), ROLE_TAG]
    volume_id = str(
        ec2.create_volume(
            ClientToken=uuid.uuid4().hex,  # idempotent: a retried request cannot make two volumes
            AvailabilityZone=az,
            Size=size_gb,
            VolumeType="gp3",
            TagSpecifications=[{"ResourceType": "volume", "Tags": tags}],  # tagged at creation
        )["VolumeId"]
    )
    ec2.get_waiter("volume_available").wait(VolumeIds=[volume_id])
    return volume_id


def attach_home(ec2: Any, box: Box, volume_id: str, expires_at: datetime) -> None:
    """Attach once the instance is running; EC2 refuses to attach to a pending one."""
    ec2.get_waiter("instance_running").wait(
        InstanceIds=[box.instance_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60}
    )
    # A reused volume still carries the previous box's expiry; keep rule 2's tag honest.
    # Also remember which instance type this was, so the next --resume can try another first,
    # and reset the drain handler's verdict: the volume is in use again, so "clean" is stale.
    ec2.create_tags(
        Resources=[volume_id],
        Tags=[
            {"Key": "portal:expires-at", "Value": rfc3339(expires_at)},
            {"Key": "portal:last-instance-type", "Value": box.instance_type},
            {"Key": RESUMABLE_TAG, "Value": "in-use"},
        ],
    )
    ec2.attach_volume(VolumeId=volume_id, InstanceId=box.instance_id, Device=DEVICE)


def orphans(volumes: list[HomeVolume], running_box_names: set[str]) -> list[HomeVolume]:
    """Home volumes whose box is not running: they still bill for storage every month."""
    return [v for v in volumes if v.box_name not in running_box_names]


def delete_home_volume(ec2: Any, volume_id: str) -> None:
    """Irreversible. Callers must have had the box name typed back to them first."""
    # Right after terminate the volume is still detaching; EC2 refuses to delete it until then.
    ec2.get_waiter("volume_available").wait(
        VolumeIds=[volume_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60}
    )
    ec2.delete_volume(VolumeId=volume_id)
