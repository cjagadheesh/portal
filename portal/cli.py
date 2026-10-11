"""portal CLI: one function per command. Day 1 stubs; each is filled in by a later plan day."""

import contextlib
import getpass
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import boto3
import typer
from botocore.exceptions import BotoCoreError, ClientError
from rich import box as rich_box
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
from rich.table import Table

from portal import config, state, terraform
from portal import cost as cost_calc
from portal import doctor as doctor_calls
from portal import status as status_calc
from portal import sync as sync_calls
from portal.aws import control_plane as control_plane_calls
from portal.aws import ec2 as ec2_calls
from portal.aws import home as home_calls
from portal.aws import pricing as pricing_calls
from portal.aws import ssm as ssm_calls
from portal.errors import PortalError, translate_boto_error
from portal.lock import box_lock
from portal.ttl import parse_ttl

app = typer.Typer(
    help="Ephemeral AWS Spot dev boxes that cannot run up a surprise bill.",
    no_args_is_help=True,
    # Typer's default traceback prints every local variable, which can include credentials.
    pretty_exceptions_show_locals=False,
)


def _show_version(show: bool) -> None:
    if show:
        console.print(f"portal {importlib.metadata.version('portal-dev')}")
        raise typer.Exit


def _enable_debug(on: bool) -> None:
    if on:
        os.environ["PORTAL_DEBUG"] = "1"


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_show_version, is_eager=True, help="Show the version."),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            envvar="PORTAL_DEBUG",
            callback=_enable_debug,
            help="Show the full traceback if portal hits an unexpected error.",
        ),
    ] = False,
) -> None:
    """Ephemeral AWS Spot dev boxes that cannot run up a surprise bill."""


console = Console()


@contextmanager
def _clean_errors(instance_types: list[str] | None = None) -> Iterator[None]:
    """Turn expected failures into one red line and exit code 1, never a traceback."""
    try:
        yield
    except PortalError as e:
        console.print(str(e), style="red", markup=False)
        raise typer.Exit(1) from e
    except (ClientError, BotoCoreError) as e:
        console.print(translate_boto_error(e, instance_types), style="red", markup=False)
        raise typer.Exit(1) from e
    except OSError as e:
        # Unwritable ~/.portal, a full disk, a vanished directory: say so, no traceback.
        console.print(f"File system problem: {e}", style="red", markup=False)
        raise typer.Exit(1) from e


def _default_region() -> str | None:
    return (
        os.environ.get("PORTAL_REGION")
        or config.load().region
        or boto3.session.Session().region_name
    )


def _control_plane() -> state.ControlPlane:
    cp = state.load_control_plane()
    if cp is None:
        # state.json was deleted or this is a second laptop. AWS still has the resources, so
        # find them by tag instead of asking the user to run `init` against an existing setup.
        region = _default_region()
        if region:
            cp = control_plane_calls.discover(
                ec2_calls.client("ec2", region), ec2_calls.client("iam", region), region
            )
            if cp is not None:
                state.save_control_plane(cp)
    if cp is None:
        raise PortalError("No control plane found. Run `portal init` first.")
    wanted = os.environ.get("PORTAL_REGION")
    if wanted and wanted != cp.region:
        raise PortalError(
            f"Region mismatch: PORTAL_REGION={wanted} but the control plane lives in "
            f"{cp.region}. Unset PORTAL_REGION or run `portal init --region {wanted}`."
        )
    return cp


def _my_boxes(ec2: Any, owner: str, name: str | None = None) -> list[state.Box]:
    """Every command starts here: read AWS tags, refresh the local cache, then filter."""
    reconciled = state.reconcile(ec2_calls.find_boxes(ec2, owner))
    return [b for b in reconciled.boxes if name is None or b.name == name]


# SSM's default session is `ssm-user` in /usr/bin. Switch to the `dev` user bootstrap
# creates (home dir, docker group); fall back to ssm-user while bootstrap is still running.
_LOGIN_AS_DEV = (
    "if id dev >/dev/null 2>&1; then exec sudo -iu dev; fi; "
    "echo 'portal: bootstrap still running (see /var/log/portal-bootstrap.log); "
    "this shell is ssm-user'; exec bash -l"
)


def _session_tools(command: str) -> str:
    """Return the `aws` path; commands that open SSM sessions need it plus the plugin."""
    aws_cli = shutil.which("aws")
    if aws_cli is None or shutil.which("session-manager-plugin") is None:
        raise PortalError(
            f"`portal {command}` needs both the AWS CLI (`aws`) and `session-manager-plugin` "
            "on PATH."
        )
    return aws_cli


