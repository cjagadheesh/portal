# ADR-003: SSM Session Manager instead of SSH

**Status:** Accepted
**Date:** 2026-10-21

## Context
`portal ssh` needs a shell on a box that anyone on the internet could otherwise try to
reach. Rule 1 of the project is that the security group has no inbound rules at all.

## Decision
Boxes run the SSM agent (instance profile with `AmazonSSMManagedInstanceCore`) and the CLI
opens a Session Manager session by shelling out to `aws ssm start-session`. The agent
connects out to AWS, so nothing connects in. The CLI waits until the instance reports
`PingStatus=Online` before connecting, and starts the session as the `dev` user.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| SSH with a key pair | Familiar, works with any client | Needs port 22 open; keys to generate, lose and leak | Breaks rule 1 |
| SSH through EC2 Instance Connect | No long-lived keys | Still needs port 22 reachable | Breaks rule 1 |
| SSH tunnelled through SSM (ProxyCommand) | Normal `ssh`/`rsync` tooling | Still needs sshd and a key on the box | More moving parts than a plain SSM shell; kept as an option for `portal sync` |
| boto3 `start_session` only | No `aws` CLI dependency | boto3 only creates the session; the terminal protocol lives in the session-manager-plugin | Would mean reimplementing the plugin |

## Consequences
- No inbound rules, no key pairs, and every session is logged in CloudTrail under the
  caller's IAM identity.
- Access is governed by IAM, so revoking a user's access is one policy change.
- Users must install the AWS CLI and the Session Manager plugin; `portal ssh` checks for
  both and says what is missing.
- A new box takes about a minute to register with SSM, so `portal ssh` has to wait (with a
  timeout) instead of failing straight away. It prints how long registration took, which is
  the number to compare against the Packer AMI in ADR-005.
- The box needs outbound internet (public IP or NAT) to reach SSM; `portal up` only
  launches into subnets that assign public IPs.
