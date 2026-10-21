"""Code that runs ON the box every minute, or only in an emergency, exercised for real."""

from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import NoRegionError, ProfileNotFound
from moto import mock_aws

from agent import drain, idle, run, watcher
from agent.imds import Imds
from portal import errors, state, terraform
from portal import sync as sync_calls
from portal.aws import ssm as ssm_calls
from portal.errors import PortalError
from tests import fake_imds

QUIET = idle.Signals(ssm_sessions=0, load5=0.0, docker_builds=0, keepalive_mtime=None)
BUSY = idle.Signals(ssm_sessions=2, load5=0.0, docker_builds=0, keepalive_mtime=None)


# --- agent.run: the entry point systemd calls every minute --------------------------------------


def test_the_tick_entry_point_shuts_the_box_down_when_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idle, "read_signals", lambda: QUIET)
    commands: list[list[str]] = []
    monkeypatch.setattr(run.subprocess, "run", lambda argv, **kw: commands.append(argv))
    state_file = tmp_path / "last-active"
    state_file.write_text("0")  # idle since the epoch
    code = run.main(
        [
            "--idle-timeout-minutes",
            "30",
            "--state-file",
            str(state_file),
            "--log-file",
            str(tmp_path / "log"),
        ]
    )
    assert code == 0
    assert commands == [["shutdown", "-h", "now", commands[0][3]]]
    assert commands[0][3].startswith("portal-agent: idle for")
    assert "SHUTTING DOWN" in (tmp_path / "log").read_text()


def test_the_tick_entry_point_warns_about_expiry_when_it_stays_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idle, "read_signals", lambda: BUSY)
    called: list[Path] = []
    monkeypatch.setattr(run, "warn_expiry", lambda log_file, *a: called.append(log_file))
    run.main(
        [
            "--idle-timeout-minutes",
            "30",
            "--state-file",
            str(tmp_path / "s"),
            "--log-file",
            str(tmp_path / "l"),
        ]
    )
    assert called == [tmp_path / "l"]


def test_an_unreachable_metadata_service_is_a_warning_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idle, "read_signals", lambda: BUSY)
    monkeypatch.setattr(run, "Imds", lambda: Imds("http://127.0.0.1:1", timeout_s=0.2))
    run.warn_expiry(tmp_path / "log", tmp_path / "warned")
    assert "WARN expiry warnings skipped" in (tmp_path / "log").read_text()


def test_an_unexpected_failure_in_the_warning_code_is_logged_and_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> idle.Signals:
        raise ValueError("surprise")

    monkeypatch.setattr(idle, "read_signals", boom)
    run.warn_expiry(tmp_path / "log", tmp_path / "warned")  # must not raise
    assert "ERROR expiry warning" in (tmp_path / "log").read_text()


