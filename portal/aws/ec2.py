"""EC2 calls: launch a tagged spot box, find boxes again by tag, terminate them."""

import base64
import contextlib
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from portal import bootstrap
from portal.aws import session
from portal.aws import ssm as ssm_calls
from portal.errors import PortalError
from portal.state import Box, ControlPlane

_BOX_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
# Canonical's AMI parameters use Debian-style arch names, EC2 uses its own.
_CANONICAL_ARCH = {"arm64": "arm64", "x86_64": "amd64"}


def client(service: str, region: str) -> Any:
    return session.client(service, region)


def check_box_name(name: str) -> None:
    # The name ends up in a tag value and a launch template name; keep it boring and safe.
    if not _BOX_NAME.fullmatch(name):
        raise PortalError(
            f"Invalid box name '{name}'. Use lowercase letters, digits and hyphens (max 31)."
        )


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def box_tags(owner: str, name: str, expires_at: datetime) -> list[dict[str, str]]:
    """Hard rule 2: the four tags the reaper and every command rely on."""
    return [
        {"Key": "portal:managed", "Value": "true"},
        {"Key": "portal:owner", "Value": owner},
        {"Key": "portal:box", "Value": name},
        {"Key": "portal:expires-at", "Value": rfc3339(expires_at)},
    ]


def find_boxes(ec2: Any, owner: str, name: str | None = None) -> list[Box]:
    """Boxes are found by tag, never by remembered ids: AWS is the source of truth."""
    filters = [
        {"Name": "tag:portal:managed", "Values": ["true"]},
        {"Name": "tag:portal:owner", "Values": [owner]},
        {"Name": "instance-state-name", "Values": ["pending", "running"]},
    ]
    if name:
        filters.append({"Name": "tag:portal:box", "Values": [name]})
    boxes = []
    for page in ec2.get_paginator("describe_instances").paginate(Filters=filters):
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                boxes.append(
                    Box(
                        name=tags.get("portal:box", ""),
                        instance_id=inst["InstanceId"],
                        instance_type=inst["InstanceType"],
                        az=inst["Placement"]["AvailabilityZone"],
                        state=inst["State"]["Name"],
                        expires_at=tags.get("portal:expires-at", ""),
                        launched_at=rfc3339(inst["LaunchTime"]) if "LaunchTime" in inst else "",
                        market="spot" if inst.get("InstanceLifecycle") == "spot" else "on-demand",
                    )
                )
    return boxes


def canonical_arch(ec2: Any, instance_types: list[str]) -> str:
    """One fleet can only run one CPU architecture, because it boots one AMI."""
    archs = set()
    for page in ec2.get_paginator("describe_instance_types").paginate(InstanceTypes=instance_types):
        for info in page["InstanceTypes"]:
            supported = info["ProcessorInfo"]["SupportedArchitectures"]
            archs.add("arm64" if "arm64" in supported else "x86_64")
    if len(archs) != 1:
        raise PortalError(
            "All --instance-type values must share one CPU architecture "
            "(arm64 and x86_64 need different AMIs)."
        )
    return _CANONICAL_ARCH[archs.pop()]


_EC2_ARCH = {"arm64": "arm64", "amd64": "x86_64"}


def find_baked_ami(ec2: Any, prefix: str, arch: str) -> str | None:
    """Newest AMI that *this account* built with Packer, found by name. Owners=["self"] means
    no account id is written anywhere, and only our own images can ever match."""
    if not prefix:
        return None
    images = [
        image
        for page in ec2.get_paginator("describe_images").paginate(
            Owners=["self"],
            Filters=[
                {"Name": "name", "Values": [f"{prefix}*"]},
                {"Name": "architecture", "Values": [_EC2_ARCH[arch]]},
                {"Name": "state", "Values": ["available"]},
            ],
        )
        for image in page["Images"]
    ]
    return str(max(images, key=lambda i: i["CreationDate"])["ImageId"]) if images else None


