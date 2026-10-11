"""Re-learn the control plane's names from AWS tags, with no local state and no Terraform."""

from typing import Any

from portal.errors import PortalError
from portal.state import ControlPlane


def discover(ec2: Any, iam: Any, region: str) -> ControlPlane | None:
    """Find the control plane on a fresh machine, or after ~/.portal/state.json is deleted.

    The control-plane security group is the only one tagged portal:managed without a
    portal:box tag (boxes never create security groups). Terraform names the group and the
    instance profile alike (`<prefix>-box`) and the budget `<prefix>-monthly`, so the group's
    name gives us the other two.
    """
    groups = [
        sg
        for page in ec2.get_paginator("describe_security_groups").paginate(
            Filters=[{"Name": "tag:portal:managed", "Values": ["true"]}]
        )
        for sg in page["SecurityGroups"]
        if not any(t["Key"] == "portal:box" for t in sg.get("Tags", []))
    ]
    if not groups:
        return None
    if len(groups) > 1:
        ids = ", ".join(g["GroupId"] for g in groups)
        raise PortalError(f"Found several portal control planes in {region} ({ids}); expected one.")
    group = groups[0]
    profile = group["GroupName"]
    try:
        iam.get_instance_profile(InstanceProfileName=profile)
    except iam.exceptions.NoSuchEntityException:
        return None  # half-deleted control plane: better to say "run init" than to guess
    return ControlPlane(
        region=region,
        instance_profile_name=profile,
        security_group_id=group["GroupId"],
        budget_name=f"{profile.removesuffix('-box')}-monthly",
    )
