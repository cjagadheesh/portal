"""Config: ~/.portal/config.yaml (you, your account) merged with .portal.yaml (this project).

Where things belong:
- Global, ~/.portal/config.yaml: region, timezone. They describe you and your AWS account,
  so every project should agree on them.
- Project, .portal.yaml (committed with the repo): instance_types, disk_gb, home_gb, default_ttl,
  idle_timeout_minutes, box_name. They describe what this codebase needs to build.

Precedence, lowest to highest: built-in defaults < global file < project file < CLI flags.
Both files accept every key, so a project can still override the region if it must.
"""

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from portal.errors import PortalError
from portal.ttl import parse_ttl

PROJECT_FILE = ".portal.yaml"
# Catch a typo here, in one sentence, instead of as an AWS error halfway through a launch.
REGION_FORMAT = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d")
INSTANCE_TYPE_FORMAT = re.compile(r"[a-z0-9-]+\.[a-z0-9-]+")


def check_region(region: str) -> str:
    if not REGION_FORMAT.fullmatch(region):
        raise PortalError(
            f"'{region}' does not look like an AWS region name (letters, a direction, a number)."
        )
    return region


def check_instance_type(instance_type: str) -> str:
    if not INSTANCE_TYPE_FORMAT.fullmatch(instance_type):
        raise PortalError(
            f"'{instance_type}' does not look like an instance type (e.g. c7g.xlarge)."
        )
    return instance_type


class Config(BaseModel):
    # A typo like `instance_type:` must fail loudly, not silently fall back to a default.
    model_config = ConfigDict(extra="forbid")

    region: str | None = None
    timezone: str = "UTC"
    # A list, in order of preference: spot capacity is per type per AZ and runs out, so one
    # type is a single point of failure. No default, because rule 3 forbids hardcoding one.
    instance_types: list[str] = Field(default_factory=list)
    disk_gb: int = Field(default=30, ge=8, le=1000)  # root volume; Ubuntu's 8 GB fills with docker
    home_gb: int = Field(default=20, ge=1, le=16384)  # /home/dev; size is fixed at creation
    # Used only to show what kept home volumes cost. No default: prices are never hardcoded
    # (rule 3). Look yours up on the EBS pricing page for your region.
    gp3_usd_per_gb_month: float | None = Field(default=None, gt=0)
    # Only for `portal cost`: INR per 1 USD. No default and no built-in fetch, because a rate
    # that is silently out of date is worse than none. The rate used is printed with the output.
    inr_per_usd: float | None = Field(default=None, gt=0)
    # Name prefix of the AMIs your own Packer build produces (packer/portal.pkr.hcl). If one
    # exists in your account `up` boots from it; otherwise from stock Ubuntu. "" disables.
    ami_name_prefix: str = "portal-ubuntu-2404-"
    # A brake on sprawl: refuse to start box number N+1. It exists because the point of this tool is
    # that you cannot run up a surprise bill, and ten forgotten boxes are a bill.
    max_running_boxes: int = Field(default=3, ge=1, le=50)
    default_ttl: str = "4h"
    # Hard ceiling for `up --ttl` and `extend`. It bounds the worst case of "forgot to run
    # down": a box can never be asked to live longer than this from now.
    max_ttl: str = "24h"
    idle_timeout_minutes: int = Field(default=30, ge=5)
    box_name: str = "dev"

    @field_validator("instance_types")
    @classmethod
    def _types_are_valid_and_distinct(cls, types: list[str]) -> list[str]:
        if len(set(types)) != len(types):
            raise ValueError("instance_types contains duplicates")
        for instance_type in types:
            try:
                check_instance_type(instance_type)
            except PortalError as e:
                raise ValueError(str(e)) from e
        return types

    @field_validator("region")
    @classmethod
    def _region_is_valid(cls, region: str | None) -> str | None:
        if region is None:
            return None
        try:
            return check_region(region)
        except PortalError as e:
            raise ValueError(str(e)) from e

    @field_validator("default_ttl", "max_ttl")
    @classmethod
    def _ttl_parses(cls, ttl: str) -> str:
        try:
            parse_ttl(ttl)
        except PortalError as e:
            raise ValueError(str(e)) from e
        return ttl

    @model_validator(mode="after")
    def _default_fits_under_max(self) -> "Config":
        if parse_ttl(self.default_ttl) > parse_ttl(self.max_ttl):
            raise ValueError(
                f"default_ttl {self.default_ttl} is longer than max_ttl {self.max_ttl}"
            )
        return self


def global_path() -> Path:
    return Path(os.environ.get("PORTAL_HOME", Path.home() / ".portal")) / "config.yaml"


def find_project_file(start: Path) -> Path | None:
    """Walk up from `start` like git does, so the config works from any subdirectory."""
    for directory in (start, *start.parents):
        candidate = directory / PROJECT_FILE
        if candidate.is_file():
            return candidate
    return None


def _read(path: Path) -> dict[str, Any]:
    """Validate one file on its own, so an error names the file it came from."""
    try:
        raw = yaml.safe_load(path.read_text()) or {}  # safe_load: config must never run code
    except yaml.YAMLError as e:
        raise PortalError(f"{path}: not valid YAML ({e})") from e
    if not isinstance(raw, dict):
        raise PortalError(f"{path}: expected key: value pairs at the top level")
    try:
        Config.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(
            (f"{'.'.join(map(str, err['loc']))}: " if err["loc"] else "")
            + str(err["msg"]).removeprefix("Value error, ")
            for err in e.errors()
        )
        raise PortalError(f"{path}: {problems}") from e
    return raw


def load(cwd: Path | None = None) -> Config:
    merged: dict[str, Any] = {}
    glob = global_path()
    if glob.is_file():
        merged.update(_read(glob))
    project = find_project_file(cwd or Path.cwd())
    if project is not None:
        merged.update(_read(project))
    return Config.model_validate(merged)
