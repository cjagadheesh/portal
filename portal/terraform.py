"""Thin wrapper around the terraform binary for the control plane."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from portal.errors import PortalError, translate_terraform_error


def resolve_control_plane_dir(package_dir: Path, repo_root: Path, home: Path) -> Path:
    """Where terraform runs, and therefore where its state file lives.

    From a git checkout that is infra/control-plane in place (state is gitignored there).
    From an installed package the bundled Terraform is read-only and thrown away on upgrade,
    so it is copied to ~/.portal/terraform and run there: state survives upgrades. Re-copying
    on every run keeps the code current while leaving state, provider cache and lock file alone.
    """
    bundled = package_dir / "infra_src"
    if not bundled.is_dir():
        return repo_root / "infra" / "control-plane"
    work = home / "terraform"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(
        bundled,
        work,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".terraform", "*.tfstate", "*.tfstate.backup", "build", ".terraform.lock.hcl"
        ),
    )
    lock = work / "control-plane" / ".terraform.lock.hcl"
    if not lock.exists() and (bundled / "control-plane" / ".terraform.lock.hcl").exists():
        shutil.copy(bundled / "control-plane" / ".terraform.lock.hcl", lock)
    return work / "control-plane"


def control_plane_dir() -> Path:
    override = os.environ.get("PORTAL_CONTROL_PLANE_DIR")
    if override:
        return Path(override)
    package_dir = Path(__file__).resolve().parent
    home = Path(os.environ.get("PORTAL_HOME", Path.home() / ".portal"))
    return resolve_control_plane_dir(package_dir, package_dir.parent, home)


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run terraform in the control-plane dir. Tests monkeypatch this; they never call AWS."""
    binary = shutil.which("terraform")
    if binary is None:
        raise PortalError("terraform not found on PATH. Install it from developer.hashicorp.com.")
    return subprocess.run(  # noqa: S603 - fixed binary, args built by us, no shell
        [binary, f"-chdir={control_plane_dir()}", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _ok(result: subprocess.CompletedProcess[str], ok_codes: tuple[int, ...] = (0,)) -> str:
    if result.returncode not in ok_codes:
        raise PortalError(translate_terraform_error(result.stderr))
    return result.stdout


def _var_args(variables: dict[str, str]) -> list[str]:
    return [f"-var={k}={v}" for k, v in variables.items()]


def init() -> None:
    _ok(run(["init", "-input=false", "-no-color"]))


def plan(variables: dict[str, str]) -> tuple[bool, str]:
    """Return (has_changes, plan text). -detailed-exitcode: 0 = no changes, 2 = changes."""
    result = run(["plan", "-input=false", "-no-color", "-detailed-exitcode", *_var_args(variables)])
    text = _ok(result, ok_codes=(0, 2))
    return result.returncode == 2, text


_PLAN_LINE = re.compile(r"Plan: \d+ to add, \d+ to change, (\d+) to destroy")


def resources_to_destroy(plan_text: str) -> int:
    """How many resources a plan would destroy (replacements count: they destroy the old one)."""
    match = _PLAN_LINE.search(plan_text)
    return int(match.group(1)) if match else 0


def apply(variables: dict[str, str]) -> None:
    _ok(run(["apply", "-input=false", "-no-color", "-auto-approve", *_var_args(variables)]))


def outputs() -> dict[str, str]:
    raw = json.loads(_ok(run(["output", "-json", "-no-color"])))
    return {name: str(item["value"]) for name, item in raw.items()}
