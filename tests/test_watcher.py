import json
from pathlib import Path
from typing import Any

from agent import watcher
from agent.imds import Imds
from tests import fake_imds

NOTICE = {"action": "terminate", "time": "2030-01-01T12:02:00Z"}


def test_polls_every_five_seconds_until_a_notice_then_reacts_once(tmp_path: Path) -> None:
    log = tmp_path / "log"
    sleeps: list[float] = []
    seen: list[dict[str, Any]] = []
    with fake_imds.running() as (fake, base):

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 3:  # AWS publishes the notice after the third quiet poll
                fake.paths[watcher.ACTION_PATH] = json.dumps(NOTICE)

        result = watcher.watch(Imds(base), seen.append, log, sleep=sleep)
        gets = [r for r in fake.requests if r[0] == "GET"]
        assert fake.token_requests == 1  # IMDSv2: one token, reused for every poll
        assert all(
            path == watcher.ACTION_PATH and token == fake_imds.TOKEN for _, path, token in gets
        )
    assert result == NOTICE and seen == [NOTICE]
    assert sleeps == [5, 5, 5]
    assert "SPOT INTERRUPTION NOTICE" in log.read_text()


def test_an_unparseable_notice_is_still_treated_as_a_notice(tmp_path: Path) -> None:
    with fake_imds.running() as (fake, base):
        fake.paths[watcher.ACTION_PATH] = "<<garbage>>"
        seen: list[dict[str, Any]] = []
        watcher.watch(Imds(base), seen.append, tmp_path / "log", sleep=lambda s: None)
    assert seen[0]["action"] == "unknown"


def test_metadata_outage_is_survived_and_logged_once(tmp_path: Path) -> None:
    log = tmp_path / "log"
    imds = Imds("http://127.0.0.1:1", timeout_s=0.2)  # nothing listens: every poll fails
    assert watcher.watch(imds, lambda n: None, log, sleep=lambda s: None, max_polls=4) is None
    assert log.read_text().count("WARN could not read metadata service") == 1


def test_no_notice_means_no_reaction(tmp_path: Path) -> None:
    with fake_imds.running() as (_, base):
        seen: list[dict[str, Any]] = []
        assert (
            watcher.watch(
                Imds(base), seen.append, tmp_path / "l", sleep=lambda s: None, max_polls=3
            )
            is None
        )
    assert seen == []
