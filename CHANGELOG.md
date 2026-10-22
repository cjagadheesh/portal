# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

First release. **Built and tested offline only; it has not run against a real AWS account.**
Run `portal doctor` and the drills in `docs/` on your first deployment.

### Added
- `portal init`: the control plane with Terraform (egress-only security group, SSM-only instance
  role, reaper Lambda, AWS Budget); asks before applying a plan that destroys anything.
- `portal up`: tagged spot launch with an ordered instance-type fallback, an explicit prompt before
  any on-demand fallback, a persistent `/home/dev` EBS volume, `--resume`, a baked-image fast path.
- `portal ssh`, `portal sync [--watch]`, `portal agent-log`: SSM Session Manager and an
  SSM port-forward to a loopback-only rsync daemon. No inbound ports, no SSH keys.
- `portal status`, `portal cost`, `portal volumes`, `portal extend`, `portal down [--purge]`.
- `portal doctor`: read-only checks of a real deployment.
- Four independent shutdown layers: TTL, on-box idle agent, tag-scoped reaper Lambda, budget alarm.
- Spot interruption watcher and ordered drain of `/home/dev`.
- Packer image build over SSM; weekly rebuild workflow.

### Reliability and safety
- `portal sync --watch` reports a clear error instead of a traceback if rsync disappears mid-session.
- Every creating call carries an idempotency token; all clients retry with backoff and have
  timeouts; cleanup is best-effort and never turns a successful launch into an error.
- Per-box file lock, a running-box limit, input validation, private state files, and a crash
  handler that writes details to a private file instead of printing locals.
- Fault-injection tests make every AWS call fail once and check that nothing is left running.
- Core logic is mutation-tested (the reaper, expiry, idle and drain rules, cost, config limits);
  the agent archive in user data is byte-for-byte reproducible.
