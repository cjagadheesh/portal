"""Production behaviours: locking, crash handling, validation, limits and private files."""

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from portal import cli, config, state
from portal.cli import app
from portal.errors import PortalError
from portal.lock import box_lock
from tests.test_boxes import aws, runner  # noqa: F401  (aws is a fixture)

UP = ["up", "--ttl", "1h", "-t", "t3.micro", "--name", "pr"]


# --- locking ----------


def test_a_second_command_on_the_same_box_is_refused_not_raced() -> None:
    with (
        box_lock("dev"),
        pytest.raises(PortalError, match="already working on box 'dev'"),
        box_lock("dev"),
    ):
        pass


def test_different_boxes_do_not_block_each_other_and_the_lock_is_released() -> None:
    with box_lock("a"), box_lock("b"):
        pass
    with box_lock("a"):  # released on exit
        pass


def test_a_lock_held_by_another_process_blocks_us_and_dies_with_it(tmp_path: Path) -> None:
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time; from portal.lock import box_lock\n"
            "with box_lock('shared'):\n    print('held', flush=True); time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ},
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        with pytest.raises(PortalError, match="already working"), box_lock("shared"):
            pass
    finally:
        holder.kill()  # no cleanup code gets to run...
        holder.wait()
    with box_lock("shared"):  # ...and the lock is still free: the OS released it
        pass


def test_up_refuses_while_another_up_holds_the_lock(aws: Any) -> None:  # noqa: F811
    with box_lock("pr"):
        result = runner.invoke(app, UP)
    assert result.exit_code == 1 and "already working on box 'pr'" in result.output
    assert not [r for r in aws.describe_instances()["Reservations"]]  # nothing launched


@pytest.mark.parametrize("command", [["down", "--name", "pr"], ["extend", "1h", "--name", "pr"]])
def test_down_and_extend_take_the_same_lock(aws: Any, command: list[str]) -> None:  # noqa: F811
    with box_lock("pr"):
        result = runner.invoke(app, command)
    assert result.exit_code == 1 and "already working" in result.output


# --- the running-box limit ----------


def test_the_box_limit_stops_a_fourth_box_with_instructions(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".portal.yaml").write_text("max_running_boxes: 2\n")
    monkeypatch.chdir(tmp_path)
    for name in ("one", "two"):
        assert (
            runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro", "--name", name]).exit_code
            == 0
        )
    third = runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro", "--name", "three"])
    assert third.exit_code == 1
    assert "2 running boxes (one, two)" in third.output and "max_running_boxes: 2" in third.output
    assert runner.invoke(app, ["down", "--name", "one", "--yes"]).exit_code == 0
    assert (
        runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro", "--name", "three"]).exit_code
        == 0
    )


# --- input validation ----------


@pytest.mark.parametrize("region", ["", "mars", "us-east", "US-EAST-1", "us-east-1; rm -rf /"])
def test_bad_regions_are_rejected(region: str) -> None:
    with pytest.raises(PortalError, match="region"):
        config.check_region(region)


@pytest.mark.parametrize(
    "region", ["us-east-1", "ap-south-1", "eu-central-2", "us-gov-west-1", "cn-north-1"]
)
def test_real_regions_pass(region: str) -> None:
    assert config.check_region(region) == region


@pytest.mark.parametrize(
    "name", ["c7g.xlarge", "t3.micro", "m5.metal", "u-6tb1.112xlarge", "mac2-m2.metal"]
)
def test_real_instance_types_pass(name: str) -> None:
    assert config.check_instance_type(name) == name


@pytest.mark.parametrize("name", ["", "c7g", "C7G.XLARGE", "c7g.xlarge extra", "$(id)", "a.b.c"])
def test_bad_instance_types_are_rejected(name: str) -> None:
    with pytest.raises(PortalError, match="instance type"):
        config.check_instance_type(name)


def test_up_rejects_a_mistyped_instance_type_before_calling_aws(aws: Any) -> None:  # noqa: F811
    result = runner.invoke(app, ["up", "--ttl", "1h", "-t", "c7g"])
    assert result.exit_code == 1 and "does not look like an instance type" in result.output


def test_init_rejects_a_mistyped_region_before_running_terraform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from portal import terraform

    monkeypatch.setattr(terraform, "init", lambda: pytest.fail("terraform must not run"))
    result = runner.invoke(app, ["init", "--region", "mumbai", "--budget-usd", "5"])
    assert result.exit_code == 1 and "does not look like an AWS region" in result.output


def test_config_files_get_the_same_validation(tmp_path: Path) -> None:
    (tmp_path / ".portal.yaml").write_text("region: mumbai\n")
    with pytest.raises(PortalError, match="region"):
        config.load(tmp_path)
    (tmp_path / ".portal.yaml").write_text("instance_types: [c7g]\n")
    with pytest.raises(PortalError, match="instance type"):
        config.load(tmp_path)
    (tmp_path / ".portal.yaml").write_text("max_running_boxes: 0\n")
    with pytest.raises(PortalError, match="max_running_boxes"):
        config.load(tmp_path)


# --- private files ----------


def test_state_is_private_to_the_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("PORTAL_HOME", str(home))
    state.save_control_plane(
        state.ControlPlane(
            region="x-1", instance_profile_name="p", security_group_id="sg", budget_name="b"
        )
    )
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "state.json").stat().st_mode) == 0o600
    with box_lock("x"):
        pass
    assert stat.S_IMODE((home / "locks").stat().st_mode) == 0o700


# --- the last-resort crash handler ----------


def boom(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("something nobody planned for")


def test_an_unexpected_error_becomes_one_line_and_a_private_crash_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path / "h"))
    monkeypatch.setattr(sys, "argv", ["portal", "status"])
    monkeypatch.setattr(state, "load_control_plane", boom)
    with pytest.raises(SystemExit) as exit_info:
        cli.run()
    assert exit_info.value.code == 2
    shown = capsys.readouterr()
    text = shown.out + shown.err
    assert "unexpected error (RuntimeError: something nobody planned for)" in text
    assert "Traceback" not in text and "--debug" in text
    log = tmp_path / "h" / "last-crash.log"
    assert "RuntimeError: something nobody planned for" in log.read_text()
    assert log.read_text().splitlines()[0].endswith(" status")  # version and arguments
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_debug_mode_lets_the_real_traceback_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("PORTAL_DEBUG", "1")
    monkeypatch.setattr(sys, "argv", ["portal", "status"])
    monkeypatch.setattr(state, "load_control_plane", boom)
    with pytest.raises(RuntimeError, match="nobody planned"):
        cli.run()


def test_the_crash_log_does_not_include_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret-value-123")
    monkeypatch.setattr(sys, "argv", ["portal", "status"])
    monkeypatch.setattr(state, "load_control_plane", boom)
    with pytest.raises(SystemExit):
        cli.run()
    assert "super-secret-value-123" not in (tmp_path / "h" / "last-crash.log").read_text()


def test_expected_failures_do_not_go_through_the_crash_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path / "h"))
    monkeypatch.setattr(sys, "argv", ["portal", "up", "--ttl", "1h", "-t", "c7g"])
    with pytest.raises(SystemExit) as exit_info:
        cli.run()
    assert exit_info.value.code == 1  # an ordinary, explained failure
    assert not (tmp_path / "h" / "last-crash.log").exists()


def test_python_dash_m_portal_works() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "portal", "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0 and result.stdout.startswith("portal ")