NameOption = Annotated[
    str | None,
    typer.Option(help="Box name; one running box per name. Default: box_name from config."),
]


def _report_attempt(instance_type: str, market: str, outcome: str) -> None:
    colour = "green" if outcome == "launched" else "yellow"
    console.print(f"  {market} {instance_type}: {outcome}", style=colour, markup=False)


# Ask the user before launching anything costing more than about this much per hour.
ON_DEMAND_WARN_INR_PER_HOUR = 10


def _confirm_on_demand(region: str, types: list[str], inr_per_usd: float | None) -> bool:
    """No spot capacity anywhere in the list. On-demand always has capacity but costs several
    times more, so it is never started without an explicit yes (and never by --yes)."""
    price = None
    # If the lookup fails the warning below says the price is unknown; the choice is still theirs.
    with contextlib.suppress(ClientError, BotoCoreError):
        price = pricing_calls.on_demand_usd_per_hour(
            pricing_calls.client(), ec2_calls.client("ssm", region), region, types[0]
        )
    if price is None:
        cost = "its price could not be looked up (check the EC2 pricing page)"
    else:
        cost = f"about ${price:.4f}/hour"
        if inr_per_usd:
            inr = price * inr_per_usd
            cost += f" (₹{inr:.2f}/hour at your rate of {inr_per_usd:g})"
            if inr > ON_DEMAND_WARN_INR_PER_HOUR:
                cost += f", ABOVE the ₹{ON_DEMAND_WARN_INR_PER_HOUR}/hour you asked to be warned at"
    console.print(
        f"\nNo spot capacity for any of: {', '.join(types)}.",
        style="yellow",
        markup=False,
    )
    return typer.confirm(
        f"Start {types[0]} ON-DEMAND instead? It costs {cost} until the box expires or you "
        "run `portal down`.",
        default=False,
    )


@app.command()
def init(
    budget_usd: Annotated[
        float,
        typer.Option(
            min=0.01, help="Monthly account spend limit in USD (AWS Budgets bills in USD)."
        ),
    ],
    region: Annotated[
        str | None,
        typer.Option(
            envvar="PORTAL_REGION",
            help="AWS region for the control plane. Default: region from ~/.portal/config.yaml.",
        ),
    ] = None,
    alert_email: Annotated[
        str | None, typer.Option(help="Email for budget alerts (needs a confirm click).")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the terraform plan; change nothing.")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Apply a plan that destroys resources without asking first."),
    ] = False,
    reaper_dry_run: Annotated[
        bool,
        typer.Option(
            "--reaper-dry-run",
            help="Deploy the reaper in log-only mode: it logs what it would terminate but "
            "terminates nothing. Re-run `init` without this flag to arm it.",
        ),
    ] = False,
) -> None:
    """Create the control plane (IAM, security group, reaper Lambda, budget) once per account.

    Safe to re-run: if nothing would change, it only refreshes ~/.portal/state.json.
    """
    with _clean_errors():
        region = region or config.load().region
        if not region:
            raise PortalError(
                "No region given. Pass --region or set region in ~/.portal/config.yaml."
            )
        config.check_region(region)
        variables = {
            "region": region,
            "owner": getpass.getuser(),
            "monthly_budget_usd": str(budget_usd),
        }
        if alert_email:
            variables["alert_email"] = alert_email
        if reaper_dry_run:
            variables["reaper_dry_run"] = "true"
        terraform.init()
        changes, plan_text = terraform.plan(variables)
        if dry_run:
            console.print(plan_text, markup=False, highlight=False)  # [..] in a plan is text
            return
        if changes:
            doomed = terraform.resources_to_destroy(plan_text)
            if doomed and not yes:
                # Rule 4. A first `init` only adds things; destroying means replacing part of a
                # control plane that running boxes depend on (their security group, their role).
                console.print(plan_text, markup=False, highlight=False)
                if not typer.confirm(
                    f"This plan DESTROYS {doomed} resource(s) (listed above). Apply anyway?",
                    default=False,
                ):
                    console.print("aborted; nothing changed")
                    raise typer.Exit(1)
            terraform.apply(variables)
            console.print("control plane applied")
        else:
            console.print("control plane already up to date; nothing to do")
        out = terraform.outputs()
        missing = [
            k for k in ("instance_profile_name", "security_group_id", "budget_name") if k not in out
        ]
        if missing:
            raise PortalError(
                f"Terraform did not report {', '.join(missing)}. The control plane may be "
                "half-applied; run `portal init` again."
            )
        state.save_control_plane(
            state.ControlPlane(
                region=region,
                instance_profile_name=out["instance_profile_name"],
                security_group_id=out["security_group_id"],
                budget_name=out["budget_name"],
            )
        )
    console.print(f"saved to {state.state_path()}")


