"""The four Day 19 drill scenarios, rehearsed offline against moto.

These prove the logic, not the deployment: they cannot show that the real Lambda is invoked,
that its IAM role works, or that the SSM process names match. docs/safety-net-drill.md has
the same four scenarios to run against real AWS.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from portal.aws import home as home_calls
from portal.cli import app
from tests.test_boxes import OWNER, aws, runner  # noqa: F401  (aws is a fixture)
from tests.test_reaper import GRACE, launch, managed, reaper, stamp, state_of

UP = ["up", "--ttl", "1h", "-t", "t3.micro", "--name", "drill"]


def instances(ec2: Any) -> list[dict[str, Any]]:
    return [i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]


class HardKill(BaseException):
    """Stands in for SIGKILL: not an Exception, so no `except` in the CLI can run."""


def after_expiry(hours: float = 1.1) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


def test_1a_ctrl_c_mid_launch_cleans_up_after_itself(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(home_calls, "create_home_volume", interrupted)
    runner.invoke(app, UP)
    (inst,) = instances(aws)
    assert inst["State"]["Name"] in ("shutting-down", "terminated")


def test_1b_hard_kill_mid_launch_leaves_a_tagged_box_the_reaper_collects(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def killed(*args: Any, **kwargs: Any) -> None:
        raise HardKill

    monkeypatch.setattr(home_calls, "create_home_volume", killed)
    with pytest.raises(HardKill):  # propagates through the CLI untouched, like a real kill
        runner.invoke(app, UP)
    (inst,) = instances(aws)
    tags = {t["Key"]: t["Value"] for t in inst["Tags"]}
    # Tags were applied at creation, so even a box orphaned by a dead CLI is findable.
    assert {"portal:managed", "portal:owner", "portal:box", "portal:expires-at"} <= set(tags)
    assert tags["portal:owner"] == OWNER
    assert inst["State"]["Name"] == "running"  # the cost leak, bounded by the TTL
    assert reaper.reap(aws, datetime.now(UTC) + timedelta(minutes=30), GRACE)["terminated"] == 0
    assert reaper.reap(aws, after_expiry(), GRACE)["terminated"] == 1
    assert state_of(aws, inst["InstanceId"]) in ("shutting-down", "terminated")


def test_2_dead_agent_and_idle_box_is_stopped_by_the_reaper_at_expiry(aws: Any) -> None:  # noqa: F811
    # Nothing here models the agent at all: that is the point. The reaper reads only tags.
    iid = launch(aws, managed(stamp(datetime.now(UTC) + timedelta(hours=1))))
    assert reaper.reap(aws, datetime.now(UTC) + timedelta(minutes=59), GRACE)["terminated"] == 0
    assert state_of(aws, iid) == "running"
    assert reaper.reap(aws, after_expiry(), GRACE)["terminated"] == 1


def test_3_expires_at_set_in_the_past_by_hand_is_caught_on_the_next_run(aws: Any) -> None:  # noqa: F811
    iid = launch(aws, managed(stamp(datetime.now(UTC) + timedelta(hours=3))))
    aws.create_tags(
        Resources=[iid],
        Tags=[{"Key": "portal:expires-at", "Value": stamp(datetime.now(UTC) - timedelta(days=1))}],
    )
    assert reaper.reap(aws, datetime.now(UTC), GRACE)["terminated"] == 1


def test_4_instance_launched_by_hand_with_the_tags_is_adopted(aws: Any) -> None:  # noqa: F811
    iid = launch(
        aws, managed(stamp(datetime.now(UTC) - timedelta(minutes=1)), **{"portal:box": "by-hand"})
    )
    assert reaper.reap(aws, datetime.now(UTC), GRACE)["terminated"] == 1
    assert state_of(aws, iid) in ("shutting-down", "terminated")


def test_4b_hand_launched_with_managed_tag_but_no_expiry_dies_after_the_grace(aws: Any) -> None:  # noqa: F811
    iid = launch(aws, {"portal:managed": "true"})
    assert reaper.reap(aws, datetime.now(UTC) + timedelta(minutes=5), GRACE)["terminated"] == 0
    assert (
        reaper.reap(aws, datetime.now(UTC) + GRACE + timedelta(minutes=5), GRACE)["terminated"] == 1
    )
    assert state_of(aws, iid) in ("shutting-down", "terminated")


def test_known_gap_an_untagged_instance_is_invisible_to_the_reaper(aws: Any) -> None:  # noqa: F811
    """By design: the reaper cannot and must not touch what it cannot prove is its own.
    This is the case layer 4 (the AWS Budget alarm) exists for."""
    iid = launch(aws, None)
    assert reaper.reap(aws, after_expiry(24 * 30), GRACE)["scanned"] == 0
    assert state_of(aws, iid) == "running"
