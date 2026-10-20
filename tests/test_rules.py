"""The project's hard rules, checked mechanically so they cannot quietly rot."""

import ast
import re
import socket
from pathlib import Path

import boto3
import pytest

REPO = Path(__file__).resolve().parent.parent
PYTHON_SOURCES = [
    *REPO.glob("portal/**/*.py"),
    *REPO.glob("agent/*.py"),
    REPO / "infra/reaper/handler.py",
]
INFRA_SOURCES = [
    *REPO.glob("infra/**/*.tf"),
    *REPO.glob("infra/**/*.tftpl"),
    *REPO.glob("packer/*"),
]


def code_only(path: Path) -> str:
    """File text minus comment-only lines, so prose about a rule is not mistaken for a violation."""
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


# --- rule 5: tests never touch real AWS ----------------------------------------------------


def test_the_suite_runs_without_the_developers_aws_setup() -> None:
    credentials = boto3.Session().get_credentials()
    assert credentials is not None and credentials.access_key == "testing"
    assert not Path(str(boto3.Session()._session.get_config_variable("config_file"))).exists()


def test_outbound_network_is_blocked_but_loopback_is_not() -> None:
    with socket.socket() as s, pytest.raises(RuntimeError, match="reach the network"):
        s.connect(("93.184.216.34", 443))
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with socket.socket() as client:
            client.connect(server.getsockname())  # allowed


# --- rule 1: no inbound security group rules -----------------------------------------------


def test_nothing_in_the_repo_opens_an_inbound_rule() -> None:
    for path in INFRA_SOURCES:
        text = code_only(path)
        assert "aws_vpc_security_group_ingress_rule" not in text, path
        assert not re.search(r"\bingress\s*[{=]", text), f"{path} declares ingress"
        assert "from_port" not in text, path


# --- rule 3: no hardcoded region, AMI or account --------------------------------------------

REGION = re.compile(
    r"\b(?:us|eu|ap|sa|ca|me|af|il)-(?:east|west|south|north|central|southeast|northeast)-\d\b"
)
# Two AWS *service endpoints* that exist in one region only; they are not the user's region.
ENDPOINT_REGION_FILES = {"portal/cost.py", "portal/aws/pricing.py"}


def test_no_region_is_hardcoded() -> None:
    for path in [*PYTHON_SOURCES, *INFRA_SOURCES]:
        relative = path.relative_to(REPO).as_posix()
        if relative in ENDPOINT_REGION_FILES:
            continue
        # docstrings and comments may mention a region as an example; code may not
        tree_text = path.read_text() if path.suffix != ".py" else _string_free_code(path)
        stripped = "\n".join(ln for ln in tree_text.splitlines() if not ln.lstrip().startswith("#"))
        assert not REGION.search(stripped), f"{relative} hardcodes a region"


