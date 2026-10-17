"""portal reaper: terminates expired portal boxes. Runs in Lambda every 5 minutes.

This is layer 3 of the shutdown design and it trusts nothing but EC2 tags. It does not read
the CLI's state, does not talk to the CLI or the on-box agent, and does not care why a box
is still running: if `portal:managed=true` and `portal:expires-at` is in the past, it goes.

WHAT IF THE EXPIRES-AT TAG IS MISSING OR UNREADABLE? Tags are applied at creation, so a
managed box without a valid expiry means someone removed or hand-edited it (or made the
box by hand). Never terminating it would leave a box with no end date, which is exactly the
leak this layer exists to stop. Terminating it instantly would punish a typo while the
person is fixing it. So: leave it alone for a grace period measured from its launch time
(default 60 minutes), then terminate. The grace is bounded, so the worst case is a box
that lives one extra hour; the home volume survives either way.

CLOCK SKEW. This Lambda compares expiries against AWS time, so the CLI computes them from
AWS time too (the HTTP Date header of an EC2 response) instead of the laptop's clock. A wrong
laptop clock therefore cannot make a box live longer or die earlier than asked. There is no
margin here: one could only ever extend a box's life, so the reaper compares exactly.

PARTIAL FAILURES. Terminations go in small batches. If a batch is rejected (one stale
instance id fails the whole EC2 call), each instance is retried alone so one bad box cannot
protect the others. After everything has been attempted the function raises, so the
invocation shows as failed and CloudWatch error metrics see it instead of it passing silently.
"""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Stopped boxes still pay for their disks; terminated/shutting-down ones are already going.
LIVE_STATES = ["pending", "running", "stopping", "stopped"]
BATCH = 50
PAGE_SIZE = 200
DEFAULT_GRACE_MINUTES = 60


@dataclass(frozen=True)
class Decision:
    instance_id: str
    action: str  # "terminate" | "keep" | "wait"
    reason: str


def parse_expiry(value: str | None) -> datetime | None:
    """RFC3339 with a timezone ('...Z' or '+05:30'). Anything else counts as unreadable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    # A timestamp with no timezone is ambiguous; guessing UTC could kill a box hours early.
    return None if parsed.tzinfo is None else parsed.astimezone(UTC)


def decide(instance: dict[str, Any], now: datetime, grace: timedelta) -> Decision:
    iid = instance["InstanceId"]
    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
    # The EC2 filter already guarantees this; checking again means a bug in the filter
    # still cannot make us terminate something we do not own.
    if tags.get("portal:managed") != "true":
        return Decision(iid, "keep", "not tagged portal:managed=true")

    expiry = parse_expiry(tags.get("portal:expires-at"))
    if expiry is not None:
        if now >= expiry:
            return Decision(iid, "terminate", f"expired {now - expiry} ago")
        return Decision(iid, "keep", f"expires in {expiry - now}")

    launched = instance.get("LaunchTime")
    age = now - launched if launched else timedelta(0)
    shown = tags.get("portal:expires-at")
    if age >= grace:
        return Decision(iid, "terminate", f"expires-at missing/unreadable ({shown!r}), age {age}")
    return Decision(
        iid, "wait", f"expires-at missing/unreadable ({shown!r}); grace ends in {grace - age}"
    )


def find_candidates(ec2: Any, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[
            {"Name": "tag:portal:managed", "Values": ["true"]},
            {"Name": "instance-state-name", "Values": LIVE_STATES},
        ],
        PaginationConfig={"PageSize": page_size},
    )
    return [i for page in pages for r in page["Reservations"] for i in r["Instances"]]


def terminate(ec2: Any, ids: list[str]) -> list[str]:
    """Terminate `ids`; return the ones that failed. Falls back to one-by-one on a batch error."""
    failed: list[str] = []
    for start in range(0, len(ids), BATCH):
        batch = ids[start : start + BATCH]
        try:
            ec2.terminate_instances(InstanceIds=batch)
            continue
        except ClientError as e:
            log("batch_failed", error=e.response["Error"]["Code"], instances=batch)
        for iid in batch:
            try:
                ec2.terminate_instances(InstanceIds=[iid])
            except ClientError as e:
                log("terminate_failed", instance=iid, error=e.response["Error"]["Code"])
                failed.append(iid)
    return failed


def log(event: str, **fields: Any) -> None:
    # One JSON object per line: greppable in CloudWatch Logs Insights.
    print(json.dumps({"event": event, **fields}, default=str))


def reap(
    ec2: Any,
    now: datetime,
    grace: timedelta,
    dry_run: bool = False,
    page_size: int = PAGE_SIZE,
) -> dict[str, Any]:
    decisions = [decide(i, now, grace) for i in find_candidates(ec2, page_size)]
    for d in decisions:
        log("decision", instance=d.instance_id, action=d.action, reason=d.reason, dry_run=dry_run)
    doomed = [d.instance_id for d in decisions if d.action == "terminate"]
    failed: list[str] = []
    if doomed and not dry_run:
        failed = terminate(ec2, doomed)
    summary = {
        "scanned": len(decisions),
        "terminated": 0 if dry_run else len(doomed) - len(failed),
        "would_terminate": len(doomed) if dry_run else 0,
        "waiting": sum(d.action == "wait" for d in decisions),
        "failed": failed,
    }
    log("summary", **summary)
    return summary


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    grace = timedelta(
        minutes=int(os.environ.get("REAPER_INVALID_TAG_GRACE_MINUTES", DEFAULT_GRACE_MINUTES))
    )
    dry_run = os.environ.get("REAPER_DRY_RUN", "false").lower() == "true"
    # Retries ride out EC2 throttling; the reaper must not give up because AWS was busy.
    ec2 = boto3.client("ec2", config=Config(retries={"mode": "standard", "max_attempts": 8}))
    summary = reap(ec2, datetime.now(UTC), grace, dry_run)
    if summary["failed"]:
        raise RuntimeError(f"could not terminate: {', '.join(summary['failed'])}")
    return summary
