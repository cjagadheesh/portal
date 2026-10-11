"""Translate tool failures into messages a human can act on. No raw tracebacks reach the user."""

import re

from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    NoRegionError,
    ProfileNotFound,
)

# Terraform surfaces the AWS error text verbatim, e.g.
# "User: arn:aws:iam::123:user/x is not authorized to perform: iam:CreateRole on resource: arn:..."
_DENIED = re.compile(
    r"not authorized to perform: (?P<action>[\w:*]+)(?: on resource: (?P<arn>\S+))?"
)


class PortalError(Exception):
    """An expected failure; the CLI prints the message and exits non-zero."""


def _denied(action: str, arn: str) -> str:
    return (
        f"AccessDenied: your AWS identity lacks IAM action {action} on {arn}. Grant it and re-run."
    )


_CAPACITY_CODES = {
    "InsufficientInstanceCapacity",
    "InsufficientCapacity",
    "SpotMaxPriceTooLow",
    "Unsupported",
}
# These ids only exist in one region, so "not found" for a thing we just read from state
# almost always means the CLI is pointed at a different region than the control plane.
_WRONG_REGION_CODES = {
    "InvalidGroup.NotFound",
    "InvalidSecurityGroupID.NotFound",
    "InvalidVpcID.NotFound",
    "InvalidSubnetID.NotFound",
    "InvalidAMIID.NotFound",
}
_EXPIRED_TOKEN_CODES = {"ExpiredToken", "ExpiredTokenException", "RequestExpired"}


def capacity_advice(instance_types: list[str] | None, message: str = "") -> str:
    """Name the next fallback type if the list has one; otherwise say how to add one."""
    types = instance_types or []
    failed = next((t for t in types if t in message), types[0] if types else None)
    if failed is not None and types.index(failed) + 1 < len(types):
        nxt = types[types.index(failed) + 1]
        return f"Next fallback to try: {nxt} (re-run with -t {nxt} first)."
    tried = ", ".join(types) if types else "none given"
    return (
        f"No fallback left (tried: {tried}). Add another with --instance-type; "
        "a different size or family has its own spot capacity pool."
    )


def translate_boto_error(exc: Exception, instance_types: list[str] | None = None) -> str:
    """Turn a boto3 failure into one actionable line; the CLI never shows a traceback."""
    if isinstance(exc, NoCredentialsError):
        return "No AWS credentials found. Configure them (e.g. `aws configure sso`) and retry."
    if isinstance(exc, ProfileNotFound):
        return f"AWS profile not found: {exc}. Check AWS_PROFILE or ~/.aws/config."
    if isinstance(exc, NoRegionError):
        return "No AWS region configured. Set PORTAL_REGION or run `portal init --region ...`."
    if isinstance(exc, EndpointConnectionError):
        return (
            f"Could not reach AWS ({exc.kwargs.get('endpoint_url', 'endpoint')}). "
            "Check your network and that the region name is valid."
        )
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", "")
        if code.startswith("AccessDenied") or code in (
            "UnauthorizedOperation",
            "UnauthorizedAccess",
        ):
            match = _DENIED.search(message)
            # If AWS does not name the action, the API operation is the best clue we have.
            action = match.group("action") if match else f"(operation {exc.operation_name})"
            arn = (match.group("arn") if match else None) or "(resource not reported)"
            return _denied(action, arn)
        if code == "MaxSpotInstanceCountExceeded":
            # A quota, not a capacity pool: switching instance type will not help.
            return (
                f"{code}: your account's spot vCPU quota is used up. Terminate other spot "
                "instances or request a quota increase in Service Quotas."
            )
        if code in _CAPACITY_CODES:
            return f"{code}: {message} {capacity_advice(instance_types, message)}"
        if code in ("InvalidInstanceId", "TargetNotConnected"):
            return (
                "The box's SSM agent is not online yet (a new box takes ~1 minute to register). "
                "Wait a bit and retry `portal ssh`."
            )
        if code in _WRONG_REGION_CODES:
            return (
                f"{code}: {message} This usually means a region mismatch: the control plane "
                "was created in a different region than the one in use. Check PORTAL_REGION "
                "and ~/.portal/state.json."
            )
        if code in _EXPIRED_TOKEN_CODES or code in ("AuthFailure", "InvalidClientTokenId"):
            return (
                f"{code}: your AWS credentials are expired or invalid. "
                "Log in again (e.g. `aws sso login`) and retry."
            )
        return f"AWS error {code} during {exc.operation_name}: {message}"
    return f"AWS call failed: {exc}"


def translate_terraform_error(stderr: str) -> str:
    """Turn terraform's stderr into one actionable line (AccessDenied gets the exact IAM action)."""
    match = _DENIED.search(stderr)
    if match:
        return _denied(match.group("action"), match.group("arn") or "(resource not reported)")
    if "AccessDenied" in stderr or "UnauthorizedOperation" in stderr:
        return "AccessDenied: AWS refused a call but did not name the action; see terraform output."
    if "No valid credential sources" in stderr or "NoCredentialProviders" in stderr:
        return "No AWS credentials found. Configure them (e.g. `aws configure sso`) and retry."
    if any(
        marker in stderr
        for marker in (
            "InvalidClientTokenId",
            "ExpiredToken",
            "SignatureDoesNotMatch",
            "AuthFailure",
        )
    ):
        return (
            "AWS rejected your credentials (invalid or expired). "
            "Log in again (e.g. `aws sso login`) and retry."
        )
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    # Terraform prints "Error: <what happened>" first and a source-code excerpt last, so the
    # last line is usually useless. Prefer the error line.
    errors = [line for line in lines if line.startswith("Error:")]
    chosen = errors[0] if errors else (lines[-1] if lines else "no output")
    return f"terraform failed: {chosen.removeprefix('Error:').strip()}"
