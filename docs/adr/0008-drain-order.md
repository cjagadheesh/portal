# ADR-008: Spot interruption drain order

**Status:** Accepted
**Date:** 2026-10-21

## Context
AWS gives a spot instance about two minutes' notice before reclaiming it. The only thing
worth saving is `/home/dev`, which lives on its own EBS volume. The question is the order
of operations, because the obvious mistakes lose data: unmounting before flushing, or
flushing while something is still writing.

## Decision
Announce, stop containers, ask remaining processes to exit (SIGTERM), `sync`, `umount`
(SIGKILL stragglers and retry if busy), then tag the volume `portal:resumable=clean|dirty`.
The reason for each step's position is in the docstring of `agent/drain.py`; the short
version is **writers stop before the flush, the flush precedes the unmount, and the tag
comes last because it reports how the unmount went.**

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| Just `shutdown -h now` | One line | systemd's stop order and timeouts are not tuned for a 2-minute budget; no tag | Loses the guarantee |
| Detach the volume via the EC2 API | Cleanest detach | Needs `ec2:DetachVolume` on the box role, and a box that detaches its own disk is more privilege than the design should hand it | A clean unmount already makes the volume resumable |
| Snapshot on notice | Survives volume loss | Snapshots of a mounted volume are crash-consistent only, cost money, and two minutes is not enough for a large volume | The volume itself persists |

## Consequences
- The box gets exactly one extra IAM permission: `ec2:CreateTags` with the single key
  `portal:resumable`, only on volumes already tagged `portal:managed=true`
  (`infra/control-plane/box-policy.json.tftpl`, tested in `tests/test_drain.py`).
- A "dirty" tag means the volume was not cleanly unmounted. XFS replays its journal on the
  next mount, so it normally still works; `portal up --resume` warns.
- Failure of any step never stops the later ones, so a hung docker daemon cannot prevent
  the unmount.
- Unverified until a real interruption: that two minutes is enough for a large dev
  workload, and that `fuser`/`lsblk` behave as assumed on Ubuntu's NVMe-backed instances.
