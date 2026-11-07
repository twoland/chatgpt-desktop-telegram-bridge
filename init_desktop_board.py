"""Create the optional legacy desktop board after the operator verifies it is free."""
import argparse
from pathlib import Path

from codex_telegram_bridge.desktop_booking import BoardError, initialize_legacy_board


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args()
    try:
        path = initialize_legacy_board(args.home)
    except (BoardError, FileExistsError) as exc:
        raise SystemExit(f"No board replaced: {exc}")
    print(f"Created {path}. Every desktop user must follow this board; it is cooperative, not an OS lock.")


if __name__ == "__main__":
    main()
