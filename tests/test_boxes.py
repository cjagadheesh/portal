import getpass
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from portal import state
from portal.aws import ec2 as ec2_calls
from portal.aws import ssm as ssm_calls
from portal.cli import app
from portal.errors import PortalError
from portal.ttl import parse_ttl

runner = CliRunner()
REGION = "us-east-1"  # moto needs a real region name; production code never hardcodes one
OWNER = getpass.getuser()


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """Fake AWS with a control plane saved in state, as `portal init` would leave it."""
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path))
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        vpc_id = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
        sg = ec2.create_security_group(GroupName="portal-box", Description="t", VpcId=vpc_id)
        state.save_control_plane(
            state.ControlPlane(
                region=REGION,
                instance_profile_name="portal-box",
                security_group_id=sg["GroupId"],
                budget_name="portal-monthly",
            )
        )
        # moto reserves /aws/* in SSM, so the AMI lookup is stubbed.
        # Any AMI moto knows will do; the root device name is read from it.
        ami = ec2.describe_images()["Images"][0]["ImageId"]
        monkeypatch.setattr(ssm_calls, "latest_ubuntu_ami", lambda ssm, arch: ami)
        yield ec2


def all_instances(ec2: Any) -> list[dict[str, Any]]:
    return [i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]


def test_up_launches_instance_with_all_four_tags(aws: Any) -> None:
    before = datetime.now(UTC)
    result = runner.invoke(app, ["up", "--ttl", "2h", "-t", "t3.micro", "--name", "box1"])
    assert result.exit_code == 0, result.output
    (inst,) = all_instances(aws)
    tags = {t["Key"]: t["Value"] for t in inst["Tags"]}
    assert tags["portal:managed"] == "true"
    assert tags["portal:owner"] == OWNER
    assert tags["portal:box"] == "box1"
    expires = datetime.strptime(tags["portal:expires-at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert timedelta(hours=1, minutes=59) < expires - before < timedelta(hours=2, minutes=1)
    # The throwaway launch template must not be left behind.
    assert aws.describe_launch_templates()["LaunchTemplates"] == []


def test_up_refuses_a_second_box_with_the_same_name(aws: Any) -> None:
    args = ["up", "--ttl", "1h", "-t", "t3.micro"]
    assert runner.invoke(app, args).exit_code == 0
    second = runner.invoke(app, args)
    assert second.exit_code == 1
    assert "already running" in second.output
    assert len(all_instances(aws)) == 1


def test_up_rejects_mixed_architectures(aws: Any) -> None:
    result = runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro", "-t", "t4g.micro"])
    assert result.exit_code == 1
    assert "architecture" in result.output
    assert all_instances(aws) == []


def test_up_without_init_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path))
    result = runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro"])
    assert result.exit_code == 1
    assert "portal init" in result.output


def test_capacity_error_lists_types_and_suggests_more() -> None:
    msg = ec2_calls.explain_fleet_errors(
        [{"ErrorCode": "InsufficientInstanceCapacity", "ErrorMessage": "none"}],
        ["a.large", "b.large"],
    )
    assert "a.large, b.large" in msg
    assert "--instance-type" in msg


def test_down_only_terminates_my_boxes(aws: Any) -> None:
    runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro", "--name", "mine"])
    # Someone else's managed box and an untagged instance must both survive `down`.
    for tags in (
        [
            {"Key": "portal:managed", "Value": "true"},
            {"Key": "portal:owner", "Value": "other"},
            {"Key": "portal:box", "Value": "mine"},
        ],
        [],
    ):
        aws.run_instances(
            ImageId="ami-12345678",
            MinCount=1,
            MaxCount=1,
            InstanceType="t3.micro",
            TagSpecifications=[{"ResourceType": "instance", "Tags": tags}] if tags else [],
        )
    result = runner.invoke(app, ["down", "--name", "mine", "--yes"])
    assert result.exit_code == 0, result.output
    by_state = [i["State"]["Name"] for i in all_instances(aws)]
    assert by_state.count("running") == 2
    mine = next(
        i for i in all_instances(aws) if {"Key": "portal:owner", "Value": OWNER} in i["Tags"]
    )
    assert mine["State"]["Name"] in ("shutting-down", "terminated")


def test_down_asks_before_terminating(aws: Any) -> None:
    runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro"])
    result = runner.invoke(app, ["down"], input="n\n")
    assert "aborted" in result.output
    assert [i["State"]["Name"] for i in all_instances(aws)] == ["running"]


