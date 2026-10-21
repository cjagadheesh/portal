# ADR-004: A separate home volume instead of snapshots

**Status:** Accepted
**Date:** 2026-10-21

## Context
Spot instances can be reclaimed with two minutes' notice. Whatever holds the developer's work
must survive that, and `portal down`, without making the next boot slow.

## Decision
`/home/dev` lives on its own EBS volume, tagged like everything else, created on the first
`up` for a box name and reattached on every later one. The root volume is disposable. The
volume is pinned to one AZ, so a resumed box launches in the same AZ.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| Snapshot on shutdown, restore on boot | Survives an AZ outage; cheaper idle storage | Two minutes is too little to snapshot a large volume; restores are lazy-loaded and slow at first read | Fails the interruption window |
| Keep the root volume (stop instead of terminate) | Nothing to reattach | Spot "stop" needs persistent requests; pays for the whole root disk; stale OS state accumulates | Wrong cost and cleanliness |
| EFS | Cross-AZ, no attach step | Latency on many small files (git, node_modules); costs more per GB | Poor fit for developer workloads |
| Sync to S3 or git only | No volume to manage | Loses anything not pushed | Not a safety net |

## Consequences
- The box pins to the volume's AZ, which can reduce spot capacity choices; the fallback list
  covers other instance types within that AZ.
- A kept volume bills for storage while no box runs (`portal volumes` shows it and flags orphans).
- Deleting it takes a typed confirmation (`down --purge`).
- It is not a backup: an AZ failure or an accidental `rm` still loses data.
