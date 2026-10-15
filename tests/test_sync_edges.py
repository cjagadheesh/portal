"""Edges of portal/sync.py, driven by small stand-in programs instead of patched subprocess calls.

A stand-in `rsync` or `aws` is a real executable, so the code under test runs its real
subprocess handling (pipes, exit codes, timeouts) exactly as it would on a laptop.
"""

import shutil
import stat
import sys
import time
from pathlib import Path

import pytest

from portal import sync
from portal.errors import PortalError


def executable(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def rsync_printing(tmp_path: Path, banner: str, code: int = 0) -> Path:
    return executable(tmp_path / "rsync", f"#!/bin/sh\necho '{banner}'\nexit {code}\n")


def fake_aws(tmp_path: Path, body: str) -> str:
    """A stand-in `aws` that knows which local port it was asked to open."""
    script = (
        f"#!{sys.executable}\n"
        "import json, socket, sys, time\n"
        "params = json.loads(sys.argv[sys.argv.index('--parameters') + 1])\n"
        "port = int(params['localPortNumber'][0])\n"
        f"{body}\n"
    )
    return str(executable(tmp_path / "aws", script))


# --- check_rsync ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("banner", "accepted"),
    [
        ("rsync  version 3.0.9  protocol version 30", False),
        ("rsync  version 2.6.9  protocol version 29", False),
        ("openrsync: protocol version 29", False),  # macOS's, which lacks --info=progress2
        ("rsync  version 3.1.0  protocol version 31", True),
        ("rsync  version 3.2.7  protocol version 31", True),
        ("rsync  version 4.0.0  protocol version 32", True),
    ],
)
def test_only_rsync_3_1_or_newer_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, banner: str, accepted: bool
) -> None:
    path = rsync_printing(tmp_path, banner)
    monkeypatch.setenv("PATH", str(tmp_path))
    if accepted:
        assert sync.check_rsync() == str(path)
    else:
        with pytest.raises(PortalError, match=r"rsync 3\.1\+"):
            sync.check_rsync()


def test_an_rsync_that_fails_to_report_a_version_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable(tmp_path / "rsync", "#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(PortalError, match=r"rsync 3\.1\+"):
        sync.check_rsync()


# --- snapshot (needs the real rsync, which every machine that can use portal sync has) ------

needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")


@needs_rsync
def test_the_snapshot_changes_when_a_file_changes(tmp_path: Path) -> None:
    rsync = sync.check_rsync()
    (tmp_path / "app.py").write_text("print(1)\n")
    before = sync.snapshot(rsync, tmp_path)
    assert "app.py" in before
    (tmp_path / "app.py").write_text("print('a longer line')\n")
    assert sync.snapshot(rsync, tmp_path) != before


@needs_rsync
def test_a_folder_rsync_cannot_read_is_a_portal_error_not_a_traceback(tmp_path: Path) -> None:
    with pytest.raises(PortalError, match="rsync could not read"):
        sync.snapshot(sync.check_rsync(), tmp_path / "does-not-exist")


# --- run_rsync ------------------------------------------------------------------------------


def test_progress_reports_the_latest_percentage_in_the_output(tmp_path: Path) -> None:
    script = executable(
        tmp_path / "rsync",
        "#!/bin/sh\nprintf '10%%\\r50%%\\r99%%\\r'\n",  # rsync redraws one line with \r
    )
    seen: list[int] = []
    sync.run_rsync([str(script)], seen.append)
    assert seen and seen[-1] == 99


def test_a_failed_rsync_quotes_its_last_line_of_complaint(tmp_path: Path) -> None:
    script = executable(
        tmp_path / "rsync",
        "#!/bin/sh\necho 'first problem' >&2\necho 'last problem' >&2\nexit 23\n",
    )
    with pytest.raises(PortalError) as error:
        sync.run_rsync([str(script)])
    assert "(exit 23): last problem." in str(error.value)
    assert "first problem" not in str(error.value)


def test_a_failed_rsync_with_no_complaint_says_so(tmp_path: Path) -> None:
    script = executable(tmp_path / "rsync", "#!/bin/sh\nexit 1\n")
    with pytest.raises(PortalError, match=r"\(exit 1\): no output\."):
        sync.run_rsync([str(script)])


# --- port_forward ---------------------------------------------------------------------------


def test_a_tunnel_that_exits_at_once_is_reported_without_waiting_for_the_timeout(
    tmp_path: Path,
) -> None:
    aws = fake_aws(tmp_path, "sys.exit(3)")
    started = time.monotonic()
    with pytest.raises(PortalError, match="box: no output"), sync.port_forward(aws, "i-1", "r", 30):
        pass
    assert time.monotonic() - started < 10  # not the 30 s timeout


def test_a_tunnel_that_exits_shows_what_it_said(tmp_path: Path) -> None:
    aws = fake_aws(tmp_path, "sys.stderr.write('TargetNotConnected\\n'); sys.exit(3)")
    with (
        pytest.raises(PortalError, match="TargetNotConnected"),
        sync.port_forward(aws, "i-1", "r", 30),
    ):
        pass


def test_a_tunnel_that_never_opens_gives_up_after_the_timeout(tmp_path: Path) -> None:
    aws = fake_aws(tmp_path, "time.sleep(60)")
    started = time.monotonic()
    with (
        pytest.raises(PortalError, match="Could not open"),
        sync.port_forward(aws, "i-1", "r", 0.6),
    ):
        pass
    elapsed = time.monotonic() - started
    assert 0.6 <= elapsed < 10  # it did wait the timeout, and did not wait much longer


def test_a_tunnel_that_opens_late_is_waited_for(tmp_path: Path) -> None:
    body = (
        "time.sleep(0.7)\n"
        "s = socket.socket()\n"
        "s.bind(('127.0.0.1', port))\n"
        "s.listen()\n"
        "time.sleep(60)"
    )
    aws = fake_aws(tmp_path, body)
    with sync.port_forward(aws, "i-1", "r", 20) as port:
        assert sync._can_connect(port)
