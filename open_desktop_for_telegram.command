#!/bin/zsh
set -eu
cd "${0:A:h}"
exec ./desktop-source/.venv/bin/python ./open_desktop.py