@app.command()
def up(
    ttl: Annotated[
        str | None,
        typer.Option(
            help="How long the box may live, e.g. 4h, 90m, 1h30m; then it is terminated. "
            "Default: default_ttl from config."
        ),
    ] = None,
    instance_type: Annotated[
        list[str] | None,
        typer.Option(
            "--instance-type",
            "-t",
            help="Instance type; repeat for fallbacks: -t TYPE1 -t TYPE2. "
            "Default: instance_types from config.",
        ),
    ] = None,
    name: NameOption = None,
    timezone: Annotated[
        str | None,
        typer.Option(
            envvar="PORTAL_TIMEZONE",
            help="Timezone set on the box, e.g. Asia/Kolkata. Default: timezone from config.",
        ),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Continue a box whose home volume already exists (after a spot interruption or "
            "a `down`): same name, same home, and the ORIGINAL expiry instead of a fresh TTL.",
        ),
    ] = False,
) -> None:
    """Launch a spot dev box that expires automatically after its TTL.

    Tags are applied at creation, so the reaper can stop the box even if this
    command or your laptop dies. Instance types are tried in the order you list
    them; if none has spot capacity you are asked before anything on-demand starts.
    """
    with _clean_errors():
        cfg = config.load()
        types = [
            config.check_instance_type(t)
            for t in dict.fromkeys(instance_type or cfg.instance_types)  # drop repeats, keep order
        ]
        if not types:
            raise PortalError(
                "No instance types. Pass -t TYPE or set instance_types in .portal.yaml."
            )
    # Second block so capacity errors can name the resolved fallback list.
    with _clean_errors(types), contextlib.ExitStack() as stack:
        name = name or cfg.box_name
        ec2_calls.check_box_name(name)
        stack.enter_context(box_lock(name))
        if resume and ttl:
            raise PortalError(
                "--resume keeps the original expiry, so it cannot be combined with --ttl. "
                "Use `portal extend` afterwards to add time."
            )
        lifetime = parse_ttl(ttl or cfg.default_ttl)
        if lifetime > parse_ttl(cfg.max_ttl):
            raise PortalError(
                f"TTL {ttl or cfg.default_ttl} is longer than max_ttl ({cfg.max_ttl}, from your "
                "config). Pick a shorter --ttl or raise max_ttl."
            )
        cp = _control_plane()
        owner = getpass.getuser()
        ec2 = ec2_calls.client("ec2", cp.region)
        now = ec2_calls.aws_now(ec2)
        expires_at = now + lifetime
        mine = _my_boxes(ec2, owner)
        existing = [b for b in mine if b.name == name]
        if existing:
            raise PortalError(
                f"Box '{name}' is already running as {existing[0].instance_id}. "
                "Run `portal down` first or pick another --name."
            )
        if len(mine) >= cfg.max_running_boxes:
            names = ", ".join(b.name for b in mine)
            raise PortalError(
                f"You already have {len(mine)} running boxes ({names}); the limit is "
                f"max_running_boxes: {cfg.max_running_boxes}. Run `portal down` on one, "
                "or raise the limit in your config."
            )
        home = home_calls.find_home_volume(ec2, owner, name)
        if home is not None:
            home_calls.check_reusable(home)
        order = list(types)
        if resume:
            if home is None:
                raise PortalError(
                    f"Nothing to resume: there is no home volume for '{name}'. "
                    "Start fresh with `portal up` (without --resume)."
                )
            original = status_calc.parse_rfc3339(home.expires_at)
            if original is None:
                raise PortalError(
                    f"Home volume {home.volume_id} has no readable expiry to resume. "
                    "Start with `portal up --ttl ...` instead; your files are kept."
                )
            if original <= now:
                raise PortalError(
                    f"The original expiry ({home.expires_at}) has passed, so there is no time "
                    "left to resume. Start with `portal up --ttl ...`; your files are kept."
                )
            expires_at = original
            if home.resumable == "dirty":
                console.print(
                    "note: the last box could not unmount /home/dev cleanly; XFS will replay "
                    "its journal on mount, which normally just works.",
                    style="yellow",
                    markup=False,
                )
            # The type we were just reclaimed from is the likeliest to be short of capacity;
            # try the others first and keep it as the last resort.
            if home.last_instance_type in order and len(order) > 1:
                order.remove(home.last_instance_type)
                order.append(home.last_instance_type)
        running = {b.name for b in mine}
        for orphan in home_calls.orphans(home_calls.list_home_volumes(ec2, owner), running):
            if orphan.box_name != name:  # this one is about to be reused, not orphaned
                console.print(
                    f"note: home volume {orphan.volume_id} ({orphan.size_gb} GB) belongs to "
                    f"'{orphan.box_name}', which is not running; it still bills for storage. "
                    f"`portal down --name {orphan.box_name} --purge` deletes it.",
                    style="yellow",
                    markup=False,
                )
        box = ec2_calls.launch_box(
            ec2,
            ec2_calls.client("ssm", cp.region),
            cp,
            owner,
            name,
            order,
            expires_at,
            timezone=timezone or cfg.timezone,
            disk_gb=cfg.disk_gb,
            # EBS volumes live in one AZ, so an existing home pins the box next to it.
            az=home.az if home else None,
            idle_timeout_minutes=cfg.idle_timeout_minutes,
            ami_prefix=cfg.ami_name_prefix,
            on_attempt=_report_attempt,
            confirm_on_demand=lambda tried: _confirm_on_demand(cp.region, tried, cfg.inr_per_usd),
        )
        console.print(f"{box.instance_id} ({box.instance_type}, {box.az}) launching as '{name}'")
        created_here = ""
        try:
            if home is None:
                volume_id = created_here = home_calls.create_home_volume(
                    ec2, owner, name, box.az, cfg.home_gb, expires_at
                )
                what = f"new, {cfg.home_gb} GB"
            else:
                volume_id, what = home.volume_id, f"reused, {home.size_gb} GB"
            home_calls.attach_home(ec2, box, volume_id, expires_at)
        except (ClientError, BotoCoreError, KeyboardInterrupt):
            # A box without its home would put your work on the root disk, which dies with
            # it. Don't leave one running (and billing); the volume, if any, is kept. Ctrl+C
            # counts: it arrives exactly in this window, between launch and attach. A hard
            # kill cannot be caught here; the reaper handles that case.
            try:
                ec2_calls.terminate_boxes(ec2, [box])
                console.print(f"could not attach the home volume; terminated {box.instance_id}")
            except (ClientError, BotoCoreError):
                console.print(
                    f"could not attach the home volume AND could not terminate {box.instance_id}; "
                    f"run `portal down --name {name}` now",
                    style="red",
                    markup=False,
                )
            if created_here:
                # Created a moment ago and never used, so it holds nothing worth keeping and
                # would only sit there billing. If it is still attached the delete is refused,
                # which is fine: an empty volume is harmless (the box formats a blank one).
                with contextlib.suppress(ClientError, BotoCoreError):
                    ec2.delete_volume(VolumeId=created_here)
            raise
    image = (
        "baked portal image (fast boot)"
        if box.image == "baked"
        else "stock Ubuntu (installs packages at boot; run the Packer build for faster boots)"
    )
    console.print(f"image: {image}")
    console.print(f"home volume {volume_id} ({what}) attached at /home/dev")
    console.print(f"expires {box.expires_at}; connect with `portal ssh --name {name}`")


