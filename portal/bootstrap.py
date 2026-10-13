"""User-data script that turns a bare Ubuntu AMI into a dev box (until the Packer AMI)."""

import ast
import base64
import gzip
import io
import re
import tarfile
from pathlib import Path

from portal.errors import PortalError

# The timezone is pasted into a root shell script, so only accept tz-database-looking names.
_TIMEZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*")

# `portal sync` connects to this port on the box (through SSM), so both sides share one number.
RSYNC_PORT = 873

# The slow part of a boot (apt and uv, roughly a minute): exactly what a Packer image bakes in.
# The same text feeds both the boot script and the image script, so they cannot drift.
_INSTALL = """# On first boot cloud-init and unattended-upgrades also hold the apt lock; wait for it
# instead of failing the whole bootstrap under `set -e`.
APT="apt-get -o DPkg::Lock::Timeout=300"
$APT update -y
$APT install -y docker.io git ripgrep fzf curl xfsprogs rsync psmisc python3-boto3

# uv has no apt package. UV_UNMANAGED_INSTALL drops the binary into a system dir without
# editing shell profiles or needing $HOME, which cloud-init does not reliably set.
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL=/usr/local/bin sh
fi
"""

_SCRIPT = """#!/bin/bash
# portal bootstrap. Safe to run twice: every step checks before it changes anything.
set -euo pipefail
mkdir -p /var/lib/portal
# Everything below is logged so a slow or broken boot can be debugged after the fact.
exec > >(tee -a /var/log/portal-bootstrap.log) 2>&1
START=$(date +%s)
echo "portal bootstrap started $(date -u +%FT%TZ)"
if [ -f /var/lib/portal/bootstrap.done ]; then
  echo "already bootstrapped; nothing to do"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
timedatectl set-timezone {timezone}
{install}

# --- home volume: /home/dev lives on its own EBS volume that outlives the box ---
# `portal up` attaches it only once the instance is running, so wait for it. It is the one
# EBS disk that is not the root disk (by-id names skip instance-store NVMe drives).
ROOT_DISK=/dev/$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")
HOME_DEV=""
for _ in $(seq 1 120); do
  for link in /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol* /dev/xvdf; do
    case "$link" in *-part*) continue ;; esac
    [ -e "$link" ] || continue
    dev=$(readlink -f "$link")
    if [ "$dev" != "$ROOT_DISK" ]; then HOME_DEV=$dev; break 2; fi
  done
  sleep 5
done
if [ -z "$HOME_DEV" ]; then
  echo "FATAL: home volume never appeared; not creating dev so no work lands on the root disk"
  exit 1
fi
echo "home volume is $HOME_DEV"

# Format only a device that is provably blank: no filesystem signature (blkid -p exits 2 for
# "nothing found") AND an all-zero first MiB. Reformatting is data loss, so a disk with any data
# but no recognisable filesystem (a damaged superblock, say) is refused, not "fixed". The
# blank-disk test needs no flag from the CLI, so a volume left over from an `up` that was
# interrupted before the first boot still gets its filesystem on the next one.
set +e; blkid -p "$HOME_DEV" >/dev/null 2>&1; rc=$?; set -e
if [ "$rc" -eq 2 ]; then
  if [ -z "$(head -c 1048576 "$HOME_DEV" | tr -d '\\0' | head -c 1)" ]; then
    echo "blank home volume: creating XFS filesystem"
    mkfs.xfs -q "$HOME_DEV"
  else
    echo "FATAL: $HOME_DEV has data but no filesystem signature; refusing to format it."
    echo "Inspect it from the root disk before deleting anything (xfs_repair -n, file -s)."
    exit 1
  fi
fi
UUID=$(blkid -s UUID -o value "$HOME_DEV")
mkdir -p /home/dev
# By UUID, because device names can change between boots; nofail so a missing volume
# never stops the box from booting (SSM still works, so you can debug it).
grep -q "$UUID" /etc/fstab || echo "UUID=$UUID /home/dev xfs defaults,nofail 0 2" >> /etc/fstab
systemctl daemon-reload
mountpoint -q /home/dev || mount /home/dev

# The agent and your work run as `dev`, not root. A fixed uid means files on a reused home
# volume still belong to `dev` on the next box. The docker group lets dev run docker
# without sudo (effectively root on the box, fine for a single-user disposable machine).
id dev >/dev/null 2>&1 \\
  || useradd --uid 1500 --no-create-home --home-dir /home/dev --shell /bin/bash dev
if [ ! -f /home/dev/.bashrc ]; then
  cp -rT /etc/skel /home/dev
  chown -R dev:dev /home/dev
fi
chown dev:dev /home/dev
usermod -aG docker dev
systemctl enable --now docker

# `portal sync` pushes files with rsync through an SSM port-forward. The daemon listens on
# loopback only, so nothing off the box can reach it; the only way in is an SSM session,
# which IAM controls. No SSH keys needed. It writes as `dev` and is confined to /home/dev.
cat > /etc/rsyncd.conf <<'RSYNCD'
address = 127.0.0.1
port = {rsync_port}
# Every connection comes from 127.0.0.1 through the SSM tunnel; skip the DNS lookups.
reverse lookup = no
forward lookup = no
uid = dev
gid = dev
[home]
    path = /home/dev
    read only = no
    list = no
RSYNCD
systemctl enable rsync
systemctl restart rsync

# --- portal-agent: powers the box off when it has been idle (layer 2 of the shutdown design) ---
# The code travels inside this script as a gzipped tarball (user data is capped at 16 KB).
mkdir -p /opt/portal
base64 -d <<'AGENT_TGZ' | tar xz -C /opt/portal
{agent_archive}
AGENT_TGZ
cat > /etc/systemd/system/portal-agent.service <<'UNIT'
[Unit]
Description=portal agent: shut the box down when it is idle
[Service]
Type=oneshot
WorkingDirectory=/opt/portal
ExecStart=/usr/bin/python3 -m agent.run --idle-timeout-minutes {idle_timeout_minutes}
UNIT
cat > /etc/systemd/system/portal-agent.timer <<'UNIT'
[Unit]
Description=Run the portal agent every minute
[Timer]
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=5s
[Install]
WantedBy=timers.target
UNIT
cat > /etc/systemd/system/portal-watcher.service <<'UNIT'
[Unit]
Description=portal watcher: react to spot interruption notices
[Service]
Type=simple
WorkingDirectory=/opt/portal
ExecStart=/usr/bin/python3 -m agent.watcher
# After a notice it drains and exits 0; do not restart it then. Crashes do restart.
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now portal-watcher.service
# Enabled last, so the idle clock starts only once the box is fully set up.
systemctl enable --now portal-agent.timer

touch /var/lib/portal/bootstrap.done
echo "portal bootstrap finished in $(( $(date +%s) - START ))s"
"""


