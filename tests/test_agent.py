import base64
import io
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

from agent import idle, run
from portal import bootstrap
from portal.aws import ssm as ssm_calls
from portal.cli import app
from tests.test_boxes import aws, runner  # noqa: F401  (aws is a fixture)

QUIET = idle.Signals(ssm_sessions=0, load5=0.0, docker_builds=0, keepalive_mtime=None)
BUSY = idle.Signals(ssm_sessions=1, load5=0.0, docker_builds=0, keepalive_mtime=None)
REPO = Path(__file__).resolve().parent.parent


def test_idle_box_is_logged_then_shut_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(idle, "read_signals", lambda: QUIET)
    state, log = tmp_path / "last-active", tmp_path / "agent.log"
    state.write_text("0")  # last active at the epoch: far past any timeout
    reasons: list[str] = []

    def shutdown(reason: str) -> None:
        # The reason must already be on disk: after this call the box may be gone.
        assert "SHUTTING DOWN" in log.read_text()
        reasons.append(reason)

    assert run.tick(1800, state, log, shutdown=shutdown) is True
    assert len(reasons) == 1 and "idle for" in reasons[0]


def test_active_box_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(idle, "read_signals", lambda: BUSY)
    state, log = tmp_path / "s", tmp_path / "l"
    state.write_text("0")
    called: list[str] = []
    assert run.tick(1800, state, log, shutdown=called.append) is False
    assert called == []
    assert "ok: active: 1 SSM session(s) open" in log.read_text()


def test_dry_run_logs_but_never_powers_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(idle, "read_signals", lambda: QUIET)
    state, log = tmp_path / "s", tmp_path / "l"
    state.write_text("0")
    called: list[str] = []
    assert run.tick(1800, state, log, dry_run=True, shutdown=called.append) is False
    assert called == []
    assert "DRY RUN would shut down" in log.read_text()


def test_a_crash_is_logged_and_reraised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> idle.Signals:
        raise OSError("proc vanished")

    monkeypatch.setattr(idle, "read_signals", broken)
    with pytest.raises(OSError):
        run.tick(1800, tmp_path / "s", tmp_path / "l")
    assert "ERROR" in (tmp_path / "l").read_text()


# --- what actually ships to the box -------------------------------------------------------


def shipped(script: str) -> dict[str, bytes]:
    match = re.search(r"<<'AGENT_TGZ' \| tar xz -C /opt/portal\n(.+?)\nAGENT_TGZ", script, re.S)
    assert match, "agent tarball missing from the bootstrap script"
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(match[1])), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}  # type: ignore[union-attr]


def test_the_box_gets_exactly_the_repos_agent_code() -> None:
    files = shipped(bootstrap.render("UTC"))
    assert set(files) == {f"agent/{p.name}" for p in (REPO / "agent").glob("*.py")}
    assert {"agent/idle.py", "agent/run.py", "agent/expiry.py", "agent/imds.py"} <= set(files)
    for name, data in files.items():
        # The box gets the repo's code without its docstrings and comments (16 KB user-data cap).
        assert data.decode() == bootstrap.minify((REPO / name).read_text())


def test_user_data_stays_well_under_the_16kb_ec2_limit() -> None:
    assert len(bootstrap.render("Asia/Kolkata", 120).encode()) < 15_000


def test_archive_is_deterministic() -> None:
    assert bootstrap.agent_archive() == bootstrap.agent_archive()


def test_systemd_units_run_every_minute_and_start_last() -> None:
    script = bootstrap.render("UTC", idle_timeout_minutes=45)
    assert "--idle-timeout-minutes 45" in script
    assert "OnUnitActiveSec=60" in script
    assert script.index("daemon-reload") < script.index("enable --now portal-agent.timer")
    # Enabled after everything else, so the idle clock starts on a finished box.
    assert script.index("enable --now portal-agent.timer") > script.index("systemctl restart rsync")
    assert script.index("enable --now portal-agent.timer") < script.index(
        "touch /var/lib/portal/bootstrap.done"
    )


def test_the_shipped_agent_really_runs(tmp_path: Path) -> None:
    """Unpack the bytes the box would get and run one dry-run tick with them."""
    for name, data in shipped(bootstrap.render("UTC")).items():
        target = tmp_path / "opt" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    state = tmp_path / "last-active"
    state.write_text("0")
    result = subprocess.run(  # noqa: S603
        [
            sys.executable, "-m", "agent.run", "--idle-timeout-minutes", "30", "--dry-run",
            "--state-file", str(state), "--log-file", str(tmp_path / "agent.log"),
        ],
        cwd=tmp_path / "opt",
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    log = (tmp_path / "agent.log").read_text()
    assert "ERROR" not in log
    assert "ok:" in log or "DRY RUN" in log


# --- launch template and CLI ----------------------------------------------------------------


def test_instance_initiated_shutdown_terminates_the_box(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".portal.yaml").write_text("idle_timeout_minutes: 45\n")
    monkeypatch.chdir(tmp_path)
    seen: list[dict[str, Any]] = []
    real = aws.create_launch_template

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(aws, "create_launch_template", spy)
    monkeypatch.setattr("portal.aws.ec2.client", lambda service, region: aws)
    assert runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro"]).exit_code == 0
    data = seen[0]["LaunchTemplateData"]
    # `shutdown -h now` from the agent must end the instance, not leave a stopped, billing one.
    assert data["InstanceInitiatedShutdownBehavior"] == "terminate"
    assert "--idle-timeout-minutes 45" in base64.b64decode(data["UserData"]).decode()


def test_agent_log_tails_the_log_over_ssm(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro"]).exit_code == 0
    monkeypatch.setattr("portal.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ssm_calls, "is_online", lambda ssm, iid: True)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], check: bool) -> Any:
        calls.append(argv)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr("portal.cli.subprocess.run", fake_run)
    assert runner.invoke(app, ["agent-log", "-n", "20"]).exit_code == 0
    assert runner.invoke(app, ["agent-log", "--follow"]).exit_code == 0
    plain, follow = (c[c.index("--parameters") + 1] for c in calls)
    assert "tail -n 20 /var/log/portal-agent.log" in plain and "tail -n 20 -f" not in plain
    assert "tail -n 50 -f /var/log/portal-agent.log" in follow
    assert "no agent log yet" in plain  # friendly message before the first tick


def test_agent_log_with_no_box(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr("portal.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    result = runner.invoke(app, ["agent-log"])
    assert result.exit_code == 1
    assert "portal up" in result.output


def test_minify_keeps_behaviour_and_drops_prose() -> None:
    source = (
        '"""module doc"""\n# a comment\nX = 1\n\n\n'
        'def f(a):\n    """function doc"""\n    return a + X\n\n\n'
        'class C:\n    """class doc"""\n'
    )
    out = bootstrap.minify(source)
    assert "doc" not in out and "comment" not in out
    namespace: dict[str, object] = {}
    exec(compile(out, "<min>", "exec"), namespace)  # noqa: S102 - our own tiny fixture
    assert namespace["f"](2) == 3  # type: ignore[operator]
