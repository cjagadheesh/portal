"""`portal doctor`: check a real deployment, read-only, and say what to fix.

Everything here is something the drills in docs/ ask a human to verify. Each check is
independent: it returns ok / warn / fail, or "skip" when the credentials lack permission for the
read it needs (a missing permission is information, not an error). Nothing is created, changed
or deleted.
"""

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from portal import config, state, sync
from portal.aws import ec2 as ec2_calls
from portal.aws import home as home_calls
from portal.aws import session
from portal.aws import ssm as ssm_calls
from portal.errors import PortalError, translate_boto_error

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
EXPECTED_BOX_POLICY = "AmazonSSMManagedInstanceCore"
# "All Standard (A, C, D, H, I, M, R, T, Z) Spot Instance Requests" vCPU quota.
SPOT_VCPU_QUOTA = ("ec2", "L-34B43A08")
REAPER_ALIVE_WITHIN = timedelta(minutes=15)  # it runs every 5; allow for a late or missed run


@dataclass(frozen=True)
class Check:
    group: str
    name: str
    status: str
    detail: str
    fix: str = ""


@dataclass
class Context:
    """What the checks share. `clients` can be pre-filled (tests do) to avoid real AWS."""

    owner: str
    load_control_plane: Callable[[], state.ControlPlane]
    region: str = ""  # used until a control plane tells us the real one
    clients: dict[str, Any] = field(default_factory=dict)
    cfg: config.Config = field(default_factory=config.Config)
    cp: state.ControlPlane | None = None
    identity: dict[str, str] = field(default_factory=dict)

    def client(self, service: str) -> Any:
        if service not in self.clients:
            region = self.cp.region if self.cp else self.region
            if not region:
                raise PortalError("No region known. Set PORTAL_REGION or `region:` in your config.")
            self.clients[service] = session.client(service, region)
        return self.clients[service]

    @property
    def plane(self) -> state.ControlPlane:
        if self.cp is None:
            raise Skipped("no control plane")
        return self.cp

    @property
    def prefix(self) -> str:
        """Terraform names things `<prefix>-box`, `<prefix>-reaper`; the profile has the prefix."""
        return self.plane.instance_profile_name.removesuffix("-box")


class Skipped(Exception):
    """A check that cannot run here, with the reason."""


Outcome = tuple[str, str, str]  # status, detail, fix
CheckFn = Callable[[Context], Outcome]


def _denied(error: ClientError) -> bool:
    code = error.response.get("Error", {}).get("Code", "")
    return code.startswith("AccessDenied") or code in (
        "UnauthorizedOperation",
        "UnauthorizedAccess",
    )


def _run(ctx: Context, group: str, name: str, fn: CheckFn) -> Check:
    try:
        status, detail, fix = fn(ctx)
    except Skipped as why:
        return Check(group, name, SKIP, str(why))
    except ClientError as e:
        if _denied(e):
            action = e.response.get("Error", {}).get("Message", "")
            return Check(
                group, name, SKIP, f"no permission ({e.operation_name}); {action[:80]}".strip("; ")
            )
        return Check(group, name, FAIL, translate_boto_error(e))
    except NoCredentialsError:
        return Check(group, name, FAIL, "no AWS credentials", "Run `aws configure sso` and retry.")
    except (PortalError, BotoCoreError) as e:
        return Check(
            group, name, FAIL, str(e) if isinstance(e, PortalError) else translate_boto_error(e)
        )
    return Check(group, name, status, detail, fix)


# --- local tools ----------


def _tool(binary: str, needed_for: str, install_hint: str) -> CheckFn:
    def check(ctx: Context) -> Outcome:
        path = shutil.which(binary)
        if path:
            return OK, path, ""
        return FAIL, f"`{binary}` not found on PATH (needed for {needed_for})", install_hint

    return check


def _rsync(ctx: Context) -> Outcome:
    try:
        return OK, sync.check_rsync(), ""
    except PortalError as e:
        return FAIL, str(e), "Install rsync 3.1+ (macOS: brew install rsync)."


def _config(ctx: Context) -> Outcome:
    config.load()  # raises PortalError naming the file and the problem
    if not ctx.cfg.instance_types:
        return WARN, "no instance_types set", "Add `instance_types: [...]` to .portal.yaml."
    return OK, f"{len(ctx.cfg.instance_types)} instance type(s), box '{ctx.cfg.box_name}'", ""


# --- who am I, and where is the control plane ----------