def _agent_dir() -> Path:
    """The agent's source: bundled inside the installed wheel, or the repo checkout in dev."""
    here = Path(__file__).resolve().parent
    bundled = here / "agent_src"
    return bundled if bundled.is_dir() else here.parent / "agent"


def minify(source: str) -> str:
    """Drop docstrings and comments (ast.unparse keeps behaviour, not prose).

    User data is capped at 16 KB and the agent's explanations are about half its size. They
    stay in the repo, where they are read; the box only needs the code.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree) + "\n"


def agent_archive() -> str:
    """The agent as base64(tar.gz) with a fixed mtime, so identical code gives identical output."""
    buffer = io.BytesIO()
    # tarfile's own "w:gz" stamps the current time into the gzip header, so build the gzip layer
    # by hand with mtime=0; otherwise two launches of identical code differ in user data.
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as packed,
        tarfile.open(fileobj=packed, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for path in sorted(_agent_dir().glob("*.py")):
            data = minify(path.read_text()).encode()
            info = tarfile.TarInfo(f"agent/{path.name}")
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buffer.getvalue()).decode()


def render(timezone: str, idle_timeout_minutes: int = 30) -> str:
    if not _TIMEZONE.fullmatch(timezone) or ".." in timezone:
        raise PortalError(f"Invalid timezone '{timezone}'. Use a tz name like UTC or Asia/Kolkata.")
    # A baked image carries a marker file; on one of those the install step is skipped.
    guarded = (
        "if [ ! -f /var/lib/portal/image.done ]; then\n"
        + "".join(f"  {line}\n" if line else "\n" for line in _INSTALL.splitlines())
        + 'else\n  echo "baked image: skipping package install"\nfi'
    )
    return _SCRIPT.format(
        install=guarded,
        timezone=timezone,
        rsync_port=RSYNC_PORT,
        agent_archive=agent_archive(),
        idle_timeout_minutes=int(idle_timeout_minutes),
    )


def render_image() -> str:
    """Provisioning script for the Packer build (packer/provision.sh is generated from this)."""
    return (
        "#!/bin/bash\n"
        "# Generated by `python -m portal.bootstrap`; do not edit. Bakes the slow part of a boot.\n"
        "set -euo pipefail\n"
        "mkdir -p /var/lib/portal\n"
        "export DEBIAN_FRONTEND=noninteractive\n" + _INSTALL + "\nsystemctl enable docker\n"
        "apt-get clean\nrm -rf /var/lib/apt/lists/*\n"
        "touch /var/lib/portal/image.done\n"
    )


if __name__ == "__main__":
    print(render_image(), end="")
