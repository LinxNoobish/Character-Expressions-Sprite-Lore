#!/usr/bin/env python3
"""
Sprite Lore uninstaller: removes exactly what the installer placed, nothing else.

    python3 spritelore_uninstall.py              uninstall
    python3 spritelore_uninstall.py --dry-run    only list what would happen
    python3 spritelore_uninstall.py --no-restore don't put back files that were
                                                 there before the first install
                                                 (delete them and their backups)

It reads ~/.config/spritelore/install_manifest.json, written by the installer:
  * files the installer placed are deleted
  * a file that was already there before the first install is put back from
    its <file>.spritelore-bak copy
  * a file you changed after it was installed is left alone (reported)
  * old ComfyUI nodes the installer renamed to *.disabled get their names back
  * the CharacterVault route and header button lines are taken out
    (only those lines; the rest of App.tsx / VaultHeader.tsx is untouched)
  * the systemd user service is stopped and disabled before its file goes
  * folders the installer created are removed only if they end up empty

Never touched: your settings, profiles, character guides, rename logs, sprites,
lore written into PNGs, ComfyUI output, or anything else the installer didn't put
there. Works from the unzipped pack or from the copy in the install folder.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
IS_WIN = platform.system() == "Windows"
SERVICE = "spritelore.service"

APP_MARKS = ("import { SpriteLorePage } from './pages/sprite-lore/SpriteLorePage';", 'path="/sprite-lore"')
HDR_MARKS = ("import { SpriteLoreNavLink } from '../../pages/sprite-lore/SpriteLoreNavLink';",
             "<SpriteLoreNavLink />")


def config_dir():
    env = os.environ.get("SPRITELORE_CONFIG")
    if env:
        return os.path.dirname(os.path.abspath(os.path.expanduser(env)))
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    return os.path.join(base, "spritelore")


def sha256_file(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def say(msg):
    print(msg, flush=True)


def drop_lines(path, marks, dry):
    """Remove only the lines the installer added. Returns how many."""
    if not os.path.isfile(path):
        return 0
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    kept = [ln for ln in lines if not any(m in ln for m in marks)]
    n = len(lines) - len(kept)
    if n and not dry:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(kept))
    return n


def remove_pyc(py_path, dry):
    """Compiled copies Python made of a removed .py file (__pycache__/<name>.*.pyc)."""
    cache = os.path.join(os.path.dirname(py_path), "__pycache__")
    stem = os.path.splitext(os.path.basename(py_path))[0] + "."
    if not os.path.isdir(cache):
        return
    for n in os.listdir(cache):
        if n.startswith(stem) and n.endswith(".pyc"):
            dry or os.remove(os.path.join(cache, n))
    if not dry and not os.listdir(cache):
        os.rmdir(cache)


def main():
    ap = argparse.ArgumentParser(description="Remove exactly what the Sprite Lore installer placed.")
    ap.add_argument("--dry-run", action="store_true", help="only list what would happen")
    ap.add_argument("--no-restore", action="store_true",
                    help="delete files that existed before the first install instead of putting them back")
    args = ap.parse_args()
    dry, restore = args.dry_run, not args.no_restore

    mpath = os.path.join(config_dir(), "install_manifest.json")
    try:
        with open(mpath, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        raise SystemExit(f"No install record at {mpath}; nothing was installed by this installer "
                         f"(or it was already removed). Nothing changed.")
    say("Sprite Lore uninstall" + ("  (dry run, nothing is changed)" if dry else ""))

    removed, restored, kept, missing, already = [], [], [], [], []

    # 1. stop the service before its file goes
    unit = m.get("service")
    if unit and os.path.exists(unit) and not IS_WIN and shutil.which("systemctl"):
        if not dry:
            run(["systemctl", "--user", "disable", "--now", SERVICE])
        say(f"  service {SERVICE} stopped and disabled")

    # 2. CharacterVault: only the lines the patch added
    for cv in m.get("patched", []):
        n = drop_lines(os.path.join(cv, "src", "App.tsx"), APP_MARKS, dry)
        n += drop_lines(os.path.join(cv, "src", "components", "vault", "VaultHeader.tsx"), HDR_MARKS, dry)
        if n:
            say(f"  CharacterVault: removed {n} Sprite Lore line(s) from App.tsx / VaultHeader.tsx ({cv})")

    # 3. old ComfyUI nodes the installer disabled get their names back
    for src, dst in reversed(m.get("renamed", [])):
        if os.path.exists(dst) and not os.path.exists(src):
            dry or os.rename(dst, src)
            say(f"  renamed back: {os.path.basename(dst)} -> {os.path.basename(src)}")

    # 4. files
    files = m.get("files", {})
    for path in sorted(files, key=len, reverse=True):
        e = files[path]
        if path == mpath:
            continue
        if not os.path.exists(path):
            missing.append(path)
            continue
        if e.get("sha") and sha256_file(path) != e["sha"]:
            kept.append(path)                      # changed after it was installed: yours now
            continue
        orig, bak = e.get("original", "none"), e.get("backup")
        if orig == "identical" and restore:
            already.append(path)                    # it was there, byte for byte, before Sprite Lore
            continue
        if orig == "backup" and bak and os.path.exists(bak) and restore:
            dry or os.replace(bak, path)
            restored.append(path)
            continue
        if not dry:
            os.remove(path)
            if bak and os.path.exists(bak) and not restore:
                os.remove(bak)
            if path.endswith(".py"):
                remove_pyc(path, dry)
        removed.append(path)

    # 5. folders the installer filled wholesale (its own venv)
    for tree in m.get("trees", []):
        if os.path.isfile(os.path.join(tree, "pyvenv.cfg")):
            dry or shutil.rmtree(tree)
            removed.append(tree + os.sep)

    # 6. the record itself, then folders it created if they're empty now
    if not dry:
        os.remove(mpath)
    for d in sorted(m.get("dirs", []), key=len, reverse=True):
        if os.path.isdir(d) and not dry:
            try:
                os.rmdir(d)                         # only succeeds when empty
            except OSError:
                pass
    if unit and not dry and not IS_WIN and shutil.which("systemctl"):
        run(["systemctl", "--user", "daemon-reload"])

    say(f"\n  removed   {len(removed)}")
    for p in removed:
        say(f"    {p}")
    if restored:
        say(f"  put back  {len(restored)} (were there before Sprite Lore was installed)")
        for p in restored:
            say(f"    {p}")
    if kept:
        say(f"  left      {len(kept)} (changed after install, so they're yours now)")
        for p in kept:
            say(f"    {p}")
    if already:
        say(f"  left      {len(already)} (identical files were already there before Sprite Lore)")
    if missing:
        say(f"  already gone {len(missing)}")
    left = [d for d in m.get("dirs", []) if os.path.isdir(d)]
    if left and not dry:
        say("  folders kept because they hold other files: " + ", ".join(sorted(left)))
    say("\nDone." if not dry else "\nDry run finished; run without --dry-run to uninstall.")


if __name__ == "__main__":
    main()
