from pathlib import Path

import pytest

from agent import idle
from agent.idle import Signals, evaluate

QUIET = Signals(ssm_sessions=0, load5=0.02, docker_builds=0, keepalive_mtime=None)
TIMEOUT = 30 * 60
T0 = 1_000_000.0


def test_first_run_never_shuts_down() -> None:
    verdict = evaluate(QUIET, None, T0, TIMEOUT)
    assert not verdict.idle
    assert verdict.last_active == T0


def test_quiet_box_goes_idle_only_after_the_full_timeout() -> None:
    assert not evaluate(QUIET, T0, T0 + TIMEOUT - 60, TIMEOUT).idle
    assert evaluate(QUIET, T0, T0 + TIMEOUT, TIMEOUT).idle


@pytest.mark.parametrize(
    "busy",
    [
        Signals(1, 0.0, 0, None),  # someone is connected (shell or sync --watch)
        Signals(0, 0.5, 0, None),  # load exactly at the threshold counts
        Signals(0, 3.0, 0, None),
        Signals(0, 0.0, 1, None),  # a docker build waiting on the network has no load
    ],
)
def test_any_activity_signal_resets_the_clock(busy: Signals) -> None:
    verdict = evaluate(busy, T0, T0 + 10 * TIMEOUT, TIMEOUT)
    assert not verdict.idle
    assert verdict.last_active == T0 + 10 * TIMEOUT  # the clock restarted at "now"


def test_just_below_the_load_threshold_is_quiet() -> None:
    almost = Signals(0, 0.49, 0, None)
    assert evaluate(almost, T0, T0 + TIMEOUT, TIMEOUT).idle


def test_a_40_minute_build_survives_because_it_keeps_resetting_the_clock() -> None:
    last: float | None = None
    busy = Signals(0, 1.2, 0, None)
    for minute in range(40):
        last = evaluate(busy, last, T0 + minute * 60, TIMEOUT).last_active
    last_busy = T0 + 39 * 60
    assert not evaluate(QUIET, last, last_busy + 29 * 60, TIMEOUT).idle
    assert evaluate(QUIET, last, last_busy + 30 * 60, TIMEOUT).idle


def test_the_accepted_failure_mode_a_quiet_detached_job_is_killed() -> None:
    # A detached test suite blocked on the network: no session, load 0.1, not a docker build.
    waiting_job = Signals(0, 0.1, 0, None)
    assert evaluate(waiting_job, T0, T0 + TIMEOUT, TIMEOUT).idle


def test_keepalive_file_is_the_escape_hatch() -> None:
    touched = Signals(0, 0.1, 0, keepalive_mtime=T0 + 25 * 60)
    assert not evaluate(touched, T0, T0 + TIMEOUT + 60, TIMEOUT).idle  # clock restarted at touch
    assert evaluate(touched, T0, T0 + 25 * 60 + TIMEOUT, TIMEOUT).idle


def test_old_keepalive_does_not_extend_anything() -> None:
    stale = Signals(0, 0.1, 0, keepalive_mtime=T0 - 10 * TIMEOUT)
    assert evaluate(stale, T0, T0 + TIMEOUT, TIMEOUT).idle


# --- reading the real /proc layout, faked in a temp dir ---------------------------------


