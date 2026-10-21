# Usage reference

Everything the `portal` commands do, in detail. The short version is in the [README](../README.md).

## Requirements

Linux and macOS (it uses file locks, rsync and a POSIX shell). On Windows, use WSL.

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- [Terraform](https://developer.hashicorp.com/terraform/install) ≥ 1.5 (for `portal init`)
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
  and the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
  (for `portal ssh` and `portal sync`)
- rsync 3.1+ (for `portal sync`)
- AWS credentials in your environment (`aws configure sso`, env vars or a profile)
- A VPC with a public subnet in the region. The default VPC works.

## Install (from source)

```bash
git clone <this repo> && cd portal
uv sync
uv run portal --help
```

Run it from the clone (or `uv tool install --editable .`): `portal init` applies the
Terraform in `infra/control-plane/`, and keeps Terraform's state file there.
**Don't delete Terraform's state file.** It's how `init` knows the control plane already exists.
From a git checkout it is `infra/control-plane/terraform.tfstate`; from a pip-installed portal it
is `~/.portal/terraform/control-plane/terraform.tfstate` (the bundled Terraform is copied there so
upgrades don't lose it). Only `init` needs it; every other command works from any machine.

## Usage

### `portal init` — once per AWS account

Creates the control plane with Terraform: an IAM role and instance profile that can only
talk to SSM, an egress-only security group, the reaper Lambda with its schedule and tag-scoped
IAM role, and a monthly AWS Budget with SNS alerts at 80%, 100% and forecast-100%.

```bash
portal init --region ap-south-1 --budget-usd 20 --alert-email you@example.com
portal init --region ap-south-1 --budget-usd 20 --dry-run          # show the plan only
portal init --region ap-south-1 --budget-usd 20 --reaper-dry-run   # reaper logs, kills nothing
```

A first `init` only adds resources. If a later run's plan would **destroy** any (replacing the
security group or a role that running boxes depend on), `init` shows the plan and asks, with
"no" as the default; `--yes` applies it without asking.

The budget covers the **whole account**, not just portal's resources, on purpose: it's
there for the case where tagging itself is broken. Running `init` again with no changes is
a no-op. Outputs are cached in `~/.portal/state.json`.

### `portal up` — launch a box

```bash
portal up                                   # everything from config
portal up --ttl 2h -t c7g.xlarge --name scratch
```

Every option overrides the matching [config](#configuration) key:

| Option | Config key | Meaning |
|---|---|---|
| `--ttl` | `default_ttl` | Lifetime: `4h`, `90m`, `1h30m`. Written to `portal:expires-at`. |
| `-t/--instance-type` (repeatable) | `instance_types` | Fallback list of instance types. All must share one CPU architecture. |
| `--name` | `box_name` | Box name. One running box per name. |
| `--timezone` | `timezone` | Timezone set on the box. |
| `--resume` | | Continue from the existing home volume with its original expiry. |

`up` requests one spot instance through an EC2 Fleet, trying your instance types **in the
order you listed them** (your first choice wins whenever it has room, and each attempt is
printed). Within a type it spreads over every allowed subnet with the `capacity-optimized`
strategy. On first boot, cloud-init installs docker, git, uv, ripgrep and fzf (unless a baked
image is used, see below), mounts the home volume at `/home/dev`, and creates the `dev` user.
The log is at `/var/log/portal-bootstrap.log`.

**When no spot type has capacity**, `up` asks before starting anything on-demand. The prompt
shows the on-demand price (looked up from the AWS Price List API, never hardcoded) and, if you
set `inr_per_usd`, warns when it is above ₹10/hour. The default answer is no, and there is no
flag to auto-approve it. On-demand boxes are marked in `portal status`.

**`--resume`** continues a box whose home volume already exists, typically after a spot
interruption: same name, same home, and the *original* expiry (it can't be combined with
`--ttl`; use `portal extend` for more time). It tries your other instance types before the one
that was just reclaimed, and mentions it if the last unmount was not clean.

**The home volume.** The first `up` for a box name creates an EBS volume (`home_gb`, default
20 GB) in whatever AZ the box landed in, and formats it XFS. Every later `up` with the same
name reattaches that volume, so the new box is pinned to the volume's AZ, because EBS
volumes can't cross AZs. If the volume can't be attached, `up` terminates the new box
rather than let you work on a disk that will be deleted.

### `portal ssh` — get a shell

```bash
portal ssh [--name dev] [--timeout 180]
```

Waits until the box has registered with SSM (and prints how many seconds after launch that
took), then opens a Session Manager shell as the
`dev` user. If bootstrap hasn't finished yet you land as `ssm-user`, and a message says so.

### `portal status` — what's running

A table of your boxes: instance, type, AZ, state, uptime, current spot price, estimated
cost so far, and TTL left (amber under 30 min, red under 10). The cost is an estimate:
current spot price × uptime.

`status` reads AWS tags every time, so it works from a second laptop or after
`~/.portal/state.json` is deleted. The control plane is rediscovered the same way, by its
tags, so a second laptop needs only AWS credentials and a region (`PORTAL_REGION` or
`region:` in config), not Terraform or the original clone.

### `portal sync` — push a folder to the box

```bash
portal sync [--src .] [--dest NAME] [--name dev] [--watch]
```

Pushes a local folder to `/home/dev/<dest>` (default: the folder's own name) with rsync.
It's one-way and never deletes files that exist only on the box. Files matched by `.gitignore`
or `.portalignore` (same syntax, in any subfolder) are skipped. With `--watch` it keeps
running and pushes again within about a second of any change, and a changed file inside an
ignored folder is ignored too.

How it works without opening a port: the box runs an rsync daemon bound to `127.0.0.1` only.
`sync` opens an SSM port-forward from a free local port to it, points rsync at that port,
and closes the tunnel when done. IAM decides who can open the forward, and there are no SSH
keys. Anything running on the box can also talk to that loopback daemon, which is fine for a
single-user box. Boxes launched before this feature have no daemon; `down` and `up` again.

Needs rsync 3.1+ locally (on macOS: `brew install rsync`) plus the same AWS CLI and
Session Manager plugin as `ssh`. Change detection in `--watch` compares local file names,
sizes and modification times, so it needs no extra dependency. SSM closes a port-forward that has
carried no traffic for a while (20 minutes by default), which is what a quiet `--watch` looks
like, so a push that finds the tunnel dead reconnects once and retries; a second failure is
reported.

### Faster boots: the Packer image

A stock boot installs docker, uv, rsync and friends from the internet, which is most of the
time before you can connect. `packer/portal.pkr.hcl` bakes exactly that step into an AMI in
**your** account; `portal up` then boots from the newest image whose name starts with
`ami_name_prefix` (default `portal-ubuntu-2404-`) and falls back to stock Ubuntu if none
exists. Per-box setup (home volume, `dev` user, agent, rsync) still runs at boot from user
data, so one image serves every box.

```bash
packer init packer/
packer build -var region=... -var arch=arm64 -var instance_type=... \
  -var subnet_id=... -var security_group_id=... -var instance_profile=... packer/
```

The security group and instance profile are the control plane's, so the build has no inbound
rules either: Packer talks to the build instance through SSM (needs the Session Manager
plugin). The build instance is tagged to expire in an hour, so the reaper cleans up a hung
build. `.github/workflows/ami.yml` rebuilds it every Monday using OIDC (no stored AWS keys).
`packer/provision.sh` is generated from the same code that renders the boot script
(`uv run python -m portal.bootstrap > packer/provision.sh`) and a test fails if they drift.

| Boot path | Launch → SSM online |
|---|---|
| Stock Ubuntu + install at boot | _not measured yet_ |
| Baked image | _not measured yet_ |

Fill the table from the "SSM online Ns after launch" line `portal ssh` prints. Aim: under 90 s.

### Spot interruptions

If AWS reclaims the box, a watcher on it (polling the metadata service every 5 seconds) runs a
**drain** within the two-minute warning: tell attached terminals, stop containers, ask
processes using `/home/dev` to exit, `sync`, unmount, and tag the volume
`portal:resumable=clean` (or `dirty`). The order matters and is explained in
[ADR-008](adr/0008-drain-order.md). Your work is on the home volume, so
`portal up` (or `portal up --resume`) on a new box brings it straight back. To rehearse the
drain without an interruption: `sudo python3 -m agent.watcher --dry-run` logs what it would
do; see [docs/incidents/spot-interruption-drill.md](incidents/spot-interruption-drill.md).

The box's role can add exactly one tag key (`portal:resumable`) to volumes already tagged
`portal:managed=true`, and nothing else beyond SSM.

### `portal cost` — what it costs

Shows today, this week, this month and a projected month for everything tagged
`portal:managed=true`, plus a per-box breakdown. The projection is a run rate: total of the
finished days this month ÷ finished days × days in the month. Add `inr_per_usd: 83.5` (your
own number; portal doesn't guess exchange rates) to see rupees next to dollars, with the rate
printed under the table.

Read this as a trend, not a meter: Cost Explorer lags real usage by up to a day, each request
costs about $0.01, and `portal:managed` and `portal:box` must be activated as **cost
allocation tags** in the Billing console before they appear (and only count from then on).
Control-plane resources (the reaper, budget) show up as "(control plane)".

### `portal extend` — more time

```bash
portal extend 2h [--name dev]
```

Adds time to the box's `portal:expires-at` (and its disks' tags). It adds to the *current*
expiry and never lets the box end up more than `max_ttl` (default 24h, from config) from now;
the error says how much you can still add. A box that is already past its expiry can't be
extended, because the reaper may be terminating it right now: start a new one with
`portal up`. Expiry times come from AWS's clock, not your laptop's, so a wrong laptop clock
can't make a box live longer or shorter than you asked.

Sessions attached to the box get a message in their terminal at 30, 10 and 5 minutes
before the expiry. After `portal extend` the warnings re-arm. The box reads its own expiry
tag from its metadata service, so this needs no extra AWS permission.

### Idle shutdown and `portal agent-log`

Every box runs `portal-agent`, a systemd timer that checks once a minute and powers the box
off (which terminates it) after `idle_timeout_minutes` (default 30) of inactivity. The home
volume survives. The box counts as active while any of these is true:

- an SSM session is open (a shell, or `portal sync --watch`)
- the 5-minute load average is at least 0.5
- a `docker build` is running
- `~/.portal-keepalive` was touched, which restarts the idle clock from that moment

Merely running containers, detached `tmux` sessions and `nohup` jobs do **not** count: a
forgotten database would otherwise keep the box alive and billing forever. **The accepted
failure mode:** a job that is detached, isn't a docker build and mostly waits (a long test
suite blocked on the network) looks idle and is shut down after the timeout. Run
`touch ~/.portal-keepalive` before walking away from one. Full reasoning is in
[ADR-006](adr/0006-idle-detection.md).

```bash
portal agent-log [--lines 50] [--follow]
```

Shows the agent's log from the box: one line a minute saying why it is active, how long it
has been quiet, or why it shut down. The agent code is packed into the box's user data
(`portal/bootstrap.py`), so a box always runs the agent version that launched it.

### `portal doctor` — check a deployment

```bash
portal doctor            # everything
portal doctor --local    # only this machine's tools and config; no AWS calls
```

Read-only. It checks what the drills in this repo ask a human to verify:

| Area | Checks |
|---|---|
| tools | `aws`, the Session Manager plugin, rsync 3.1+, terraform |
| config | your config files parse and make sense |
| account | credentials work; the control plane is found |
| safety | the security group has **zero** inbound rules; the box role is SSM-only; the reaper exists, is armed (not dry-run), is scheduled, and has actually run in the last 15 minutes; the budget exists |
| billing | `portal:managed` and `portal:box` are active cost allocation tags |
| launch | public subnets exist; your instance types are offered in their zones; a baked image or the stock one resolves; your spot vCPU quota fits your largest type |
| housekeeping | your clock vs AWS's; home volumes sitting unused |

A check you lack permission for is **skipped**, not failed (the extra read permissions are in
[iam-policy-doctor.json](iam-policy-doctor.json)). Failures and warnings print what to do about
them, and the exit code is 1 if anything failed, so it works in a script.

### `portal down` — terminate

```bash
portal down [--name dev] [--yes]
portal down --name dev --purge     # also delete /home/dev forever
```

The root disk is deleted. The `/home/dev` volume is detached and kept for the next
`portal up --name <same name>`. An unattached gp3 volume still costs storage, about
$0.08–0.10 per GB-month depending on region.

`--purge` also deletes the home volume. It works even when no box is running, and it
always makes you type the box name: `--yes` doesn't skip that step, so a deletion can't come
from one keystroke or a flag left in a script.

### `portal volumes` — what's being kept

Lists your home volumes with size, AZ, state, age and monthly cost. A volume whose box isn't
running is marked **orphaned**, and `portal up` warns about orphans for other box names.
Monthly cost is shown once you set `gp3_usd_per_gb_month` in your config. portal never
hardcodes prices.

## Configuration

Two optional YAML files. Settings apply in this order, later ones winning: built-in defaults,
then `~/.portal/config.yaml` (you and your account), then `.portal.yaml` (the project, found
by walking up from the current directory like git does), then CLI flags.

```yaml
# ~/.portal/config.yaml
region: ap-south-1
timezone: Asia/Kolkata
```

```yaml
# .portal.yaml — commit it with your repo
instance_types: [c7g.xlarge, m7g.xlarge, c7g.2xlarge]   # ordered fallbacks, one CPU architecture
disk_gb: 30            # root volume size, deleted with the box (default 30)
home_gb: 20            # /home/dev volume size, kept across boxes (default 20; set at creation)
gp3_usd_per_gb_month: 0.0912   # optional; your region's EBS gp3 price, for `portal volumes`
inr_per_usd: 83.5      # optional; only for `portal cost` (you choose the rate)
max_running_boxes: 3   # `up` refuses to start box number 4 (default 3)
default_ttl: 4h        # used when `portal up` gets no --ttl (default 4h)
ami_name_prefix: portal-ubuntu-2404-   # your Packer images; "" to always use stock Ubuntu
max_ttl: 24h           # hard ceiling for --ttl and `extend` (default 24h)
idle_timeout_minutes: 30
box_name: dev
```

`instance_types` is a list because spot capacity runs out per type per AZ, so a single type
is a single point of failure. It has no default: portal never guesses an instance type for
you. Unknown keys are errors, so a typo like `instance_type:` can't silently fall back to a
default.

| Env var | Meaning |
|---|---|
| `PORTAL_REGION` | Overrides `region`. If it disagrees with the control plane's region, commands stop before calling AWS. |
| `PORTAL_TIMEZONE` | Overrides `timezone` for `portal up`. |
| `PORTAL_HOME` | State and global config directory (default `~/.portal`). |

## AWS permissions used

The calls `portal` makes after `init`, for writing your own IAM policy. A minimal policy
file is planned for day 30.

- EC2: `DescribeInstances`, `DescribeInstanceTypes`, `DescribeSecurityGroups`,
  `DescribeSubnets`, `DescribeSpotPriceHistory`, `CreateLaunchTemplate`,
  `DeleteLaunchTemplate`, `CreateFleet`, `RunInstances`, `CreateTags`, `TerminateInstances`,
  `DescribeImages`, `DescribeVolumes`, `CreateVolume`, `AttachVolume`, `DeleteVolume`,
  `DescribeAvailabilityZones` (read AWS's clock); `Describe*` of images is also used to find
  your baked AMI
- IAM: `PassRole` on the `portal-box` role, `GetInstanceProfile` (to rediscover the control plane)
- Cost Explorer: `ce:GetCostAndUsage` (for `portal cost`)
- Pricing: `pricing:GetProducts` (only for the on-demand price shown in the fallback prompt)
- SSM: `GetParameter` (AMI lookup), `DescribeInstanceInformation`, `StartSession` (the shell
  and the rsync port-forward)

`portal init` additionally needs permission to create the IAM role and instance profile,
the security group, the SNS topic and the Budget.

When AWS refuses a call, `portal` prints the exact IAM action and resource ARN it was
denied, never a Python traceback.

## Development

```bash
uv run ruff check                  # lint
uv run ruff format .               # format
uv run mypy --strict portal agent  # types
uv run pytest                      # all tests; offline, uses moto, needs no AWS account
uv run pytest tests/test_boxes.py::test_up_launches_instance_with_all_four_tags
terraform -chdir=infra/control-plane validate
```

Tests never touch real AWS. `moto` doesn't support SSM's reserved `/aws/*` parameters or
`DescribeInstanceInformation`, so tests stub the AMI lookup and the "SSM online" check.

```
portal/          CLI (typer): cli.py has one function per command
  aws/ec2.py     launch, find-by-tag, terminate, spot price
  aws/home.py    the /home/dev EBS volume: find, create, attach
  aws/ssm.py     AMI lookup, wait for SSM registration
  bootstrap.py   cloud-init user data
  config.py      ~/.portal/config.yaml + .portal.yaml (pydantic)
  state.py       ~/.portal/state.json cache + reconcile() from AWS tags
  status.py      TTL / uptime / cost maths
  sync.py        rsync push, SSM port-forward, --watch loop
  errors.py      boto3 and terraform errors → one actionable line
infra/control-plane/   Terraform: IAM, security group, budget (and the reaper module)
infra/reaper/    the reaper Lambda: handler.py and its Terraform
packer/          the AMI build
agent/           runs ON the box (stdlib only): idle rule (idle.py), systemd tick (run.py)
docs/            architecture, build plan, ADRs, incident write-ups
tests/
```

Design decisions are recorded in [docs/adr/](adr/), and things that broke along the
way in [docs/what-broke.md](what-broke.md).

## Troubleshooting

- **Something unexpected.** Anything portal did not anticipate is written to
  `~/.portal/last-crash.log` (private; it holds the traceback and the command line, never your
  environment) and you get one line saying so. Re-run with `--debug` (or `PORTAL_DEBUG=1`) to see
  the traceback instead. Please report it.
- **"Another portal command is already working on box ..."**: two commands on one machine would
  race on the same box, so the second waits its turn. The lock is released by the OS if the
  first one dies; there is nothing to clean up.
- **"You already have N running boxes"**: `max_running_boxes` (default 3) is a brake against
  forgotten boxes. Take one down, or raise the limit.
- **Throttling.** Every AWS call retries with backoff (8 retries) and has a connect/read timeout,
  and every call that creates something carries an idempotency token, so a retried request can
  never launch a second instance.