def public_subnets(ec2: Any, security_group_id: str) -> list[dict[str, str]]:
    """Subnets in the control plane's VPC that give boxes a public IP (needed to reach SSM)."""
    vpc_ids = [
        sg["VpcId"]
        for page in ec2.get_paginator("describe_security_groups").paginate(
            GroupIds=[security_group_id]
        )
        for sg in page["SecurityGroups"]
    ]
    subnets = [
        {"SubnetId": s["SubnetId"], "AvailabilityZone": s["AvailabilityZone"]}
        for page in ec2.get_paginator("describe_subnets").paginate(
            Filters=[{"Name": "vpc-id", "Values": vpc_ids}]
        )
        for s in page["Subnets"]
        if s.get("MapPublicIpOnLaunch")
    ]
    if not subnets:
        raise PortalError(
            "No subnet with auto-assigned public IPs in the control-plane VPC, so the box "
            "could not reach SSM. Use a VPC with a public subnet (the default VPC has them)."
        )
    return subnets


def _root_device_name(ec2: Any, ami: str) -> str:
    """The root disk can only be resized by naming its device, which differs per AMI."""
    for page in ec2.get_paginator("describe_images").paginate(ImageIds=[ami]):
        for image in page["Images"]:
            return str(image["RootDeviceName"])
    raise PortalError(f"AMI {ami} not found in this region.")


def explain_fleet_errors(errors: list[dict[str, Any]], instance_types: list[str]) -> str:
    codes = {e["ErrorCode"] for e in errors}
    if any("capacity" in c.lower() for c in codes):
        tried = ", ".join(instance_types)
        return (
            f"No spot capacity for: {tried}. Add another fallback with --instance-type "
            "(a different size or family usually has its own capacity pool), or retry later."
        )
    if "MaxSpotInstanceCountExceeded" in codes:
        return (
            "Your spot instance quota is exhausted. "
            "Terminate other spot instances or request a quota increase."
        )
    return "; ".join(f"{e['ErrorCode']}: {e.get('ErrorMessage', '')}" for e in errors)


