import json
from pathlib import Path

import pytest

from agent import drain, watcher
from agent.imds import Imds
from tests import fake_imds, iam_eval

VOLUME_SERIAL = "vol0abc123def456789"


class Script:
    """Records every command the drain runs, in order, and answers from a script."""

    def __init__(self, answers: dict[str, tuple[int, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.answers = answers or {}
        self.umounts = 0
        self.events: list[str] = []

    def run(self, argv: list[str], timeout_s: float) -> tuple[int, str]:
        self.calls.append(argv)
        key = " ".join(argv)
        self.events.append(argv[0] if argv[0] != "fuser" else f"fuser-{argv[2]}")
        if argv[0] == "umount":
            self.umounts += 1
            return self.answers.get(f"umount#{self.umounts}", (0, ""))
        defaults = {
            "findmnt -no SOURCE /home/dev": (0, "/dev/nvme1n1\n"),
            "lsblk -dno SERIAL /dev/nvme1n1": (0, VOLUME_SERIAL + "\n"),
            "docker ps -q": (0, "abc\ndef\n"),
        }
        return self.answers.get(key) or defaults.get(key) or (0, "")

    def names(self) -> list[str]:
        return self.events


def make_ctx(
    tmp_path: Path, script: Script, tags: list[tuple[str, str]], **kw: object
) -> drain.Context:
    def announce(message: str) -> int:
        script.events.append("announce")
        return 1

    def tag(volume_id: str, state: str) -> None:
        script.events.append("tag")
        tags.append((volume_id, state))

    return drain.Context(tmp_path / "log", script.run, announce, tag, sleep=lambda s: None, **kw)  # type: ignore[arg-type]


def test_the_order_is_the_design(tmp_path: Path) -> None:
    script, tags = Script(), []
    ctx = make_ctx(tmp_path, script, tags)
    assert drain.drain(ctx) == "clean"
    events = script.names()
    order = ["announce", "docker", "fuser--TERM", "sync", "umount", "tag"]
    positions = [events.index(e) for e in order]
    assert positions == sorted(positions), events
    # Writers are stopped before the flush, and the flush precedes the unmount.
    assert events.index("docker") < events.index("sync") < events.index("umount")
    assert tags == [("vol-0abc123def456789", "clean")]
    assert ["docker", "stop", "-t", "10", "abc", "def"] in script.calls


def test_busy_unmount_kills_stragglers_then_syncs_again(tmp_path: Path) -> None:
    script, tags = Script({"umount#1": (32, "")}), []
    assert drain.drain(make_ctx(tmp_path, script, tags)) == "clean"
    events = script.names()
    assert events.count("umount") == 2 and events.count("sync") == 2
    assert events.index("fuser--KILL") < events.index("sync", events.index("fuser--KILL"))
    assert tags == [("vol-0abc123def456789", "clean")]  # clean: the retry succeeded


def test_failed_unmount_is_reported_dirty_and_tagged_last(tmp_path: Path) -> None:
    script, tags = Script({"umount#1": (32, ""), "umount#2": (32, "")}), []
    assert drain.drain(make_ctx(tmp_path, script, tags)) == "dirty"
    assert tags == [("vol-0abc123def456789", "dirty")]
    assert script.names()[-1] == "tag"


def test_a_missing_or_hung_docker_does_not_stop_the_unmount(tmp_path: Path) -> None:
    script, tags = Script({"docker ps -q": (127, "")}), []
    assert drain.drain(make_ctx(tmp_path, script, tags)) == "clean"
    assert "docker" in script.names() and ["docker", "stop"] not in [c[:2] for c in script.calls]
    hung, tags2 = Script({"docker ps -q": (0, "abc\n")}), []
    ctx = make_ctx(tmp_path, hung, tags2)
    original = hung.run

    def stop_times_out(argv: list[str], timeout_s: float) -> tuple[int, str]:
        return (124, "") if argv[:2] == ["docker", "stop"] else original(argv, timeout_s)

    ctx.run = stop_times_out
    assert drain.drain(ctx) == "clean"


def test_unknown_volume_means_no_tag_but_the_drain_still_completes(tmp_path: Path) -> None:
    script, tags = Script({"findmnt -no SOURCE /home/dev": (1, "")}), []
    assert drain.drain(make_ctx(tmp_path, script, tags)) == "clean"
    assert tags == [] and "could not identify the home volume" in (tmp_path / "log").read_text()


def test_a_tagging_failure_is_logged_not_raised(tmp_path: Path) -> None:
    script = Script()

    def broken(volume_id: str, state: str) -> None:
        raise PermissionError("denied")

    ctx = drain.Context(tmp_path / "log", script.run, lambda m: 0, broken, sleep=lambda s: None)
    assert drain.drain(ctx) == "clean"
    assert "could not tag vol-0abc123def456789: denied" in (tmp_path / "log").read_text()


def test_dry_run_runs_nothing_that_changes_the_box(tmp_path: Path) -> None:
    script, tags = Script(), []
    drain.drain(make_ctx(tmp_path, script, tags, dry_run=True))
    ran = [c[0] for c in script.calls]
    assert set(ran) <= {"findmnt", "lsblk"}  # read-only lookups only
    assert tags == [] and "DRY RUN" in (tmp_path / "log").read_text()


def test_out_of_time_skips_the_optional_steps(tmp_path: Path) -> None:
    script, tags = Script({"umount#1": (32, "")}), []
    ticks = iter([0.0])  # then every later reading is 200 s
    ctx = make_ctx(tmp_path, script, tags)
    ctx.clock = lambda: next(ticks, 200.0)
    assert drain.drain(ctx) == "dirty"
    assert "docker" in script.names() and ["docker", "stop"] not in [c[:2] for c in script.calls]
    assert script.names().count("umount") == 1  # no retry once the time is gone


def timed_ctx(
    tmp_path: Path, script: Script, **kw: object
) -> tuple[drain.Context, list[tuple[str, float]], list[float]]:
    """A context that also records each command's timeout and each sleep."""
    timeouts: list[tuple[str, float]] = []
    sleeps: list[float] = []

    def run(argv: list[str], timeout_s: float) -> tuple[int, str]:
        timeouts.append((argv[0] if argv[0] != "fuser" else f"fuser{argv[2]}", timeout_s))
        return script.run(argv, timeout_s)

    ctx = make_ctx(tmp_path, script, [], **kw)
    ctx.run = run
    ctx.sleep = sleeps.append
    return ctx, timeouts, sleeps


def test_every_step_has_its_own_time_limit_and_the_sleeps_are_short(tmp_path: Path) -> None:
    # The whole drain must fit in about two minutes, so these numbers are part of the design.
    ctx, timeouts, sleeps = timed_ctx(tmp_path, Script({"umount#1": (32, "")}))
    drain.drain(ctx)
    assert timeouts == [
        ("findmnt", 5),
        ("lsblk", 5),
        ("docker", 10),  # docker ps
        ("docker", 40),  # docker stop
        ("fuser-TERM", 10),
        ("sync", 30),
        ("umount", 20),
        ("fuser-KILL", 10),
        ("sync", 20),
        ("umount", 20),
    ]
    assert sleeps == [5, 2]


def test_a_clean_drain_sleeps_once_and_a_dry_run_never_sleeps(tmp_path: Path) -> None:
    ctx, _, sleeps = timed_ctx(tmp_path, Script())
    drain.drain(ctx)
    assert sleeps == [5]
    dry, _, dry_sleeps = timed_ctx(tmp_path, Script(), dry_run=True)
    drain.drain(dry)
    assert dry_sleeps == []


def test_exactly_the_budget_is_still_in_time_but_a_moment_later_is_not(tmp_path: Path) -> None:
    for now, stops in ((110.0, True), (110.5, False)):  # AWS gives ~120 s; 110 leaves a margin
        script = Script()
        ticks = iter([0.0])
        ctx = make_ctx(tmp_path, script, [])
        ctx.clock = lambda ticks=ticks, now=now: next(ticks, now)  # type: ignore[misc]
        drain.drain(ctx)
        assert (["docker", "stop", "-t", "10", "abc", "def"] in script.calls) is stops


@pytest.mark.parametrize(
    "answers",
    [
        {"findmnt -no SOURCE /home/dev": (0, "  \n")},  # nothing mounted there
        {"findmnt -no SOURCE /home/dev": (1, "/dev/nvme1n1\n")},  # failed but printed something
        {"lsblk -dno SERIAL /dev/nvme1n1": (1, VOLUME_SERIAL + "\n")},
        {"lsblk -dno SERIAL /dev/nvme1n1": (0, "SN12345\n")},  # not an EBS serial
    ],
)
def test_the_home_volume_is_only_named_when_every_lookup_agrees(
    tmp_path: Path, answers: dict[str, tuple[int, str]]
) -> None:
    ctx = make_ctx(tmp_path, Script(answers), [])
    assert drain.volume_id_of_home(ctx) is None
    assert drain.volume_id_of_home(make_ctx(tmp_path, Script(), [])) == "vol-0abc123def456789"


def test_the_log_says_which_volume_or_that_it_is_unknown(tmp_path: Path) -> None:
    drain.drain(make_ctx(tmp_path, Script(), []))
    assert "drain: home volume: vol-0abc123def456789\n" in (tmp_path / "log").read_text()
    other = tmp_path / "other"
    other.mkdir()
    drain.drain(make_ctx(other, Script({"findmnt -no SOURCE /home/dev": (1, "")}), []))
    assert "drain: home volume: unknown\n" in (other / "log").read_text()


def test_logging_creates_missing_folders_and_tolerates_existing_ones(tmp_path: Path) -> None:
    ctx = drain.Context(tmp_path / "a" / "b" / "log", Script().run, lambda m: 0, lambda v, s: None)
    drain.log(ctx, "one")
    drain.log(ctx, "two")  # the folder exists now; this must not raise
    assert (tmp_path / "a" / "b" / "log").read_text().count("drain:") == 2


def test_the_real_runner_reports_output_timeouts_and_missing_programs() -> None:
    assert drain.real_run(["echo", "hi"], 5) == (0, "hi\n")
    assert drain.real_run(["false"], 5) == (1, "")  # a failing command is a code, not an error
    assert drain.real_run(["sleep", "5"], 0.1) == (124, "")
    assert drain.real_run(["portal-no-such-program"], 5) == (127, "")


def test_dry_run_is_off_unless_asked_for(tmp_path: Path) -> None:
    assert drain.Context.real(tmp_path / "log").dry_run is False
    assert drain.Context.real(tmp_path / "log", dry_run=True).dry_run is True


# --- watcher + drain together, over real IMDS calls ------------------------------------------


def test_a_notice_triggers_exactly_one_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, tags = Script(), []
    ctx = make_ctx(tmp_path, script, tags)
    drained: list[str] = []
    with fake_imds.running() as (fake, base):
        fake.paths[watcher.ACTION_PATH] = json.dumps(
            {"action": "terminate", "time": "2030-01-01T12:02:00Z"}
        )
        watcher.watch(
            Imds(base),
            lambda n: drained.append(drain.drain(ctx)),
            tmp_path / "w",
            sleep=lambda s: None,
        )
    assert drained == ["clean"] and tags


# --- the box's one extra permission --------------------------------------------------------


def box_policy() -> dict[str, object]:
    import re

    text = (
        Path(__file__).resolve().parent.parent / "infra/control-plane/box-policy.json.tftpl"
    ).read_text()
    values = {"partition": "aws", "region": "us-east-1", "account_id": "000000000000"}
    return dict(json.loads(re.sub(r"\$\{(\w+)\}", lambda m: values[m[1]], text)))


VOLUME = "arn:aws:ec2:us-east-1:000000000000:volume/vol-0abc"


def test_box_can_only_set_the_resumable_tag_on_managed_volumes() -> None:
    policy = box_policy()
    managed = {"portal:managed": "true"}
    assert iam_eval.allowed(policy, "ec2:CreateTags", VOLUME, managed, ["portal:resumable"])
    assert not iam_eval.allowed(policy, "ec2:CreateTags", VOLUME, managed, ["portal:expires-at"])
    assert not iam_eval.allowed(
        policy, "ec2:CreateTags", VOLUME, managed, ["portal:resumable", "Name"]
    )
    assert not iam_eval.allowed(policy, "ec2:CreateTags", VOLUME, {}, ["portal:resumable"])
    assert not iam_eval.allowed(
        policy, "ec2:CreateTags", VOLUME, {"portal:managed": "false"}, ["portal:resumable"]
    )
    instance = "arn:aws:ec2:us-east-1:000000000000:instance/i-1"
    assert not iam_eval.allowed(policy, "ec2:CreateTags", instance, managed, ["portal:resumable"])
    assert not iam_eval.allowed(policy, "ec2:DeleteTags", VOLUME, managed, ["portal:resumable"])
    assert not iam_eval.allowed(policy, "ec2:TerminateInstances", instance, managed)


def test_drain_now_flag_drains_without_waiting_for_a_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[bool] = []
    monkeypatch.setattr(
        drain,
        "drain",
        lambda ctx: called.append(ctx.dry_run) or "clean",  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr(watcher, "watch", lambda *a, **k: pytest.fail("must not poll"))
    assert watcher.main(["--drain-now", "--dry-run", "--log-file", str(tmp_path / "l")]) == 0
    assert called == [True]