def _string_free_code(path: Path) -> str:
    """Python source with docstrings removed (examples in prose are fine, code is not)."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_the_two_endpoint_regions_are_the_only_allowed_exceptions() -> None:
    for relative in ENDPOINT_REGION_FILES:
        assert "ENDPOINT_REGION" in (REPO / relative).read_text()


# --- rule 7: no secrets, no account-bound ids -----------------------------------------------


def test_no_account_numbers_or_ami_ids_in_shipped_files() -> None:
    for path in [*PYTHON_SOURCES, *INFRA_SOURCES, *REPO.glob("docs/**/*.md"), REPO / "README.md"]:
        text = path.read_text()
        assert not re.search(r"(?<![\w-])\d{12}(?![\w-])", text), (
            f"{path}: looks like an account id"
        )
        assert not re.search(r"\bami-[0-9a-f]{8,}\b", text), f"{path}: contains an AMI id"


def test_tests_only_use_obviously_fake_account_ids() -> None:
    allowed = {"000000000000", "111111111111"}
    for path in REPO.glob("tests/*.py"):
        found = set(re.findall(r"(?<![\w-])\d{12}(?![\w-])", path.read_text()))
        assert found <= allowed, f"{path.name}: {found - allowed}"


def test_no_credentials_are_committed() -> None:
    patterns = [
        r"AKIA[0-9A-Z]{16}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"aws_secret_access_key\s*=",
    ]
    for path in [
        *PYTHON_SOURCES,
        *INFRA_SOURCES,
        *REPO.glob("docs/**/*.md"),
        *REPO.glob(".github/**/*.yml"),
    ]:
        for pattern in patterns:
            assert not re.search(pattern, path.read_text()), f"{path}: matches {pattern}"
    assert (
        "*.pem" in (REPO / ".gitignore").read_text() and ".env" in (REPO / ".gitignore").read_text()
    )


# --- rule 6: every list/describe call paginates ----------------------------------------------

# Calls that have a paginator and must therefore never be made directly.
PAGINATED = {
    "describe_instances", "describe_volumes", "describe_images", "describe_security_groups",
    "describe_subnets", "describe_instance_types", "describe_spot_price_history",
    "describe_instance_information", "get_products", "list_instance_profiles",
}  # fmt: skip


def test_paginated_calls_always_go_through_a_paginator() -> None:
    offenders = []
    for path in PYTHON_SOURCES:
        for node in ast.walk(ast.parse(path.read_text())):
            is_method_call = isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            if is_method_call and node.func.attr in PAGINATED:  # type: ignore[attr-defined]
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno} {node.func.attr}")  # type: ignore[attr-defined]
    assert not offenders, "call these through get_paginator(): " + ", ".join(offenders)


# --- CI mirrors what we promise locally ---------------------------------------------------------


def test_ci_runs_every_gate_on_three_pythons() -> None:
    ci = (REPO / ".github/workflows/ci.yml").read_text()
    for needle in (
        'python: ["3.11", "3.12", "3.13"]',
        "ruff check",
        "mypy --strict portal agent",
        "pytest",
        "terraform fmt -check",
        "validate",
        "tflint",
        "packer validate",
    ):
        assert needle in ci, needle
    assert "AWS_ACCESS_KEY_ID" not in ci and "secrets." not in ci  # tests need no credentials


def test_the_project_declares_the_python_floor_ci_tests() -> None:
    assert 'requires-python = ">=3.11"' in (REPO / "pyproject.toml").read_text()


# --- the documented IAM policy covers every API call the code makes -----------------------------

# boto3 method (or paginator/waiter name) -> the IAM action it needs.
API_ACTIONS = {
    "attach_volume": "ec2:AttachVolume",
    "create_fleet": "ec2:CreateFleet",
    "create_launch_template": "ec2:CreateLaunchTemplate",
    "create_tags": "ec2:CreateTags",
    "create_volume": "ec2:CreateVolume",
    "delete_launch_template": "ec2:DeleteLaunchTemplate",
    "delete_volume": "ec2:DeleteVolume",
    "describe_availability_zones": "ec2:DescribeAvailabilityZones",
    "get_cost_and_usage": "ce:GetCostAndUsage",
    "get_instance_profile": "iam:GetInstanceProfile",
    "get_parameter": "ssm:GetParameter",
    "terminate_instances": "ec2:TerminateInstances",
    # paginators
    "describe_images": "ec2:DescribeImages",
    "describe_instance_information": "ssm:DescribeInstanceInformation",
    "describe_instance_types": "ec2:DescribeInstanceTypes",
    "describe_instances": "ec2:DescribeInstances",
    "describe_security_groups": "ec2:DescribeSecurityGroups",
    "describe_spot_price_history": "ec2:DescribeSpotPriceHistory",
    "describe_subnets": "ec2:DescribeSubnets",
    "describe_volumes": "ec2:DescribeVolumes",
    "get_products": "pricing:GetProducts",
    # `portal doctor` (optional, docs/iam-policy-doctor.json)
    "describe_budget": "budgets:ViewBudget",
    "describe_instance_type_offerings": "ec2:DescribeInstanceTypeOfferings",
    "describe_log_groups": "logs:DescribeLogGroups",
    "describe_log_streams": "logs:DescribeLogStreams",
    "describe_rule": "events:DescribeRule",
    "get_function_configuration": "lambda:GetFunctionConfiguration",
    "get_service_quota": "servicequotas:GetServiceQuota",
    "list_attached_role_policies": "iam:ListAttachedRolePolicies",
    "list_cost_allocation_tags": "ce:ListCostAllocationTags",
    "list_role_policies": "iam:ListRolePolicies",
    # needs no permission at all: AWS always allows asking who you are
    "get_caller_identity": "sts:GetCallerIdentity",
    # waiters poll a describe call
    "instance_running": "ec2:DescribeInstances",
    "volume_available": "ec2:DescribeVolumes",
}
# Needed indirectly: CreateFleet launches through RunInstances and passes the box role; the
# AWS CLI (not boto3) opens sessions and the plugin ends/resumes them.
NEEDS_NO_PERMISSION = {"sts:GetCallerIdentity"}
DOCTOR_ONLY = {
    "budgets:ViewBudget", "ec2:DescribeInstanceTypeOfferings", "logs:DescribeLogGroups",
    "logs:DescribeLogStreams", "events:DescribeRule", "lambda:GetFunctionConfiguration",
    "servicequotas:GetServiceQuota", "iam:ListAttachedRolePolicies", "ce:ListCostAllocationTags",
    "iam:ListRolePolicies",
}  # fmt: skip
INDIRECT = {
    "ec2:RunInstances",
    "iam:PassRole",
    "ssm:StartSession",
    "ssm:TerminateSession",
    "ssm:ResumeSession",
}
AWS_LOOKING = re.compile(
    r"^(describe|create|delete|terminate|attach|detach|modify|run|start|stop|get|list|put|update)_[a-z_]+$"
)
NOT_AWS = {"get_paginator", "get_waiter", "create_connection"}


def policy_actions(name: str) -> set[str]:
    import json

    actions: set[str] = set()
    for stmt in json.loads((REPO / "docs" / name).read_text())["Statement"]:
        actions |= {stmt["Action"]} if isinstance(stmt["Action"], str) else set(stmt["Action"])
    return actions


def test_every_api_call_the_code_makes_is_in_the_documented_policy() -> None:
    own_functions: set[str] = set()
    used: set[str] = set()
    for path in REPO.glob("portal/**/*.py"):
        tree = ast.parse(path.read_text())
        own_functions |= {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            attr = node.func.attr
            if attr in ("get_paginator", "get_waiter") and node.args:
                first = node.args[0]
                assert isinstance(first, ast.Constant), f"{path}: use a literal name"
                used.add(str(first.value))
            elif AWS_LOOKING.match(attr) and attr not in NOT_AWS:
                used.add(attr)
    unknown = {name for name in used - own_functions if name not in API_ACTIONS}
    assert not unknown, f"add these to API_ACTIONS and docs/iam-policy.json: {sorted(unknown)}"
    needed = {API_ACTIONS[n] for n in used if n in API_ACTIONS} - NEEDS_NO_PERMISSION
    daily = policy_actions("iam-policy.json")
    assert (needed - DOCTOR_ONLY) | INDIRECT <= daily, (
        f"docs/iam-policy.json lacks: {sorted(((needed - DOCTOR_ONLY) | INDIRECT) - daily)}"
    )
    optional = policy_actions("iam-policy-doctor.json")
    assert needed & DOCTOR_ONLY <= optional, (
        f"docs/iam-policy-doctor.json lacks: {sorted((needed & DOCTOR_ONLY) - optional)}"
    )
    assert not DOCTOR_ONLY & daily, "doctor-only permissions must stay out of the daily policy"


def test_the_policies_are_least_privilege_in_shape() -> None:
    import json

    for name in ("iam-policy.json", "iam-policy-init.json", "iam-policy-doctor.json"):
        doc = json.loads((REPO / "docs" / name).read_text())
        for stmt in doc["Statement"]:
            actions = [stmt["Action"]] if isinstance(stmt["Action"], str) else stmt["Action"]
            assert not any(a == "*" or a.endswith(":*") for a in actions), (
                f"{name}: wildcard action"
            )
            assert not re.search(r"\b\d{12}\b", json.dumps(stmt)), f"{name}: real account id"
    daily = json.loads((REPO / "docs" / "iam-policy.json").read_text())
    for stmt in daily["Statement"]:
        # Destructive actions must be scoped by the portal:managed tag.
        if stmt["Action"] in (
            "ec2:TerminateInstances",
            "ec2:DeleteVolume",
            "ec2:DeleteLaunchTemplate",
        ):
            assert stmt["Condition"]["StringEquals"]["aws:ResourceTag/portal:managed"] == "true"


# --- the docs do not point at files that do not exist -------------------------------------------


def test_relative_markdown_links_resolve() -> None:
    link = re.compile(r"\]\((?!https?:|#|mailto:)([^)\s#]+)")
    broken = []
    for doc in [REPO / "README.md", REPO / "CONTRIBUTING.md", *REPO.glob("docs/**/*.md")]:
        for target in link.findall(doc.read_text()):
            if not (doc.parent / target).exists():
                broken.append(f"{doc.relative_to(REPO)} -> {target}")
    assert not broken, broken
