# Architecture

## The core problem
A cloud dev box is only useful if it is *never* a billing risk. The CLI alone cannot
guarantee that: if the laptop crashes, loses wifi, or the user closes the lid on a
train, anything that depends on the CLI to clean up has already failed.

So the shutdown guarantee is layered, and **each layer is independent of the one
above it**:

| Layer | Runs where | Handles | Fails when |
|---|---|---|---|
| 1. CLI TTL | Laptop | Normal `portal down` | Laptop dies |
| 2. Agent idle-detect | On the box | User walks away | Box wedges / agent crashes |
| 3. Reaper Lambda | AWS, every 5 min | Everything else | AWS region outage |
| 4. AWS Budget alarm | AWS billing | Catastrophic misconfiguration | — |

Layer 3 is the one that matters. It trusts nothing: it scans for instances tagged
`portal:managed=true` whose `portal:expires-at` is in the past, and terminates them.
It does not read local state, does not talk to the CLI, and does not care why the
instance is still alive.

## Component diagram

```
LAPTOP
  portal CLI ──┬─ portal init   → terraform apply (control plane, once per account)
               ├─ portal up     → spot fleet request + attach home volume + tags
               ├─ portal ssh    → ssm start-session (no inbound ports)
               ├─ portal sync   → rsync over SSM port-forward
               ├─ portal cost   → Cost Explorer, filtered by portal:managed tag
               ├─ portal extend → update portal:expires-at tag
               └─ portal down   → terminate instance (home volume survives)
                      │
                      │ boto3
                      ▼
AWS ACCOUNT (ap-south-1)
  ┌─ Control plane (Terraform, created once by `portal init`) ────────────┐
  │   IAM role + instance profile  (AmazonSSMManagedInstanceCore only)    │
  │   Security group               (egress only — ZERO inbound rules)     │
  │   Reaper Lambda + EventBridge rule (rate: 5 minutes)                  │
  │   AWS Budget + SNS topic       (hard spend ceiling)                   │
  └───────────────────────────────────────────────────────────────────────┘

  ┌─ Per box (created by `portal up`) ────────────────────────────────────┐
  │   EC2 Spot instance   ── root volume (ephemeral, destroyed with box)  │
  │          │                                                            │
  │          └─ EBS "home" volume, /home/dev  ← SURVIVES the box          │
  │                                                                       │
  │   Tags: portal:managed=true  portal:owner=<user>                      │
  │         portal:box=<name>    portal:expires-at=<RFC3339>              │
  └───────────────────────────────────────────────────────────────────────┘

ON THE BOX
  portal-agent (systemd timer, every 60s)
    ├─ idle check: no active SSM session AND load < threshold for N minutes
    │              → shutdown -h now
    └─ spot watcher: IMDSv2 /latest/meta-data/spot/instance-action
                     → flush writes, unmount /home/dev, mark volume resumable
```

## Why the home volume is separate from root
Spot instances are cattle. Separating `/home/dev` onto its own EBS volume means an
interruption costs you ~90 seconds, not your work. `portal up --resume` reattaches
the existing volume to a fresh instance. The root volume is disposable by design.

## Why SSM instead of SSH
- No inbound security group rules, so the box is not reachable from the internet at all
- No key pairs to manage, lose, or commit
- Access is governed by IAM, and every session is logged in CloudTrail
- Port forwarding still works, which covers rsync and any local-port dev server

## Trade-offs recorded as ADRs
- [ADR-001](adr/0001-spot-over-on-demand.md): spot, on-demand only by consent (measurements pending)
- [ADR-002](adr/0002-four-layer-shutdown.md): why not just the CLI
- [ADR-003](adr/0003-ssm-over-ssh.md): SSM over SSH
- [ADR-004](adr/0004-separate-home-volume.md): separate home volume vs snapshots
- [ADR-005](adr/0005-baked-ami-vs-cloud-init.md): baked AMI vs cloud-init (measurements pending)
- [ADR-006](adr/0006-idle-detection.md): what counts as idle
- [ADR-007](adr/0007-reaper-semantics.md): missing tags, clock skew, partial failures
- [ADR-008](adr/0008-drain-order.md): order of the spot-interruption drain

## Reliability decisions (what happens when AWS misbehaves)
- **Retries and timeouts live in one place** (`portal/aws/session.py`): adaptive backoff for
  throttling, a 10 s connect and 60 s read timeout, and a `portal/<version>` user agent so CloudTrail
  shows what made a call.
- **Anything that creates something is idempotent.** `create_fleet`, `create_launch_template` and
  `create_volume` carry a `ClientToken`, because a response lost to a timeout followed by an
  automatic retry must not launch a second instance (the one failure that costs money silently).
- **Cleanup is best-effort and never masks success.** A launch template that cannot be deleted
  is free and tagged; a failed attach terminates the new box and deletes the empty volume it
  made; if even that fails, the user is told which box to remove.
- **Formatting is decided on the box, from the disk.** Only a device with no filesystem
  signature *and* an all-zero first MiB is formatted; a disk with data but no signature is
  refused. So an interrupted `up` cannot leave a volume that the next box refuses to mount.
- **Concurrent commands on one machine queue behind a per-box lock** (`portal/lock.py`), released
  by the OS if the process dies.
- **The test suite proves recoverability, not just success:** `tests/test_chaos.py` makes each AWS
  call fail once, in turn, and checks that nothing is left running and the next run works.
- **`portal doctor`** verifies the deployed safety design against the real account, read-only.
