from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from typer.testing import CliRunner

from portal.aws import ec2 as ec2_calls
from portal.cli import app
from portal.errors import translate_boto_error
from tests.test_boxes import aws  # noqa: F401  (fixture)

runner = CliRunner()


def client_error(code: str, message: str = "msg", op: str = "RunInstances") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, op)


def test_access_denied_names_action_and_arn() -> None:
    msg = translate_boto_error(
        client_error(
            "UnauthorizedOperation",
            "You are not authorized to perform: ec2:CreateFleet on resource: "
            "arn:aws:ec2:us-east-1:000000000000:fleet/f-1",
        )
    )
    assert "ec2:CreateFleet" in msg
    assert "arn:aws:ec2:us-east-1:000000000000:fleet/f-1" in msg


def test_access_denied_without_action_falls_back_to_operation() -> None:
    msg = translate_boto_error(client_error("AccessDenied", "nope", op="DescribeInstances"))
    assert "DescribeInstances" in msg


def test_capacity_error_suggests_next_fallback() -> None:
    msg = translate_boto_error(
        client_error("InsufficientInstanceCapacity", "no capacity for c-a"), ["c-a", "c-b"]
    )
    assert "c-b" in msg


def test_capacity_error_with_no_fallback_left() -> None:
    msg = translate_boto_error(client_error("InsufficientInstanceCapacity"), ["only-one"])
    assert "No fallback left" in msg
    assert "--instance-type" in msg


def test_no_credentials_ssm_not_online_region_and_network() -> None:
    assert "credentials" in translate_boto_error(NoCredentialsError())
    assert "SSM agent is not online" in translate_boto_error(client_error("InvalidInstanceId"))
    assert "region mismatch" in translate_boto_error(client_error("InvalidGroup.NotFound"))
    assert "expired" in translate_boto_error(client_error("ExpiredToken"))
    err = EndpointConnectionError(endpoint_url="https://ec2.nowhere.amazonaws.com")
    assert "Could not reach AWS" in translate_boto_error(err)


def test_unknown_error_is_still_one_line() -> None:
    msg = translate_boto_error(client_error("SomethingNew", "weird"))
    assert "SomethingNew" in msg
    assert "\n" not in msg


def test_region_mismatch_is_caught_before_any_call(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PORTAL_REGION", "eu-west-1")
    result = runner.invoke(app, ["down", "--yes"])
    assert result.exit_code == 1
    assert "Region mismatch" in result.output


def test_cli_never_shows_a_traceback_on_aws_failure(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise client_error(
            "UnauthorizedOperation", "not authorized to perform: ec2:DescribeInstances"
        )

    monkeypatch.setattr(ec2_calls, "find_boxes", boom)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "ec2:DescribeInstances" in result.output
    assert "Traceback" not in result.output


def test_spot_quota_is_not_reported_as_capacity() -> None:
    from botocore.exceptions import ClientError

    from portal.errors import translate_boto_error

    err = ClientError(
        {"Error": {"Code": "MaxSpotInstanceCountExceeded", "Message": "quota"}}, "CreateFleet"
    )
    msg = translate_boto_error(err, ["a.large", "b.large"])
    assert "quota" in msg
    assert "fallback" not in msg.lower()
