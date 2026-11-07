#!/bin/zsh
set -eu
cd "${0:A:h}"
umask 077
exec ./desktop-source/.venv/bin/python -m codex_telegram_bridge.local_mode run --config "$PWD/private/config.toml"
