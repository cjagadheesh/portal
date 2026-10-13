"""Local cache at ~/.portal/state.json. AWS tags stay the source of truth; this is only a cache."""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError


class ControlPlane(BaseModel):
    """Outputs of infra/control-plane that `portal up` needs."""

    region: str
    instance_profile_name: str
    security_group_id: str
    budget_name: str


@dataclass(frozen=True)
class Box:
    name: str
    instance_id: str
    instance_type: str
    az: str
    state: str
    expires_at: str
    launched_at: str = ""
    market: str = "spot"  # "spot" or "on-demand" (the fallback when spot has no capacity)
    image: str = ""  # "baked" (portal's Packer AMI) or "stock" (Ubuntu + install at boot)


@dataclass(frozen=True)
class Reconciled:
    """What AWS says right now, plus how the local cache disagreed with it."""

    boxes: list[Box]
    vanished: list[str]  # instance ids the cache knew that AWS no longer reports
    discovered: list[str]  # instance ids AWS reports that the cache had never seen


def state_path() -> Path:
    # PORTAL_HOME lets tests (and curious users) keep state out of the real home dir.
    return Path(os.environ.get("PORTAL_HOME", Path.home() / ".portal")) / "state.json"


def load_control_plane() -> ControlPlane | None:
    raw = _read().get("control_plane")
    if raw is None:
        return None
    try:
        return ControlPlane.model_validate(raw)
    except ValidationError:
        return None  # written by another version: the cache is disposable, AWS tags rebuild it


def _read() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}  # a corrupt cache is not an error: AWS tags can rebuild it
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any]) -> None:
    path = state_path()
    # Resource ids and the region are not secrets, but there is no reason for other users on
    # the machine to read them: private directory, private file.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Write then rename so a crash never leaves half a JSON file.
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2))
    tmp.replace(path)


def save_control_plane(cp: ControlPlane) -> None:
    data = _read()
    data["control_plane"] = cp.model_dump()
    _write(data)


def reconcile(live: list[Box]) -> Reconciled:
    """Make the local cache match AWS. Tags are the truth; the cache never wins an argument.

    `live` is what find_boxes just read from AWS tags. Deleting state.json, running from a
    second laptop or terminating a box by hand all end the same way: next command, the cache
    is rewritten from `live`.
    """
    data = _read()
    cached = {b["instance_id"] for b in data.get("boxes", []) if "instance_id" in b}
    live_ids = {b.instance_id for b in live}
    data["boxes"] = [asdict(b) for b in live]
    _write(data)
    return Reconciled(
        boxes=live,
        vanished=sorted(cached - live_ids),
        discovered=sorted(live_ids - cached),
    )
