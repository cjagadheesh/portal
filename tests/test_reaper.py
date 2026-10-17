import importlib.util
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

HANDLER = Path(__file__).resolve().parent.parent / "infra" / "reaper" / "handler.py"
REGION = "us-east-1"
GRACE = timedelta(minutes=60)


def load_handler() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reaper_handler", HANDLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reaper = load_handler()


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def ec2(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        yield boto3.client("ec2", region_name=REGION)


def launch(ec2: Any, tags: dict[str, str] | None) -> str:
    spec = (
        [{"ResourceType": "instance", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}]
        if tags is not None
        else []
    )
    resp = ec2.run_instances(
        ImageId="ami-12345678",
        MinCount=1,
        MaxCount=1,
        InstanceType="t3.micro",
        TagSpecifications=spec,
    )
    return str(resp["Instances"][0]["InstanceId"])


def state_of(ec2: Any, iid: str) -> str:
    return str(
        ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]["State"][
            "Name"
        ]
    )


def managed(expires: str | None, **extra: str) -> dict[str, str]:
    tags = {"portal:managed": "true", "portal:owner": "x", "portal:box": "b", **extra}
    if expires is not None:
        tags["portal:expires-at"] = expires
    return tags


NOW = datetime.now(UTC).replace(microsecond=0)  # whole seconds, as tags are


def run(ec2: Any, now: datetime = NOW, **kwargs: Any) -> dict[str, Any]:
    return dict(reaper.reap(ec2, now, GRACE, **kwargs))


# --- the core promise ---------------------------------------------------------------------


def test_terminates_expired_and_spares_unexpired(ec2: Any) -> None:
    expired = launch(ec2, managed(stamp(NOW - timedelta(minutes=1))))
    live = launch(ec2, managed(stamp(NOW + timedelta(hours=2))))
    summary = run(ec2)
    assert summary["terminated"] == 1
    assert state_of(ec2, expired) in ("shutting-down", "terminated")
    assert state_of(ec2, live) == "running"


def test_expiry_boundary_is_inclusive(ec2: Any) -> None:
    iid = launch(ec2, managed(stamp(NOW)))
    run(ec2)
    assert state_of(ec2, iid) in ("shutting-down", "terminated")


def test_never_touches_what_it_does_not_own(ec2: Any) -> None:
    long_ago = stamp(NOW - timedelta(days=30))
    untagged = launch(ec2, None)
    not_managed = launch(ec2, {"portal:expires-at": long_ago})  # has expiry but no managed tag
    managed_false = launch(ec2, {"portal:managed": "false", "portal:expires-at": long_ago})
    other_tags = launch(ec2, {"Name": "prod-db", "portal:expires-at": long_ago})
    summary = run(ec2, now=NOW + timedelta(days=60))
    assert summary["scanned"] == 0  # filtered at the EC2 level
    for iid in (untagged, not_managed, managed_false, other_tags):
        assert state_of(ec2, iid) == "running"


def test_decide_refuses_unmanaged_even_if_the_filter_failed() -> None:
    instance = {
        "InstanceId": "i-1",
        "Tags": [{"Key": "portal:expires-at", "Value": "2000-01-01T00:00:00Z"}],
    }
    assert reaper.decide(instance, NOW, GRACE).action == "keep"


# --- missing or unreadable expiry: grace, then terminate --------------------------------------


@pytest.mark.parametrize(
    "bad", [None, "", "tomorrow", "2026-13-45T00:00:00Z", "2026-10-21T10:00:00", "1700000000"]
)
def test_bad_expiry_waits_through_grace_then_terminates(ec2: Any, bad: str | None) -> None:
    # Fresh clock, taken after the launch: instances get a real LaunchTime, so a clock fixed
    # at import time would make this test fail whenever the suite is slow.
    launched = datetime.now(UTC)
    iid = launch(ec2, managed(bad))
    first = run(ec2, now=launched + timedelta(minutes=10))
    assert first["waiting"] == 1 and first["terminated"] == 0
    assert state_of(ec2, iid) == "running"
    later = run(ec2, now=launched + GRACE + timedelta(minutes=5))
    assert later["terminated"] == 1
    assert state_of(ec2, iid) in ("shutting-down", "terminated")


def test_offset_timestamps_are_understood(ec2: Any) -> None:
    # 10:00 at +05:30 is 04:30 UTC. Read as naive UTC it would look 5.5 hours in the future.
    iid = launch(ec2, managed("2026-10-21T10:00:00+05:30"))
    run(ec2, now=datetime(2026, 10, 21, 4, 31, tzinfo=UTC))
    assert state_of(ec2, iid) in ("shutting-down", "terminated")
    other = launch(ec2, managed("2026-10-21T10:00:00+05:30"))
    run(ec2, now=datetime(2026, 10, 21, 4, 29, tzinfo=UTC))
    assert state_of(ec2, other) == "running"


# --- operations -------------------------------------------------------------------------------


def test_dry_run_decides_but_changes_nothing(ec2: Any) -> None:
    iid = launch(ec2, managed(stamp(NOW - timedelta(hours=1))))
    summary = run(ec2, dry_run=True)
    assert summary["would_terminate"] == 1 and summary["terminated"] == 0
    assert state_of(ec2, iid) == "running"


def test_paginates(ec2: Any) -> None:
    ids = [launch(ec2, managed(stamp(NOW - timedelta(minutes=5)))) for _ in range(5)]
    summary = run(ec2, page_size=2)
    assert summary["scanned"] == 5
    assert all(state_of(ec2, i) in ("shutting-down", "terminated") for i in ids)


