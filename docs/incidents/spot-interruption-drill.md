# Spot interruption drill (Day 25)

**Status: not run yet.** This is the runbook and the postmortem skeleton. No real
interruption has happened, so nothing below is a result. After running it, replace the
placeholders, rename the file `2026-MM-DD-spot-interruption-drill.md`, and leave in
whatever failed: a postmortem where everything worked is not credible.

## What the offline tests already cover
`tests/test_drain.py` checks the drain's order (writers stop, then sync, then unmount, then
tag), the busy-unmount retry, the dirty path, and that one notice triggers one drain.
`tests/test_fallback_resume.py` checks `up --resume`. They cannot show that two minutes is
enough, that `fuser`, `lsblk` and `umount` behave as assumed on Nitro instances, or that the
notice really appears at the metadata path.

## Run it
1. A box with something to lose: `portal up --ttl 1h --name drill`, then `portal ssh` and
   write a file, start a container that writes into `/home/dev`, and leave a process with a
   file open in `/home/dev` (`tail -f /home/dev/x &`).
2. **Cheap rehearsal first** (no interruption needed):
   `sudo python3 -m agent.watcher --drain-now --dry-run` from `/opt/portal`, then read
   `portal agent-log`. Then the real drain on a throwaway box: drop `--dry-run`.
3. **Real interruption** with AWS Fault Injection Service: an experiment template using the
   action `aws:ec2:send-spot-instance-interruptions` against the instance (it needs an IAM
   role for FIS and `durationBeforeInterruption` of at least 2 minutes). Start the
   experiment and watch `portal agent-log -f`.
4. Fallback if FIS is too much setup: `aws ec2 terminate-instances` on the instance. This
   gives *no* IMDS notice, so it tests only what happens after a hard stop (is the volume
   recoverable?), not the watcher.
5. Afterwards: `aws ec2 describe-volumes` for the `portal:resumable` tag, then
   `portal up --resume --name drill` and check the file, the container's data and the TTL.

## Questions the drill must answer
- How many seconds passed between the notice and the unmount finishing?
- Was the volume tagged `clean`? If `dirty`, what held it open?
- Did `--resume` keep the original expiry and land on a different instance type first?
- Did the attached terminal show the warning? (It writes to `/dev/pts/*`.)

## Postmortem (fill in after the drill)
**Date / duration / impact:** ________
**Timeline (UTC):**
| Time | Event |
|---|---|
| | notice appeared in IMDS |
| | watcher logged it |
| | containers stopped |
| | unmount finished |
| | instance terminated |
**What worked:** ________
**What did not:** ________
**Root causes:** ________
**Action items:** ________
