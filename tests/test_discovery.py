from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from portal import state
from portal.aws import control_plane
from portal.cli import _control_plane
from portal.errors import PortalError

REGION = "us-east-1"
TAGS = [{"Key": "portal:managed", "Value": "true"}, {"Key": "portal:owner", "Value": "someone"}]


@pytest.fixture
def clouds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Any, Any]]:
    """An account with a control plane already applied, and a laptop that has never seen it."""
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("PORTAL_REGION", raising=False)
    monkeypatch.chdir(tmp_path)
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        iam = boto3.client("iam", region_name=REGION)
        yield ec2, iam


def apply_control_plane(ec2: Any, iam: Any, prefix: str = "portal") -> str:
    vpc = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
    sg = ec2.create_security_group(
        GroupName=f"{prefix}-box",
        Description="t",
        VpcId=vpc,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": TAGS}],
    )
    iam.create_instance_profile(InstanceProfileName=f"{prefix}-box")
    return str(sg["GroupId"])


def test_second_laptop_finds_the_control_plane_by_tag(clouds: tuple[Any, Any]) -> None:
    ec2, iam = clouds
    sg_id = apply_control_plane(ec2, iam)
    cp = _control_plane()  # no state.json, no terraform state: only AWS knows
    assert cp.security_group_id == sg_id
    assert cp.instance_profile_name == "portal-box"
    assert cp.budget_name == "portal-monthly"
    assert cp.region == REGION
    assert state.load_control_plane() == cp  # cached for next time


def test_custom_prefix_is_followed(clouds: tuple[Any, Any]) -> None:
    ec2, iam = clouds
    apply_control_plane(ec2, iam, prefix="team")
    cp = control_plane.discover(ec2, iam, REGION)
    assert cp is not None
    assert (cp.instance_profile_name, cp.budget_name) == ("team-box", "team-monthly")


def test_per_box_security_groups_are_not_mistaken_for_the_control_plane(
    clouds: tuple[Any, Any],
) -> None:
    ec2, iam = clouds
    vpc = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
    ec2.create_security_group(
        GroupName="other",
        Description="t",
        VpcId=vpc,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": [*TAGS, {"Key": "portal:box", "Value": "dev"}],
            }
        ],
    )
    assert control_plane.discover(ec2, iam, REGION) is None


def test_nothing_applied_says_run_init(clouds: tuple[Any, Any]) -> None:
    with pytest.raises(PortalError, match="portal init"):
        _control_plane()


def test_missing_instance_profile_is_not_guessed_around(clouds: tuple[Any, Any]) -> None:
    ec2, iam = clouds
    apply_control_plane(ec2, iam)
    iam.delete_instance_profile(InstanceProfileName="portal-box")
    assert control_plane.discover(ec2, iam, REGION) is None


def test_two_control_planes_is_an_error(clouds: tuple[Any, Any]) -> None:
    ec2, iam = clouds
    apply_control_plane(ec2, iam, "a")
    apply_control_plane(ec2, iam, "b")
    with pytest.raises(PortalError, match="several"):
        control_plane.discover(ec2, iam, REGION)
