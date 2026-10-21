"""`portal doctor`: every check, healthy and broken, with fake AWS services (moto for EC2/IAM)."""

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from moto import mock_aws

from portal import doctor, state
from portal.aws import ec2 as ec2_calls
from portal.aws import session
from portal.cli import app
from portal.errors import PortalError
from tests.test_boxes import runner

REGION = "us-east-1"
ACCOUNT = "000000000000"
_ALLOW = {"Effect": "Allow", "Resource": "*"}
VALID_POLICY = json.dumps(
    {"Version": "2012-10-17", "Statement": [{**_ALLOW, "Action": "s3:ListAllMyBuckets"}]}
)
TRUST = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)


def error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "nope"}}, operation)


class Pager:
    def __init__(self, key: str, items: list[Any]) -> None:
        self.key, self.items = key, items

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [{self.key: self.items}]


class Fake:
    """A service whose methods return canned answers, or raise when told to."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers

    def __getattr__(self, name: str) -> Any:
        if name not in self.answers:
            raise AttributeError(name)
        answer = self.answers[name]

        def call(**kwargs: Any) -> Any:
            if isinstance(answer, Exception):
                raise answer
            return answer

        return call

    def get_paginator(self, name: str) -> Any:
        answer = self.answers[name]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Healthy:
    """A whole healthy deployment. Tests break one thing at a time through `world.<attr>`."""

    def __init__(self, ec2: Any, iam: Any) -> None:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        self.sts = Fake(
            get_caller_identity={"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/tester"}
        )
        self.lambda_ = Fake(
            get_function_configuration={
                "State": "Active",
                "Runtime": "python3.12",
                "Environment": {"Variables": {"REAPER_DRY_RUN": "false"}},
            }
        )
        self.events = Fake(
            describe_rule={"State": "ENABLED", "ScheduleExpression": "rate(5 minutes)"}
        )
        self.logs = Fake(
            describe_log_groups=Pager(
                "logGroups", [{"logGroupName": "/aws/lambda/portal-reaper", "retentionInDays": 14}]
            ),
            describe_log_streams=Pager("logStreams", [{"lastEventTimestamp": now_ms - 120_000}]),
        )
        self.budgets = Fake(
            describe_budget={"Budget": {"BudgetLimit": {"Amount": "20", "Unit": "USD"}}}
        )
        self.ce = Fake(
            list_cost_allocation_tags={
                "CostAllocationTags": [{"TagKey": "portal:managed"}, {"TagKey": "portal:box"}]
            }
        )
        self.quotas = Fake(get_service_quota={"Quota": {"Value": 64.0}})
        self.ssm = Fake(get_parameter={"Parameter": {"Value": "ami-stock"}})
        self.ec2, self.iam = ec2, iam

    def clients(self) -> dict[str, Any]:
        return {
            "sts": self.sts, "lambda": self.lambda_, "events": self.events, "logs": self.logs,
            "budgets": self.budgets, "ce": self.ce, "service-quotas": self.quotas,
            "ssm": self.ssm, "ec2": self.ec2, "iam": self.iam,
        }  # fmt: skip


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Healthy]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".portal.yaml").write_text("instance_types: [t3.micro]\n")
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        iam = boto3.client("iam", region_name=REGION)
        vpc = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
        sg = ec2.create_security_group(GroupName="portal-box", Description="t", VpcId=vpc)[
            "GroupId"
        ]
        iam.create_role(RoleName="portal-box", AssumeRolePolicyDocument=TRUST)
        # moto does not ship AWS's managed policies, so make a stand-in with the same name.
        stand_in = iam.create_policy(
            PolicyName="AmazonSSMManagedInstanceCore", PolicyDocument=VALID_POLICY
        )
        iam.attach_role_policy(RoleName="portal-box", PolicyArn=stand_in["Policy"]["Arn"])
        iam.put_role_policy(
            RoleName="portal-box",
            PolicyName="mark-home-volume-resumable",
            PolicyDocument=VALID_POLICY,
        )
        iam.create_instance_profile(InstanceProfileName="portal-box")
        iam.add_role_to_instance_profile(InstanceProfileName="portal-box", RoleName="portal-box")
        inst = ec2.run_instances(
            ImageId="ami-12345678", MinCount=1, MaxCount=1, InstanceType="t3.micro"
        )
        ec2.create_image(
            InstanceId=inst["Instances"][0]["InstanceId"], Name="portal-ubuntu-2404-amd64-1"
        )
        ec2.terminate_instances(InstanceIds=[inst["Instances"][0]["InstanceId"]])
        plane = state.ControlPlane(
            region=REGION,
            instance_profile_name="portal-box",
            security_group_id=sg,
            budget_name="portal-monthly",
        )
        monkeypatch.setattr(doctor.shutil, "which", lambda tool: f"/usr/bin/{tool}")
        monkeypatch.setattr("portal.sync.check_rsync", lambda: "/usr/bin/rsync")
        healthy = Healthy(ec2, iam)
        healthy.plane = plane  # type: ignore[attr-defined]
        yield healthy


def check(world: Healthy, **overrides: Any) -> dict[str, doctor.Check]:
    clients = world.clients()
    clients.update(overrides)
    ctx = doctor.Context(
        owner="tester",
        load_control_plane=lambda: world.plane,
        clients=clients,
        region=REGION,  # type: ignore[attr-defined]
    )
    return {c.name: c for c in doctor.run_checks(ctx)}


def statuses(results: dict[str, doctor.Check]) -> dict[str, str]:
    return {name: c.status for name, c in results.items()}


# --- a healthy deployment ----------


def test_a_healthy_deployment_passes_every_check(world: Healthy) -> None:
    results = check(world)
    bad = {n: (c.status, c.detail) for n, c in results.items() if c.status != doctor.OK}
    assert not bad, bad
    assert len(results) >= 18
    assert "000000000000" in results["credentials"].detail


def test_doctor_only_reads(world: Healthy) -> None:
    def snapshot() -> tuple[Any, ...]:
        ec2 = world.ec2
        return (
            len(ec2.describe_instances()["Reservations"]),
            sorted(v["VolumeId"] for v in ec2.describe_volumes()["Volumes"]),
            sorted(g["GroupId"] for g in ec2.describe_security_groups()["SecurityGroups"]),
            sorted(t["Key"] + t["ResourceId"] for t in ec2.describe_tags()["Tags"]),
            len(ec2.describe_launch_templates()["LaunchTemplates"]),
        )

    before = snapshot()
    check(world)
    assert snapshot() == before


# --- each safety property, broken one at a time ----------


def test_an_inbound_rule_is_a_failure_that_names_the_rule(world: Healthy) -> None:
    sg = world.plane.security_group_id  # type: ignore[attr-defined]
    world.ec2.authorize_security_group_ingress(
        GroupId=sg,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )
    result = check(world)["no inbound rules"]
    assert (
        result.status == doctor.FAIL
        and "ports: 22" in result.detail
        and "hard rule 1" in result.detail
    )


def test_an_extra_policy_on_the_box_role_is_flagged(world: Healthy) -> None:
    world.iam.put_role_policy(
        RoleName="portal-box", PolicyName="oops-admin", PolicyDocument=VALID_POLICY
    )
    result = check(world)["box role is SSM-only"]
    assert result.status == doctor.WARN and "oops-admin" in result.detail


def test_a_box_role_without_ssm_is_a_failure(world: Healthy) -> None:
    attached = world.iam.list_attached_role_policies(RoleName="portal-box")["AttachedPolicies"]
    world.iam.detach_role_policy(RoleName="portal-box", PolicyArn=attached[0]["PolicyArn"])
    assert check(world)["box role is SSM-only"].status == doctor.FAIL


def test_a_reaper_in_dry_run_is_loudly_a_warning(world: Healthy) -> None:
    world.lambda_ = Fake(
        get_function_configuration={
            "State": "Active",
            "Environment": {"Variables": {"REAPER_DRY_RUN": "true"}},
        }
    )
    result = check(world, **{"lambda": world.lambda_})["reaper function"]
    assert (
        result.status == doctor.WARN
        and "DRY-RUN" in result.detail
        and "--reaper-dry-run" in result.fix
    )


def test_a_missing_reaper_is_a_failure(world: Healthy) -> None:
    gone = Fake(
        get_function_configuration=error("ResourceNotFoundException", "GetFunctionConfiguration")
    )
    assert check(world, **{"lambda": gone})["reaper function"].status == doctor.FAIL


def test_a_disabled_schedule_is_a_failure(world: Healthy) -> None:
    events = Fake(describe_rule={"State": "DISABLED", "ScheduleExpression": "rate(5 minutes)"})
    assert check(world, events=events)["reaper schedule"].status == doctor.FAIL


@pytest.mark.parametrize(
    ("streams", "expected"),
    [
        ([], doctor.WARN),  # never ran
        ([{"lastEventTimestamp": int((time.time() - 3600) * 1000)}], doctor.WARN),  # stale
        ([{"lastEventTimestamp": int(time.time() * 1000)}], doctor.OK),
    ],
)
def test_the_reaper_must_have_run_recently(
    world: Healthy, streams: list[Any], expected: str
) -> None:
    logs = Fake(
        describe_log_groups=Pager(
            "logGroups", [{"logGroupName": "/aws/lambda/portal-reaper", "retentionInDays": 14}]
        ),
        describe_log_streams=Pager("logStreams", streams),
    )
    assert check(world, logs=logs)["reaper is running"].status == expected


def test_wrong_log_retention_is_mentioned(world: Healthy) -> None:
    logs = Fake(
        describe_log_groups=Pager(
            "logGroups", [{"logGroupName": "/aws/lambda/portal-reaper", "retentionInDays": 365}]
        ),
        describe_log_streams=Pager("logStreams", [{"lastEventTimestamp": int(time.time() * 1000)}]),
    )
    assert "retention is 365" in check(world, logs=logs)["reaper is running"].detail


def test_a_missing_budget_is_a_failure(world: Healthy) -> None:
    budgets = Fake(describe_budget=error("NotFoundException", "DescribeBudget"))
    assert check(world, budgets=budgets)["budget alarm"].status == doctor.FAIL


def test_inactive_cost_tags_explain_why_portal_cost_is_empty(world: Healthy) -> None:
    ce = Fake(list_cost_allocation_tags={"CostAllocationTags": [{"TagKey": "portal:managed"}]})
    result = check(world, ce=ce)["cost allocation tags"]
    assert (
        result.status == doctor.WARN
        and "portal:box" in result.detail
        and "portal cost" in result.fix
    )


# --- can a box launch ----------


def test_an_instance_type_nobody_offers_here_is_a_failure(world: Healthy) -> None:
    world.ec2 = _OfferingsOverride(world.ec2, {})  # type: ignore[assignment]
    assert check(world)["instance types offered"].status == doctor.FAIL


def test_mixed_cpu_architectures_are_a_failure(world: Healthy, tmp_path: Path) -> None:
    (tmp_path / ".portal.yaml").write_text("instance_types: [t3.micro, t4g.micro]\n")
    result = check(world)["instance types offered"]
    assert result.status == doctor.FAIL and "architecture" in result.detail


def test_no_baked_image_is_only_a_warning_when_the_stock_image_resolves(world: Healthy) -> None:
    for image in world.ec2.describe_images(Owners=["self"])["Images"]:
        world.ec2.deregister_image(ImageId=image["ImageId"])
    result = check(world)["boot image"]
    assert result.status == doctor.WARN and "stock Ubuntu ami-stock" in result.detail


def test_no_image_at_all_is_a_failure(world: Healthy) -> None:
    for image in world.ec2.describe_images(Owners=["self"])["Images"]:
        world.ec2.deregister_image(ImageId=image["ImageId"])
    ssm = Fake(get_parameter=error("ParameterNotFound", "GetParameter"))
    assert check(world, ssm=ssm)["boot image"].status == doctor.FAIL


def test_a_spot_quota_smaller_than_the_instance_is_a_failure(world: Healthy) -> None:
    quotas = Fake(get_service_quota={"Quota": {"Value": 1.0}})  # t3.micro has 2 vCPUs
    result = check(world, **{"service-quotas": quotas})["spot vCPU quota"]
    assert result.status == doctor.FAIL and "2 vCPUs" in result.detail


def test_unused_home_volumes_are_listed(world: Healthy) -> None:
    from portal.aws import home

    home.create_home_volume(
        world.ec2, "tester", "forgotten", "us-east-1a", 7, datetime.now(UTC) + timedelta(hours=1)
    )
    result = check(world)["kept home volumes"]
    assert result.status == doctor.WARN and "7 GB" in result.detail and "forgotten" in result.detail


def test_a_wrong_clock_is_noticed(world: Healthy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ec2_calls, "aws_now", lambda ec2: datetime.now(UTC) - timedelta(minutes=10))
    result = check(world)["clock"]
    assert result.status == doctor.WARN and "off AWS's" in result.detail


class _OfferingsOverride:
    """The moto EC2 client with describe_instance_type_offerings answering from a dict."""

    def __init__(self, real: Any, offered: dict[str, list[str]]) -> None:
        self._real, self._offered = real, offered

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def get_paginator(self, name: str) -> Any:
        if name != "describe_instance_type_offerings":
            return self._real.get_paginator(name)
        items = [
            {"InstanceType": t, "Location": z} for t, zones in self._offered.items() for z in zones
        ]
        return Pager("InstanceTypeOfferings", items)


# --- degrading gracefully ----------


@pytest.mark.parametrize(
    ("service", "method", "check_name"),
    [
        ("lambda", "get_function_configuration", "reaper function"),
        ("events", "describe_rule", "reaper schedule"),
        ("budgets", "describe_budget", "budget alarm"),
        ("ce", "list_cost_allocation_tags", "cost allocation tags"),
        ("service-quotas", "get_service_quota", "spot vCPU quota"),
    ],
)
def test_a_missing_permission_is_a_skip_not_a_failure(
    world: Healthy, service: str, method: str, check_name: str
) -> None:
    denied = Fake(**{method: error("AccessDeniedException", method)})
    result = check(world, **{service: denied})[check_name]
    assert result.status == doctor.SKIP and "no permission" in result.detail


def test_without_credentials_everything_else_is_skipped_with_the_reason(world: Healthy) -> None:
    sts = Fake(get_caller_identity=NoCredentialsError())
    results = check(world, sts=sts)
    assert results["credentials"].status == doctor.FAIL
    aws_checks = [n for _, n, _ in doctor.AWS]
    assert all(
        results[n].status == doctor.SKIP and results[n].detail == "no credentials"
        for n in aws_checks
    )


def test_without_a_control_plane_everything_else_is_skipped(world: Healthy) -> None:
    def missing() -> state.ControlPlane:
        raise PortalError("No control plane found. Run `portal init` first.")

    ctx = doctor.Context(
        owner="t", load_control_plane=missing, clients=world.clients(), region=REGION
    )
    results = {c.name: c for c in doctor.run_checks(ctx)}
    assert (
        results["control plane"].status == doctor.FAIL
        and "portal init" in results["control plane"].detail
    )
    assert all(results[n].status == doctor.SKIP for _, n, _ in doctor.AWS)


def test_local_mode_makes_no_aws_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(session, "client", lambda *a: pytest.fail("no AWS in --local mode"))
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: None)
    ctx = doctor.Context(owner="t", load_control_plane=lambda: pytest.fail("no AWS"))
    results = doctor.run_checks(ctx, local_only=True)
    assert {c.group for c in results} == {"tools", "config"}
    assert any(c.status == doctor.FAIL and "aws" in c.detail for c in results)


def test_a_broken_config_file_is_a_failed_check_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".portal.yaml").write_text("instance_type: oops\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: "/x")
    monkeypatch.setattr("portal.sync.check_rsync", lambda: "/x")
    ctx = doctor.Context(owner="t", load_control_plane=lambda: pytest.fail("no AWS"))
    broken = {c.name: c for c in doctor.run_checks(ctx, local_only=True)}["config files"]
    assert broken.status == doctor.FAIL and ".portal.yaml" in broken.detail


# --- the command ----------


def test_the_command_prints_a_table_fix_hints_and_a_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        doctor.shutil, "which", lambda tool: None if tool == "session-manager-plugin" else "/x"
    )
    monkeypatch.setattr("portal.sync.check_rsync", lambda: "/x")
    result = runner.invoke(app, ["doctor", "--local"], env={"COLUMNS": "140"})
    assert result.exit_code == 1  # the missing plugin is a failure
    assert "Session Manager plugin" in result.output and "✗" in result.output
    assert "Install the Session Manager plugin." in result.output
    assert "1 failed" in result.output


def test_the_command_exits_zero_when_nothing_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".portal.yaml").write_text("instance_types: [t3.micro]\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: "/x")
    monkeypatch.setattr("portal.sync.check_rsync", lambda: "/x")
    result = runner.invoke(app, ["doctor", "--local"], env={"COLUMNS": "140"})
    assert result.exit_code == 0 and "0 failed" in result.output


def test_every_check_has_a_unique_name_and_a_known_group() -> None:
    names = [n for _, n, _ in doctor.LOCAL + doctor.AWS]
    assert len(names) == len(set(names))
    assert {g for g, _, _ in doctor.LOCAL + doctor.AWS} <= {
        "tools", "config", "safety", "billing", "launch", "housekeeping"
    }  # fmt: skip