def fake_proc(
    tmp_path: Path, procs: dict[str, list[str]], loadavg: str = "0.01 0.02 0.03 1/100 9"
) -> Path:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "loadavg").write_text(loadavg)
    for pid, argv in procs.items():
        (proc / pid).mkdir()
        (proc / pid / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
    (proc / "self").mkdir()  # non-numeric entries must be ignored
    return proc


def test_reads_sessions_builds_and_load(tmp_path: Path) -> None:
    proc = fake_proc(
        tmp_path,
        {
            "10": ["/snap/amazon-ssm-agent/1/ssm-session-worker", "sess-1"],
            "11": ["/usr/bin/ssm-session-worker", "sess-2"],
            "12": ["docker", "build", "-t", "x", "."],
            "13": ["docker", "buildx", "build", "."],
            "14": ["docker", "run", "--rm", "alpine"],  # running a container is not a build
            "15": ["tmux", "new"],
            "16": ["/usr/bin/ssm-agent-worker"],  # the always-on agent is not a session
        },
        loadavg="0.10 0.75 0.50 1/100 9",
    )
    signals = idle.read_signals(proc, tmp_path / "nokeepalive")
    assert signals == Signals(ssm_sessions=2, load5=0.75, docker_builds=2, keepalive_mtime=None)


def test_unreadable_processes_are_skipped(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, {})
    (proc / "99").mkdir()  # no cmdline file: the process vanished mid-scan
    assert idle.read_signals(proc, tmp_path / "k").ssm_sessions == 0


def test_check_remembers_last_activity_between_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state" / "last-active"
    monkeypatch.setattr(idle, "read_signals", lambda: QUIET)
    assert not idle.check(state, TIMEOUT, now=T0).idle  # first run starts the clock
    assert not idle.check(state, TIMEOUT, now=T0 + 600).idle
    assert idle.check(state, TIMEOUT, now=T0 + TIMEOUT).idle


def test_corrupt_state_file_behaves_like_a_first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "last-active"
    state.write_text("not a number")
    monkeypatch.setattr(idle, "read_signals", lambda: QUIET)
    assert not idle.check(state, TIMEOUT, now=T0 + 10 * TIMEOUT).idle


def test_the_reasons_given_in_the_log_are_exact_and_in_minutes() -> None:
    quiet = evaluate(QUIET, T0, T0 + 29 * 60, TIMEOUT)
    assert quiet.reason == "quiet for 29 of 30 min"
    idle_verdict = evaluate(QUIET, T0, T0 + 45 * 60, TIMEOUT)
    assert idle_verdict.reason == "idle for 45 min (limit 30)"
    assert evaluate(Signals(2, 0.0, 1, None), T0, T0, TIMEOUT).reason == (
        "active: 2 SSM session(s) open, 1 docker build(s) running"
    )


def test_the_load_figure_comes_from_the_five_minute_column(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, {}, loadavg="9.00 0.40 7.00 1/100 9")  # 1-min and 15-min are decoys
    assert idle.read_signals(proc, tmp_path / "k").load5 == 0.4


def test_signals_and_verdicts_cannot_be_modified_after_the_fact() -> None:
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        QUIET.load5 = 9.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        evaluate(QUIET, None, T0, TIMEOUT).idle = True  # type: ignore[misc]


def test_a_process_that_vanishes_mid_scan_is_skipped_not_fatal(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, {"10": ["/usr/bin/ssm-session-worker", "s"]})
    (proc / "11").mkdir()  # no cmdline file at all
    (proc / "12").mkdir()
    (proc / "12" / "cmdline").write_bytes(b"")  # a kernel thread: empty command line
    assert idle.read_signals(proc, tmp_path / "k").ssm_sessions == 1


def test_the_quiet_reason_divides_both_numbers_by_sixty() -> None:
    # 30 and 60 minutes are far enough apart that dividing by 61 would show up as 49 / 59.
    verdict = evaluate(QUIET, T0, T0 + 50 * 60, 60 * 60)
    assert verdict.reason == "quiet for 50 of 60 min"
    assert evaluate(QUIET, T0, T0 + 61 * 60, 60 * 60).reason == "idle for 61 min (limit 60)"


@pytest.mark.parametrize(
    ("argv", "is_build"),
    [
        (["docker", "build", "."], True),
        (["docker", "buildx", "build", "."], True),  # build is at argv[2]
        (["/usr/bin/docker", "builder", "prune"], True),
        (["docker-compose", "build"], True),
        (["docker", "--context", "remote", "build"], False),  # only the first two arguments count
        (["docker", "run", "image"], False),
        (["python", "build"], False),  # not docker at all
    ],
)
def test_which_docker_commands_count_as_a_build(argv: list[str], is_build: bool) -> None:
    assert idle._is_docker_build(argv) is is_build
