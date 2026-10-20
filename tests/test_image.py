import re
from pathlib import Path
from typing import Any

import pytest

from portal import bootstrap
from portal.aws import ec2 as ec2_calls
from portal.cli import app
from tests.test_boxes import aws, runner  # noqa: F401  (aws is a fixture)

REPO = Path(__file__).resolve().parent.parent
PACKER = REPO / "packer" / "portal.pkr.hcl"


def test_committed_provision_script_matches_the_generator() -> None:
    # Regenerate with: uv run python -m portal.bootstrap > packer/provision.sh
    assert (REPO / "packer" / "provision.sh").read_text() == bootstrap.render_image()


def test_the_image_bakes_only_the_slow_install_step() -> None:
    image = bootstrap.render_image()
    for needle in ("apt", "docker.io", "uv/install.sh", "image.done", "systemctl enable docker"):
        assert needle in image
    # Per-box things must stay at boot, or one image could not serve every box.
    for needle in ("/home/dev", "useradd", "rsyncd", "portal-agent", "mkfs"):
        assert needle not in image


def test_boot_skips_the_install_only_on_a_baked_image() -> None:
    boot = bootstrap.render("UTC")
    assert "if [ ! -f /var/lib/portal/image.done ]; then" in boot
    assert "baked image: skipping package install" in boot
    # the same install text appears in both scripts, so they cannot drift apart
    install_lines = [ln.strip() for ln in bootstrap._INSTALL.splitlines() if ln.strip()]
    assert all(line in boot and line in bootstrap.render_image() for line in install_lines)


def test_packer_template_keeps_the_hard_rules() -> None:
    text = "\n".join(
        ln for ln in PACKER.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    assert 'ssh_interface = "session_manager"' in text  # rule 1: no inbound SSH
    assert "security_group_id           = var.security_group_id" in text
    assert "ingress" not in text and "temporary_security_group_source_cidrs" not in text
    assert not re.search(r"\bami-[0-9a-f]{8}", text), "AMI ids must be looked up, not written down"
    assert not re.search(r"\b\d{12}\b", text), "no account numbers in the repo"
    assert "amazon-parameterstore" in text and 'http_tokens                 = "required"' in text
    assert '"portal:managed" = "true"' in text and '"portal:expires-at"' in text  # rule 2


def test_the_ami_workflow_has_no_secrets_and_no_long_lived_keys() -> None:
    text = (REPO / ".github" / "workflows" / "ami.yml").read_text()
    assert "id-token: write" in text and "role-to-assume" in text
    assert "AWS_ACCESS_KEY_ID" not in text and "secrets." not in text
    assert "diff - packer/provision.sh" in text  # CI also catches a stale provision.sh


# --- choosing the image at `up` ------------------------------------------------------------


def make_image(ec2: Any, name: str) -> str:
    inst = ec2.run_instances(
        ImageId="ami-12345678", MinCount=1, MaxCount=1, InstanceType="t3.micro"
    )
    return str(
        ec2.create_image(InstanceId=inst["Instances"][0]["InstanceId"], Name=name)["ImageId"]
    )


def test_find_baked_ami_picks_the_newest_matching_prefix(aws: Any) -> None:  # noqa: F811
    make_image(aws, "other-thing-1")
    old = make_image(aws, "portal-ubuntu-2404-amd64-20300101000000")
    new = make_image(aws, "portal-ubuntu-2404-amd64-20300202000000")
    found = ec2_calls.find_baked_ami(aws, "portal-ubuntu-2404-", "amd64")
    assert found in (old, new)
    images = {i["ImageId"]: i for i in aws.describe_images(Owners=["self"])["Images"]}
    assert images[found]["CreationDate"] == max(images[i]["CreationDate"] for i in (old, new))


def test_no_baked_image_and_an_empty_prefix_both_mean_stock(aws: Any) -> None:  # noqa: F811
    assert ec2_calls.find_baked_ami(aws, "portal-ubuntu-2404-", "amd64") is None
    make_image(aws, "portal-ubuntu-2404-amd64-1")
    assert ec2_calls.find_baked_ami(aws, "", "amd64") is None  # prefix "" turns the feature off
    assert ec2_calls.find_baked_ami(aws, "portal-ubuntu-2404-", "arm64") is None  # wrong arch


def test_up_boots_from_the_baked_image_when_one_exists(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baked = make_image(aws, "portal-ubuntu-2404-amd64-20300101000000")
    monkeypatch.chdir(tmp_path)
    seen: list[dict[str, Any]] = []
    real = aws.create_launch_template

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(aws, "create_launch_template", spy)
    monkeypatch.setattr("portal.aws.ec2.client", lambda service, region: aws)
    result = runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro", "--name", "img"])
    assert result.exit_code == 0, result.output
    assert seen[0]["LaunchTemplateData"]["ImageId"] == baked
    assert "baked portal image" in result.output


def test_up_falls_back_to_stock_ubuntu_and_says_how_to_speed_it_up(aws: Any) -> None:  # noqa: F811
    result = runner.invoke(app, ["up", "--ttl", "1h", "-t", "t3.micro", "--name", "stock"])
    assert result.exit_code == 0, result.output
    assert "stock Ubuntu" in result.output and "Packer" in result.output