def test_the_warning_reaches_sessions_and_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    pts = tmp_path / "pts"
    pts.mkdir()
    (pts / "1").write_text("")
    monkeypatch.setattr(idle, "read_signals", lambda: BUSY)
    monkeypatch.setattr("agent.expiry.PTS_DIR", pts)
    soon = (datetime.now(UTC) + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with fake_imds.running() as (fake, base):
        fake.paths["/latest/meta-data/tags/instance/portal:expires-at"] = soon
        monkeypatch.setattr(run, "Imds", lambda: Imds(base))
        monkeypatch.setattr("agent.expiry.broadcast", lambda message, pts_dir=pts: 1)
        run.warn_expiry(tmp_path / "log", tmp_path / "warned")
    assert "warned sessions: portal: this box expires in about" in (tmp_path / "log").read_text()


# --- agent.drain: the real command runner and the real tagging call ------------------------------


def test_real_run_reports_exit_codes_timeouts_and_missing_programs() -> None:
    assert drain.real_run(["echo", "hello"], 5) == (0, "hello\n")
    assert drain.real_run(["sh", "-c", "exit 3"], 5)[0] == 3
    assert drain.real_run(["sleep", "5"], 0.2)[0] == 124  # timed out
    assert drain.real_run(["definitely-not-a-program"], 5)[0] == 127


def test_tagging_the_home_volume_really_calls_ec2(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeImds:
        def get(self, path: str) -> str | None:
            return "us-east-1" if path.endswith("placement/region") else None

    monkeypatch.setattr(drain, "Imds", FakeImds)
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        volume = ec2.create_volume(AvailabilityZone="us-east-1a", Size=1)["VolumeId"]
        drain.tag_with_boto3(volume, "clean")
        tags = ec2.describe_volumes(VolumeIds=[volume])["Volumes"][0]["Tags"]
    assert {"Key": "portal:resumable", "Value": "clean"} in tags


def test_the_watcher_entry_point_drains_once_on_a_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drained: list[bool] = []
    monkeypatch.setattr(drain, "drain", lambda ctx: drained.append(ctx.dry_run) or "clean")  # type: ignore[func-returns-value]
    monkeypatch.setattr(
        watcher, "watch", lambda imds, on_notice, log_file, **k: on_notice({"action": "terminate"})
    )
    assert watcher.main(["--dry-run", "--log-file", str(tmp_path / "log")]) == 0
    assert drained == [True]
    assert "started; polling every 5s" in (tmp_path / "log").read_text()


# --- the laptop side: small helpers with real failure modes ---------------------------------------


def test_ami_lookup_reads_the_public_parameter() -> None:
    asked: list[str] = []

    class Ssm:
        def get_parameter(self, Name: str) -> dict[str, Any]:
            asked.append(Name)
            return {"Parameter": {"Value": "ami-from-canonical"}}

    assert ssm_calls.latest_ubuntu_ami(Ssm(), "arm64") == "ami-from-canonical"
    assert asked == [
        "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
    ]


def test_online_means_the_ping_status_says_so() -> None:
    def ssm_with(status: str | None) -> Any:
        class Pager:
            def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                infos = [] if status is None else [{"PingStatus": status}]
                return [{"InstanceInformationList": infos}]

        class Ssm:
            def get_paginator(self, name: str) -> Pager:
                assert name == "describe_instance_information"
                return Pager()

        return Ssm()

    assert ssm_calls.is_online(ssm_with("Online"), "i-1")
    assert not ssm_calls.is_online(ssm_with("ConnectionLost"), "i-1")
    assert not ssm_calls.is_online(ssm_with(None), "i-1")  # not registered yet


def test_missing_rsync_and_unreadable_sources_are_clear_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sync_calls.shutil, "which", lambda tool: None)
    with pytest.raises(PortalError, match="needs rsync"):
        sync_calls.check_rsync()
    monkeypatch.undo()
    with pytest.raises(PortalError, match="could not read"):
        sync_calls.snapshot("rsync", tmp_path / "does-not-exist")


def test_a_missing_terraform_binary_says_where_to_get_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terraform.shutil, "which", lambda tool: None)
    with pytest.raises(PortalError, match="terraform not found"):
        terraform.run(["version"])


def test_a_damaged_state_file_is_treated_as_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path))
    for junk in ("{not json", "[1, 2, 3]", '"just a string"'):
        (tmp_path / "state.json").write_text(junk)
        assert state.load_control_plane() is None
        assert state.reconcile([]).boxes == []  # and reconcile rewrites a good file
        assert (tmp_path / "state.json").read_text().startswith("{")


def test_reconcile_reports_what_changed_since_last_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path))
    old = state.Box("a", "i-old", "t", "az", "running", "")
    new = state.Box("b", "i-new", "t", "az", "running", "")
    state.reconcile([old])
    result = state.reconcile([new])
    assert result.vanished == ["i-old"] and result.discovered == ["i-new"]


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (ProfileNotFound(profile="nope"), "profile not found"),
        (NoRegionError(), "No AWS region"),
        (ValueError("odd"), "AWS call failed"),
    ],
)
def test_other_aws_failures_still_become_one_line(exc: Exception, needle: str) -> None:
    assert needle in errors.translate_boto_error(exc)


@pytest.mark.parametrize(
    ("stderr", "needle"),
    [
        ("Error: AccessDenied: nope", "did not name the action"),
        ("No valid credential sources found", "No AWS credentials"),
        ("", "no output"),
    ],
)
def test_terraform_failures_are_translated(stderr: str, needle: str) -> None:
    assert needle in errors.translate_terraform_error(stderr)
