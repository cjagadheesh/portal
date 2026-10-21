"""Edge cases of the commands that users hit in real life: wrong answers, empty accounts,
bad config and a shell that exits non-zero."""

from pathlib import Path
from typing import Any

import pytest

from portal.aws import ssm as ssm_calls
from portal.cli import app
from tests.test_boxes import aws, runner  # noqa: F401  (aws is a fixture)
from tests.test_home import home_volumes

UP = ["up", "--ttl", "1h", "-t", "t3.micro", "--name", "ed"]


def test_region_mismatch_stops_every_command_before_it_calls_aws(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PORTAL_REGION", "eu-west-9")
    for command in (
        ["up", "--ttl", "1h", "-t", "t3.micro"],
        ["down"],
        ["volumes"],
        ["extend", "1h"],
    ):
        result = runner.invoke(app, command)
        assert result.exit_code == 1 and "Region mismatch" in result.output, command


def test_a_broken_config_file_is_reported_with_its_path_by_every_command(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".portal.yaml").write_text("instance_type: oops\n")
    monkeypatch.chdir(tmp_path)
    for command in (["up"], ["down"], ["extend", "1h"], ["cost"], ["volumes"]):
        result = runner.invoke(app, command)
        assert result.exit_code == 1, command
        # The terminal wraps long temp paths mid-word; compare with the line breaks removed.
        unwrapped = "".join(result.output.split())
        assert ".portal.yaml" in unwrapped and "Traceback" not in unwrapped, command


def test_declining_the_down_prompt_leaves_the_box_running(aws: Any) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    result = runner.invoke(app, ["down", "--name", "ed"], input="n\n")
    assert "aborted" in result.output
    running = [
        i
        for r in aws.describe_instances()["Reservations"]
        for i in r["Instances"]
        if i["State"]["Name"] == "running"
    ]
    assert len(running) == 1


def test_a_wrong_name_at_the_purge_prompt_deletes_nothing(aws: Any) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    result = runner.invoke(app, ["down", "--name", "ed", "--purge"], input="nope\n")
    assert result.exit_code == 1 and "nothing deleted" in result.output
    assert len(home_volumes(aws)) == 1


def test_purge_with_nothing_to_delete_says_so(aws: Any) -> None:  # noqa: F811
    result = runner.invoke(app, ["down", "--name", "ghost", "--purge"])
    assert (
        result.exit_code == 0 and "no running box named 'ghost' and no home volume" in result.output
    )


def test_empty_account_listings_say_so(aws: Any) -> None:  # noqa: F811
    assert "no home volumes" in runner.invoke(app, ["volumes"]).output
    assert "no running boxes" in runner.invoke(app, ["status"]).output


def test_ssh_passes_the_sessions_exit_code_through(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert runner.invoke(app, UP).exit_code == 0
    monkeypatch.setattr("portal.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ssm_calls, "is_online", lambda ssm, iid: True)
    monkeypatch.setattr(
        "portal.cli.subprocess.run", lambda argv, check: type("R", (), {"returncode": 130})()
    )
    assert runner.invoke(app, ["ssh", "--name", "ed"]).exit_code == 130


def test_a_box_that_never_registers_with_ssm_times_out_with_advice(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert runner.invoke(app, UP).exit_code == 0
    monkeypatch.setattr("portal.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ssm_calls, "is_online", lambda ssm, iid: False)
    monkeypatch.setattr(ssm_calls.time, "sleep", lambda s: None)
    ticks = iter(range(0, 10_000, 40))
    monkeypatch.setattr(ssm_calls.time, "monotonic", lambda: next(ticks))
    result = runner.invoke(app, ["ssh", "--name", "ed", "--timeout", "60"])
    assert result.exit_code == 1 and "did not register with SSM" in result.output


def test_sync_rejects_a_destination_that_climbs_out_of_home(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("portal.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr("portal.sync.check_rsync", lambda: "/usr/bin/rsync")
    result = runner.invoke(app, ["sync", "--src", str(tmp_path), "--dest", "../../etc"])
    assert result.exit_code == 1 and "Invalid --dest" in result.output


def test_version_and_bare_invocation_do_not_need_aws() -> None:
    assert "portal " in runner.invoke(app, ["--version"]).output
    assert "Usage" in runner.invoke(app, []).output
