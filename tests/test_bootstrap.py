import time

import pytest

from portal import bootstrap
from portal.errors import PortalError


def test_script_installs_tools_and_is_idempotent() -> None:
    script = bootstrap.render("UTC")
    for needle in ("docker.io", "git", "ripgrep", "fzf", "uv/install.sh", "usermod -aG docker dev"):
        assert needle in script
    assert "bootstrap.done" in script  # second run exits early
    assert "id dev" in script  # user creation is guarded


@pytest.mark.parametrize("bad", ["", "UTC;id", "../etc", "a b", "$(id)"])
def test_bad_timezones_are_rejected(bad: str) -> None:
    with pytest.raises(PortalError):
        bootstrap.render(bad)


def test_apt_waits_for_the_first_boot_lock() -> None:
    script = bootstrap.render("UTC")
    assert "DPkg::Lock::Timeout" in script
    assert "UV_UNMANAGED_INSTALL" in script


def test_the_agent_archive_does_not_depend_on_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # gzip puts a timestamp in its header; identical code must still give identical user data.
    monkeypatch.setattr(time, "time", lambda: 1_000_000_000.0)
    first = bootstrap.agent_archive()
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    assert bootstrap.agent_archive() == first
