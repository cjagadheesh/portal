# ADR-005: A Packer AMI for the slow install, cloud-init for everything per-box

**Status:** Accepted, measurements pending
**Date:** 2026-10-21

## Context
A stock Ubuntu boot installs docker, uv, rsync and more over the network before the box is
useful. Everything else a box needs depends on the box (its home volume, user, agent, timezone).

## Decision
`packer/portal.pkr.hcl` bakes only the install step into an AMI in the user's own account.
Per-box setup stays in user data. `portal up` boots from the newest image whose name matches
`ami_name_prefix` and falls back to stock Ubuntu if there is none. Both paths run the same
script; a marker file skips the install on a baked image. The Packer build reaches its instance
through SSM (no inbound rule) and is tagged to expire, so the reaper cleans up a hung build.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| cloud-init only | Nothing to build or keep fresh | Slowest boot; depends on mirrors being up | The default today; a baseline to beat |
| Bake everything | Fastest boot | One image per config; the agent and home volume vary per box; stale agent code | Per-box state does not belong in an image |
| Container image on a stock AMI | Fast to rebuild | Still pays the docker pull; more moving parts on the box | No simpler |

## Measurements (not yet taken)
`portal ssh` prints "SSM online Ns after launch". Take it on both paths, several times each, and
record the medians in the README table. The target is under 90 seconds on the baked path.

## Consequences
- A weekly rebuild (`.github/workflows/ami.yml`) keeps security updates fresh; until it has
  run, boots are stock.
- AMIs and snapshots cost a few cents a month in storage.
- The image is account-specific; nothing about it is committed.
