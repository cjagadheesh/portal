# Safety-net drill (Day 19)

Everything in `tests/test_safety_net.py` rehearses these four scenarios offline. This file
is the same drill for **real AWS**, to run once on your first deployment. The offline tests
cannot show that the Lambda is actually invoked, that its IAM role works, or that process
names on the box match what the agent looks for. Only this drill can.

Fill in the **Result** lines as you go, and copy anything that failed into
`docs/what-broke.md`. A drill where everything passed first time is worth being suspicious of.

## First: `portal doctor`
Run `portal doctor` before anything else. It checks, read-only, the parts of this drill that are
just facts about your account: the security group has zero inbound rules, the box role is
SSM-only, the reaper exists, is armed and has run in the last 15 minutes, the budget exists and
the cost tags are active. If it reports a failure, fix that first; the scenarios below test
behaviour, not setup.

## Before you start
1. `portal init --region <r> --budget-usd <small number> --reaper-dry-run`, so the reaper
   only logs for now.
2. Read the logs: `aws logs tail /aws/lambda/portal-reaper --since 15m`. You should see a
   `summary` line every five minutes. If not, stop: the schedule or the Lambda is broken.
3. Re-run `portal init` **without** `--reaper-dry-run` to arm it.
4. Use a short TTL (`--ttl 15m`) and a cheap instance type for every scenario below.

## Scenario 1: kill the CLI mid-launch
```bash
portal up --ttl 15m --name drill1 &      # note the PID
kill -9 %1                                # as soon as "launching as" appears
```
**Expect:** a running instance tagged with all four `portal:*` tags and no home volume
attachment (the offline test shows this window exists). Check the tags in the console.
Then wait for expiry + 5 minutes.
**Reaper should:** terminate it; look for `"action": "terminate"` in the logs.
**Also try Ctrl+C instead of `kill -9`:** the CLI should terminate the new box itself.
Result: ________

## Scenario 2: stop the agent on the box
```bash
portal up --ttl 15m --name drill2
portal ssh --name drill2
sudo systemctl stop portal-agent.timer     # and keep a session open, or be idle
```
**Expect:** the idle shutdown never happens (the agent is dead).
**Reaper should:** terminate it at expiry. Cost of this failure is bounded by the TTL.
Also check `portal agent-log` before stopping the timer: does it show one line a minute?
And while a session is open, does `pgrep -a ssm-session-worker` print a process? If it
prints something different, fix `SESSION_WORKER` in `agent/idle.py`.
Result: ________

## Scenario 3: expires-at in the past, set by hand
```bash
portal up --ttl 4h --name drill3
aws ec2 create-tags --resources <instance-id> \
  --tags Key=portal:expires-at,Value=2020-01-01T00:00:00Z
```
**Reaper should:** terminate it within 5 minutes.
**Then try bad values:** `Value=tomorrow` and a value without a timezone. These should
survive for the grace period (60 minutes by default) and then be terminated.
Result: ________

## Scenario 4: an instance launched by hand with the tags
```bash
aws ec2 run-instances --image-id <ami> --instance-type <type> --subnet-id <subnet> \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=portal:managed,Value=true},{Key=portal:expires-at,Value=<now+10min, RFC3339 UTC>}]'
```
**Reaper should:** terminate it at expiry. **And an instance with no tags at all must be left
alone**: that is correct behaviour, and it is the case the AWS Budget alarm is for.
Result: ________

## The IAM claim (Day 17): "the policy cannot terminate an untagged instance"
`tests/test_reaper_policy.py` checks the policy file. To check the *deployed* role with AWS's
own evaluator:
```bash
aws iam simulate-principal-policy \
  --policy-source-arn <reaper role arn> \
  --action-names ec2:TerminateInstances \
  --resource-arns <an untagged instance arn> \
  --context-entries ContextKeyName=aws:ResourceTag/portal:managed,ContextKeyType=string,ContextKeyValues=
```
Expect `implicitDeny`. Repeat with `ContextKeyValues=true` and expect `allowed`.
Result: ________

## Findings from the offline rehearsal (already fixed)
- Ctrl+C between launch and home-volume attach used to leave a running box with no home
  until its TTL. `portal up` now terminates it. A hard kill still can't be caught, which is
  scenario 1's reason to exist.