@app.command()
def down(
    name: NameOption = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Skip the y/n prompt (never skips --purge's check).")
    ] = False,
    purge: Annotated[
        bool,
        typer.Option(
            "--purge/--keep-home",
            help="--purge also deletes the /home/dev volume forever; you must type the box "
            "name to confirm. Default: --keep-home.",
        ),
    ] = False,
) -> None:
    """Terminate the box. Its root disk is lost; the /home/dev volume is kept.

    Only instances and volumes tagged as yours and managed by portal are touched.
    """
    with _clean_errors(), contextlib.ExitStack() as stack:
        cp = _control_plane()
        ec2 = ec2_calls.client("ec2", cp.region)
        name = name or config.load().box_name
        stack.enter_context(box_lock(name))
        owner = getpass.getuser()
        boxes = _my_boxes(ec2, owner, name)
        home = home_calls.find_home_volume(ec2, owner, name) if purge else None
        if not boxes and home is None:
            suffix = " and no home volume" if purge else ""
            console.print(f"no running box named '{name}'{suffix}")
            return
        ids = ", ".join(b.instance_id for b in boxes)
        # Rule 4. Once `portal sync` exists, this is where unsynced work gets warned about.
        if home is not None:
            # Deleting your work must take more than one keystroke, and --yes does not count:
            # a flag left in shell history or a script would make it one.
            what = f"box {ids} and " if boxes else ""
            typed = typer.prompt(
                f"This permanently deletes {what}home volume {home.volume_id} "
                f"({home.size_gb} GB, everything in /home/dev). Type '{name}' to confirm"
            )
            if typed.strip() != name:
                console.print("name did not match; nothing deleted")
                raise typer.Exit(1)
        elif not yes and not typer.confirm(
            f"Terminate {ids}? Its root disk is deleted (docker images, /tmp, installed packages); "
            "/home/dev (home volume) is kept."
        ):
            console.print("aborted; box left running")
            return
        if boxes:
            ec2_calls.terminate_boxes(ec2, boxes)
            console.print(f"terminating {ids}")
        if home is not None:
            with console.status(f"waiting for {home.volume_id} to detach..."):
                home_calls.delete_home_volume(ec2, home.volume_id)
            console.print(f"deleted home volume {home.volume_id}")
        elif purge:
            console.print(f"no home volume for '{name}' to delete")
        else:
            console.print("home volume kept for the next `portal up`")


