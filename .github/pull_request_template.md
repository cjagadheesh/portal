## What and why

## Checklist
- [ ] `uv run ruff check && uv run mypy --strict portal agent infra/reaper/handler.py && uv run pytest` pass
- [ ] New AWS calls: a test, a paginator if they list, and an entry in `docs/iam-policy.json`
- [ ] New AWS resources carry the `portal:*` tags, applied at creation
- [ ] Nothing hardcoded (region, AMI, instance type, price) and no inbound rules
- [ ] I said what I could **not** test offline (anything that needs a real account)
