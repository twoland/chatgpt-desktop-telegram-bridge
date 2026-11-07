"""Install/update the bundled skill without credentials or per-message instructions."""
import argparse
import os
from pathlib import Path

from codex_telegram_bridge.telegram_skill import bridge_id, install_skill


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    destination = install_skill(root, args.skills_dir)
    print(f"Telegram skill installed: {destination}\nBridge ID: {bridge_id(root)}")
    print(f"Collaboration playbook installed: {destination.parent / 'collaborative-work'}")
    print("Have every connected agent load collaborative-work/SKILL.md and telegram/SKILL.md; see README setup instructions.")


if __name__ == "__main__":
    main()