@app.command()
def volumes() -> None:
    """List your /home/dev volumes: size, age, and what they cost while kept.

    Volumes whose box is not running are marked orphaned: they still bill for storage.
    Monthly cost needs gp3_usd_per_gb_month in your config.
    """
    with _clean_errors():
        cp = _control_plane()
        ec2 = ec2_calls.client("ec2", cp.region)
        owner = getpass.getuser()
        vols = home_calls.list_home_volumes(ec2, owner)
        running = {b.name for b in _my_boxes(ec2, owner)}
        price = config.load().gp3_usd_per_gb_month
    if not vols:
        console.print("no home volumes")
        return
    now = datetime.now(UTC)
    table = Table(title=f"home volumes ({cp.region})", box=rich_box.SIMPLE_HEAD)
    for column in ("box", "volume", "size", "AZ", "state", "age", "$/month", ""):
        table.add_column(column, overflow="fold")
    for v in vols:
        orphaned = v.box_name not in running
        table.add_row(
            escape(v.box_name),
            v.volume_id,
            f"{v.size_gb} GB",
            v.az,
            v.state,
            f"{(now - v.created_at).days}d",
            "-" if price is None else f"{v.size_gb * price:.2f}",
            "[yellow]orphaned[/]" if orphaned else "",
        )
    console.print(table)
    if price is None:
        console.print("set gp3_usd_per_gb_month in ~/.portal/config.yaml to see monthly cost")


def _run_in_session(command_name: str, name: str | None, timeout: int, remote_command: str) -> int:
    """Wait for the box to register with SSM, then run `remote_command` in a session."""
    # boto3 can only create a session; the plugin speaks the websocket protocol that
    # carries the terminal, so we hand off to `aws ssm start-session`.
    aws_cli = _session_tools(command_name)
    cp = _control_plane()
    name = name or config.load().box_name
    boxes = _my_boxes(ec2_calls.client("ec2", cp.region), getpass.getuser(), name)
    if not boxes:
        raise PortalError(f"No running box named '{name}'. Start one with `portal up`.")
    target = boxes[0].instance_id
    with console.status(f"waiting for {target} to register with SSM..."):
        ssm_calls.wait_until_online(ec2_calls.client("ssm", cp.region), target, timeout)
    # This is the number to record for the cloud-init vs Packer AMI comparison (ADR-005).
    age = status_calc.uptime(boxes[0].launched_at, datetime.now(UTC))
    if age is not None:
        console.print(f"SSM online {int(age.total_seconds())}s after launch")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            aws_cli,
            "ssm",
            "start-session",
            "--target",
            target,
            "--region",
            cp.region,
            "--document-name",
            "AWS-StartInteractiveCommand",
            "--parameters",
            json.dumps({"command": [remote_command]}),
        ],
        check=False,
    )
    return result.returncode


TimeoutOption = Annotated[
    int, typer.Option(min=1, help="Seconds to wait for the box to be reachable.")
]


@app.command()
def ssh(name: NameOption = None, timeout: TimeoutOption = 180) -> None:
    """Open a shell on the box over SSM Session Manager (no inbound ports, no keys).

    Needs the AWS CLI and the Session Manager plugin on your PATH:
    https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html
    """
    with _clean_errors():
        code = _run_in_session("ssh", name, timeout, _LOGIN_AS_DEV)
    raise typer.Exit(code)


_AGENT_LOG = "/var/log/portal-agent.log"


@app.command(name="agent-log")
def agent_log(
    name: NameOption = None,
    lines: Annotated[int, typer.Option("--lines", "-n", min=1, help="Lines to show.")] = 50,
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Keep streaming new lines; Ctrl+C to stop.")
    ] = False,
    timeout: TimeoutOption = 180,
) -> None:
    """Show the idle-shutdown agent's log from the box (why it is, or is not, shutting down).

    One line per minute: "ok: <what counts as active>", "quiet for X of Y min", or
    "SHUTTING DOWN: <reason>". Needs the same tools as `portal ssh`.
    """
    tail = f"tail -n {int(lines)}{' -f' if follow else ''} {_AGENT_LOG}"
    missing = "echo 'no agent log yet (the agent starts about a minute after boot)'"
    remote = f"if [ -f {_AGENT_LOG} ]; then {tail}; else {missing}; fi"
    with _clean_errors():
        code = _run_in_session("agent-log", name, timeout, remote)
    raise typer.Exit(code)


