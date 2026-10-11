"""One place that builds every boto3 client: the same retry and timeout behaviour everywhere."""

import importlib.metadata
from typing import Any

import boto3
from botocore.config import Config


def _version() -> str:
    try:
        return importlib.metadata.version("portal-dev")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def config() -> Config:
    return Config(
        # "adaptive" backs off when AWS throttles us and slows down further if it keeps doing
        # so; eight attempts rides out a burst of RequestLimitExceeded instead of failing a
        # `portal up` halfway.
        retries={"mode": "adaptive", "max_attempts": 8},
        # Fail a dead connection in 10 s instead of hanging; 60 s covers AWS's slowest calls.
        connect_timeout=10,
        read_timeout=60,
        # Shows up in CloudTrail, so "what created this?" has an answer.
        user_agent_extra=f"portal/{_version()}",
    )


def client(service: str, region: str) -> Any:
    return boto3.client(service, region_name=region, config=config())