def _identity(ctx: Context) -> Outcome:
    who = ctx.client("sts").get_caller_identity()
    ctx.identity = {"account": who["Account"], "arn": who["Arn"]}
    return OK, f"account {who['Account']} as {who['Arn']}", ""


def _control_plane(ctx: Context) -> Outcome:
    ctx.cp = ctx.load_control_plane()
    return OK, f"{ctx.cp.region}: security group {ctx.cp.security_group_id}", ""


def _clock(ctx: Context) -> Outcome:
    skew = abs(ec2_calls.aws_now(ctx.client("ec2")) - datetime.now(UTC))
    if skew > timedelta(seconds=60):
        return (
            WARN,
            f"your clock is {int(skew.total_seconds())}s off AWS's",
            "Harmless for portal (expiry uses AWS time) but fix it: it breaks other tools.",
        )
    return OK, f"within {int(skew.total_seconds()) + 1}s of AWS", ""


# --- the safety design, checked live ----------


def _no_inbound_rules(ctx: Context) -> Outcome:
    cp = ctx.plane
    ec2 = ctx.client("ec2")
    groups = [
        g
        for page in ec2.get_paginator("describe_security_groups").paginate(
            GroupIds=[cp.security_group_id]
        )
        for g in page["SecurityGroups"]
    ]
    if not groups:
        return FAIL, f"security group {cp.security_group_id} not found", "Run `portal init`."
    inbound = groups[0].get("IpPermissions", [])
    if inbound:
        ports = ", ".join(str(p.get("FromPort", "all")) for p in inbound)
        return (
            FAIL,
            f"{len(inbound)} inbound rule(s) (ports: {ports}): hard rule 1 is broken",
            "Remove them in the console, or `terraform apply` again, and find who added them.",
        )
    return OK, "zero inbound rules; access is SSM only", ""


def _box_role(ctx: Context) -> Outcome:
    cp = ctx.plane
    iam = ctx.client("iam")
    profile = iam.get_instance_profile(InstanceProfileName=cp.instance_profile_name)
    roles = profile["InstanceProfile"]["Roles"]
    if len(roles) != 1:
        return FAIL, f"instance profile has {len(roles)} roles, expected 1", "Run `portal init`."
    role = roles[0]["RoleName"]
    attached = {
        p["PolicyName"]
        for page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=role)
        for p in page["AttachedPolicies"]
    }
    inline = {
        n
        for page in iam.get_paginator("list_role_policies").paginate(RoleName=role)
        for n in page["PolicyNames"]
    }
    extra = (attached - {EXPECTED_BOX_POLICY}) | (inline - {"mark-home-volume-resumable"})
    if EXPECTED_BOX_POLICY not in attached:
        return (
            FAIL,
            f"role {role} lacks {EXPECTED_BOX_POLICY}: boxes cannot register with SSM",
            "Run `portal init`.",
        )
    if extra:
        return (
            WARN,
            f"role {role} has unexpected policies: {', '.join(sorted(extra))}",
            "Boxes should only talk to SSM.",
        )
    return OK, f"role {role}: SSM plus the one resumable-tag permission", ""


def _reaper_function(ctx: Context) -> Outcome:
    function = f"{ctx.prefix}-reaper"
    conf = ctx.client("lambda").get_function_configuration(FunctionName=function)
    env = conf.get("Environment", {}).get("Variables", {})
    if conf.get("State", "Active") != "Active":
        return (
            FAIL,
            f"{function} is {conf.get('State')}",
            "Check the function in the Lambda console.",
        )
    if env.get("REAPER_DRY_RUN", "false").lower() == "true":
        return (
            WARN,
            f"{function} is in DRY-RUN mode: it logs but terminates nothing",
            "Run `portal init` without --reaper-dry-run to arm it.",
        )
    return OK, f"{function} ({conf.get('Runtime')}) is armed", ""


def _reaper_schedule(ctx: Context) -> Outcome:
    rule = ctx.client("events").describe_rule(Name=f"{ctx.prefix}-reaper-schedule")
    if rule.get("State") != "ENABLED":
        return (
            FAIL,
            f"schedule is {rule.get('State')}",
            "Enable the EventBridge rule or run `portal init`.",
        )
    return OK, f"{rule.get('ScheduleExpression')} is enabled", ""