def test_one_bad_instance_does_not_protect_the_others(
    ec2: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [launch(ec2, managed(stamp(NOW - timedelta(minutes=5)))) for _ in range(3)]
    poisoned = ids[1]
    real = ec2.terminate_instances

    def flaky(InstanceIds: list[str]) -> Any:
        if poisoned in InstanceIds:
            raise ClientError(
                {"Error": {"Code": "IncorrectInstanceState", "Message": "x"}}, "TerminateInstances"
            )
        return real(InstanceIds=InstanceIds)

    monkeypatch.setattr(ec2, "terminate_instances", flaky)
    summary = run(ec2)
    assert summary["failed"] == [poisoned]
    assert summary["terminated"] == 2
    assert state_of(ec2, poisoned) == "running"
    assert all(state_of(ec2, i) in ("shutting-down", "terminated") for i in (ids[0], ids[2]))


def test_lambda_handler_fails_loudly_after_a_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reaper,
        "reap",
        lambda *a, **k: {"scanned": 2, "terminated": 1, "waiting": 0, "failed": ["i-9"]},
    )
    monkeypatch.setattr(reaper.boto3, "client", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="i-9"):
        reaper.lambda_handler({}, None)


def test_environment_controls_grace_and_dry_run(ec2: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    iid = launch(ec2, managed(stamp(NOW - timedelta(hours=1))))
    monkeypatch.setenv("REAPER_DRY_RUN", "true")
    assert reaper.lambda_handler({}, None)["would_terminate"] == 1
    assert state_of(ec2, iid) == "running"
    monkeypatch.setenv("REAPER_DRY_RUN", "false")
    assert reaper.lambda_handler({}, None)["terminated"] == 1


def test_handler_needs_nothing_but_boto3() -> None:
    """The Lambda zip contains only handler.py, so any other import would fail in AWS."""
    import ast

    tree = ast.parse(HANDLER.read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= {"json", "os", "dataclasses", "datetime", "typing", "boto3", "botocore"}


# --- exact edges, with a stand-in EC2 so no clock or moto timing is involved ----------------


def instance(tags: dict[str, str], launched: datetime | None = None) -> dict[str, Any]:
    found: dict[str, Any] = {
        "InstanceId": "i-1",
        "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
    }
    if launched is not None:
        found["LaunchTime"] = launched
    return found


def test_an_instance_is_expired_at_the_exact_second_and_not_a_moment_before() -> None:
    at = reaper.decide(instance(managed(stamp(NOW))), NOW, GRACE)
    assert (at.action, at.reason) == ("terminate", "expired 0:00:00 ago")
    before = reaper.decide(instance(managed(stamp(NOW + timedelta(seconds=1)))), NOW, GRACE)
    assert (before.action, before.reason) == ("keep", "expires in 0:00:01")


def test_a_missing_expiry_is_terminated_at_exactly_the_grace_period() -> None:
    tags = managed(None)
    at = reaper.decide(instance(tags, NOW - GRACE), NOW, GRACE)
    assert at.action == "terminate"
    almost = reaper.decide(instance(tags, NOW - GRACE + timedelta(seconds=1)), NOW, GRACE)
    assert (almost.action, almost.reason) == (
        "wait",
        "expires-at missing/unreadable (None); grace ends in 0:00:01",
    )


def test_decisions_cannot_be_modified_after_the_fact() -> None:
    decision = reaper.decide(instance(managed(stamp(NOW))), NOW, GRACE)
    with pytest.raises(FrozenInstanceError):
        decision.action = "keep"  # type: ignore[misc]


class StandInEc2:
    """Answers describe_instances from a fixed list and records what was asked and terminated."""

    def __init__(self, instances: list[dict[str, Any]] | None = None) -> None:
        self.instances = instances or []
        self.paginate_args: dict[str, Any] = {}
        self.terminated: list[list[str]] = []

    def get_paginator(self, name: str) -> "StandInEc2":
        assert name == "describe_instances"
        return self

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.paginate_args = kwargs
        return [{"Reservations": [{"Instances": self.instances}]}]

    def terminate_instances(self, InstanceIds: list[str]) -> None:
        self.terminated.append(InstanceIds)


def test_instances_are_terminated_in_batches_of_fifty() -> None:
    fake = StandInEc2()
    ids = [f"i-{n}" for n in range(51)]
    assert reaper.terminate(fake, ids) == []
    assert [len(batch) for batch in fake.terminated] == [50, 1]
    fake = StandInEc2()
    reaper.terminate(fake, ids[:50])
    assert [len(batch) for batch in fake.terminated] == [50]


def test_candidates_are_requested_two_hundred_to_a_page() -> None:
    fake = StandInEc2()
    reaper.find_candidates(fake)
    assert fake.paginate_args["PaginationConfig"] == {"PageSize": 200}


def test_the_lambda_waits_one_hour_by_default_and_retries_throttling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    young = instance(managed(None), now - timedelta(minutes=59, seconds=30))
    old = {
        **instance(managed(None), now - timedelta(minutes=60, seconds=30)),
        "InstanceId": "i-old",
    }
    fake = StandInEc2([young, old])
    made: list[Any] = []

    def fake_client(service: str, **kwargs: Any) -> StandInEc2:
        made.append((service, kwargs))
        return fake

    monkeypatch.setattr(reaper.boto3, "client", fake_client)
    monkeypatch.delenv("REAPER_INVALID_TAG_GRACE_MINUTES", raising=False)
    monkeypatch.delenv("REAPER_DRY_RUN", raising=False)
    summary = reaper.lambda_handler({}, None)
    assert (summary["terminated"], summary["waiting"]) == (1, 1)
    assert fake.terminated == [["i-old"]]
    service, kwargs = made[0]
    assert service == "ec2"
    assert kwargs["config"].retries == {"mode": "standard", "max_attempts": 8}
