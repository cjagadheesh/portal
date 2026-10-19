import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from portal.aws import pricing as pricing_calls
from portal.cli import app
from tests.test_boxes import aws, runner  # noqa: F401  (aws is a fixture)
from tests.test_home import home_volumes

CAPACITY = {"ErrorCode": "InsufficientInstanceCapacity", "ErrorMessage": "none left"}


class ScriptedEc2:
    """The moto EC2 client, except create_fleet can be told which (type, market) has no room."""

    def __init__(
        self, real: Any, no_room: set[tuple[str, str]], error: dict[str, str] | None = None
    ):
        self._real = real
        self.no_room = no_room
        self.error = error or CAPACITY
        self.attempts: list[tuple[str, str]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def create_fleet(self, **kwargs: Any) -> Any:
        instance_type = kwargs["LaunchTemplateConfigs"][0]["Overrides"][0]["InstanceType"]
        market = kwargs["TargetCapacitySpecification"]["DefaultTargetCapacityType"]
        self.attempts.append((instance_type, market))
        if (instance_type, market) in self.no_room:
            return {"Instances": [], "Errors": [{**self.error, "LaunchTemplateAndOverrides": {}}]}
        return self._real.create_fleet(**kwargs)


@pytest.fixture
def scripted(aws: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:  # noqa: F811
    monkeypatch.chdir(tmp_path)
    holder = ScriptedEc2(aws, set())
    monkeypatch.setattr(
        "portal.aws.ec2.client", lambda service, region: holder if service == "ec2" else None
    )
    monkeypatch.setattr(pricing_calls, "client", lambda: None)
    monkeypatch.setattr(pricing_calls, "on_demand_usd_per_hour", lambda *a: 0.5)
    return holder


def running(ec2: Any) -> list[dict[str, Any]]:
    found = [i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]
    return [i for i in found if i["State"]["Name"] in ("pending", "running")]


UP = ["up", "--ttl", "2h", "-t", "t3.micro", "-t", "t3.small", "--name", "f1"]


# --- Day 24: ordered fallback and the on-demand question ---------------------------------------


def test_walks_the_list_in_order_and_reports_every_attempt(scripted: Any) -> None:
    scripted.no_room = {("t3.micro", "spot")}
    result = runner.invoke(app, UP)
    assert result.exit_code == 0, result.output
    assert scripted.attempts == [("t3.micro", "spot"), ("t3.small", "spot")]
    assert "spot t3.micro: no capacity" in result.output
    assert "spot t3.small: launched" in result.output
    assert running(scripted)[0]["InstanceType"] == "t3.small"


def test_preferred_type_wins_when_it_has_room(scripted: Any) -> None:
    assert runner.invoke(app, UP).exit_code == 0
    assert scripted.attempts == [("t3.micro", "spot")]  # never tried the second


def test_no_spot_anywhere_then_no_means_nothing_starts(scripted: Any) -> None:
    scripted.no_room = {("t3.micro", "spot"), ("t3.small", "spot")}
    result = runner.invoke(app, UP, input="n\n")
    assert result.exit_code == 1
    assert "No spot capacity" in result.output and "ON-DEMAND" in result.output
    assert all(market == "spot" for _, market in scripted.attempts)
    assert running(scripted) == []


def test_no_answer_at_all_is_a_no(scripted: Any) -> None:
    scripted.no_room = {("t3.micro", "spot"), ("t3.small", "spot")}
    result = runner.invoke(app, UP)  # no stdin: the default must be "no"
    assert result.exit_code == 1 and running(scripted) == []


def test_yes_starts_on_demand_in_the_same_order(scripted: Any) -> None:
    scripted.no_room = {("t3.micro", "spot"), ("t3.small", "spot"), ("t3.micro", "on-demand")}
    result = runner.invoke(app, UP, input="y\n")
    assert result.exit_code == 0, result.output
    assert scripted.attempts[-2:] == [("t3.micro", "on-demand"), ("t3.small", "on-demand")]
    assert running(scripted)[0]["InstanceType"] == "t3.small"
    assert aws_has_no_leftover_template(scripted)


def aws_has_no_leftover_template(ec2: Any) -> bool:
    return not ec2.describe_launch_templates()["LaunchTemplates"]


def test_prompt_states_the_price_and_flags_anything_over_the_rupee_limit(
    scripted: Any, tmp_path: Path
) -> None:
    (tmp_path / ".portal.yaml").write_text("inr_per_usd: 100\n")
    scripted.no_room = {("t3.micro", "spot"), ("t3.small", "spot")}
    result = runner.invoke(app, UP, input="n\n", env={"COLUMNS": "200"})
    assert "$0.5000/hour" in result.output and "₹50.00/hour" in result.output
    assert "ABOVE the ₹10/hour" in result.output


def test_unknown_price_is_said_out_loud(scripted: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing_calls, "on_demand_usd_per_hour", lambda *a: None)
    scripted.no_room = {("t3.micro", "spot"), ("t3.small", "spot")}
    result = runner.invoke(app, UP, input="n\n", env={"COLUMNS": "200"})
    assert "price could not be looked up" in result.output


def test_a_quota_error_stops_the_walk_instead_of_burning_through_types(scripted: Any) -> None:
    scripted.no_room = {("t3.micro", "spot"), ("t3.small", "spot")}
    scripted.error = {"ErrorCode": "MaxSpotInstanceCountExceeded", "ErrorMessage": "vCPU limit"}
    result = runner.invoke(app, UP, input="n\n")
    assert result.exit_code == 1
    assert scripted.attempts == [("t3.micro", "spot")]
    assert "quota" in result.output


def test_price_lookup_reads_the_pricing_api_shape() -> None:
    product = {
        "terms": {
            "OnDemand": {
                "SKU.TERM": {
                    "priceDimensions": {"SKU.TERM.RATE": {"pricePerUnit": {"USD": "0.1344000000"}}}
                }
            }
        }
    }

    class Pricing:
        def get_paginator(self, name: str) -> Any:
            assert name == "get_products"
            outer = self

            class Pager:
                def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                    outer.kwargs = kwargs
                    return [{"PriceList": [json.dumps(product)]}]

            return Pager()

    class Ssm:
        class exceptions:
            ParameterNotFound = KeyError

        def get_parameter(self, Name: str) -> dict[str, Any]:
            assert Name == "/aws/service/global-infrastructure/regions/test-region-1/longName"
            return {"Parameter": {"Value": "Test Region (One)"}}

    pricing = Pricing()
    price = pricing_calls.on_demand_usd_per_hour(pricing, Ssm(), "test-region-1", "x.large")
    assert price == pytest.approx(0.1344)
    fields = {f["Field"]: f["Value"] for f in pricing.kwargs["Filters"]}  # type: ignore[attr-defined]
    assert fields["location"] == "Test Region (One)" and fields["instanceType"] == "x.large"
    assert fields["operatingSystem"] == "Linux" and fields["tenancy"] == "Shared"


# --- Day 23: up --resume ---------------------------------------------------------------------


def volume_tags(ec2: Any) -> dict[str, str]:
    (vol,) = home_volumes(ec2)
    return {t["Key"]: t["Value"] for t in vol["Tags"]}


def instance_expiry(ec2: Any) -> str:
    (inst,) = running(ec2)
    return next(t["Value"] for t in inst["Tags"] if t["Key"] == "portal:expires-at")


def test_attach_records_the_type_and_resets_the_drain_verdict(scripted: Any) -> None:
    assert runner.invoke(app, UP).exit_code == 0
    tags = volume_tags(scripted)
    assert tags["portal:last-instance-type"] == "t3.micro"
    assert tags["portal:resumable"] == "in-use"


def test_resume_keeps_the_original_expiry_not_a_fresh_ttl(scripted: Any) -> None:
    assert runner.invoke(app, UP).exit_code == 0
    original = instance_expiry(scripted)
    assert runner.invoke(app, ["down", "--name", "f1", "--yes"]).exit_code == 0
    result = runner.invoke(app, ["up", "--resume", "-t", "t3.micro", "--name", "f1"])
    assert result.exit_code == 0, result.output
    assert instance_expiry(scripted) == original
    assert "reused" in result.output


def test_resume_without_a_home_volume_says_so(scripted: Any) -> None:
    result = runner.invoke(app, ["up", "--resume", "-t", "t3.micro", "--name", "f1"])
    assert result.exit_code == 1 and "Nothing to resume" in result.output
    assert running(scripted) == []


def test_resume_and_ttl_cannot_be_combined(scripted: Any) -> None:
    result = runner.invoke(app, ["up", "--resume", "--ttl", "1h", "-t", "t3.micro"])
    assert result.exit_code == 1 and "cannot be combined with --ttl" in result.output


def test_resume_after_the_original_expiry_refuses_but_keeps_the_files(scripted: Any) -> None:
    assert runner.invoke(app, UP).exit_code == 0
    assert runner.invoke(app, ["down", "--name", "f1", "--yes"]).exit_code == 0
    (vol,) = home_volumes(scripted)
    past = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    scripted.create_tags(
        Resources=[vol["VolumeId"]], Tags=[{"Key": "portal:expires-at", "Value": past}]
    )
    result = runner.invoke(app, ["up", "--resume", "-t", "t3.micro", "--name", "f1"])
    assert (
        result.exit_code == 1
        and "has passed" in result.output
        and "files are kept" in result.output
    )
    assert len(home_volumes(scripted)) == 1 and running(scripted) == []


def test_resume_tries_a_different_type_first_then_the_interrupted_one(scripted: Any) -> None:
    assert runner.invoke(app, UP).exit_code == 0  # lands on t3.micro
    assert runner.invoke(app, ["down", "--name", "f1", "--yes"]).exit_code == 0
    scripted.attempts.clear()
    scripted.no_room = {("t3.small", "spot")}
    result = runner.invoke(
        app, ["up", "--resume", "-t", "t3.micro", "-t", "t3.small", "--name", "f1"]
    )
    assert result.exit_code == 0, result.output
    # t3.micro was where we were reclaimed from, so it goes last; t3.small has no room.
    assert scripted.attempts == [("t3.small", "spot"), ("t3.micro", "spot")]


def test_resume_after_a_dirty_unmount_mentions_it(scripted: Any) -> None:
    assert runner.invoke(app, UP).exit_code == 0
    assert runner.invoke(app, ["down", "--name", "f1", "--yes"]).exit_code == 0
    (vol,) = home_volumes(scripted)
    scripted.create_tags(
        Resources=[vol["VolumeId"]], Tags=[{"Key": "portal:resumable", "Value": "dirty"}]
    )
    result = runner.invoke(
        app, ["up", "--resume", "-t", "t3.micro", "--name", "f1"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0 and "could not unmount /home/dev cleanly" in result.output
