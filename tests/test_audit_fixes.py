"""Regression tests for bugs found in the full-project audit (see docs/what-broke.md)."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from portal import sync as sync_calls
from portal import terraform
from portal.aws import ec2 as ec2_calls
from portal.aws import home as home_calls
from portal.aws import pricing as pricing_calls
from portal.aws import ssm as ssm_calls
from portal.cli import app
from portal.errors import PortalError
from tests.test_boxes import aws, runner  # noqa: F401  (aws is a fixture)
from tests.test_fallback_resume import ScriptedEc2, running

UP = ["up", "--ttl", "1h", "-t", "t3.micro", "-t", "t3.small", "--name", "au"]


def test_a_plan_with_square_brackets_is_printed_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = 'instance_types = ["a.large", "b.large"]\n[sender] will be created'
    monkeypatch.setattr(terraform, "init", lambda: None)
    monkeypatch.setattr(terraform, "plan", lambda variables: (True, plan))
    result = runner.invoke(app, ["init", "--region", "xx-test-1", "--budget-usd", "5", "--dry-run"])
    assert result.exit_code == 0
    assert '["a.large", "b.large"]' in result.output and "[sender]" in result.output


def test_init_reports_a_terraform_failure_as_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        raise PortalError("terraform failed: bad [thing] happened")

    monkeypatch.setattr(terraform, "init", fail)
    result = runner.invoke(app, ["init", "--region", "xx-test-1", "--budget-usd", "5"])
    assert result.exit_code == 1 and "bad [thing] happened" in result.output


@pytest.fixture
def scripted(aws: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    holder = ScriptedEc2(aws, set())
    monkeypatch.setattr(
        "portal.aws.ec2.client", lambda service, region: holder if service == "ec2" else None
    )
    return holder


def test_repeated_instance_types_are_tried_once(scripted: Any) -> None:
    scripted.no_room = {("t3.micro", "spot")}
    result = runner.invoke(
        app,
        ["up", "--ttl", "1h", "-t", "t3.micro", "-t", "t3.micro", "-t", "t3.small", "--name", "au"],
    )
    assert result.exit_code == 0, result.output
    assert scripted.attempts == [("t3.micro", "spot"), ("t3.small", "spot")]


def test_a_type_not_offered_in_the_az_moves_on_to_the_next_type(scripted: Any) -> None:
    scripted.no_room = {("t3.micro", "spot")}
    scripted.error = {"ErrorCode": "Unsupported", "ErrorMessage": "not offered in this AZ"}
    result = runner.invoke(app, UP)
    assert result.exit_code == 0, result.output
    assert running(scripted)[0]["InstanceType"] == "t3.small"


def test_a_permissions_error_stops_the_walk_instead_of_trying_every_type(scripted: Any) -> None:
    scripted.no_room = {("t3.micro", "spot"), ("t3.small", "spot")}
    scripted.error = {"ErrorCode": "UnauthorizedOperation", "ErrorMessage": "no"}
    result = runner.invoke(app, UP, input="n\n")
    assert result.exit_code == 1 and scripted.attempts == [("t3.micro", "spot")]


def test_the_network_interface_is_tagged_at_creation_too(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []
    real = aws.create_launch_template

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(aws, "create_launch_template", spy)
    monkeypatch.setattr("portal.aws.ec2.client", lambda service, region: aws)
    assert runner.invoke(app, UP).exit_code == 0
    specs = seen[0]["LaunchTemplateData"]["TagSpecifications"]
    assert {s["ResourceType"] for s in specs} >= {"instance", "volume", "network-interface"}
    for spec in specs:
        keys = {t["Key"] for t in spec["Tags"]}
        assert {"portal:managed", "portal:owner", "portal:box", "portal:expires-at"} <= keys


def test_if_cleanup_itself_fails_the_user_is_told_which_box_to_remove(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def attach_fails(*args: Any, **kwargs: Any) -> None:
        raise ClientError({"Error": {"Code": "VolumeInUse", "Message": "x"}}, "AttachVolume")

    def terminate_fails(*args: Any, **kwargs: Any) -> None:
        raise ClientError(
            {"Error": {"Code": "RequestLimitExceeded", "Message": "x"}}, "TerminateInstances"
        )

    monkeypatch.setattr(home_calls, "attach_home", attach_fails)
    monkeypatch.setattr(ec2_calls, "terminate_boxes", terminate_fails)
    result = runner.invoke(app, UP)
    assert result.exit_code == 1
    assert "could not terminate" in result.output and "portal down --name au" in result.output
    assert "Traceback" not in result.output


# --- sync --watch survives the tunnel being closed by SSM's idle timeout -------------------------


def test_watch_reconnects_when_the_tunnel_has_dropped(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert runner.invoke(app, UP).exit_code == 0
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr("portal.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(sync_calls, "check_rsync", lambda: "/usr/bin/rsync")
    monkeypatch.setattr(ssm_calls, "is_online", lambda ssm, iid: True)
    opened: list[int] = []
    closed: list[int] = []

    @contextmanager
    def fake_forward(aws_cli: str, instance_id: str, region: str) -> Iterator[int]:
        port = 4000 + len(opened)
        opened.append(port)
        try:
            yield port
        finally:
            closed.append(port)

    pushes: list[str] = []

    def fake_rsync(args: list[str], on_percent: Any = None) -> None:
        pushes.append(args[-1])
        if len(pushes) == 2:  # the first push inside --watch finds the tunnel dead
            raise PortalError("rsync failed (exit 10): connection reset")

    def one_change(take: Any, push: Any, on_pushed: Any, **kwargs: Any) -> None:
        push()
        on_pushed()

    monkeypatch.setattr(sync_calls, "port_forward", fake_forward)
    monkeypatch.setattr(sync_calls, "run_rsync", fake_rsync)
    monkeypatch.setattr(sync_calls, "watch", one_change)
    result = runner.invoke(app, ["sync", "--name", "au", "--src", str(project), "--watch"])
    assert result.exit_code == 0, result.output
    assert "reconnecting" in result.output
    assert opened == [4000, 4001] and closed == [4000, 4001]  # old tunnel closed, new one opened
    assert pushes[0].startswith("rsync://127.0.0.1:4000/") and pushes[-1].startswith(
        "rsync://127.0.0.1:4001/"
    )


def test_a_second_failure_after_reconnecting_is_reported_not_retried_forever(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert runner.invoke(app, UP).exit_code == 0
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr("portal.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(sync_calls, "check_rsync", lambda: "/usr/bin/rsync")
    monkeypatch.setattr(ssm_calls, "is_online", lambda ssm, iid: True)

    @contextmanager
    def fake_forward(aws_cli: str, instance_id: str, region: str) -> Iterator[int]:
        yield 4000

    calls: list[int] = []

    def flaky(args: list[str], on_percent: Any = None) -> None:
        calls.append(1)
        if len(calls) > 1:
            raise PortalError("rsync failed (exit 10): still down")

    monkeypatch.setattr(sync_calls, "port_forward", fake_forward)
    monkeypatch.setattr(sync_calls, "run_rsync", flaky)
    monkeypatch.setattr(sync_calls, "watch", lambda take, push, done, **k: push())
    result = runner.invoke(app, ["sync", "--name", "au", "--src", str(project), "--watch"])
    assert result.exit_code == 1 and "still down" in result.output
    assert len(calls) == 3  # initial push, failed push, one retry: no loop


def test_status_marks_on_demand_boxes_and_does_not_show_a_spot_price_for_them(
    scripted: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(pricing_calls, "client", lambda: None)
    monkeypatch.setattr(pricing_calls, "on_demand_usd_per_hour", lambda *a: 0.5)
    scripted.no_room = {("t3.micro", "spot"), ("t3.small", "spot")}
    assert runner.invoke(app, UP, input="y\n").exit_code == 0
    monkeypatch.setattr(ec2_calls, "spot_price", lambda *a: asked.append("asked") or 0.01)
    result = runner.invoke(app, ["status"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "on-demand" in result.output
    assert asked == []  # never asked for a spot price for an on-demand box


def test_a_state_file_from_another_version_is_ignored_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from portal import state

    monkeypatch.setenv("PORTAL_HOME", str(tmp_path))
    (tmp_path / "state.json").write_text('{"control_plane": {"region": "x", "unexpected": 1}}')
    assert state.load_control_plane() is None


def test_an_unwritable_home_is_one_clear_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("")
    monkeypatch.setenv("PORTAL_HOME", str(blocker / "inside"))  # can't mkdir under a file
    monkeypatch.setattr(terraform, "init", lambda: None)
    monkeypatch.setattr(terraform, "plan", lambda variables: (False, "No changes."))
    monkeypatch.setattr(
        terraform,
        "outputs",
        lambda: {"instance_profile_name": "p", "security_group_id": "sg-1", "budget_name": "b"},
    )
    result = runner.invoke(app, ["init", "--region", "xx-test-1", "--budget-usd", "5"])
    assert result.exit_code == 1
    assert "File system problem" in result.output and "Traceback" not in result.output


def test_missing_terraform_outputs_are_reported_not_a_keyerror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path))
    monkeypatch.setattr(terraform, "init", lambda: None)
    monkeypatch.setattr(terraform, "plan", lambda variables: (False, "No changes."))
    monkeypatch.setattr(terraform, "outputs", lambda: {"budget_name": "b"})
    result = runner.invoke(app, ["init", "--region", "xx-test-1", "--budget-usd", "5"])
    assert result.exit_code == 1
    assert "did not report instance_profile_name, security_group_id" in result.output
