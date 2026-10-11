from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from portal import config
from portal.cli import app
from portal.errors import PortalError
from tests.test_boxes import aws, runner  # noqa: F401  (aws is a fixture)


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


def test_defaults_without_any_file(home: Path) -> None:
    cfg = config.load(home)
    assert cfg.instance_types == []  # rule 3: no hardcoded instance type
    assert cfg.default_ttl == "4h"
    assert cfg.box_name == "dev"


def test_project_overrides_global_and_is_found_from_a_subdirectory(home: Path) -> None:
    (home / "home" / "config.yaml").write_text("region: eu-west-1\ndefault_ttl: 2h\n")
    project = home / "repo"
    (project / "src" / "deep").mkdir(parents=True)
    (project / ".portal.yaml").write_text("default_ttl: 6h\ninstance_types: [a.large, b.large]\n")
    cfg = config.load(project / "src" / "deep")
    assert cfg.region == "eu-west-1"  # from global
    assert cfg.default_ttl == "6h"  # project wins
    assert cfg.instance_types == ["a.large", "b.large"]  # order preserved


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("instance_type: a.large\n", "instance_type"),  # typo must not be ignored
        ("default_ttl: forever\n", "default_ttl"),
        ("instance_types: [a.large, a.large]\n", "duplicates"),
        ("disk_gb: 2\n", "disk_gb"),
        ("- just\n- a list\n", "key: value"),
        ("region: [unclosed\n", "not valid YAML"),
    ],
)
def test_bad_config_names_the_file_and_the_problem(home: Path, text: str, needle: str) -> None:
    (home / ".portal.yaml").write_text(text)
    with pytest.raises(PortalError) as err:
        config.load(home)
    assert ".portal.yaml" in str(err.value)
    assert needle in str(err.value)


def test_up_uses_config_types_and_disk(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".portal.yaml").write_text(
        "instance_types: [t3.small]\ndisk_gb: 40\nbox_name: cfgbox\n"
    )
    monkeypatch.chdir(tmp_path)
    # moto ignores a launch template's disk size, so check what we asked EC2 for instead.
    templates: list[dict[str, Any]] = []
    real_create = aws.create_launch_template

    def spy(**kwargs: Any) -> Any:
        templates.append(kwargs)
        return real_create(**kwargs)

    monkeypatch.setattr(aws, "create_launch_template", spy)
    monkeypatch.setattr(
        "portal.aws.ec2.client", lambda service, region: aws if service == "ec2" else None
    )
    result = runner.invoke(app, ["up"])
    assert result.exit_code == 0, result.output
    (inst,) = [i for r in aws.describe_instances()["Reservations"] for i in r["Instances"]]
    assert inst["InstanceType"] == "t3.small"
    tags = {t["Key"]: t["Value"] for t in inst["Tags"]}
    assert tags["portal:box"] == "cfgbox"
    (mapping,) = templates[0]["LaunchTemplateData"]["BlockDeviceMappings"]
    assert mapping["Ebs"]["VolumeSize"] == 40
    assert mapping["Ebs"]["DeleteOnTermination"] is True


def test_flags_beat_config(aws: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # noqa: F811
    (tmp_path / ".portal.yaml").write_text("instance_types: [t3.small]\n")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["up", "-t", "t3.micro"]).exit_code == 0
    (inst,) = [i for r in aws.describe_instances()["Reservations"] for i in r["Instances"]]
    assert inst["InstanceType"] == "t3.micro"


def test_up_without_any_instance_type_explains_where_to_set_it(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["up"])
    assert result.exit_code == 1
    assert "instance_types" in result.output


def test_the_built_in_defaults_are_the_documented_ones() -> None:
    cfg = config.Config()
    assert (cfg.disk_gb, cfg.home_gb, cfg.max_running_boxes, cfg.idle_timeout_minutes) == (
        30,
        20,
        3,
        30,
    )


@pytest.mark.parametrize(
    ("field", "low", "high"),
    [
        ("disk_gb", 8, 1000),
        ("home_gb", 1, 16384),
        ("max_running_boxes", 1, 50),
        ("idle_timeout_minutes", 5, None),  # no upper limit
    ],
)
def test_numeric_limits_are_inclusive_at_both_ends(field: str, low: int, high: int | None) -> None:
    config.Config(**{field: low})
    with pytest.raises(ValidationError):
        config.Config(**{field: low - 1})
    if high is not None:
        config.Config(**{field: high})
        with pytest.raises(ValidationError):
            config.Config(**{field: high + 1})


def test_default_ttl_may_equal_max_ttl_but_not_exceed_it() -> None:
    config.Config(default_ttl="6h", max_ttl="6h")
    with pytest.raises(ValidationError, match="longer than max_ttl"):
        config.Config(default_ttl="6h", max_ttl="5h59m")
