"""Install Telegram and its collaboration playbook, preserving local registries."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

COLLABORATION_FILES = (
    "SKILL.md", "agents/openai.yaml", "references/authority-and-approvals.md",
    "references/resources-and-sensitive-actions.md", "references/decision-examples.md",
)
MANAGED_MARKER = ".bridge-managed.json"
MANAGED_IDENTITY = {"version": 1, "managed_by": "codex-telegram-bridge"}


def _write_private_file(target: Path, content: bytes):
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    try:
        temporary.chmod(0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def bridge_id(root: Path) -> str:
    # A paired installation keeps its identity when its folder moves. Existing
    # agent requests and the optional governance transport still use that ID.
    root = root.resolve()
    identity_file = root / "private/bridge-id.json"
    receipt = root / "installation.json"
    identity = None
    if identity_file.is_file():
        saved = json.loads(identity_file.read_text())
        if saved.get("version") != 1 or "bridge_id" not in saved:
            raise ValueError("Invalid saved bridge ID; inspect private/bridge-id.json")
        identity = saved["bridge_id"]
    elif receipt.is_file():
        identity = (json.loads(receipt.read_text()).get("telegram_skill") or {}).get("bridge_id")
    if identity is not None or identity_file.is_file():
        if not isinstance(identity, str) or len(identity) != 12 or any(
            char not in "0123456789abcdef" for char in identity
        ):
            raise ValueError("Invalid saved bridge ID; inspect the installation identity before starting")
        return identity
    return hashlib.sha256(str(root).encode()).hexdigest()[:12]


def install_skill(root: Path, skills_dir: Path) -> Path:
    root = root.resolve()
    source = root / "skills/telegram"
    files = ["SKILL.md", "agents/openai.yaml", "scripts/send_update.py"]
    contents = {name: (source / name).read_bytes() for name in files}
    collaboration_source = root / "skills/collaborative-work"
    try:
        collaboration_contents = {name: (collaboration_source / name).read_bytes() for name in COLLABORATION_FILES}
    except FileNotFoundError as exc:
        raise ValueError("The bundled collaborative-work playbook is incomplete; use a complete bridge checkout") from exc
    interpreter = root / "desktop-source/.venv/bin/python"
    if not interpreter.is_file() or not (root / "send_progress.py").is_file():
        raise ValueError("Create this bridge's Python environment before installing its skill")
    destination = skills_dir.expanduser() / "telegram"
    collaboration = destination.parent / "collaborative-work"
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry_path = destination / "bridges.json"
        if registry_path.is_file():
            registry = json.loads(registry_path.read_text())
            if registry.get("version") != 1 or not isinstance(registry.get("bridges"), dict):
                raise ValueError("Unrecognized Telegram skill registry; inspect before updating")
        else:
            if (destination / "SKILL.md").exists():
                raise ValueError("An unmanaged Telegram skill already exists; choose another skills directory")
            registry = {"version": 1, "bridges": {}}
        marker_path = collaboration / MANAGED_MARKER
        if marker_path.exists():
            if json.loads(marker_path.read_text()) != MANAGED_IDENTITY:
                raise ValueError("Unrecognized collaborative-work installation; inspect before updating")
        elif collaboration.exists() and any(collaboration.iterdir()):
            raise ValueError("An unmanaged collaborative-work skill already exists; inspect before updating")
        identity = bridge_id(root)
        (root / "private").mkdir(mode=0o700, exist_ok=True)
        _write_private_file(root / "private/bridge-id.json",
                            (json.dumps({"version": 1, "bridge_id": identity}) + "\n").encode())
        # Install the required dependency before updating its Telegram caller.
        # The marker permits retrying a partially interrupted managed install.
        _write_private_file(marker_path, (json.dumps(MANAGED_IDENTITY) + "\n").encode())
        for name, content in collaboration_contents.items():
            _write_private_file(collaboration / name, content)
        registry["bridges"][identity] = {"root": str(root)}
        contents["bridges.json"] = (json.dumps(registry, indent=2) + "\n").encode()
        for name, content in contents.items():
            _write_private_file(destination / name, content)
    return destination
