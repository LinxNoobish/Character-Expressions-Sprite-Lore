#!/usr/bin/env bash
# Sprite Lore installer (Linux/macOS). Options: --dry-run, --uninstall [--purge], --no-service
cd "$(dirname "$0")" && exec python3 spritelore_install.py "$@"