def _reaper_logs(ctx: Context) -> Outcome:
    logs = ctx.client("logs")
    group = f"/aws/lambda/{ctx.prefix}-reaper"
    groups = [
        g
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=group)
        for g in page["logGroups"]
        if g["logGroupName"] == group
    ]
    if not groups:
        return (
            WARN,
            f"log group {group} does not exist yet",
            "It appears with the first invocation.",
        )
    retention = groups[0].get("retentionInDays")
    streams = [
        s
        for page in logs.get_paginator("describe_log_streams").paginate(
            logGroupName=group,
            orderBy="LastEventTime",
            descending=True,
            PaginationConfig={"MaxItems": 1},
        )
        for s in page["logStreams"]
    ]
    last = streams[0].get("lastEventTimestamp") if streams else None
    if last is None:
        return (
            WARN,
            "the reaper has never logged anything",
            "Wait 5 minutes after `portal init`; if it persists, check the schedule.",
        )
    ago = datetime.now(UTC) - datetime.fromtimestamp(last / 1000, UTC)
    retention_note = "" if retention == 14 else f" (retention is {retention}, expected 14 days)"
    if ago > REAPER_ALIVE_WITHIN:
        return (
            WARN,
            f"last reaper run was {int(ago.total_seconds() // 60)} minutes ago{retention_note}",
            "It should run every 5 minutes: check the schedule and the function's errors.",
        )
    return OK, f"last ran {int(ago.total_seconds() // 60)} min ago{retention_note}", ""


def _budget(ctx: Context) -> Outcome:
    cp = ctx.plane
    if "account" not in ctx.identity:
        raise Skipped("needs the account id from the credentials check")
    budget = ctx.client("budgets").describe_budget(
        AccountId=ctx.identity["account"], BudgetName=cp.budget_name
    )["Budget"]
    limit = budget["BudgetLimit"]
    return OK, f"{cp.budget_name}: {limit['Amount']} {limit['Unit']} per month", ""


def _cost_tags(ctx: Context) -> Outcome:
    wanted = {"portal:managed", "portal:box"}
    active: set[str] = set()
    token: str | None = None
    while True:  # ListCostAllocationTags paginates with NextToken
        kwargs: dict[str, Any] = {"Status": "Active", "TagKeys": sorted(wanted)}
        if token:
            kwargs["NextToken"] = token
        page = ctx.client("ce").list_cost_allocation_tags(**kwargs)
        active |= {t["TagKey"] for t in page.get("CostAllocationTags", [])}
        token = page.get("NextToken")
        if not token:
            break
    missing = wanted - active
    if missing:
        return (
            WARN,
            f"not active as cost allocation tags: {', '.join(sorted(missing))}",
            "Billing console > Cost allocation tags > activate them. "
            "`portal cost` stays empty until then.",
        )
    return OK, "portal:managed and portal:box are active", ""


# --- can a box actually launch? ----------


def _subnets(ctx: Context) -> Outcome:
    cp = ctx.plane
    subnets = ec2_calls.public_subnets(ctx.client("ec2"), cp.security_group_id)
    zones = sorted({s["AvailabilityZone"] for s in subnets})
    return OK, f"{len(subnets)} public subnet(s) in {', '.join(zones)}", ""


def _instance_types(ctx: Context) -> Outcome:
    cp = ctx.plane
    types = ctx.cfg.instance_types
    if not types:
        raise Skipped("no instance_types configured")
    ec2 = ctx.client("ec2")
    ec2_calls.canonical_arch(ec2, types)  # mixed architectures raise a PortalError (a fail)
    zones = {s["AvailabilityZone"] for s in ec2_calls.public_subnets(ec2, cp.security_group_id)}
    offered: dict[str, set[str]] = {t: set() for t in types}
    for page in ec2.get_paginator("describe_instance_type_offerings").paginate(
        LocationType="availability-zone",
        Filters=[{"Name": "instance-type", "Values": types}],
    ):
        for offering in page["InstanceTypeOfferings"]:
            offered[offering["InstanceType"]].add(offering["Location"])
    unusable = [t for t in types if not offered[t] & zones]
    if len(unusable) == len(types):
        return (
            FAIL,
            f"none of {', '.join(types)} is offered in your subnets' zones",
            "Pick other types or another region.",
        )
    if unusable:
        return (
            WARN,
            f"{', '.join(unusable)} not offered in your subnets' zones (will be skipped)",
            "",
        )
    return OK, ", ".join(f"{t} in {len(offered[t] & zones)} zone(s)" for t in types), ""


