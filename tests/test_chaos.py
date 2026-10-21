"""Fault injection: make every AWS call fail once, one at a time, and check what is left behind.

For each command we first run it cleanly to learn which AWS calls it makes (and in what order),
then re-run it once per call with that single call raising a ClientError. The properties checked
are the ones that matter for a tool whose job is to not run up a bill:

- the user gets a sentence, never a traceback;
- a failed `up` never leaves a billing box behind (unless it says so out loud);
- there is never more than one box per name;
- the failure is recoverable: running the command again, with AWS healthy, just works.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from portal import state
from portal.aws import ec2 as ec2_calls
from portal.aws import ssm as ssm_calls
from portal.cli import app
from tests.test_boxes import runner

REGION = "us-east-1"
NAME = "ch"
UP = ["up", "--ttl", "1h", "-t", "t3.micro", "--name", NAME]
MAX_REPEATS = 2  # polling calls repeat; the first couple of repeats are the interesting ones


class Chaos:
    """The moto EC2 client, except one chosen call (operation, nth time) raises."""

    def __init__(self, real: Any, fail_at: tuple[str, int] | None = None) -> None:
        self._real = real
        self.fail_at = fail_at
        self.seen: list[tuple[str, int]] = []
        self._counts: dict[str, int] = {}

    def _hit(self, operation: str) -> None:
        nth = self._counts.get(operation, 0) + 1
        self._counts[operation] = nth
        self.seen.append((operation, nth))
        if self.fail_at == (operation, nth):
            camel = "".join(part.title() for part in operation.split("_"))
            raise ClientError(
                {"Error": {"Code": "InternalError", "Message": "injected failure"}}, camel
            )

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._real, name)
        if name == "get_paginator":
            return lambda op: _Pager(self, op, attr(op))
        if name == "get_waiter":
            return lambda op: _Waiter(self, op, attr(op))
        if not callable(attr):
            return attr

        def call(**kwargs: Any) -> Any:
            self._hit(name)
            return attr(**kwargs)

        return call


class _Pager:
    def __init__(self, chaos: Chaos, op: str, real: Any) -> None:
        self.chaos, self.op, self.real = chaos, op, real

    def paginate(self, **kwargs: Any) -> Any:
        self.chaos._hit(self.op)
        return self.real.paginate(**kwargs)


class _Waiter:
    def __init__(self, chaos: Chaos, op: str, real: Any) -> None:
        self.chaos, self.op, self.real = chaos, op, real

    def wait(self, **kwargs: Any) -> Any:
        self.chaos._hit(f"wait:{self.op}")
        return self.real.wait(**kwargs)


@contextmanager
def world(monkeypatch: pytest.MonkeyPatch, home: Path) -> Iterator[Any]:
    """A fresh fake AWS account with a control plane, as `portal init` would leave it."""
    monkeypatch.setenv("PORTAL_HOME", str(home))
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        vpc = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
        sg = ec2.create_security_group(GroupName="portal-box", Description="t", VpcId=vpc)
        state.save_control_plane(
            state.ControlPlane(
                region=REGION,
                instance_profile_name="portal-box",
                security_group_id=sg["GroupId"],
                budget_name="portal-monthly",
            )
        )
        ami = ec2.describe_images()["Images"][0]["ImageId"]
        monkeypatch.setattr(ssm_calls, "latest_ubuntu_ami", lambda ssm, arch: ami)
        yield ec2


def install(monkeypatch: pytest.MonkeyPatch, ec2: Any, fail_at: tuple[str, int] | None) -> Chaos:
    chaos = Chaos(ec2, fail_at)
    monkeypatch.setattr(
        ec2_calls, "client", lambda service, region: chaos if service == "ec2" else None
    )
    return chaos


def alive(ec2: Any) -> list[dict[str, Any]]:
    found = [i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]
    return [i for i in found if i["State"]["Name"] in ("pending", "running")]


def homes(ec2: Any) -> list[dict[str, Any]]:
    vols = ec2.describe_volumes(Filters=[{"Name": "tag:portal:role", "Values": ["home"]}])
    return list(vols["Volumes"])


def no_crash(result: Any) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in result.output, result.output


def attached_home(ec2: Any) -> bool:
    boxes = alive(ec2)
    return len(boxes) == 1 and any(
        a["InstanceId"] == boxes[0]["InstanceId"] for v in homes(ec2) for a in v["Attachments"]
    )


def fault_points(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: list[str],
    setup: list[str] | None,
    answer: str,
) -> list[tuple[str, int]]:
    """Which (call, nth) does this command make when nothing goes wrong?"""
    with world(monkeypatch, tmp_path / "probe") as ec2:
        if setup:
            assert runner.invoke(app, setup).exit_code == 0
        chaos = install(monkeypatch, ec2, None)
        assert runner.invoke(app, command, input=answer).exit_code == 0
        return [p for p in dict.fromkeys(chaos.seen) if p[1] <= MAX_REPEATS]


# --- up ----------


def test_up_survives_any_single_aws_failure_without_leaking_a_box(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    points = fault_points(monkeypatch, tmp_path, UP, None, "")
    assert len(points) >= 12, points  # the matrix is real, not accidentally empty
    problems: list[str] = []
    for index, point in enumerate(points):
        with world(monkeypatch, tmp_path / f"up-{index}") as ec2:
            install(monkeypatch, ec2, point)
            first = runner.invoke(app, UP)
            label = f"{point[0]}#{point[1]}"
            try:
                no_crash(first)
            except AssertionError as e:
                problems.append(f"{label}: crashed: {str(e)[:160]}")
                continue
            if len(alive(ec2)) > 1:
                problems.append(f"{label}: more than one box")
            if first.exit_code == 0 and not attached_home(ec2):
                problems.append(f"{label}: reported success but the box has no home volume")
            if first.exit_code != 0 and alive(ec2) and "could not terminate" not in first.output:
                problems.append(f"{label}: failed but left a running box and did not say so")
            # Recovery: with AWS healthy again, the same command must simply work.
            install(monkeypatch, ec2, None)
            if first.exit_code != 0 or not alive(ec2):
                for box in alive(ec2):  # a box the failed run admitted it left behind
                    ec2.terminate_instances(InstanceIds=[box["InstanceId"]])
                again = runner.invoke(app, UP)
                if again.exit_code != 0 or not attached_home(ec2):
                    problems.append(f"{label}: could not recover on retry: {again.output[:200]}")
            if len(homes(ec2)) > 1:
                problems.append(f"{label}: ended with {len(homes(ec2))} home volumes")
    assert not problems, "\n".join(problems)


# --- down --purge ----------


def test_purge_survives_any_single_failure_and_can_be_finished(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    purge = ["down", "--name", NAME, "--purge"]
    points = fault_points(monkeypatch, tmp_path, purge, UP, f"{NAME}\n")
    assert len(points) >= 5, points
    problems: list[str] = []
    for index, point in enumerate(points):
        with world(monkeypatch, tmp_path / f"purge-{index}") as ec2:
            assert runner.invoke(app, UP).exit_code == 0
            install(monkeypatch, ec2, point)
            first = runner.invoke(app, purge, input=f"{NAME}\n")
            label = f"{point[0]}#{point[1]}"
            try:
                no_crash(first)
            except AssertionError as e:
                problems.append(f"{label}: crashed: {str(e)[:160]}")
                continue
            install(monkeypatch, ec2, None)
            if first.exit_code != 0:  # a half-done purge must be finishable by running it again
                runner.invoke(app, purge, input=f"{NAME}\n")
                for _ in range(2):  # the home volume only frees once the box is gone
                    ec2.get_waiter("instance_terminated").wait(
                        InstanceIds=[
                            i["InstanceId"]
                            for r in ec2.describe_instances()["Reservations"]
                            for i in r["Instances"]
                        ]
                    )
                    runner.invoke(app, purge, input=f"{NAME}\n")
            if alive(ec2) or homes(ec2):
                problems.append(
                    f"{label}: purge left {len(alive(ec2))} boxes, {len(homes(ec2))} volumes"
                )
    assert not problems, "\n".join(problems)


# --- extend ----------


def test_extend_survives_any_single_failure_and_never_corrupts_the_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    extend = ["extend", "1h", "--name", NAME]
    points = fault_points(monkeypatch, tmp_path, extend, UP, "")
    assert len(points) >= 4, points
    problems: list[str] = []
    for index, point in enumerate(points):
        with world(monkeypatch, tmp_path / f"extend-{index}") as ec2:
            assert runner.invoke(app, UP).exit_code == 0
            before = _expiry(ec2)
            install(monkeypatch, ec2, point)
            first = runner.invoke(app, extend)
            label = f"{point[0]}#{point[1]}"
            try:
                no_crash(first)
            except AssertionError as e:
                problems.append(f"{label}: crashed: {str(e)[:160]}")
                continue
            if first.exit_code != 0 and _expiry(ec2) != before:
                problems.append(f"{label}: failed but the expiry changed")
            install(monkeypatch, ec2, None)
            if runner.invoke(app, extend).exit_code != 0:
                problems.append(f"{label}: retry failed")
    assert not problems, "\n".join(problems)


def _expiry(ec2: Any) -> str:
    (box,) = alive(ec2)
    return next(t["Value"] for t in box["Tags"] if t["Key"] == "portal:expires-at")


# --- read-only commands ----------


@pytest.mark.parametrize("command", [["status"], ["volumes"]])
def test_read_only_commands_never_crash_on_an_aws_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: list[str]
) -> None:
    points = fault_points(monkeypatch, tmp_path, command, UP, "")
    for index, point in enumerate(points):
        with world(monkeypatch, tmp_path / f"{command[0]}-{index}") as ec2:
            assert runner.invoke(app, UP).exit_code == 0
            install(monkeypatch, ec2, point)
            result = runner.invoke(app, command, env={"COLUMNS": "200"})
            no_crash(result)
            assert result.exit_code in (0, 1), (point, result.output)