def launch_box(
    ec2: Any,
    ssm: Any,
    cp: ControlPlane,
    owner: str,
    name: str,
    instance_types: list[str],
    expires_at: datetime,
    timezone: str = "UTC",
    disk_gb: int = 30,
    az: str | None = None,
    idle_timeout_minutes: int = 30,
    on_attempt: Callable[[str, str, str], None] | None = None,
    confirm_on_demand: Callable[[list[str]], bool] | None = None,
    ami_prefix: str = "",
) -> Box:
    """Launch one box, trying `instance_types` in order on spot, then (only if the caller
    agrees) on-demand. `az` pins it next to an existing home volume (EBS is per-AZ).

    on_attempt(type, market, outcome) reports every try; confirm_on_demand(types) must return
    True before a single on-demand instance is started, because that costs real money.
    """
    user_data = bootstrap.render(timezone, idle_timeout_minutes)  # validate before touching AWS
    arch = canonical_arch(ec2, instance_types)
    baked = find_baked_ami(ec2, ami_prefix, arch)
    # A baked image boots without installing packages; otherwise fall back to stock Ubuntu.
    ami = baked or ssm_calls.latest_ubuntu_ami(ssm, arch)
    root_device = _root_device_name(ec2, ami)
    subnets = public_subnets(ec2, cp.security_group_id)
    if az is not None:
        subnets = [s for s in subnets if s["AvailabilityZone"] == az]
        if not subnets:
            raise PortalError(
                f"Your home volume is in {az}, but there is no public subnet there to launch into."
            )
    tags = box_tags(owner, name, expires_at)

    # create_fleet cannot take an inline launch spec, so a throwaway launch template carries
    # it. The tags live in the template's TagSpecifications, which makes EC2 apply them as the
    # instance is created: a crash cannot leave an untagged instance the reaper would miss.
    template_id = ec2.create_launch_template(
        # A client token makes the request idempotent: if the response is lost and botocore
        # retries, AWS returns the same template instead of creating a second one.
        ClientToken=uuid.uuid4().hex,
        LaunchTemplateName=f"portal-{name}-{uuid.uuid4().hex[:8]}",
        LaunchTemplateData={
            "ImageId": ami,
            "IamInstanceProfile": {"Name": cp.instance_profile_name},
            "SecurityGroupIds": [cp.security_group_id],
            # Resize the root disk; it is still deleted with the box (work lives on /home/dev).
            "BlockDeviceMappings": [
                {
                    "DeviceName": root_device,
                    "Ebs": {
                        "VolumeSize": disk_gb,
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                    },
                }
            ],
            # Launch templates want user data base64-encoded (run_instances does it for you).
            "UserData": base64.b64encode(user_data.encode()).decode(),
            # `shutdown -h` (the idle agent) must end the instance, not leave a stopped one
            # that keeps billing for its disks.
            "InstanceInitiatedShutdownBehavior": "terminate",
            # IMDSv2 only: blocks the classic SSRF-steals-credentials attack.
            # InstanceMetadataTags lets the on-box agent read this box's own tags (its expiry)
            # from IMDS, with no IAM permission and no API call.
            "MetadataOptions": {"HttpTokens": "required", "InstanceMetadataTags": "enabled"},
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": tags},
                {"ResourceType": "volume", "Tags": tags},
                # Rule 2: the box's network interface is a resource we create too.
                {"ResourceType": "network-interface", "Tags": tags},
            ],
        },
        TagSpecifications=[{"ResourceType": "launch-template", "Tags": tags}],
    )["LaunchTemplate"]["LaunchTemplateId"]

    def attempt(instance_type: str, market: str) -> tuple[Box | None, list[dict[str, Any]]]:
        """One create_fleet call for one instance type, spread over every allowed subnet."""
        capacity: dict[str, Any] = {"TotalTargetCapacity": 1, "DefaultTargetCapacityType": market}
        options: dict[str, Any] = (
            {"SpotOptions": {"AllocationStrategy": "capacity-optimized"}}
            if market == "spot"
            else {"OnDemandOptions": {"AllocationStrategy": "lowest-price"}}
        )
        # "instant" returns the result synchronously instead of retrying in the background.
        resp = ec2.create_fleet(
            # Fresh token per attempt, reused by botocore's own retries of that attempt. This is
            # the cost-critical one: without it, a timeout on a successful create_fleet followed
            # by an automatic retry could launch TWO instances and only track one.
            ClientToken=uuid.uuid4().hex,
            Type="instant",
            LaunchTemplateConfigs=[
                {
                    "LaunchTemplateSpecification": {
                        "LaunchTemplateId": template_id,
                        "Version": "$Latest",
                    },
                    "Overrides": [
                        {"InstanceType": instance_type, "SubnetId": s["SubnetId"]} for s in subnets
                    ],
                }
            ],
            TargetCapacitySpecification=capacity,
            TagSpecifications=[{"ResourceType": "fleet", "Tags": tags}],
            **options,
        )
        launched = [i for i in resp.get("Instances", []) if i.get("InstanceIds")]
        if not launched:
            return None, list(resp.get("Errors", []))
        first = launched[0]
        subnet_id = first["LaunchTemplateAndOverrides"]["Overrides"]["SubnetId"]
        zone = next(s["AvailabilityZone"] for s in subnets if s["SubnetId"] == subnet_id)
        return (
            Box(
                name=name,
                instance_id=first["InstanceIds"][0],
                instance_type=first["InstanceType"],
                az=zone,
                state="pending",
                expires_at=rfc3339(expires_at),
                launched_at=rfc3339(datetime.now(UTC)),
                market=market,
                image="baked" if baked else "stock",
            ),
            [],
        )

    def walk(market: str) -> Box | None:
        """Try the types in the order the user listed them: that order is their preference."""
        for instance_type in instance_types:
            box, errors = attempt(instance_type, market)
            if box is not None:
                if on_attempt:
                    on_attempt(instance_type, market, "launched")
                return box
            if not _is_capacity_problem(errors):
                raise PortalError(explain_fleet_errors(errors, instance_types))
            if on_attempt:
                on_attempt(instance_type, market, "no capacity")
        return None

    try:
        box = walk("spot")
        if box is None and confirm_on_demand is not None and confirm_on_demand(instance_types):
            box = walk("on-demand")
    finally:
        # The instance no longer needs the template once the fleet calls are done. Best effort:
        # a leftover template is free, tagged, and must never turn a successful launch into an
        # error that leaves a running box behind.
        with contextlib.suppress(ClientError, BotoCoreError):
            ec2.delete_launch_template(LaunchTemplateId=template_id)
    if box is None:
        tried = ", ".join(instance_types)
        raise PortalError(
            f"No spot capacity for: {tried}. Add another fallback with --instance-type "
            "(a different size or family usually has its own capacity pool), or retry later."
        )
    return box


