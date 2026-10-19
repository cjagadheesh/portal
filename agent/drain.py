"""The drain: what the box does with the ~2 minutes between a spot interruption notice and
termination. The goal is that `/home/dev` is cleanly unmounted and the volume is marked
resumable, so `portal up --resume` can pick it straight up.

THE ORDER, AND WHY EACH STEP SITS WHERE IT DOES. Order matters here: unmounting before
flushing, or flushing while something is still writing, is how work gets lost.

  1. Announce. Log it and write to every attached terminal. It is cheap, and people need
     the longest possible warning, so it goes first. Also note which EBS volume backs
     /home/dev now, because after unmounting there is no mount left to ask.
  2. Stop containers (docker stop, 10 s grace each). A container with a bind mount into
     /home/dev is still writing; if we flushed first, its last writes would miss the flush.
     Stopping writers comes before flushing.
  3. Ask every process still using /home/dev to exit (SIGTERM), then wait a few seconds.
     Editors and build tools get one chance to save and close their files. SIGTERM, not
     SIGKILL, because a killed process cannot flush its own buffers.
  4. sync. Push everything the kernel is holding to the volume. umount flushes too, but if
     umount hangs on a busy filesystem we still want the data on disk before we try it.
  5. umount /home/dev. A cleanly unmounted XFS needs no log replay on the next attach. If it
     is busy: SIGKILL what is left (they had their chance in step 3), sync again, retry.
  6. Tag the volume portal:resumable=clean (or dirty if umount never succeeded). LAST, because
     the tag reports the outcome of step 5; tagging earlier could call a volume clean that
     then failed to unmount. If the tag cannot be written, nothing is lost: the data is
     already safe, so this step only logs.

Every step logs its result and a failing step never stops the ones after it: a hung docker
daemon must not stop the unmount.
"""

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent import expiry
from agent.imds import Imds

HOME = "/home/dev"
RESUMABLE_TAG = "portal:resumable"
BUDGET_S = 110  # AWS gives about 120 s; stop starting new optional work after this

Run = Callable[[list[str], float], tuple[int, str]]


def real_run(argv: list[str], timeout_s: float) -> tuple[int, str]:
    try:
        result = subprocess.run(  # noqa: S603 - argv built here, no shell
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""
    return result.returncode, result.stdout


@dataclass
class Context:
    log_file: Path
    run: Run
    announce: Callable[[str], int]
    tag_volume: Callable[[str, str], None]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    dry_run: bool = False

    @classmethod
    def real(cls, log_file: Path, dry_run: bool = False) -> "Context":
        return cls(log_file, real_run, expiry.broadcast, tag_with_boto3, dry_run=dry_run)


def tag_with_boto3(volume_id: str, state: str) -> None:
    import boto3  # installed from the python3-boto3 package; kept out of the other agent modules

    region = Imds().get("/latest/meta-data/placement/region")
    boto3.client("ec2", region_name=region).create_tags(
        Resources=[volume_id], Tags=[{"Key": RESUMABLE_TAG, "Value": state}]
    )


def log(ctx: Context, message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ctx.log_file.parent.mkdir(parents=True, exist_ok=True)
    with ctx.log_file.open("a") as f:
        f.write(f"{stamp} drain: {message}\n")


def volume_id_of_home(ctx: Context) -> str | None:
    """EBS volume ids are exposed as the NVMe serial number, with the dash removed."""
    code, source = ctx.run(["findmnt", "-no", "SOURCE", HOME], 5)
    device = source.strip()
    if code != 0 or not device:
        return None
    code, serial = ctx.run(["lsblk", "-dno", "SERIAL", device], 5)
    serial = serial.strip()
    return f"vol-{serial[3:]}" if code == 0 and serial.startswith("vol") else None


def step(ctx: Context, name: str, argv: list[str], timeout_s: float) -> tuple[int, str]:
    if ctx.dry_run:
        log(ctx, f"DRY RUN {name}: {' '.join(argv)}")
        return 0, ""
    code, out = ctx.run(argv, timeout_s)
    log(ctx, f"{name}: exit {code}")
    return code, out


def drain(ctx: Context) -> str:
    """Run the drain. Returns "clean" or "dirty" (the state written to the volume tag)."""
    started = ctx.clock()

    def out_of_time() -> bool:
        return ctx.clock() - started > BUDGET_S

    # 1. announce, and learn the volume id while /home/dev is still mounted
    log(ctx, "spot interruption notice received; draining")
    ctx.announce("portal: this spot box is being reclaimed in about 2 minutes. Saving /home/dev.")
    volume_id = volume_id_of_home(ctx)
    log(ctx, f"home volume: {volume_id or 'unknown'}")

    # 2. stop writers
    code, ids = step(ctx, "list containers", ["docker", "ps", "-q"], 10)
    if code == 0 and ids.split() and not out_of_time():
        step(ctx, "stop containers", ["docker", "stop", "-t", "10", *ids.split()], 40)

    # 3. let processes using the mount finish and close their files
    step(ctx, "ask processes to exit", ["fuser", "-k", "-TERM", "-m", HOME], 10)
    if not ctx.dry_run:
        ctx.sleep(5)

    # 4. flush
    step(ctx, "sync", ["sync"], 30)

    # 5. unmount, forcing stragglers off if needed
    code, _ = step(ctx, "umount", ["umount", HOME], 20)
    if code != 0 and not out_of_time():
        step(ctx, "kill remaining users", ["fuser", "-k", "-KILL", "-m", HOME], 10)
        if not ctx.dry_run:
            ctx.sleep(2)
        step(ctx, "sync again", ["sync"], 20)
        code, _ = step(ctx, "umount again", ["umount", HOME], 20)
    state = "clean" if code == 0 else "dirty"

    # 6. record the outcome, last
    if volume_id is None:
        log(ctx, "could not identify the home volume; not tagging")
    elif ctx.dry_run:
        log(ctx, f"DRY RUN would tag {volume_id} {RESUMABLE_TAG}={state}")
    else:
        try:
            ctx.tag_volume(volume_id, state)
            log(ctx, f"tagged {volume_id} {RESUMABLE_TAG}={state}")
        except Exception as e:  # nothing is lost if this fails: the data is already safe
            log(ctx, f"could not tag {volume_id}: {e}")
    log(ctx, f"finished ({state})")
    return state
