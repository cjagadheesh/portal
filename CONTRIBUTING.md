# Contributing

Thanks for looking. This is a small project with strong opinions about safety, so read the
five rules first; they are checked by tests.

## The rules a change must not break
1. **No inbound security group rules.** Access is SSM only. If you think you need SSH
   ingress, you need a port forward.
2. **Every AWS resource portal creates carries the `portal:*` tags**, applied at creation.
3. **Nothing hardcoded**: no region, AZ, AMI id, instance type or price. Config or lookup.
4. **Destructive operations ask first** (`--yes` skips a y/n prompt, never a typed confirmation).
5. **Tests never touch real AWS.** Use `moto` or a local fake. A test that needs credentials
   is broken; the suite blocks outbound network access to make sure.

Also: every list/describe call goes through a paginator, and no secrets, account ids or
AMI ids are committed. `tests/test_rules.py` enforces these.

## Setup
```bash
uv sync
uv run ruff check && uv run ruff format --check .
uv run mypy --strict portal agent infra/reaper/handler.py
uv run pytest                  # offline
terraform -chdir=infra/control-plane init -backend=false && terraform -chdir=infra/control-plane validate
```
You need `rsync` installed for the real-rsync tests (they skip without it) and Terraform for
the Terraform checks. Python 3.11 is the floor; CI runs 3.11, 3.12 and 3.13.

## How we work
- **Explain the *why* in a comment** when a choice isn't obvious. Prefer three plain
  functions over a clever abstraction.
- **A new AWS call** needs: a `moto` test, a paginator if it lists, an entry in
  `docs/iam-policy.json` and in `API_ACTIONS` in `tests/test_rules.py`.
- **A new AWS resource** needs the tags from rule 2.
- **A design decision** gets an ADR in `docs/adr/` (copy `0000-template.md`). **Something that
  surprised you** gets an entry in `docs/what-broke.md`: what it looked like, what you
  thought, what it was, how you found it.
- Changing `portal/bootstrap.py` means regenerating `packer/provision.sh`:
  `uv run python -m portal.bootstrap > packer/provision.sh` (a test fails otherwise).
- Changes to the on-box agent (`agent/`) must stay standard-library only, and small: the
  code is shipped in the box's 16 KB user data.

## Be honest about what you tested
Offline tests prove logic, not deployment. If your change affects how things behave on a real
box or in a real account, say in the pull request that it is untested there, or run the
matching drill in `docs/safety-net-drill.md`.
