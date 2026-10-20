"""Prove what the reaper's IAM role can and cannot do, using the exact file Terraform deploys."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests import iam_eval

REAPER_DIR = Path(__file__).resolve().parent.parent / "infra" / "reaper"
ACCOUNT, REGION = "000000000000", "us-east-1"
LOG_GROUP = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/portal-reaper"


def render() -> dict[str, Any]:
    values = {
        "partition": "aws",
        "region": REGION,
        "account_id": ACCOUNT,
        "log_group_arn": LOG_GROUP,
    }
    text = (REAPER_DIR / "policy.json.tftpl").read_text()
    return dict(json.loads(re.sub(r"\$\{(\w+)\}", lambda m: values[m[1]], text)))


def allowed(action: str, resource: str, resource_tags: dict[str, str] | None = None) -> bool:
    return iam_eval.allowed(render(), action, resource, resource_tags)


INSTANCE = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0123456789abcdef0"


def test_can_terminate_a_managed_instance() -> None:
    assert allowed("ec2:TerminateInstances", INSTANCE, {"portal:managed": "true"})


@pytest.mark.parametrize(
    "tags",
    [
        {},  # untagged
        {"portal:managed": "false"},
        {"portal:managed": "True"},  # IAM string matching is case sensitive
        {"Name": "prod-database"},
        {"portal:owner": "me", "portal:expires-at": "2000-01-01T00:00:00Z"},  # no managed tag
    ],
)
def test_cannot_terminate_anything_else(tags: dict[str, str]) -> None:
    assert not allowed("ec2:TerminateInstances", INSTANCE, tags)


def test_cannot_terminate_in_another_region_or_account() -> None:
    managed = {"portal:managed": "true"}
    other_region = f"arn:aws:ec2:eu-west-1:{ACCOUNT}:instance/i-1"
    other_account = f"arn:aws:ec2:{REGION}:111111111111:instance/i-1"
    assert not allowed("ec2:TerminateInstances", other_region, managed)
    assert not allowed("ec2:TerminateInstances", other_account, managed)


def test_cannot_terminate_non_instance_resources() -> None:
    managed = {"portal:managed": "true"}
    assert not allowed(
        "ec2:TerminateInstances", f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-1", managed
    )


@pytest.mark.parametrize(
    "action",
    [
        "ec2:RunInstances",
        "ec2:StopInstances",
        "ec2:DeleteVolume",
        "ec2:CreateTags",
        "ec2:DeleteTags",
        "ec2:ModifyInstanceAttribute",
        "iam:PassRole",
        "s3:GetObject",
        "ssm:StartSession",
    ],
)
def test_has_no_other_powers(action: str) -> None:
    managed = {"portal:managed": "true"}
    assert not allowed(action, INSTANCE, managed)
    assert not allowed(action, "*", managed)


def test_can_describe_and_write_only_its_own_logs() -> None:
    assert allowed("ec2:DescribeInstances", "*")
    assert allowed("logs:PutLogEvents", f"{LOG_GROUP}:log-stream:2026/10/21")
    assert not allowed(
        "logs:PutLogEvents", f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:other:log-stream:x"
    )


def test_policy_names_every_action_explicitly() -> None:
    for stmt in render()["Statement"]:
        actions = [stmt["Action"]] if isinstance(stmt["Action"], str) else stmt["Action"]
        assert not any("*" in a for a in actions), "wildcard actions are not allowed here"


def test_terraform_deploys_this_same_file() -> None:
    assert 'templatefile("${path.module}/policy.json.tftpl"' in (REAPER_DIR / "iam.tf").read_text()
    assert "rate(5 minutes)" in (REAPER_DIR / "schedule.tf").read_text()
    assert "retention_in_days = var.log_retention_days" in (REAPER_DIR / "lambda.tf").read_text()
    assert "default     = 14" in (REAPER_DIR / "variables.tf").read_text()
