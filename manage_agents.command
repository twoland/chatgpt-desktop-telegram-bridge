#!/bin/zsh
set -eu
cd "${0:A:h}"
exec ./desktop-source/.venv/bin/python ./manage_chat_routes.py "$@"
