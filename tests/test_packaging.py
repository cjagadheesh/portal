import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from portal import terraform
from portal.cli import app

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text())


def fake_package(root: Path) -> Path:
    """A package directory shaped like an installed portal: infra_src/{control-plane,reaper}."""
    package = root / "site-packages" / "portal"
    (package / "infra_src" / "control-plane").mkdir(parents=True)
    (package / "infra_src" / "reaper").mkdir()
    (package / "infra_src" / "control-plane" / "main.tf").write_text("v1")
    (package / "infra_src" / "control-plane" / ".terraform.lock.hcl").write_text("lock-v1")
    (package / "infra_src" / "reaper" / "handler.py").write_text("code-v1")
    return package


def test_a_checkout_runs_terraform_in_place(tmp_path: Path) -> None:
    package = tmp_path / "repo" / "portal"
    package.mkdir(parents=True)
    got = terraform.resolve_control_plane_dir(package, tmp_path / "repo", tmp_path / "home")
    assert got == tmp_path / "repo" / "infra" / "control-plane"


def test_an_installed_package_runs_terraform_from_a_persistent_copy(tmp_path: Path) -> None:
    package, home = fake_package(tmp_path), tmp_path / "home"
    work = terraform.resolve_control_plane_dir(package, package.parent, home)
    assert work == home / "terraform" / "control-plane"
    assert (work / "main.tf").read_text() == "v1"
    # the module path `../reaper` still resolves from the copy
    assert (work.parent / "reaper" / "handler.py").read_text() == "code-v1"


def test_upgrading_replaces_the_code_but_never_the_state(tmp_path: Path) -> None:
    package, home = fake_package(tmp_path), tmp_path / "home"
    work = terraform.resolve_control_plane_dir(package, package.parent, home)
    (work / "terraform.tfstate").write_text("PRECIOUS STATE")
    (work / ".terraform").mkdir()
    (work / ".terraform" / "providers").write_text("cache")
    (work / ".terraform.lock.hcl").write_text("lock-edited-by-terraform")
    (package / "infra_src" / "control-plane" / "main.tf").write_text("v2")  # new release

    again = terraform.resolve_control_plane_dir(package, package.parent, home)
    assert (again / "main.tf").read_text() == "v2"
    assert (again / "terraform.tfstate").read_text() == "PRECIOUS STATE"
    assert (again / ".terraform" / "providers").read_text() == "cache"
    assert (again / ".terraform.lock.hcl").read_text() == "lock-edited-by-terraform"


def test_state_in_the_bundle_is_never_copied_over(tmp_path: Path) -> None:
    package = fake_package(tmp_path)
    (package / "infra_src" / "control-plane" / "terraform.tfstate").write_text("stray")
    work = terraform.resolve_control_plane_dir(package, package.parent, tmp_path / "home")
    assert not (work / "terraform.tfstate").exists()


def test_the_env_override_still_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PORTAL_CONTROL_PLANE_DIR", str(tmp_path / "custom"))
    assert terraform.control_plane_dir() == tmp_path / "custom"


def test_version_flag_matches_pyproject() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"portal {PYPROJECT['project']['version']}" in result.output


def test_wheel_is_configured_to_ship_what_a_pip_install_needs() -> None:
    wheel = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["portal"]
    assert wheel["force-include"] == {"agent": "portal/agent_src", "infra": "portal/infra_src"}
    project = PYPROJECT["project"]
    assert project["license"] == "MIT" and project["name"] == "portal-dev"
    assert project["scripts"] == {"portal": "portal.cli:run"}
    assert (REPO / "LICENSE").read_text().startswith("MIT License")
    assert (REPO / "CONTRIBUTING.md").exists()


def test_release_workflow_uses_trusted_publishing_not_stored_tokens() -> None:
    text = (REPO / ".github" / "workflows" / "release.yml").read_text()
    assert "id-token: write" in text and "gh-action-pypi-publish" in text
    assert "PYPI_TOKEN" not in text and "password:" not in text and "secrets." not in text
    assert "tags:" in text and "v*" in text
    assert "!= v$version" in text  # refuses a tag that disagrees with pyproject.toml


def test_the_changelog_has_a_section_for_the_current_version() -> None:
    changelog = (REPO / "CHANGELOG.md").read_text()
    assert f"## [{PYPROJECT['project']['version']}]" in changelog


def test_repository_hygiene_files_exist() -> None:
    for name in (
        "SECURITY.md",
        "CHANGELOG.md",
        ".pre-commit-config.yaml",
        ".github/dependabot.yml",
        ".github/pull_request_template.md",
        ".github/ISSUE_TEMPLATE/bug_report.md",
    ):
        assert (REPO / name).is_file(), name


def test_the_release_workflow_publishes_notes_from_the_changelog() -> None:
    text = (REPO / ".github/workflows/release.yml").read_text()
    assert "CHANGELOG.md" in text and "gh release create" in text
    assert "contents: write" in text


def test_packer_validation_messages_are_full_sentences() -> None:
    # `packer init` rejects a message that does not start with a capital letter and end with a
    # full stop; CI caught this once, and this keeps it from taking a push to find out again.
    import re

    root = Path(__file__).resolve().parent.parent / "packer"
    messages = re.findall(r'error_message\s*=\s*"([^"]*)"', (root / "portal.pkr.hcl").read_text())
    assert messages
    for message in messages:
        assert message[0].isupper() and message.endswith((".", "?")), message
