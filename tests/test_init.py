import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from portal import terraform
from portal.cli import app
from portal.errors import translate_terraform_error

runner = CliRunner()
OUTPUTS = {
    "instance_profile_name": {"value": "portal-profile"},
    "security_group_id": {"value": "sg-123"},
    "budget_name": {"value": "portal-monthly"},
}


@pytest.fixture
def fake_tf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    """Replace the terraform binary; plan reports changes only until the first apply."""
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path))
    calls: list[list[str]] = []
    applied = False

    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal applied
        calls.append(args)
        code, out = 0, ""
        if args[0] == "plan":
            code, out = (0, "No changes.") if applied else (2, "Plan: 3 to add")
        elif args[0] == "apply":
            applied = True
        elif args[0] == "output":
            out = json.dumps(OUTPUTS)
        return subprocess.CompletedProcess(args, code, out, "")

    monkeypatch.setattr(terraform, "run", fake_run)
    return calls


ARGS = ["init", "--region", "xx-test-1", "--budget-usd", "50"]


def test_dry_run_prints_plan_and_changes_nothing(fake_tf: list[list[str]], tmp_path: Path) -> None:
    result = runner.invoke(app, [*ARGS, "--dry-run"])
    assert result.exit_code == 0
    assert "Plan: 3 to add" in result.output
    assert not any(c[0] == "apply" for c in fake_tf)
    assert not (tmp_path / "state.json").exists()


def test_init_twice_applies_once(fake_tf: list[list[str]], tmp_path: Path) -> None:
    assert runner.invoke(app, ARGS).exit_code == 0
    second = runner.invoke(app, ARGS)
    assert second.exit_code == 0
    assert "nothing to do" in second.output
    assert sum(c[0] == "apply" for c in fake_tf) == 1
    saved = json.loads((tmp_path / "state.json").read_text())["control_plane"]
    assert saved["security_group_id"] == "sg-123"
    assert saved["region"] == "xx-test-1"


def test_access_denied_names_the_iam_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path))
    stderr = (
        "User: arn:aws:iam::000000000000:user/x is not authorized to perform: "
        "iam:CreateRole on resource: arn:aws:iam::000000000000:role/portal"
    )
    monkeypatch.setattr(
        terraform, "run", lambda args: subprocess.CompletedProcess(args, 1, "", stderr)
    )
    result = runner.invoke(app, ARGS)
    assert result.exit_code == 1
    assert "iam:CreateRole" in result.output
    assert "Traceback" not in result.output


def test_translate_prefers_the_error_line_over_the_source_excerpt() -> None:
    stderr = (
        "Planning failed.\n\nError: Invalid count argument\n\n  on main.tf line 4:\n"
        '   4: resource "x" "y" {\n'
    )
    message = translate_terraform_error(stderr)
    assert message == "terraform failed: Invalid count argument"


def test_translate_falls_back_to_the_last_line_when_there_is_no_error_line() -> None:
    assert "boom" in translate_terraform_error("something\nboom\n")


def test_translate_bad_credentials_gets_a_login_hint() -> None:
    stderr = (
        "Error: Retrieving AWS account details: validating provider credentials: "
        "api error InvalidClientTokenId: The security token included in the request is invalid.\n"
        '  12: provider "aws" {\n'
    )
    assert "credentials" in translate_terraform_error(
        stderr
    ) and "sso login" in translate_terraform_error(stderr)


def test_reaper_dry_run_flag_reaches_terraform(fake_tf: list[list[str]]) -> None:
    assert runner.invoke(app, [*ARGS, "--reaper-dry-run", "--dry-run"]).exit_code == 0
    plan = next(c for c in fake_tf if c[0] == "plan")
    assert "-var=reaper_dry_run=true" in plan
    fake_tf.clear()
    assert runner.invoke(app, [*ARGS, "--dry-run"]).exit_code == 0
    assert not any("reaper_dry_run" in a for c in fake_tf for a in c)


PLAN_ADD_ONLY = "Plan: 12 to add, 0 to change, 0 to destroy."
PLAN_REPLACES = (
    "  # aws_security_group.box must be replaced\nPlan: 1 to add, 0 to change, 1 to destroy."
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [(PLAN_ADD_ONLY, 0), (PLAN_REPLACES, 1), ("No changes.", 0), ("", 0)],
)
def test_resources_to_destroy_reads_the_plan_summary(text: str, expected: int) -> None:
    assert terraform.resources_to_destroy(text) == expected


def destructive_fake(monkeypatch: pytest.MonkeyPatch, plan: str) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(terraform, "init", lambda: None)
    monkeypatch.setattr(terraform, "plan", lambda variables: (True, plan))
    monkeypatch.setattr(terraform, "apply", lambda variables: calls.append("apply"))
    monkeypatch.setattr(
        terraform,
        "outputs",
        lambda: {k: v["value"] for k, v in OUTPUTS.items()},
    )
    return calls


def test_a_plan_that_destroys_things_asks_first_and_defaults_to_no(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path))
    calls = destructive_fake(monkeypatch, PLAN_REPLACES)
    declined = runner.invoke(app, ARGS, input="n\n")
    assert declined.exit_code == 1 and "DESTROYS 1 resource" in declined.output
    assert calls == []
    no_answer = runner.invoke(app, ARGS)  # no stdin at all: still no
    assert no_answer.exit_code == 1 and calls == []


def test_yes_flag_applies_a_destructive_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path))
    calls = destructive_fake(monkeypatch, PLAN_REPLACES)
    assert runner.invoke(app, [*ARGS, "--yes"]).exit_code == 0
    assert calls == ["apply"]


def test_a_plan_that_only_adds_needs_no_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path))
    calls = destructive_fake(monkeypatch, PLAN_ADD_ONLY)
    assert runner.invoke(app, ARGS).exit_code == 0
    assert calls == ["apply"]
