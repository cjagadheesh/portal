"""A minimal IAM evaluator for the policies this project ships (Allow statements only)."""

import fnmatch
from typing import Any


def allowed(
    policy: dict[str, Any],
    action: str,
    resource: str,
    resource_tags: dict[str, str] | None = None,
    request_tag_keys: list[str] | None = None,
) -> bool:
    tags = resource_tags or {}
    for stmt in policy["Statement"]:
        assert stmt["Effect"] == "Allow"
        actions = [stmt["Action"]] if isinstance(stmt["Action"], str) else stmt["Action"]
        resources = [stmt["Resource"]] if isinstance(stmt["Resource"], str) else stmt["Resource"]
        if not any(fnmatch.fnmatchcase(action, a) for a in actions):
            continue
        if not any(fnmatch.fnmatchcase(resource, r) for r in resources):
            continue
        if _conditions_hold(stmt.get("Condition", {}), tags, request_tag_keys):
            return True
    return False


def _conditions_hold(
    conditions: dict[str, Any], tags: dict[str, str], request_tag_keys: list[str] | None
) -> bool:
    for operator, pairs in conditions.items():
        for key, wanted in pairs.items():
            values = [wanted] if isinstance(wanted, str) else wanted
            if operator == "StringEquals" and key.startswith(
                ("aws:ResourceTag/", "ec2:ResourceTag/")
            ):
                if tags.get(key.split("/", 1)[1]) not in values:
                    return False
            elif operator == "ForAllValues:StringEquals" and key == "aws:TagKeys":
                if not request_tag_keys or not set(request_tag_keys) <= set(values):
                    return False  # IAM treats "no keys sent" as vacuously true; we are stricter
            else:
                raise AssertionError(f"evaluator does not support {operator} on {key}")
    return True
