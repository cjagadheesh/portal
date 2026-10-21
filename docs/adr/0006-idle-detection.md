# ADR-006: What counts as "idle" on the box

**Status:** Accepted
**Date:** 2026-10-21

## Context
Layer 2 of the shutdown design is an agent on the box that powers it off when you walk
away. It runs every 60 seconds. Two errors are possible: shutting down a box that is doing
work (a 40-minute build killed halfway), or never shutting down (a silent hourly cost).
The reaper and the TTL already bound the second error, so this rule leans toward keeping
the box alive when unsure, but not so far that a forgotten box never stops.

## Decision
The idle clock restarts whenever any of these is true; the box shuts down after
`idle_timeout_minutes` (default 30) with none of them:
1. an `ssm-session-worker` process exists (a shell or `portal sync --watch` is connected),
2. the 5-minute load average is at least 0.5 (absolute, not per core),
3. a `docker build`, `buildx build` or `compose build` client is running,
4. `~/.portal-keepalive` was touched, which restarts the clock from that moment.

Not counted: merely running containers, detached tmux/screen/nohup processes, network
traffic, file changes, keystrokes. Code and the full reasoning: `agent/idle.py`.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| Load average only | Simple | Misses builds waiting on the network; a quiet open shell looks idle | Kills active work |
| Any process owned by `dev` | Never kills a job | A forgotten tmux or database container keeps the box up forever | Defeats the purpose |
| Count running containers | Protects `docker compose up` stacks | Idle databases keep the box alive | Same |
| Network or file activity | Catches "something is happening" | Background updaters and package timers fake it | Unreliable signal |

## Consequences
- **Accepted failure mode:** a job detached from every session, not a docker build, and
  mostly waiting (load under 0.5) is shut down after the timeout. The home volume
  survives; running processes and the root disk do not. The `.portal-keepalive` file is
  the escape hatch.
- **Opposite failure:** an SSM session left open keeps the box up until its TTL; the
  reaper is the backstop.
- Unverified until a real box exists: that the SSM agent's session processes are named
  `ssm-session-worker` on Ubuntu's snap-installed agent. Check with
  `pgrep -a ssm-session-worker` during a session; if it differs, fix `SESSION_WORKER`.
