import base64
from typing import Any

import pytest

from portal import bootstrap
from portal.aws import home as home_calls
from portal.cli import app
from tests.test_boxes import OWNER, all_instances, aws, runner  # noqa: F401  (aws is a fixture)

UP = ["up", "--ttl", "1h", "-t", "t3.micro", "--name", "h1"]


def home_volumes(ec2: Any) -> list[dict[str, Any]]:
    vols = ec2.describe_volumes(Filters=[{"Name": "tag:portal:role", "Values": ["home"]}])
    return list(vols["Volumes"])


@pytest.fixture
def templates(aws: Any, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:  # noqa: F811
    """Record launch template requests (moto ignores parts of them, e.g. user data)."""
    seen: list[dict[str, Any]] = []
    real = aws.create_launch_template

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(aws, "create_launch_template", spy)
    monkeypatch.setattr("portal.aws.ec2.client", lambda service, region: aws)
    return seen


def user_data(template: dict[str, Any]) -> str:
    return base64.b64decode(template["LaunchTemplateData"]["UserData"]).decode()


def test_first_up_creates_tagged_home_volume_in_the_box_az(aws: Any) -> None:  # noqa: F811
    result = runner.invoke(app, UP)
    assert result.exit_code == 0, result.output
    (inst,) = all_instances(aws)
    (vol,) = home_volumes(aws)
    tags = {t["Key"]: t["Value"] for t in vol["Tags"]}
    assert tags["portal:managed"] == "true"
    assert tags["portal:owner"] == OWNER
    assert tags["portal:box"] == "h1"
    assert "portal:expires-at" in tags
    assert vol["AvailabilityZone"] == inst["Placement"]["AvailabilityZone"]
    assert vol["Size"] == 20  # config default home_gb
    assert vol["Attachments"][0]["InstanceId"] == inst["InstanceId"]
    assert "attached at /home/dev" in result.output


def test_home_survives_down_and_is_reused(aws: Any, templates: list[dict[str, Any]]) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    (first,) = home_volumes(aws)
    assert runner.invoke(app, ["down", "--name", "h1", "--yes"]).exit_code == 0
    (after_down,) = home_volumes(aws)
    assert after_down["VolumeId"] == first["VolumeId"]
    assert after_down["State"] == "available"

    second = runner.invoke(app, UP)
    assert second.exit_code == 0, second.output
    (vol,) = home_volumes(aws)
    assert vol["VolumeId"] == first["VolumeId"]
    assert "reused" in second.output
    # No CLI flag decides formatting any more: both boots run the same blank-disk check, so the
    # second one keeps the existing filesystem because blkid finds it.
    assert user_data(templates[0]) == user_data(templates[1])
    assert "provably blank" in user_data(templates[0]) and "mkfs.xfs" in user_data(templates[0])
    new_box = [i for i in all_instances(aws) if i["State"]["Name"] == "running"]
    assert new_box[0]["Placement"]["AvailabilityZone"] == vol["AvailabilityZone"]


def test_up_refuses_while_home_is_still_attached(aws: Any) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    (inst,) = all_instances(aws)
    # Simulate "box is gone from find_boxes but the volume is still attached".
    aws.create_tags(
        Resources=[inst["InstanceId"]], Tags=[{"Key": "portal:box", "Value": "elsewhere"}]
    )
    result = runner.invoke(app, UP)
    assert result.exit_code == 1
    assert "still attached" in result.output
    assert len(all_instances(aws)) == 1  # nothing new launched


def test_two_home_volumes_is_an_error_not_a_guess(aws: Any) -> None:  # noqa: F811
    from datetime import UTC, datetime

    az = aws.describe_availability_zones()["AvailabilityZones"][0]["ZoneName"]
    for _ in range(2):
        home_calls.create_home_volume(aws, OWNER, "h1", az, 1, datetime.now(UTC))
    result = runner.invoke(app, UP)
    assert result.exit_code == 1
    assert "more than one home volume" in result.output
    assert all_instances(aws) == []


def test_failed_attach_terminates_the_new_box(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from botocore.exceptions import ClientError

    def broken(*args: Any, **kwargs: Any) -> None:
        raise ClientError({"Error": {"Code": "VolumeInUse", "Message": "x"}}, "AttachVolume")

    monkeypatch.setattr(home_calls, "attach_home", broken)
    result = runner.invoke(app, UP)
    assert result.exit_code == 1
    assert "terminated" in result.output
    assert "Traceback" not in result.output
    (inst,) = all_instances(aws)
    assert inst["State"]["Name"] in ("shutting-down", "terminated")


def test_bootstrap_mounts_home_safely() -> None:
    script = bootstrap.render("UTC")
    assert "nofail" in script  # a missing volume must not stop the box booting
    assert "UUID=$UUID /home/dev xfs" in script
    assert "blkid -p" in script  # format only when no filesystem signature exists
    assert "--uid 1500" in script  # same owner on every box that reuses the volume
    # The dev user is only created after /home/dev is mounted.
    assert script.index("mount /home/dev") < script.index("useradd")