@app.command()
def status() -> None:
    """Show your boxes: state, uptime, spot price, cost so far, TTL remaining.

    Reads AWS tags every time, so it works even if ~/.portal/state.json was deleted.
    Cost is an estimate: the current spot price times uptime.
    """
    with _clean_errors():
        region = _default_region()
        cp = state.load_control_plane()
        region = cp.region if cp else region
        if region is None:
            raise PortalError("No region known. Run `portal init` or set PORTAL_REGION.")
        ec2 = ec2_calls.client("ec2", region)
        reconciled = state.reconcile(ec2_calls.find_boxes(ec2, getpass.getuser()))
        now = datetime.now(UTC)
        # SIMPLE_HEAD drops the vertical borders so nine columns still fit an 80-column terminal.
        table = Table(title=f"portal boxes ({region})", box=rich_box.SIMPLE_HEAD)
        for column in (
            "box",
            "instance",
            "type",
            "AZ",
            "state",
            "uptime",
            "spot $/h",
            "cost $",
            "TTL left",
        ):
            # fold wraps long values onto a second line instead of cutting them with "…".
            table.add_column(column, overflow="fold")
        for box in reconciled.boxes:
            # The spot price says nothing about an on-demand box (the fallback), so show "-"
            # rather than a number that looks right and is wrong.
            price = (
                ec2_calls.spot_price(ec2, box.instance_type, box.az)
                if box.market == "spot"
                else None
            )
            up = status_calc.uptime(box.launched_at, now)
            cost_usd = status_calc.cost_so_far(up, price)
            remaining = status_calc.ttl_remaining(box.expires_at, now)
            ttl_text = "unknown" if remaining is None else status_calc.format_duration(remaining)
            table.add_row(
                # Tags can be edited by anyone with EC2 access; never let one act as markup.
                escape(box.name),
                box.instance_id,
                box.instance_type
                if box.market == "spot"
                else f"{box.instance_type} [bold red]on-demand[/]",
                box.az,
                box.state,
                "-" if up is None else status_calc.format_duration(up),
                "-" if price is None else f"{price:.4f}",
                "-" if cost_usd is None else f"{cost_usd:.2f}",
                f"[{status_calc.ttl_style(remaining)}]{ttl_text}[/]",
            )
    if not reconciled.boxes:
        console.print("no running boxes")
        return
    console.print(table)


@app.command()
def sync(
    name: NameOption = None,
    src: Annotated[
        Path, typer.Option(help="Local folder to push.", exists=True, file_okay=False)
    ] = Path("."),
    dest: Annotated[
        str | None,
        typer.Option(help="Folder under /home/dev to push into. Default: the folder's own name."),
    ] = None,
    watch: Annotated[
        bool, typer.Option("--watch", help="Keep running and push again whenever a file changes.")
    ] = False,
    timeout: Annotated[
        int, typer.Option(min=1, help="Seconds to wait for the box to be reachable.")
    ] = 180,
) -> None:
    """Push a local folder to the box (one way; never deletes files on the box).

    Uses rsync over an SSM port-forward: no inbound port, no SSH key.

    Files matched by .gitignore or .portalignore are skipped, in any subfolder.

    Needs the AWS CLI, the Session Manager plugin and rsync 3.1+ on your PATH.
    """
    with _clean_errors():
        aws_cli = _session_tools("sync")
        rsync = sync_calls.check_rsync()
        source = src.resolve()
        target = sync_calls.check_dest(dest or source.name)
        cp = _control_plane()
        name = name or config.load().box_name
        boxes = _my_boxes(ec2_calls.client("ec2", cp.region), getpass.getuser(), name)
        if not boxes:
            raise PortalError(f"No running box named '{name}'. Start one with `portal up`.")
        box = boxes[0]
        with console.status(f"waiting for {box.instance_id} to register with SSM..."):
            ssm_calls.wait_until_online(
                ec2_calls.client("ssm", cp.region), box.instance_id, timeout
            )
        with contextlib.ExitStack() as tunnels:
            current: dict[str, str] = {}

            def connect() -> None:
                # Closes the old tunnel (if any) and opens a new one. SSM ends a forward that
                # has carried no traffic for a while (20 minutes by default), which is exactly
                # what a quiet --watch looks like, so a dead tunnel must be re-openable.
                tunnels.close()
                port = tunnels.enter_context(
                    sync_calls.port_forward(aws_cli, box.instance_id, cp.region)
                )
                current["url"] = f"rsync://127.0.0.1:{port}/home/{target}/"

            def push(show_bar: bool) -> None:
                args = sync_calls.push_args(rsync, source, current["url"])
                if not show_bar:
                    sync_calls.run_rsync(args)
                    return
                with Progress(
                    TextColumn("{task.description}"), BarColumn(), TaskProgressColumn(),
                    console=console, transient=True,
                ) as bar:  # fmt: skip
                    task = bar.add_task(f"{source.name} -> /home/dev/{target}", total=100)
                    sync_calls.run_rsync(args, lambda pct: bar.update(task, completed=pct))

            def push_quietly() -> None:
                try:
                    push(show_bar=False)
                except PortalError:
                    console.print("connection to the box dropped; reconnecting", style="yellow")
                    connect()
                    push(show_bar=False)  # a second failure is real: let it surface

            connect()
            push(show_bar=True)
            console.print(f"synced {source} -> /home/dev/{target}")
            if watch:
                console.print("watching for changes; Ctrl+C to stop")
                try:
                    sync_calls.watch(
                        lambda: sync_calls.snapshot(rsync, source),
                        push_quietly,
                        lambda: console.print(f"synced {datetime.now().strftime('%H:%M:%S')}"),
                    )
                except KeyboardInterrupt:
                    console.print("stopped")


