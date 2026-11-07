# Python package

This directory contains the experimental macOS desktop bridge and its isolated tests. Use the [repository quickstart](../README.md) for pairing, desktop coordination, approved file folders and launchers. Configuration examples here are references, not a replacement for the paired local setup.

```sh
uv sync --locked --extra dev
uv run --no-sync pytest
```

The installed `codex-telegram-bridge` entry point runs the local desktop adapter. Run from a source checkout when using the root administration/launcher/skill scripts. The app interface is private and build-sensitive; Linux unit-test success is not a native macOS compatibility claim. See [security notes](../SECURITY.md) before enabling a debugging endpoint or sharing diagnostic files.

Upstream MIT attribution is retained in the root README and both LICENSE files.
