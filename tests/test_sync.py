import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from portal import bootstrap
from portal import sync as sync_calls
from portal.cli import app
from portal.errors import PortalError
from tests.test_boxes import aws, runner  # noqa: F401  (aws is a fixture)

needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


# --- pure helpers -----------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "/etc", "../x", "a/../../x"])
def test_dest_cannot_escape_home(bad: str) -> None:
    with pytest.raises(PortalError):
        sync_calls.check_dest(bad)


def test_dest_allows_nested_relative_folders() -> None:
    assert sync_calls.check_dest("work/proj") == "work/proj"


def test_old_or_foreign_rsync_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda tool: "/usr/bin/rsync")
    for banner in (
        "openrsync: protocol version 29\n",
        "rsync  version 2.6.9  protocol version 29\n",
    ):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, banner=banner, **k: subprocess.CompletedProcess(a, 0, banner, ""),
        )
        with pytest.raises(PortalError, match=r"3\.1"):
            sync_calls.check_rsync()


def test_push_never_deletes_and_respects_both_ignore_files() -> None:
    args = sync_calls.push_args("rsync", Path("/work/proj"), "rsync://127.0.0.1:1/home/proj/")
    assert "--delete" not in " ".join(args)
    assert "--filter=:- .gitignore" in args
    assert "--filter=:- .portalignore" in args
    assert args[-2:] == ["/work/proj/", "rsync://127.0.0.1:1/home/proj/"]


def test_bootstrap_runs_a_loopback_only_rsync_daemon() -> None:
    script = bootstrap.render("UTC")
    assert "address = 127.0.0.1" in script
    assert f"port = {bootstrap.RSYNC_PORT}" in script
    assert "uid = dev" in script
    assert "reverse lookup = no" in script  # no DNS dependency on the box
    assert "systemctl restart rsync" in script
    assert script.index("useradd") < script.index("/etc/rsyncd.conf")  # dev exists first


# --- watch loop -------------------------------------------------------------------------


def test_watch_pushes_only_when_the_listing_changes() -> None:
    listings = iter(["a", "a", "b", "b", "b", "c"])
    pushed: list[int] = []
    sync_calls.watch(
        lambda: next(listings),
        lambda: pushed.append(1),
        lambda: None,
        sleep=lambda s: None,
        max_ticks=5,
    )
    assert len(pushed) == 2  # a->b and b->c; the steady ticks did nothing


def test_watch_ticks_faster_than_a_second() -> None:
    sleeps: list[float] = []
    sync_calls.watch(lambda: "x", lambda: None, lambda: None, sleep=sleeps.append, max_ticks=2)
    assert sleeps and all(s <= 0.5 for s in sleeps)


# --- the tunnel -------------------------------------------------------------------------


class FakeProc:
    def __init__(self, exit_code: int | None = None) -> None:
        self.exit_code = exit_code
        self.terminated = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        pass


@contextmanager
def listening_port() -> Iterator[int]:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        yield int(srv.getsockname()[1])


def test_tunnel_is_closed_even_if_the_body_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc()
    launched: list[list[str]] = []

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProc:
        launched.append(argv)
        return proc

    with listening_port() as port:
        monkeypatch.setattr(sync_calls, "free_port", lambda: port)
        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        with pytest.raises(RuntimeError), sync_calls.port_forward("aws", "i-1", "r-1") as got:
            assert got == port
            raise RuntimeError("rsync blew up")
    assert proc.terminated
    argv = launched[0]
    assert argv[argv.index("--document-name") + 1] == "AWS-StartPortForwardingSession"
    assert f'"localPortNumber": ["{port}"]' in argv[argv.index("--parameters") + 1]
    assert f'"portNumber": ["{bootstrap.RSYNC_PORT}"]' in argv[argv.index("--parameters") + 1]


def test_tunnel_that_dies_at_startup_reports_why(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_calls, "free_port", lambda: 1)  # nothing listens on port 1
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: FakeProc(exit_code=254))
    with pytest.raises(PortalError, match="port-forward"), sync_calls.port_forward("a", "i", "r"):
        pass


