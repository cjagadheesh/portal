# ADR-001: Spot instances, with on-demand only by explicit consent

**Status:** Accepted, measurements pending
**Date:** 2026-10-21

## Context
A dev box that is idle most of the day is the textbook case for cheap, interruptible capacity.
Spot typically costs a fraction of on-demand but can be reclaimed with two minutes' notice.

## Decision
Boxes are spot. Instance types are tried in the user's order. If no type has capacity, `up`
shows the on-demand price and asks for a typed yes; it never falls back silently. Interruptions
are made cheap by the separate home volume (ADR-004) and the drain handler (ADR-008).

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| On-demand by default | Never interrupted | Several times the price, which is the thing this tool is about | Defeats the purpose |
| Silent on-demand fallback | `up` always works | A surprise bill is exactly what must not happen | Breaks the contract |
| Spot with a max price | Caps the price | EC2 now caps spot at the on-demand price anyway; low caps only add interruptions | No benefit |

## Measurements (not yet taken)
This ADR is supposed to quote a measured interruption rate. None exists yet because nothing has
run on real AWS. To fill it in: run a box for a week, count interruption notices in
`portal agent-log`, and compare the price in `portal status` to the on-demand price the fallback
prompt shows. Until then the decision rests on AWS's published guidance, not on data.

## Consequences
- Interruptions happen. The cost is a reboot and a resume, not lost work.
- Capacity can be unavailable in a chosen AZ; hence ordered fallbacks and the explicit prompt.
