"""Production-hardening properties: retries, idempotency and never leaking a running box."""

from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from portal.aws import ec2 as ec2_calls
from portal.aws import session
from portal.cli import app
from tests.test_boxes import aws, runner  # noqa: F401  (aws is a fixture)
from tests.test_fallback_resume import ScriptedEc2, running

UP = ["up", "--ttl", "1h", "-t", "t3.micro", "-t", "t3.small", "--name", "rel"]


def test_every_client_retries_throttling_and_has_timeouts() -> None:
    cfg = session.config()
    assert cfg.retries == {"mode": "adaptive", "max_attempts": 8}
    assert cfg.connect_timeout == 10 and cfg.read_timeout == 60
    assert cfg.user_agent_extra and cfg.user_agent_extra.startswith("portal/")


def test_clients_come_from_the_shared_configuration() -> None:
    client = ec2_calls.client("ec2", "us-east-1")
    assert client.meta.config.retries["mode"] == "adaptive"
    assert client.meta.config.retries["total_max_attempts"] == 9  # first try + 8 retries


def test_cost_and_pricing_clients_use_it_too() -> None:
    from portal import cost
    from portal.aws import pricing

    assert cost.client().meta.config.retries["mode"] == "adaptive"
    assert pricing.client().meta.config.retries["mode"] == "adaptive"


class Recording:
    """Wraps the moto client and records the keyword arguments of the calls we care about."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.calls: dict[str, list[dict[str, Any]]] = {}

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._real, name)
        if not callable(attr) or name in {"get_paginator", "get_waiter"}:
            return attr

        def spy(**kwargs: Any) -> Any:
            self.calls.setdefault(name, []).append(kwargs)
            return attr(**kwargs)

        return spy


@pytest.fixture
def recording(aws: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Recording:  # noqa: F811
    holder = Recording(aws)
    monkeypatch.setattr(
        "portal.aws.ec2.client", lambda service, region: holder if service == "ec2" else None
    )
    return holder


def test_every_creating_call_carries_an_idempotency_token(recording: Recording) -> None:
    assert runner.invoke(app, UP).exit_code == 0
    for operation in ("create_launch_template", "create_fleet", "create_volume"):
        (call,) = recording.calls[operation]
        assert len(call["ClientToken"]) >= 16, operation  # EC2 requires a unique string


def test_each_launch_attempt_gets_its_own_token(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    holder = ScriptedEc2(aws, {("t3.micro", "spot")})
    tokens: list[str] = []
    real = holder._real.create_fleet

    def spy(**kwargs: Any) -> Any:
        tokens.append(kwargs["ClientToken"])
        return real(**kwargs)

    monkeypatch.setattr(holder._real, "create_fleet", spy)
    monkeypatch.setattr(
        "portal.aws.ec2.client", lambda service, region: holder if service == "ec2" else None
    )
    assert runner.invoke(app, UP).exit_code == 0
    # t3.micro never reached the real client (it was scripted to fail); t3.small did, once.
    assert len(tokens) == 1 and len(set(tokens)) == 1


def test_a_template_that_cannot_be_deleted_does_not_fail_a_successful_launch(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(**kwargs: Any) -> None:
        raise ClientError(
            {"Error": {"Code": "RequestLimitExceeded", "Message": "x"}}, "DeleteLaunchTemplate"
        )

    monkeypatch.setattr(aws, "delete_launch_template", refuse)
    monkeypatch.setattr("portal.aws.ec2.client", lambda service, region: aws)
    result = runner.invoke(app, UP)
    assert result.exit_code == 0, (
        result.output
    )  # the box is up and has its home; that is what matters
    assert len(running(aws)) == 1