def _image(ctx: Context) -> Outcome:
    types = ctx.cfg.instance_types
    if not types:
        raise Skipped("no instance_types configured")
    ec2 = ctx.client("ec2")
    arch = ec2_calls.canonical_arch(ec2, types)
    baked = ec2_calls.find_baked_ami(ec2, ctx.cfg.ami_name_prefix, arch)
    if baked:
        return OK, f"baked portal image {baked} (fast boot)", ""
    stock = ssm_calls.latest_ubuntu_ami(ctx.client("ssm"), arch)
    return (
        WARN,
        f"no baked image; stock Ubuntu {stock} (slower boots)",
        "Run the Packer build (docs/usage.md).",
    )


def _spot_quota(ctx: Context) -> Outcome:
    types = ctx.cfg.instance_types
    if not types:
        raise Skipped("no instance_types configured")
    needed = max(
        info["VCpuInfo"]["DefaultVCpus"]
        for page in ctx.client("ec2")
        .get_paginator("describe_instance_types")
        .paginate(InstanceTypes=types)
        for info in page["InstanceTypes"]
    )
    try:
        quota = ctx.client("service-quotas").get_service_quota(
            ServiceCode=SPOT_VCPU_QUOTA[0], QuotaCode=SPOT_VCPU_QUOTA[1]
        )["Quota"]["Value"]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "NoSuchResourceException":
            raise Skipped("quota not reported for this account") from e
        raise
    if needed > quota:
        return (
            FAIL,
            f"your largest type needs {needed} vCPUs but the spot quota is {quota:g}",
            "Request an increase in Service Quotas, or use a smaller size.",
        )
    return OK, f"largest type needs {needed} vCPUs; spot quota is {quota:g} (standard families)", ""


def _kept_volumes(ctx: Context) -> Outcome:
    ec2 = ctx.client("ec2")
    running = {b.name for b in ec2_calls.find_boxes(ec2, ctx.owner)}
    orphaned = home_calls.orphans(home_calls.list_home_volumes(ec2, ctx.owner), running)
    if not orphaned:
        return OK, "no home volumes sitting unused", ""
    gb = sum(v.size_gb for v in orphaned)
    names = ", ".join(v.box_name for v in orphaned)
    return (
        WARN,
        f"{len(orphaned)} unused home volume(s), {gb} GB ({names})",
        "`portal volumes`; `portal down --purge` to delete.",
    )


LOCAL: list[tuple[str, str, CheckFn]] = [
    ("tools", "aws CLI", _tool("aws", "ssh, sync", "Install AWS CLI v2.")),
    ("tools", "Session Manager plugin",
     _tool("session-manager-plugin", "ssh, sync", "Install the Session Manager plugin.")),
    ("tools", "rsync", _rsync),
    ("tools", "terraform", _tool("terraform", "init", "Install Terraform >= 1.5.")),
    ("config", "config files", _config),
]  # fmt: skip

AWS: list[tuple[str, str, CheckFn]] = [
    ("safety", "no inbound rules", _no_inbound_rules),
    ("safety", "box role is SSM-only", _box_role),
    ("safety", "reaper function", _reaper_function),
    ("safety", "reaper schedule", _reaper_schedule),
    ("safety", "reaper is running", _reaper_logs),
    ("safety", "budget alarm", _budget),
    ("billing", "cost allocation tags", _cost_tags),
    ("launch", "public subnets", _subnets),
    ("launch", "instance types offered", _instance_types),
    ("launch", "boot image", _image),
    ("launch", "spot vCPU quota", _spot_quota),
    ("housekeeping", "clock", _clock),
    ("housekeeping", "kept home volumes", _kept_volumes),
]


def run_checks(ctx: Context, *, local_only: bool = False) -> list[Check]:
    ctx.cfg = _safe_config()
    results = [_run(ctx, g, n, fn) for g, n, fn in LOCAL]
    if local_only:
        return results
    results.append(_run(ctx, "account", "credentials", _identity))
    # Without a control plane nothing else about the deployment can be checked.
    plane = _run(ctx, "account", "control plane", _control_plane)
    results.append(plane)
    if results[-2].status == FAIL or plane.status != OK:
        reason = "no credentials" if results[-2].status == FAIL else "no control plane"
        results += [Check(g, n, SKIP, reason) for g, n, _ in AWS]
        return results
    results += [_run(ctx, g, n, fn) for g, n, fn in AWS]
    return results


def _safe_config() -> config.Config:
    try:
        return config.load()
    except PortalError:
        return config.Config()  # the config check itself reports the problem
