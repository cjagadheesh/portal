# What broke

Same-day entries only. The "how I found it" line is what interviewers care about.

## Template
**Day:**
**Symptom:**
**First hypothesis:**
**Actual cause:**
**How I found it:**
**Time lost:**

## Day 8 — status table truncated in an 80-column terminal
**Day:** 8
**Symptom:** `portal status` showed `t3.mi…` / `runni…`; a test looking for the instance type failed.
**First hypothesis:** the value was wrong.
**Actual cause:** nine columns do not fit 80 columns; rich ellipsises cells by default.
**How I found it:** printed the captured output. Fixed with `overflow="fold"` and no vertical borders.
**Time lost:** ~10 min

## Day 7 — boot time not recorded
**Day:** 7
The bootstrap script logs `portal bootstrap finished in Ns` to `/var/log/portal-bootstrap.log`.
No real instance was launched (tests use moto only), so the number to compare against the
week-6 Packer AMI still has to be read from a real box: `portal ssh`, then
`tail /var/log/portal-bootstrap.log`.

---
Entries below were found by reading the code and the plan's "Done when" lines before any
real box existed (everything so far is tested against moto only), so there is no real
first hypothesis or time lost.

## `portal ssh` landed as the wrong user
**Day:** 6 (found in the week 2 review)
**Symptom:** none yet; the session would start as `ssm-user`, which has no `/home/dev` and is not in the docker group.
**Actual cause:** `aws ssm start-session` opens SSM's default user, not the `dev` user bootstrap creates.
**How I found it:** reading what the Day 7 "docker works as `dev`" check would exercise.
**Fix:** start the session with `AWS-StartInteractiveCommand` running `sudo -iu dev`.

## Bootstrap could fail on the apt lock
**Day:** 7 (found in the week 2 review)
**Symptom:** none yet; on first boot cloud-init and unattended-upgrades also hold the apt lock.
**Actual cause:** `apt-get` fails immediately when the lock is taken, and the script runs under `set -e`.
**How I found it:** reading the script against how Ubuntu's first boot behaves.
**Fix:** `-o DPkg::Lock::Timeout=300`. The uv installer also no longer relies on `$HOME`.

## A spot quota error was reported as lack of capacity
**Day:** 10 (found in the week 2 review)
**Cause:** `MaxSpotInstanceCountExceeded` sat in the capacity-error set, so the user was told to try the next instance type, which cannot help.

## Rebuilding the cache could pick the wrong region
**Day:** 9 (found in the week 2 review)
**Cause:** with `state.json` gone, the region fell back to the shell's default region. The control plane is now rediscovered by tag (see `portal/aws/control_plane.py`), which also works on a second laptop that has no Terraform state.

## Tag values were interpreted as rich markup
**Day:** 8 (found in the week 2 review)
**Cause:** `portal status` printed the `portal:box` tag straight into a rich table, so a name like `[/x]` could break it. Tags are plain strings anyone with EC2 access can edit.

## Ctrl+C during `portal up` left a billing box
**Day:** 19 (found while writing the offline drill, not on a real box)
**Symptom:** none yet; the test for "kill the CLI mid-launch" showed a running instance with no home volume.
**Actual cause:** the clean-up after a failed home-volume attach caught AWS errors but not `KeyboardInterrupt`, and Ctrl+C lands in exactly that window.
**How I found it:** writing the scenario as a test and asking what state the world is in afterwards.
**Fix:** also catch `KeyboardInterrupt` there. A SIGKILL still can't be handled by the CLI, which is what the reaper is for.

## `portal init` reported a line of source code as the error
**Day:** 30 (found by installing the built wheel in a clean venv and running `init` with junk credentials)
**Symptom:** `terraform failed:   12: provider "aws" {`. Not an error, a code excerpt.
**First hypothesis:** Terraform had failed to parse its own file.
**Actual cause:** Terraform prints `Error: <message>` first and a source excerpt last; the translator printed the last line. The real message was `InvalidClientTokenId`.
**How I found it:** running the installed package the way a stranger would, with fake credentials, instead of only the test suite. The earlier test fed it an input where the last line happened to be the message.
**Fix:** prefer the `Error:` line, and recognise bad/expired credentials.

---
## Full-project audit (day 30+)
Found by reading every module, running the real CLI against a local moto server over HTTP, and
measuring coverage. None was seen on a real AWS account. Each has a regression test in
`tests/test_audit_fixes.py` or `tests/test_box_side_paths.py`.

- **`sync --watch` would die after SSM's idle timeout.** SSM closes a port-forward with no traffic
  (20 minutes by default), which is exactly what a quiet watch looks like. Now a push that finds the
  tunnel dead reconnects once and retries.
- **`init` applied destructive plans without asking** (rule 4). A later `init` that replaced the
  security group or a role would have done so silently. Now it shows the plan and asks, default no;
  `--yes` skips the question.
- **A spot walk gave up on "not offered in this AZ".** Errors like `Unsupported` mean "try the next
  type", not "stop". Permission and quota errors still stop the walk.
- **The network interface was created untagged** (rule 2). It is now in the launch template's tag
  specifications with the instance and the root volume.
- **Plan and error text went through rich markup**, so `["a", "b"]` or `[sender]` in Terraform's
  output could be swallowed. Printed verbatim now.
- **A state file written by another version raised a pydantic traceback.** It is ignored and rebuilt.
- **Filesystem errors (unwritable `~/.portal`) were tracebacks.** One clear line now.
- **Missing Terraform outputs raised `KeyError`.** Reported as a half-applied control plane.
- **`status` showed a spot price for on-demand boxes**, a number that looks right and is wrong.
- **Repeated `-t` values reached `describe_instance_types` twice.** De-duplicated, order kept.
- **If the clean-up after a failed attach also failed**, the user was told nothing about the box
  still running. They are now told which box to remove.
- **A reaper test depended on how long the suite took** (a clock fixed at import time against an
  instance's real launch time). It failed in a slow 10-minute coverage run. Now uses a fresh clock.
- **The rsync daemon did DNS lookups** (failed offline) and **its test fixture assumed a non-root
  user**; both fixed.

## Found by mutation testing and a full parallel run (offline, not on a real box)

- **User data was not reproducible.** The agent tarball claimed a fixed mtime, but Python's
  `tarfile` "w:gz" stamps the current time into the gzip header, so two launches of identical code
  produced different user data. Harmless to a box, but it made a test flaky and defeated the point
  of the fixed mtime. The gzip layer is built by hand with `mtime=0` now.
- **Boundaries were untested.** Deliberately breaking comparisons, constants and operators showed
  tests that passed with `>` for `>=` in the reaper's expiry and grace checks, with a wrong default
  grace period, batch size and page size, and with wrong time limits in the spot-interruption drain.
  Each now has a test that pins the exact edge. Survivors left on purpose: default and sleep
  timeouts in `sync.py` that only change how long a failure takes to show.
- **Four tests had drifted from the region check** (`x-1`, `test-region-1` are not region names), and
  one asserted on text that rich wraps when the temp path is long. Fixed in the tests, not the code.
- **The `packer` CI job failed on the first push.** `packer init` rejects a variable validation
  message that is not a full sentence (`arch must be arm64 or amd64.` starts in lower case). Found by
  GitHub Actions, reproduced locally with Packer 1.16.1, fixed, and a test now checks the wording. The
  Packer template had never been parsed by Packer before that, since it needs no AWS account to do so.
