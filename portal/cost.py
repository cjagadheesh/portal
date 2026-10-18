"""`portal cost`: what portal-managed resources cost, from AWS Cost Explorer.

Cost Explorer is a billing API, not a live meter: data trails real usage by up to a day, the
`portal:managed` and `portal:box` tags must first be activated as cost allocation tags in the
Billing console (and only count from then on), and every API request is billed at about
one cent. So the numbers here are good for trends and budgets, and the AWS bill is the truth.
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from portal.aws import session

# Cost Explorer is a global service whose only endpoint lives in us-east-1. This is the
# service's address, not the user's region, so it is not configurable.
ENDPOINT_REGION = "us-east-1"
CONTROL_PLANE_LABEL = "(control plane)"


@dataclass(frozen=True)
class DayCost:
    day: date
    box: str  # "" for managed resources that are not part of a box (the control plane)
    usd: float


@dataclass(frozen=True)
class Summary:
    today: float
    week: float
    month: float
    projected_month: float | None  # None until a full day of data exists this month
    per_box_month: dict[str, float]
    days_in_month: int
    completed_days: int


def client() -> Any:
    return session.client("ce", ENDPOINT_REGION)


def windows(today: date) -> tuple[date, date, date]:
    """(month start, week start, end) where end is exclusive, as Cost Explorer wants it."""
    return today.replace(day=1), today - timedelta(days=today.weekday()), today + timedelta(days=1)


def fetch(ce: Any, start: date, end: date) -> list[DayCost]:
    """Daily cost of everything tagged portal:managed=true, split by the portal:box tag."""
    rows: list[DayCost] = []
    token: str | None = None
    while True:  # Cost Explorer paginates with NextPageToken
        kwargs: dict[str, Any] = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "Filter": {"Tags": {"Key": "portal:managed", "Values": ["true"]}},
            "GroupBy": [{"Type": "TAG", "Key": "portal:box"}],
        }
        if token:
            kwargs["NextPageToken"] = token
        page = ce.get_cost_and_usage(**kwargs)
        for result in page["ResultsByTime"]:
            day = date.fromisoformat(result["TimePeriod"]["Start"])
            for group in result.get("Groups", []):
                # Keys look like "portal:box$dev"; an empty value means "no box tag".
                box = group["Keys"][0].partition("$")[2]
                rows.append(DayCost(day, box, float(group["Metrics"]["UnblendedCost"]["Amount"])))
        token = page.get("NextPageToken")
        if not token:
            return rows


def summarize(rows: list[DayCost], today: date) -> Summary:
    month_start, week_start, _ = windows(today)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    in_month = [r for r in rows if r.day >= month_start]
    per_box: dict[str, float] = {}
    for r in in_month:
        label = r.box or CONTROL_PLANE_LABEL
        per_box[label] = per_box.get(label, 0.0) + r.usd
    # Today is still being billed and Cost Explorer lags, so the run rate uses only days
    # that are over. One full day of data is enough to project, a partial one is not.
    completed = (today - month_start).days
    finished_total = sum(r.usd for r in in_month if r.day < today)
    return Summary(
        today=sum(r.usd for r in rows if r.day == today),
        week=sum(r.usd for r in rows if r.day >= week_start),
        month=sum(r.usd for r in in_month),
        projected_month=finished_total / completed * days_in_month if completed else None,
        per_box_month=dict(sorted(per_box.items(), key=lambda kv: -kv[1])),
        days_in_month=days_in_month,
        completed_days=completed,
    )


def money(usd: float, inr_per_usd: float | None) -> tuple[str, str]:
    return f"${usd:,.2f}", ("-" if inr_per_usd is None else f"₹{usd * inr_per_usd:,.2f}")