def test_down_with_no_box_is_a_no_op(aws: Any) -> None:
    result = runner.invoke(app, ["down", "--yes"])
    assert result.exit_code == 0
    assert "no running box" in result.output


def test_access_denied_names_action_and_arn(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from botocore.exceptions import ClientError

    denied = ClientError(
        {
            "Error": {
                "Code": "UnauthorizedOperation",
                "Message": "User: arn:aws:iam::000000000000:user/x is not authorized to perform: "
                "ec2:CreateFleet on resource: arn:aws:ec2:us-east-1:000000000000:fleet/*",
            }
        },
        "CreateFleet",
    )

    def boom(*args: Any, **kwargs: Any) -> None:
        raise denied

    monkeypatch.setattr(ec2_calls, "launch_box", boom)
    result = runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro"])
    assert result.exit_code == 1
    assert "ec2:CreateFleet" in result.output
    assert "arn:aws:ec2:us-east-1:000000000000:fleet/*" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    ("text", "expected"),
    [("4h", timedelta(hours=4)), ("90m", timedelta(minutes=90)), ("1h30m", timedelta(minutes=90))],
)
def test_parse_ttl(text: str, expected: timedelta) -> None:
    assert parse_ttl(text) == expected


@pytest.mark.parametrize("bad", ["", "0h", "4", "h", "4d", "-1h"])
def test_parse_ttl_rejects_garbage(bad: str) -> None:
    with pytest.raises(PortalError):
        parse_ttl(bad)


# --- portal ssh -------------------------------------------------------------------------


def test_wait_until_online_polls_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([False, False, True])
    monkeypatch.setattr(ssm_calls, "is_online", lambda ssm, iid: next(answers))
    sleeps: list[float] = []
    ssm_calls.wait_until_online(None, "i-1", 60, sleep=sleeps.append, clock=lambda: 0)
    assert len(sleeps) == 2


def test_wait_until_online_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssm_calls, "is_online", lambda ssm, iid: False)
    ticks = iter(range(0, 1000, 10))
    with pytest.raises(PortalError, match="did not register"):
        ssm_calls.wait_until_online(
            None, "i-1", 30, sleep=lambda s: None, clock=lambda: next(ticks)
        )


def test_ssh_starts_session_for_the_right_instance(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro"])
    (inst,) = all_instances(aws)
    monkeypatch.setattr("portal.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ssm_calls, "is_online", lambda ssm, iid: True)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], check: bool) -> Any:
        calls.append(argv)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr("portal.cli.subprocess.run", fake_run)
    result = runner.invoke(app, ["ssh"])
    assert result.exit_code == 0, result.output
    assert "SSM online" in result.output and "s after launch" in result.output
    (argv,) = calls
    assert argv[:7] == [
        "/usr/bin/aws",
        "ssm",
        "start-session",
        "--target",
        inst["InstanceId"],
        "--region",
        REGION,
    ]
    # Lands you as `dev`, not SSM's default ssm-user.
    assert "AWS-StartInteractiveCommand" in argv
    assert "sudo -iu dev" in argv[-1]


def test_ssh_explains_missing_plugin(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "portal.cli.shutil.which", lambda tool: None if tool == "session-manager-plugin" else "/x"
    )
    result = runner.invoke(app, ["ssh"])
    assert result.exit_code == 1
    assert "session-manager-plugin" in result.output


def test_ssh_with_no_box(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("portal.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    result = runner.invoke(app, ["ssh"])
    assert result.exit_code == 1
    assert "portal up" in result.output


def test_up_sends_bootstrap_user_data(aws: Any) -> None:
    result = runner.invoke(
        app, ["up", "--ttl", "1h", "-t", "t3.micro", "--timezone", "Asia/Kolkata"]
    )
    assert result.exit_code == 0, result.output
    (inst,) = all_instances(aws)
    script = aws.describe_instance_attribute(InstanceId=inst["InstanceId"], Attribute="userData")
    import base64

    text = base64.b64decode(script["UserData"]["Value"]).decode()
    assert "timedatectl set-timezone Asia/Kolkata" in text
    assert "/var/log/portal-bootstrap.log" in text


def test_up_rejects_shell_injection_in_timezone(aws: Any) -> None:
    result = runner.invoke(
        app, ["up", "--ttl", "1h", "-t", "t3.micro", "--timezone", "UTC; rm -rf /"]
    )
    assert result.exit_code == 1
    assert "Invalid timezone" in result.output
    assert all_instances(aws) == []