@app.command()
def doctor(
    local: Annotated[
        bool, typer.Option("--local", help="Only check this machine's tools and config; no AWS.")
    ] = False,
) -> None:
    """Check your setup and your deployment, read-only, and say what to fix.

    Runs the checks from the drills in docs/: no inbound rules, the reaper is armed and actually
    running, the box role is SSM-only, the budget exists, cost tags are active, your instance
    types are offered in your zones, you have spot quota. A check you lack permission for is
    skipped, not failed. Exits 1 if anything failed.
    """
    with _clean_errors():
        ctx = doctor_calls.Context(
            owner=getpass.getuser(),
            load_control_plane=_control_plane,
            region=_default_region() or "",
        )
        results = doctor_calls.run_checks(ctx, local_only=local)
    symbols = {
        doctor_calls.OK: "[green]✓[/]",
        doctor_calls.WARN: "[yellow]![/]",
        doctor_calls.FAIL: "[red]✗[/]",
        doctor_calls.SKIP: "[dim]-[/]",
    }
    table = Table(title="portal doctor", box=rich_box.SIMPLE_HEAD)
    for column in ("", "area", "check", "result"):
        table.add_column(column, overflow="fold")
    for check in results:
        table.add_row(symbols[check.status], check.group, check.name, escape(check.detail))
    console.print(table)
    problems = [c for c in results if c.status in (doctor_calls.WARN, doctor_calls.FAIL) and c.fix]
    for check in problems:
        console.print(f"{check.name}: {check.fix}", style="yellow", markup=False)
    counts = {s: sum(c.status == s for c in results) for s in symbols}
    console.print(
        f"{counts[doctor_calls.OK]} ok, {counts[doctor_calls.WARN]} warnings, "
        f"{counts[doctor_calls.FAIL]} failed, {counts[doctor_calls.SKIP]} skipped"
    )
    if counts[doctor_calls.FAIL]:
        raise typer.Exit(1)


@app.command()
def cost() -> None:
    """Show what portal-managed resources cost: today, this week, this month, per box.

    Reads AWS Cost Explorer (each request costs about $0.01). Data trails real usage by up
    to a day, and the portal:managed and portal:box tags must be activated as cost allocation
    tags in the Billing console first. Treat it as a trend; your AWS bill is the truth.
    Set inr_per_usd in your config to also see rupees.
    """
    with _clean_errors():
        rate = config.load().inr_per_usd
        today = datetime.now(UTC).date()  # Cost Explorer days are UTC days
        month_start, week_start, end = cost_calc.windows(today)
        rows = cost_calc.fetch(cost_calc.client(), min(month_start, week_start), end)
    summary = cost_calc.summarize(rows, today)

    def add(table: Table, label: str, usd: float | None) -> None:
        if usd is None:
            table.add_row(label, "not enough data", "-")
        else:
            table.add_row(label, *cost_calc.money(usd, rate))

    totals = Table(title="portal cost", box=rich_box.SIMPLE_HEAD)
    for column in ("", "USD", "INR"):
        totals.add_column(column, justify="left" if not column else "right")
    add(totals, "today", summary.today)
    add(totals, "this week", summary.week)
    add(totals, "this month", summary.month)
    add(totals, f"projected month ({summary.completed_days}-day run rate)", summary.projected_month)
    console.print(totals)

    if summary.per_box_month:
        boxes = Table(title="this month, per box", box=rich_box.SIMPLE_HEAD)
        for column in ("box", "USD", "INR"):
            boxes.add_column(column, justify="left" if column == "box" else "right")
        for label, usd in summary.per_box_month.items():
            boxes.add_row(escape(label), *cost_calc.money(usd, rate))
        console.print(boxes)
    elif summary.month == 0:
        console.print(
            "no cost data yet. New spend appears after up to a day, and the portal:managed "
            "and portal:box tags must be activated as cost allocation tags in the Billing "
            "console first.",
            style="yellow",
        )
    if rate is None:
        console.print("set inr_per_usd in your config to also see rupees")
    else:
        console.print(f"rupees at 1 USD = {rate:g} INR (your config)")


