import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from portal import state
from portal import status as calc
from portal.cli import app
from tests.test_boxes import OWNER, aws, runner  # noqa: F401  (aws is a fixture)

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


def test_ttl_colours_follow_thresholds() -> None:
    assert calc.ttl_style(timedelta(hours=2)) == "green"
    assert calc.ttl_style(timedelta(minutes=29)) == "yellow"
    assert calc.ttl_style(timedelta(minutes=10)) == "yellow"
    assert calc.ttl_style(timedelta(minutes=9, seconds=59)) == "red"
    assert calc.ttl_style(timedelta(minutes=-5)) == "red"
    assert calc.ttl_style(None) == "red"


def test_ttl_and_cost_maths() -> None:
    remaining = calc.ttl_remaining("2030-01-01T13:30:00Z", NOW)
    assert remaining == timedelta(hours=1, minutes=30)
    assert calc.format_duration(remaining) == "1h30m"
    assert calc.format_duration(timedelta(minutes=-1)) == "expired"
    up = calc.uptime("2030-01-01T10:30:00Z", NOW)
    assert up == timedelta(hours=1, minutes=30)
    assert (
        calc.cost_so_far(up, 0.40) == 0.6000000000000001
        or abs((calc.cost_so_far(up, 0.40) or 0) - 0.6) < 1e-9
    )
    assert calc.cost_so_far(up, None) is None
    assert calc.ttl_remaining("garbage", NOW) is None


def test_status_lists_the_box(aws: Any) -> None:  # noqa: F811
    assert (
        runner.invoke(app, ["up", "--ttl", "2h", "-t", "t3.micro", "--name", "b1"]).exit_code == 0
    )
    result = runner.invoke(app, ["status"], env={"COLUMNS": "160"})
    assert result.exit_code == 0, result.output
    assert "b1" in result.output
    assert "t3.micro" in result.output


def test_status_still_finds_box_after_state_json_is_deleted(
    aws: Any,  # noqa: F811
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    assert (
        runner.invoke(app, ["up", "--ttl", "2h", "-t", "t3.micro", "--name", "b1"]).exit_code == 0
    )
    (tmp_path / "state.json").unlink()
    monkeypatch.setenv("PORTAL_REGION", "us-east-1")
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "b1" in result.output
    # ...and the cache has been rebuilt from AWS.
    cached = json.loads((tmp_path / "state.json").read_text())["boxes"]
    assert [b["name"] for b in cached] == ["b1"]


def test_reconcile_reports_stale_and_new_boxes(aws: Any) -> None:  # noqa: F811
    old = state.Box("gone", "i-gone", "t3.micro", "az", "running", "")
    new = state.Box("fresh", "i-new", "t3.micro", "az", "running", "")
    state.reconcile([old])
    result = state.reconcile([new])
    assert result.vanished == ["i-gone"]
    assert result.discovered == ["i-new"]
    assert [b["instance_id"] for b in state._read()["boxes"]] == ["i-new"]


def test_corrupt_state_file_is_rebuilt(aws: Any, tmp_path: Path) -> None:  # noqa: F811
    (tmp_path / "state.json").write_text("{not json")
    assert state.reconcile([]).boxes == []


def test_status_does_not_interpret_tag_values_as_markup(aws: Any) -> None:  # noqa: F811
    aws.run_instances(
        ImageId="ami-12345678",
        MinCount=1,
        MaxCount=1,
        InstanceType="t3.micro",
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "portal:managed", "Value": "true"},
                    {"Key": "portal:owner", "Value": OWNER},
                    {"Key": "portal:box", "Value": "[/bad]"},
                ],
            }
        ],
    )
    result = runner.invoke(app, ["status"], env={"COLUMNS": "160"})
    assert result.exit_code == 0, result.output
    assert "[/bad]" in result.output


def test_ttl_colour_boundaries_are_exact() -> None:
    # "amber under 30 minutes, red under 10": at exactly the limit it is the calmer colour.
    assert calc.ttl_style(timedelta(minutes=30)) == "green"
    assert calc.ttl_style(timedelta(minutes=30) - timedelta(seconds=1)) == "yellow"
    assert calc.ttl_style(timedelta(minutes=10)) == "yellow"
    assert calc.ttl_style(timedelta(minutes=10) - timedelta(seconds=1)) == "red"


def test_a_duration_of_exactly_zero_is_not_called_expired() -> None:
    assert calc.format_duration(timedelta(0)) == "0h00m"
    assert calc.format_duration(timedelta(seconds=-1)) == "expired"
