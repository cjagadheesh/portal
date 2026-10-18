import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent import expiry
from agent.imds import Imds
from tests import fake_imds

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
NONE: frozenset[int] = frozenset()


def minutes(m: float) -> timedelta:
    return timedelta(minutes=m)


def test_no_warning_while_plenty_of_time_is_left() -> None:
    assert expiry.next_warning(minutes(31), NONE) == (None, NONE)


def test_each_threshold_warns_once() -> None:
    msg, warned = expiry.next_warning(minutes(29), NONE)
    assert msg and "29 min" in msg and warned == {30}
    assert expiry.next_warning(minutes(25), warned) == (None, warned)
    msg, warned = expiry.next_warning(minutes(9.5), warned)
    assert msg and warned == {30, 10}
    msg, warned = expiry.next_warning(minutes(4), warned)
    assert msg and warned == {30, 10, 5}
    assert expiry.next_warning(minutes(2), warned)[0] is None


def test_a_late_agent_sends_one_message_not_three() -> None:
    msg, warned = expiry.next_warning(minutes(3), NONE)
    assert msg and "3 min" in msg
    assert warned == {30, 10, 5}


def test_extending_re_arms_the_warnings() -> None:
    _, warned = expiry.next_warning(minutes(4), NONE)
    _, warned = expiry.next_warning(minutes(64), warned)  # `portal extend 1h`
    assert warned == NONE
    assert expiry.next_warning(minutes(29), warned)[0] is not None


def test_expired_box_says_so() -> None:
    msg, _ = expiry.next_warning(minutes(-1), NONE)
    assert msg and "reached its expiry" in msg


def test_broadcast_reaches_every_terminal(tmp_path: Path) -> None:
    for n in ("0", "1"):
        (tmp_path / n).write_text("")
    (tmp_path / "ptmx").write_text("")  # not a numbered pty: skipped
    assert expiry.broadcast("hello", tmp_path) == 2
    assert "*** hello ***" in (tmp_path / "1").read_text()
    assert (tmp_path / "ptmx").read_text() == ""


def test_check_warns_attached_sessions_once_via_real_imds_calls(tmp_path: Path) -> None:
    pts = tmp_path / "pts"
    pts.mkdir()
    (pts / "3").write_text("")
    state = tmp_path / "warned"
    with fake_imds.running() as (fake, base):
        fake.paths[expiry.TAG_PATH] = (NOW + minutes(8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        imds = Imds(base)
        sent = expiry.check(imds, sessions=1, state_file=state, now=NOW, pts_dir=pts)
        assert sent and "expires in about 8 min" in sent
        assert "expires in about 8 min" in (pts / "3").read_text()
        assert expiry.check(imds, 1, state, NOW + minutes(1), pts) is None  # already told
        assert json.loads(state.read_text()) == [10, 30]
        # IMDSv2: one token, reused, sent on every GET.
        assert fake.token_requests == 1
        assert all(
            token == fake_imds.TOKEN for method, _, token in fake.requests if method == "GET"
        )


def test_nobody_connected_means_nothing_is_sent_or_remembered(tmp_path: Path) -> None:
    state = tmp_path / "warned"
    with fake_imds.running() as (fake, base):
        fake.paths[expiry.TAG_PATH] = (NOW + minutes(3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert expiry.check(Imds(base), sessions=0, state_file=state, now=NOW) is None
        assert not state.exists()
        # Someone connects later and still hears about it.
        pts = tmp_path / "pts"
        pts.mkdir()
        (pts / "1").write_text("")
        assert expiry.check(Imds(base), 1, state, NOW, pts) is not None


def test_missing_or_garbled_tag_is_ignored(tmp_path: Path) -> None:
    with fake_imds.running() as (fake, base):
        assert expiry.check(Imds(base), 1, tmp_path / "w", NOW) is None  # tag absent: 404
        fake.paths[expiry.TAG_PATH] = "not a date"
        assert expiry.check(Imds(base), 1, tmp_path / "w", NOW) is None
        fake.paths[expiry.TAG_PATH] = "2030-01-01T12:05:00"  # no timezone
        assert expiry.check(Imds(base), 1, tmp_path / "w", NOW) is None


def test_imds_client_recovers_from_a_stale_token() -> None:
    with fake_imds.running() as (fake, base):
        fake.paths["/x"] = "value"
        imds = Imds(base)
        assert imds.get("/x") == "value"
        fake.reject_next_token = True
        assert imds.get("/x") == "value"
        assert fake.token_requests == 2  # fetched a fresh token after the 401


def test_exactly_at_a_threshold_counts_as_crossed() -> None:
    message, warned = expiry.next_warning(minutes(30), NONE)
    assert message is not None and warned == {30}


def test_zero_minutes_left_says_expired_not_one_minute() -> None:
    message, _ = expiry.next_warning(minutes(0), NONE)
    assert message is not None and "reached its expiry" in message


def test_the_minutes_in_the_message_are_rounded_not_truncated() -> None:
    message, _ = expiry.next_warning(timedelta(minutes=9, seconds=30), NONE)
    assert message is not None and "about 10 min" in message
    tiny, _ = expiry.next_warning(timedelta(seconds=20), NONE)
    assert tiny is not None and "about 1 min" in tiny  # never "about 0 min"


def test_the_state_file_is_created_in_new_directories_and_rewritten_in_old_ones(
    tmp_path: Path,
) -> None:
    target = tmp_path / "does" / "not" / "exist" / "warned"
    expiry.save_warned(target, frozenset({10}))
    expiry.save_warned(target, frozenset({10, 5}))  # the directory exists the second time
    assert expiry.load_warned(target) == {10, 5}