@app.command()
def extend(
    by: Annotated[
        str,
        typer.Argument(
            metavar="DURATION", help="How much longer the box may live, e.g. 2h or 30m."
        ),
    ],
    name: NameOption = None,
) -> None:
    """Push the box's expiry later by updating its portal:expires-at tag.

    The new expiry is the current one plus BY, and may never be more than max_ttl from now.
    A box that is already past its expiry cannot be extended: the reaper may be about to
    terminate it, so start a fresh one with `portal up` instead.
    """
    with _clean_errors(), contextlib.ExitStack() as stack:
        extra = parse_ttl(by)
        cfg = config.load()
        cp = _control_plane()
        ec2 = ec2_calls.client("ec2", cp.region)
        name = name or cfg.box_name
        stack.enter_context(box_lock(name))
        boxes = _my_boxes(ec2, getpass.getuser(), name)
        if not boxes:
            raise PortalError(
                f"No running box named '{name}', so there is nothing to extend. "
                "It may have expired or been shut down; start one with `portal up`."
            )
        box = boxes[0]
        now = ec2_calls.aws_now(ec2)
        current = status_calc.parse_rfc3339(box.expires_at)
        if current is None:
            # An unreadable tag leaves the reaper only its grace period; a good one repairs it.
            console.print(
                f"note: {box.instance_id} had an unreadable expiry ({box.expires_at!r}); "
                "counting from now",
                style="yellow",
                markup=False,
            )
            new_expiry = now + extra
        else:
            if current <= now:
                raise PortalError(
                    f"Box '{name}' passed its expiry {box.expires_at} and the reaper may "
                    "terminate it at any moment, so it cannot be extended. "
                    "Start a new one with `portal up`."
                )
            new_expiry = current + extra
        ceiling = parse_ttl(cfg.max_ttl)
        if new_expiry - now > ceiling:
            room = ceiling - (current - now) if current else ceiling
            allowed = (
                f"the most you can add now is {status_calc.format_duration(room)}"
                if room >= timedelta(minutes=1)
                else "it is already at the ceiling"
            )
            raise PortalError(
                f"Extending by {by} would leave the box more than max_ttl ({cfg.max_ttl}) on the "
                f"clock; {allowed}."
            )
        ec2_calls.set_expiry(ec2, box, new_expiry)
    left = status_calc.format_duration(new_expiry - now)
    console.print(
        f"{box.instance_id} now expires {ec2_calls.rfc3339(new_expiry)} ({left} from now)"
    )


def crash_log_path() -> Path:
    return Path(os.environ.get("PORTAL_HOME", Path.home() / ".portal")) / "last-crash.log"


def run() -> None:
    """The `portal` console script: the CLI, plus a last-resort handler for bugs.

    Every failure we expect is handled inside the commands. Anything that still escapes is a
    bug in portal, so it is written to a file (private, no environment variables, no locals)
    and the user gets one line saying where, instead of a screen of traceback.
    """
    try:
        app()
    except Exception as exc:
        if os.environ.get("PORTAL_DEBUG"):
            raise
        path = crash_log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(
                    f"portal {importlib.metadata.version('portal-dev')} {' '.join(sys.argv[1:])}\n"
                )
                f.write(traceback.format_exc())
            where = f"details are in {path}"
        except OSError:
            where = "re-run with PORTAL_DEBUG=1 to see the traceback"
        console.print(
            f"portal hit an unexpected error ({type(exc).__name__}: {exc}); {where}. "
            "Re-run with --debug for the traceback, and please report it.",
            style="red",
            markup=False,
        )
        raise SystemExit(2) from exc
