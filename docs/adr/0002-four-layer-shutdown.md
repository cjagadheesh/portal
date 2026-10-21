# ADR-002: Layered shutdown instead of trusting the CLI

**Status:** Accepted
**Date:** 2026-10-21

## Context
The point of the tool is that a forgotten box cannot become a surprise bill. A cleanup that
depends on the CLI staying alive fails exactly when it is needed: the laptop dies, loses wifi
or the lid is closed on a train.

## Decision
Four layers, each independent of the ones above it: the CLI's TTL and `portal down`; an agent
on the box that powers it off when idle; a Lambda every five minutes that terminates anything
tagged `portal:managed=true` past its `portal:expires-at`; and an AWS Budget alarm. The Lambda
reads only tags, so it needs no local state and no cooperation from the other layers.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| CLI timer only | Trivial | Dies with the laptop | The failure it must survive |
| `at`/cron job on the box | Survives the laptop | Dies with the box or a wedged OS | Not independent of the thing it guards |
| EC2 stop-protection/instance scheduler | Managed | Per-instance config, no notion of per-box TTL, extendable only by API calls we would write anyway | Does not fit a tag-driven TTL |
| Reaper only | Simplest robust design | Five-minute granularity; no idle detection, no warning of the user | Layers 1 and 2 give a better experience when they work |

## Consequences
- Worst-case waste from a dead laptop and a dead agent is the TTL plus five minutes.
- An untagged instance is invisible to layers 1 to 3 by design; only the Budget alarm sees it.
- More moving parts to test; hence the offline rehearsal of every layer and the real-AWS drill.
