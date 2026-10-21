# ADR-007: Reaper semantics (missing tags, skew, partial failure)

**Status:** Accepted
**Date:** 2026-10-21

## Context
The reaper (layer 3) is the safety net that works when the laptop and the on-box agent
have both failed. It must be predictable, and it must never terminate something that is
not a portal box.

## Decision
- **Scope:** only instances tagged `portal:managed=true`, in states pending, running,
  stopping or stopped. This is enforced three times: the EC2 filter, a re-check in code,
  and the IAM policy, which only allows `TerminateInstances` on instances carrying that tag.
- **Missing or unreadable `portal:expires-at`:** wait a grace period (default 60 minutes
  from launch), then terminate. Timestamps without a timezone count as unreadable.
- **Clock skew:** compare exactly against AWS time, with no margin, and have the CLI compute
  expiries from AWS time (the HTTP Date header of an EC2 response) rather than the laptop clock.
- **Partial failure:** terminate in batches; if a batch is rejected, retry instances one by
  one; raise at the end so the invocation shows as failed.
- **`REAPER_DRY_RUN`:** log decisions without terminating, for the first deploy.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| Terminate immediately when the tag is bad | Simplest, no leaks | A typo while editing a tag kills a box | Punishes a human error |
| Never terminate when the tag is bad | Safe for the user | A box with no end date is the leak this layer exists to stop | Defeats the purpose |
| Alert only | No surprise kills | Needs a human to act at 3 a.m. | Does not bound cost |
| Margin for clock skew | Absorbs small skew | Can only ever extend a box's life | The CLI uses AWS time, so there is no skew to absorb |

## Consequences
- Worst case for a bad tag is one extra hour of runtime.
- A box with a bad tag that is older than the grace period is terminated on the next run.
- The home volume survives every reaper termination; only the instance and root disk go.
- **Tags are the trust root.** Anyone who can tag instances can opt one into reaping (by adding
  `portal:managed=true` and a past expiry) or out of it (by pushing the expiry far away). Keep
  `ec2:CreateTags` for the portal tags limited to the people you trust with that, and keep the
  Budget alarm, which does not depend on tags, as the backstop.
- The IAM policy is the last line of defence: even a bug in the handler cannot terminate an
  untagged instance, because the role has no permission to. `tests/test_reaper_policy.py`
  evaluates the deployed policy file against tagged and untagged instances.