# Codes that mean "this type cannot be had here right now", so the next type may work: no
# capacity, not offered in this AZ, or the spot price is above what is allowed.
_TRY_NEXT_TYPE = {"Unsupported", "UnsupportedOperation", "SpotMaxPriceTooLow"}


def _is_capacity_problem(errors: list[dict[str, Any]]) -> bool:
    """Capacity-style errors mean "try the next type". Anything else (quota, permissions, a
    bad request) would fail the same way for every type, so it stops the walk instead."""
    if not errors:
        return True  # an empty answer from an instant fleet means nothing was available
    return all(
        "capacity" in e.get("ErrorCode", "").lower() or e.get("ErrorCode") in _TRY_NEXT_TYPE
        for e in errors
    )


def terminate_boxes(ec2: Any, boxes: list[Box]) -> None:
    # Ids come from find_boxes, i.e. only instances carrying our managed+owner tags.
    ec2.terminate_instances(InstanceIds=[b.instance_id for b in boxes])


def spot_price(ec2: Any, instance_type: str, az: str) -> float | None:
    """Latest spot price in USD/hr for this type in this AZ; None if AWS reports none."""
    newest: tuple[datetime, float] | None = None
    pages = ec2.get_paginator("describe_spot_price_history").paginate(
        InstanceTypes=[instance_type],
        AvailabilityZone=az,
        ProductDescriptions=["Linux/UNIX"],
        StartTime=datetime.now(UTC) - timedelta(hours=1),
    )
    for page in pages:
        for point in page["SpotPriceHistory"]:
            if newest is None or point["Timestamp"] > newest[0]:
                newest = (point["Timestamp"], float(point["SpotPrice"]))
    return None if newest is None else newest[1]


def aws_now(ec2: Any) -> datetime:
    """The time according to AWS, from the HTTP Date header of any API response.

    The reaper compares expiries against AWS time, so computing them from AWS time too means
    a wrong laptop clock cannot make a box live longer or die earlier than asked. Falls back
    to the local clock if the header is missing.
    """
    # describe_availability_zones has no paginator: it returns one small, bounded list.
    resp = ec2.describe_availability_zones()
    header = resp.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("date")
    try:
        return parsedate_to_datetime(header).astimezone(UTC) if header else datetime.now(UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def set_expiry(ec2: Any, box: Box, expires_at: datetime) -> None:
    """Move a box's expiry. The instance tag is what the reaper reads; the disks follow it."""
    volume_ids = [
        v["VolumeId"]
        for page in ec2.get_paginator("describe_volumes").paginate(
            Filters=[{"Name": "attachment.instance-id", "Values": [box.instance_id]}]
        )
        for v in page["Volumes"]
    ]
    ec2.create_tags(
        Resources=[box.instance_id, *volume_ids],
        Tags=[{"Key": "portal:expires-at", "Value": rfc3339(expires_at)}],
    )
