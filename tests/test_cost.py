from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from portal import cost
from portal.cli import app
from tests.test_boxes import runner

TODAY = date(2030, 3, 14)  # a Thursday; the week started Monday the 11th


def group(box: str, usd: float) -> dict[str, Any]:
    return {
        "Keys": [f"portal:box${box}"],
        "Metrics": {"UnblendedCost": {"Amount": str(usd), "Unit": "USD"}},
    }


def day(d: date, *groups: dict[str, Any]) -> dict[str, Any]:
    return {"TimePeriod": {"Start": d.isoformat(), "End": d.isoformat()}, "Groups": list(groups)}


class FakeCe:
    """Cost Explorer with two pages, so pagination is exercised for real."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_cost_and_usage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if "NextPageToken" not in kwargs:
            return {
                "ResultsByTime": [day(date(2030, 3, 1), group("dev", 2.0), group("", 0.5))],
                "NextPageToken": "page2",
            }
        return {
            "ResultsByTime": [
                day(date(2030, 3, 2), group("dev", 4.0)),
                day(date(2030, 3, 12), group("dev", 1.0), group("other", 3.0)),
                day(TODAY, group("dev", 0.25)),
            ]
        }


def test_fetch_filters_on_managed_groups_by_box_and_follows_pages() -> None:
    ce = FakeCe()
    rows = cost.fetch(ce, date(2030, 3, 1), date(2030, 3, 15))
    assert len(ce.calls) == 2 and ce.calls[1]["NextPageToken"] == "page2"
    first = ce.calls[0]
    assert first["Filter"] == {"Tags": {"Key": "portal:managed", "Values": ["true"]}}
    assert first["GroupBy"] == [{"Type": "TAG", "Key": "portal:box"}]
    assert first["TimePeriod"] == {"Start": "2030-03-01", "End": "2030-03-15"}
    assert len(rows) == 6
    assert (
        cost.DayCost(date(2030, 3, 1), "", 0.5) in rows
    )  # an untagged-box row keeps an empty name


def test_windows_week_starts_on_monday_and_end_is_exclusive() -> None:
    month_start, week_start, end = cost.windows(TODAY)
    assert (month_start, week_start, end) == (
        date(2030, 3, 1),
        date(2030, 3, 11),
        date(2030, 3, 15),
    )


def test_summary_totals_and_projection() -> None:
    summary = cost.summarize(cost.fetch(FakeCe(), date(2030, 3, 1), date(2030, 3, 15)), TODAY)
    assert summary.today == pytest.approx(0.25)
    assert summary.week == pytest.approx(1.0 + 3.0 + 0.25)  # the 12th and today
    assert summary.month == pytest.approx(2.0 + 0.5 + 4.0 + 1.0 + 3.0 + 0.25)
    # 13 finished days (1st-13th) cost 10.5 in total; project over a 31-day month.
    assert summary.completed_days == 13
    assert summary.projected_month == pytest.approx(10.5 / 13 * 31)
    assert summary.per_box_month == {
        "dev": pytest.approx(7.25),
        "other": 3.0,
        cost.CONTROL_PLANE_LABEL: 0.5,
    }
    assert next(iter(summary.per_box_month)) == "dev"  # biggest first


def test_no_projection_on_the_first_of_the_month() -> None:
    first = date(2030, 3, 1)
    summary = cost.summarize([cost.DayCost(first, "dev", 1.0)], first)
    assert summary.projected_month is None and summary.month == 1.0


def test_money_shows_both_currencies_only_when_a_rate_is_known() -> None:
    assert cost.money(2.5, 83.0) == ("$2.50", "₹207.50")
    assert cost.money(2.5, None) == ("$2.50", "-")


# --- the command ---------------------------------------------------------------------------


@pytest.fixture
def fake_ce(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeCe:
    ce = FakeCe()
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cost, "client", lambda: ce)

    class FrozenDatetime:
        @staticmethod
        def now(tz: Any) -> Any:
            import datetime

            return datetime.datetime(2030, 3, 14, 9, 0, tzinfo=tz)

    monkeypatch.setattr("portal.cli.datetime", FrozenDatetime)
    return ce


def test_cost_command_shows_usd_inr_and_the_rate(fake_ce: FakeCe, tmp_path: Path) -> None:
    (tmp_path / ".portal.yaml").write_text("inr_per_usd: 80\n")
    result = runner.invoke(app, ["cost"], env={"COLUMNS": "120"})
    assert result.exit_code == 0, result.output
    for needle in ("today", "this week", "this month", "projected month", "$0.25", "₹20.00"):
        assert needle in result.output
    assert "1 USD = 80 INR" in result.output
    assert "dev" in result.output and "(control plane)" in result.output


def test_cost_without_a_rate_says_how_to_get_rupees(fake_ce: FakeCe) -> None:
    result = runner.invoke(app, ["cost"], env={"COLUMNS": "120"})
    assert result.exit_code == 0, result.output
    assert "inr_per_usd" in result.output and "₹" not in result.output


def test_cost_with_no_data_explains_cost_allocation_tags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Empty:
        def get_cost_and_usage(self, **kwargs: Any) -> dict[str, Any]:
            return {"ResultsByTime": []}

    monkeypatch.setenv("PORTAL_HOME", str(tmp_path / "h"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cost, "client", lambda: Empty())
    result = runner.invoke(app, ["cost"], env={"COLUMNS": "120"})
    assert result.exit_code == 0, result.output
    assert "cost allocation tags" in result.output


def test_the_week_starts_on_monday_inclusive() -> None:
    monday, sunday = date(2030, 3, 11), date(2030, 3, 10)
    rows = [cost.DayCost(sunday, "dev", 100.0), cost.DayCost(monday, "dev", 1.0)]
    assert cost.summarize(rows, TODAY).week == pytest.approx(1.0)  # Monday in, Sunday out


def test_cost_records_cannot_be_modified_after_the_fact() -> None:
    row = cost.DayCost(TODAY, "dev", 1.0)
    with pytest.raises(FrozenInstanceError):
        row.usd = 2.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        cost.summarize([row], TODAY).today = 9.0  # type: ignore[misc]
