#!/usr/bin/env bash
# Sprite Lore uninstaller: removes only what the installer placed. Options: --dry-run, --no-restore
cd "$(dirname "$0")" && exec python3 spritelore_uninstall.py "$@"
