"""Run the real boot script (what cloud-init executes as root) in a sandbox.

System commands (apt, systemctl, mount, mkfs...) are stubs that record their calls, and the
absolute paths the script writes to are redirected into a temp directory. This cannot prove the
script works on Ubuntu, but it does prove its control flow: what is skipped on a baked image,
when a filesystem is created, what order services are enabled in, and that it never trips
`set -euo pipefail` on a normal run.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from portal import bootstrap

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

STUBS_THAT_JUST_LOG = [
    "apt-get", "timedatectl", "systemctl", "mkfs.xfs", "mount", "usermod", "chown", "curl", "sleep",
]  # fmt: skip


def make_sandbox(
    root: Path, *, baked: bool, filesystem_present: bool, home_has_data: bool = False
) -> Path:
    for sub in (
        "bin",
        "etc/skel",
        "etc/systemd/system",
        "opt",
        "var/lib/portal",
        "var/log",
        "dev/disk/by-id",
    ):
        (root / sub).mkdir(parents=True)
    (root / "etc/fstab").write_text("")
    (root / "etc/skel/.bashrc").write_text("")
    log = root / "calls.log"
    for name in STUBS_THAT_JUST_LOG:
        stub = root / "bin" / name
        stub.write_text(f'#!/bin/bash\necho "{name} $*" >> "{log}"\n')
    answers = {
        "findmnt": "echo /dev/nvme0n1p1",
        "lsblk": "echo nvme0n1",
        "mountpoint": "exit 1",
        "id": f'[ -f "{root}/user-created" ]',
        "useradd": f'echo "useradd $*" >> "{log}"; touch "{root}/user-created"',
        "blkid": (
            f'echo "blkid $*" >> "{log}"\n'
            'case "$*" in\n'
            f"  -p*) exit {0 if filesystem_present else 2} ;;\n"
            "  *UUID*) echo 1234-abcd ;;\n"
            "esac"
        ),
    }
    for name, body in answers.items():
        (root / "bin" / name).write_text(f"#!/bin/bash\n{body}\n")
    for stub in (root / "bin").iterdir():
        stub.chmod(0o755)
    # Two EBS volumes as Nitro presents them: the root disk and the freshly attached home disk.
    (root / "dev/nvme0n1").touch()
    # A blank device reads as zeros; a used one has data in its first MiB.
    (root / "dev/nvme1n1").write_bytes(b"\x01" * 4096 if home_has_data else b"\x00" * 4096)
    by_id = root / "dev/disk/by-id"
    (by_id / "nvme-Amazon_Elastic_Block_Store_vol0root").symlink_to(root / "dev/nvme0n1")
    (by_id / "nvme-Amazon_Elastic_Block_Store_vol0home").symlink_to(root / "dev/nvme1n1")
    if baked:
        (root / "var/lib/portal/image.done").touch()
    return root


def run_script(root: Path) -> subprocess.CompletedProcess[str]:
    text = bootstrap.render("Asia/Kolkata", idle_timeout_minutes=45)
    for src, dst in (
        ("/var/lib/portal", f"{root}/var/lib/portal"),
        ("/var/log/", f"{root}/var/log/"),
        ("/etc/", f"{root}/etc/"),
        ("/opt/portal", f"{root}/opt/portal"),
        ("/home/dev", f"{root}/home/dev"),
        ("/dev/disk/by-id", f"{root}/dev/disk/by-id"),
        ("/dev/xvdf", f"{root}/dev/xvdf"),
        ("ROOT_DISK=/dev/$(", f"ROOT_DISK={root}/dev/$("),
        ("seq 1 120", "seq 1 2"),  # do not wait ten minutes if the volume never shows up
    ):
        text = text.replace(src, dst)
    script = root / "script.sh"
    script.write_text(text)
    env = {"PATH": f"{root / 'bin'}:/usr/bin:/bin"}
    return subprocess.run(  # noqa: S603
        [str(shutil.which("bash")), str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


def calls(root: Path) -> list[str]:
    return (root / "calls.log").read_text().splitlines()


def test_a_brand_new_volume_gets_a_filesystem_and_everything_is_set_up(tmp_path: Path) -> None:
    root = make_sandbox(tmp_path, baked=False, filesystem_present=False)
    result = run_script(root)
    assert result.returncode == 0, result.stderr
    made = calls(root)
    assert any(c.startswith("mkfs.xfs -q") and c.endswith("nvme1n1") for c in made)  # the HOME disk
    assert not any("nvme0n1" in c and c.startswith("mkfs") for c in made)  # never the root disk
    assert any(c.startswith("apt-get") and "docker.io" in c for c in made)
    assert (root / "var/lib/portal/bootstrap.done").exists()
    assert (
        "UUID=1234-abcd" in (root / "etc/fstab").read_text()
        and "nofail" in (root / "etc/fstab").read_text()
    )
    assert "uid = dev" in (root / "etc/rsyncd.conf").read_text()


def test_an_existing_volume_is_never_reformatted(tmp_path: Path) -> None:
    root = make_sandbox(tmp_path, baked=False, filesystem_present=True, home_has_data=True)
    assert run_script(root).returncode == 0
    assert not any(c.startswith("mkfs") for c in calls(root))


def test_a_blank_volume_left_by_an_interrupted_up_still_gets_its_filesystem(tmp_path: Path) -> None:
    """The CLI no longer says "this volume is new": blank is decided on the box, from the disk."""
    root = make_sandbox(tmp_path, baked=False, filesystem_present=False, home_has_data=False)
    assert run_script(root).returncode == 0
    assert any(c.startswith("mkfs.xfs") for c in calls(root))


def test_data_without_a_filesystem_signature_is_refused_not_formatted(tmp_path: Path) -> None:
    root = make_sandbox(tmp_path, baked=False, filesystem_present=False, home_has_data=True)
    result = run_script(root)
    assert result.returncode != 0 and "refusing to format" in result.stdout
    assert not any(c.startswith("mkfs") for c in calls(root))
    assert not (root / "user-created").exists()  # and no work lands on the disposable root disk


def test_a_baked_image_skips_the_package_install(tmp_path: Path) -> None:
    root = make_sandbox(tmp_path, baked=True, filesystem_present=False)
    result = run_script(root)
    assert result.returncode == 0, result.stderr
    assert "baked image: skipping package install" in result.stdout
    assert not any(c.startswith("apt-get") for c in calls(root))


def test_the_agent_and_its_units_land_where_systemd_expects_them(tmp_path: Path) -> None:
    root = make_sandbox(tmp_path, baked=False, filesystem_present=False)
    assert run_script(root).returncode == 0
    units = {p.name for p in (root / "etc/systemd/system").iterdir()}
    assert units == {"portal-agent.service", "portal-agent.timer", "portal-watcher.service"}
    assert (
        "--idle-timeout-minutes 45"
        in (root / "etc/systemd/system/portal-agent.service").read_text()
    )
    imported = subprocess.run(  # noqa: S603
        [
            str(shutil.which("python3")),
            "-c",
            "from agent import idle, run, watcher, drain, expiry, imds",
        ],
        cwd=root / "opt/portal",
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr  # runs on the box's plain Python


def test_services_start_in_a_safe_order(tmp_path: Path) -> None:
    root = make_sandbox(tmp_path, baked=False, filesystem_present=False)
    assert run_script(root).returncode == 0
    order = [
        c
        for c in calls(root)
        if c.startswith(("systemctl enable", "mount", "useradd", "systemctl restart"))
    ]
    names = [
        c.split()[0] + (" " + c.split()[2] if c.startswith("systemctl") else "") for c in order
    ]
    mount = next(i for i, c in enumerate(order) if c.startswith("mount"))
    user = next(i for i, c in enumerate(order) if c.startswith("useradd"))
    timer = next(i for i, c in enumerate(order) if "portal-agent.timer" in c)
    rsync = next(i for i, c in enumerate(order) if "restart rsync" in c)
    assert mount < user < rsync < timer, names  # the idle clock starts last, on a finished box


def test_if_the_home_volume_never_appears_nothing_lands_on_the_root_disk(tmp_path: Path) -> None:
    root = make_sandbox(tmp_path, baked=False, filesystem_present=False)
    (root / "dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0home").unlink()
    result = run_script(root)
    assert result.returncode != 0 and "home volume never appeared" in result.stdout
    assert not (root / "user-created").exists()  # no `dev` user, so no work on the disposable disk
    assert not (root / "var/lib/portal/bootstrap.done").exists()


def test_the_rendered_script_has_no_stray_control_characters() -> None:
    """A '\\0' in a normal Python string is a NUL byte, which bash silently mangles."""
    for script in (bootstrap.render("UTC"), bootstrap.render_image()):
        assert "\x00" not in script
        assert all(ch in "\n\t" or ch >= " " for ch in script)
    assert "tr -d '\\0'" in bootstrap.render("UTC")  # the two characters bash needs
