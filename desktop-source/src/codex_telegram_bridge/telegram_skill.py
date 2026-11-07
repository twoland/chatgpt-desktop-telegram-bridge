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
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]


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
        # Install the required dependency before updating its Telegram caller.
        # The marker permits retrying a partially interrupted managed install.
        _write_private_file(marker_path, (json.dumps(MANAGED_IDENTITY) + "\n").encode())
        for name, content in collaboration_contents.items():
            _write_private_file(collaboration / name, content)
        registry["bridges"][bridge_id(root)] = {"root": str(root)}
        contents["bridges.json"] = (json.dumps(registry, indent=2) + "\n").encode()
        for name, content in contents.items():
            _write_private_file(destination / name, content)
    return destination
