from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from portal import config
from portal.aws import ec2 as ec2_calls
from portal.cli import app
from portal.errors import PortalError
from tests.test_boxes import aws, runner  # noqa: F401  (aws is a fixture)

UP = ["up", "--ttl", "2h", "-t", "t3.micro", "--name", "b1"]


def expiry_tag(ec2: Any, resource_id: str) -> str:
    tags = ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": [resource_id]}])["Tags"]
    return str(next(t["Value"] for t in tags if t["Key"] == "portal:expires-at"))


def only_instance(ec2: Any) -> dict[str, Any]:
    return next(i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"])


def set_tag(ec2: Any, iid: str, value: str) -> None:
    ec2.create_tags(Resources=[iid], Tags=[{"Key": "portal:expires-at", "Value": value}])


def test_extend_moves_the_instance_and_its_disks(aws: Any) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    inst = only_instance(aws)
    before = datetime.strptime(expiry_tag(aws, inst["InstanceId"]), "%Y-%m-%dT%H:%M:%SZ")
    result = runner.invoke(app, ["extend", "2h", "--name", "b1"])
    assert result.exit_code == 0, result.output
    after = datetime.strptime(expiry_tag(aws, inst["InstanceId"]), "%Y-%m-%dT%H:%M:%SZ")
    assert after - before == timedelta(hours=2)
    volumes = [m["Ebs"]["VolumeId"] for m in inst["BlockDeviceMappings"]]
    assert volumes and all(
        expiry_tag(aws, v) == expiry_tag(aws, inst["InstanceId"]) for v in volumes
    )
    home = aws.describe_volumes(Filters=[{"Name": "tag:portal:role", "Values": ["home"]}])[
        "Volumes"
    ]
    assert expiry_tag(aws, home[0]["VolumeId"]) == expiry_tag(aws, inst["InstanceId"])


def test_extend_a_box_that_does_not_exist_fails_clearly(aws: Any) -> None:  # noqa: F811
    result = runner.invoke(app, ["extend", "1h"])
    assert result.exit_code == 1
    assert "nothing to extend" in result.output and "portal up" in result.output
    assert "Traceback" not in result.output


def test_extend_a_terminated_box_fails_clearly(aws: Any) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    assert runner.invoke(app, ["down", "--name", "b1", "--yes"]).exit_code == 0
    result = runner.invoke(app, ["extend", "1h", "--name", "b1"])
    assert result.exit_code == 1
    assert "nothing to extend" in result.output


def test_extend_an_already_expired_box_refuses_instead_of_racing_the_reaper(aws: Any) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    past = (datetime.now(UTC) - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    set_tag(aws, only_instance(aws)["InstanceId"], past)
    result = runner.invoke(app, ["extend", "1h", "--name", "b1"])
    assert result.exit_code == 1
    assert "passed its expiry" in result.output and "portal up" in result.output
    assert expiry_tag(aws, only_instance(aws)["InstanceId"]) == past  # untouched


def test_extend_cannot_exceed_max_ttl(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".portal.yaml").write_text("max_ttl: 5h\n")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, UP).exit_code == 0  # 2h on the clock
    too_much = runner.invoke(app, ["extend", "4h", "--name", "b1"])
    assert too_much.exit_code == 1
    assert "max_ttl (5h" in too_much.output and "most you can add now is 3h00m" in too_much.output
    assert runner.invoke(app, ["extend", "3h", "--name", "b1"]).exit_code == 0  # exactly 5h
    at_ceiling = runner.invoke(app, ["extend", "2h", "--name", "b1"])
    assert at_ceiling.exit_code == 1 and "already at the ceiling" in at_ceiling.output


def test_extend_repairs_an_unreadable_expiry(aws: Any) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    set_tag(aws, only_instance(aws)["InstanceId"], "tomorrow-ish")
    result = runner.invoke(app, ["extend", "1h", "--name", "b1"])
    assert result.exit_code == 0, result.output
    assert "unreadable" in result.output
    fixed = expiry_tag(aws, only_instance(aws)["InstanceId"])
    assert datetime.strptime(fixed, "%Y-%m-%dT%H:%M:%SZ")  # parses again


def test_extend_rejects_a_bad_duration(aws: Any) -> None:  # noqa: F811
    result = runner.invoke(app, ["extend", "soon"])
    assert result.exit_code == 1 and "Invalid TTL" in result.output


# --- max_ttl on `up`, and AWS time as the clock --------------------------------------------


def test_up_rejects_a_ttl_over_the_ceiling(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".portal.yaml").write_text("max_ttl: 3h\ndefault_ttl: 1h\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["up", "--ttl", "4h", "-t", "t3.micro"])
    assert result.exit_code == 1 and "max_ttl (3h" in result.output
    assert [r for r in aws.describe_instances()["Reservations"]] == []


def test_config_rejects_a_default_ttl_above_the_ceiling(tmp_path: Path) -> None:
    (tmp_path / ".portal.yaml").write_text("default_ttl: 10h\nmax_ttl: 6h\n")
    with pytest.raises(PortalError, match="longer than max_ttl"):
        config.load(tmp_path)


def test_expiry_is_computed_from_aws_time_not_the_laptop_clock(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aws_clock = datetime(2031, 5, 1, 12, 0, 0, tzinfo=UTC)  # the laptop thinks it is 2026
    monkeypatch.setattr(ec2_calls, "aws_now", lambda ec2: aws_clock)
    assert runner.invoke(app, UP).exit_code == 0
    assert expiry_tag(aws, only_instance(aws)["InstanceId"]) == "2031-05-01T14:00:00Z"


def test_aws_now_reads_the_http_date_header() -> None:
    class Client:
        def describe_availability_zones(self) -> dict[str, Any]:
            return {"ResponseMetadata": {"HTTPHeaders": {"date": "Wed, 21 Oct 2026 09:58:04 GMT"}}}

    assert ec2_calls.aws_now(Client()) == datetime(2026, 10, 21, 9, 58, 4, tzinfo=UTC)

    class NoHeader:
        def describe_availability_zones(self) -> dict[str, Any]:
            return {"ResponseMetadata": {"HTTPHeaders": {}}}

    assert abs(ec2_calls.aws_now(NoHeader()) - datetime.now(UTC)) < timedelta(seconds=5)