# --- real rsync, real daemon, no AWS ------------------------------------------------------


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    """A real rsync daemon on loopback, configured like the box's (minus uid/chroot)."""
    root = tmp_path / "boxhome"
    root.mkdir()
    port = sync_calls.free_port()
    conf = tmp_path / "rsyncd.conf"
    # As root (Docker, user namespaces) rsyncd would try to become "nobody" and refuse every
    # connection, so stay root. As a normal user any uid line fails with "setgroups failed".
    identity = "uid = 0\ngid = 0\n" if os.geteuid() == 0 else ""
    conf.write_text(
        f"address = 127.0.0.1\nport = {port}\nuse chroot = no\npid file = {tmp_path}/pid\n"
        "reverse lookup = no\nforward lookup = no\n"
        f"{identity}"
        f"[home]\n    path = {root}\n    read only = no\n"
    )
    proc = subprocess.Popen(  # noqa: S603
        [str(shutil.which("rsync")), "--daemon", "--no-detach", f"--config={conf}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if sync_calls._can_connect(port):
            break
        time.sleep(0.1)
    probe = subprocess.run(  # noqa: S603
        [str(shutil.which("rsync")), f"rsync://127.0.0.1:{port}/home/"],
        capture_output=True,
        text=True,
        check=False,
    )
    if "setgroups failed" in probe.stderr:  # e.g. an unprivileged user namespace
        proc.terminate()
        pytest.skip("this sandbox forbids setgroups, so no rsync daemon can serve here")
    yield port, root
    proc.terminate()
    proc.wait(timeout=5)


def make_project(base: Path) -> Path:
    proj = base / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "node_modules").mkdir()
    (proj / "src" / "app.py").write_text("print('hi')\n")
    (proj / "node_modules" / "junk.js").write_text("x")
    (proj / "secret.env").write_text("TOKEN=1")
    (proj / "notes.tmp").write_text("scratch")
    (proj / ".gitignore").write_text("node_modules/\nsecret.env\n")
    (proj / ".portalignore").write_text("*.tmp\n")
    return proj


@needs_rsync
def test_real_push_honours_gitignore_and_portalignore(
    daemon: tuple[int, Path], tmp_path: Path
) -> None:
    port, root = daemon
    proj = make_project(tmp_path)
    percents: list[int] = []
    sync_calls.run_rsync(
        sync_calls.push_args("rsync", proj, f"rsync://127.0.0.1:{port}/home/proj/"),
        percents.append,
    )
    assert (root / "proj" / "src" / "app.py").read_text() == "print('hi')\n"
    assert not (root / "proj" / "node_modules").exists()
    assert not (root / "proj" / "secret.env").exists()
    assert not (root / "proj" / "notes.tmp").exists()
    assert percents and percents[-1] == 100


@needs_rsync
def test_push_does_not_delete_files_that_only_exist_on_the_box(
    daemon: tuple[int, Path], tmp_path: Path
) -> None:
    port, root = daemon
    (root / "proj").mkdir()
    (root / "proj" / "made-on-box.txt").write_text("keep me")
    proj = make_project(tmp_path)
    sync_calls.run_rsync(
        sync_calls.push_args("rsync", proj, f"rsync://127.0.0.1:{port}/home/proj/")
    )
    assert (root / "proj" / "made-on-box.txt").read_text() == "keep me"


@needs_rsync
def test_watch_lands_an_edit_on_the_box_within_a_second(
    daemon: tuple[int, Path], tmp_path: Path
) -> None:
    port, root = daemon
    proj = make_project(tmp_path)
    url = f"rsync://127.0.0.1:{port}/home/proj/"
    args = sync_calls.push_args("rsync", proj, url)
    sync_calls.run_rsync(args)
    target = root / "proj" / "src" / "app.py"
    edited_at: list[float] = []

    def tick(seconds: float) -> None:
        time.sleep(seconds)
        if not edited_at:  # edit once, during the first sleep
            (proj / "src" / "app.py").write_text("print('edited, longer')\n")
            edited_at.append(time.monotonic())

    synced_at: list[float] = []
    sync_calls.watch(
        lambda: sync_calls.snapshot("rsync", proj),
        lambda: sync_calls.run_rsync(args),
        lambda: synced_at.append(time.monotonic()),
        sleep=tick,
        max_ticks=4,
    )
    assert target.read_text() == "print('edited, longer')\n"
    assert synced_at[0] - edited_at[0] < 1.0


@needs_rsync
def test_changes_to_ignored_files_do_not_trigger_a_push(tmp_path: Path) -> None:
    proj = make_project(tmp_path)
    before = sync_calls.snapshot("rsync", proj)
    (proj / "node_modules" / "junk.js").write_text("much longer content now")
    (proj / "secret.env").write_text("TOKEN=2222222")
    assert sync_calls.snapshot("rsync", proj) == before
    (proj / "src" / "app.py").write_text("print('changed')\n")
    assert sync_calls.snapshot("rsync", proj) != before


@needs_rsync
def test_rsync_failure_explains_likely_causes() -> None:
    port = sync_calls.free_port()  # nothing listening
    with pytest.raises(PortalError, match="bootstrapping"):
        sync_calls.run_rsync(
            sync_calls.push_args("rsync", Path("."), f"rsync://127.0.0.1:{port}/home/x/")
        )


# --- the command -------------------------------------------------------------------------


def test_sync_command_pushes_to_the_forwarded_port(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from portal.aws import ssm as ssm_calls

    assert (
        runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro", "--name", "s1"]).exit_code == 0
    )
    proj = tmp_path / "myproj"
    proj.mkdir()
    monkeypatch.setattr("portal.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(sync_calls, "check_rsync", lambda: "/usr/bin/rsync")
    monkeypatch.setattr(ssm_calls, "is_online", lambda ssm, iid: True)
    ran: list[list[str]] = []
    monkeypatch.setattr(sync_calls, "run_rsync", lambda args, on_percent=None: ran.append(args))

    @contextmanager
    def fake_forward(aws_cli: str, instance_id: str, region: str) -> Iterator[int]:
        yield 4242

    monkeypatch.setattr(sync_calls, "port_forward", fake_forward)
    result = runner.invoke(app, ["sync", "--name", "s1", "--src", str(proj)])
    assert result.exit_code == 0, result.output
    (args,) = ran
    assert args[-1] == "rsync://127.0.0.1:4242/home/myproj/"
    assert "synced" in result.output


def test_sync_without_rsync_installed_says_so(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # portal.cli and portal.sync share one shutil module, so this patches both.
    monkeypatch.setattr(
        shutil, "which", lambda tool: None if tool == "rsync" else f"/usr/bin/{tool}"
    )
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert "needs rsync" in result.output
    assert "Traceback" not in result.output


def test_sync_with_no_box(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr("portal.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(sync_calls, "check_rsync", lambda: "/usr/bin/rsync")
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert "portal up" in result.output
