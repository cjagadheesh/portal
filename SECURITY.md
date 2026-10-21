# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use GitHub's private vulnerability
reporting (the repository's *Security* tab, *Report a vulnerability*). Include what you found,
how to reproduce it, and which version. You will get an acknowledgement as soon as the maintainer
sees it; this is a one-person project, so please allow some days.

## Supported versions

Only the latest release receives fixes while the project is pre-1.0.

## What portal already does, and what it asks of you

- No inbound security group rules; access is SSM only. Anything that adds one is a bug.
- The reaper role can terminate only instances tagged `portal:managed=true`. **Tags are the trust
  root**: anyone who can tag instances can opt one into or out of reaping, so limit
  `ec2:CreateTags` accordingly. The AWS Budget alarm does not depend on tags.
- The box role is SSM plus one tag key on managed volumes. IMDSv2 is required.
- The rsync daemon on a box listens on loopback only and is reachable through an SSM session,
  which IAM controls. Any process on the box can also reach it; boxes are single-user.
- Local state under `~/.portal` is private (0700/0600) and contains resource ids, not secrets.
  The crash log contains a traceback and the command line, never the environment.
- Use the minimum IAM policy in `docs/iam-policy.json` rather than administrator access.
