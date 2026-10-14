#!/usr/bin/env python3
"""Adds the Sprite Lore route and header link to a CharacterVault checkout.
Idempotent; backs up each patched file once as <file>.spritelore.bak.

    python3 spritelore_patch.py /path/to/CharacterVault            add
    python3 spritelore_patch.py /path/to/CharacterVault --remove   take it out again"""

import os
import re
import shutil
import sys

_args = [a for a in sys.argv[1:] if not a.startswith("--")]
REMOVE = "--remove" in sys.argv
if not _args:
    raise SystemExit("usage: spritelore_patch.py /path/to/CharacterVault [--remove]")
CV = os.path.expanduser(_args[0])
APP = os.path.join(CV, "src", "App.tsx")
HEADER = os.path.join(CV, "src", "components", "vault", "VaultHeader.tsx")

PAGE_FILES = ["SpriteLoreApi.ts", "SpriteLorePage.tsx", "SpriteLoreSettings.tsx", "SpriteLoreTags.tsx",
              "SpriteLoreNavLink.tsx", "SpriteLoreUpgrade.tsx"]
APP_IMPORT = "import { SpriteLorePage } from './pages/sprite-lore/SpriteLorePage';"
HDR_IMPORT = "import { SpriteLoreNavLink } from '../../pages/sprite-lore/SpriteLoreNavLink';"


def backup(path):
    bak = path + ".spritelore.bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)


def add_import(src, line, after_pattern=None):
    if line in src:
        return src
    if after_pattern:
        m = re.search(after_pattern, src, re.M)
        if m:
            end = src.index("\n", m.end() - 1) + 1 if not m.group(0).endswith("\n") else m.end()
            return src[:end] + line + "\n" + src[end:]
    imports = list(re.finditer(r"^import [^;]*;[ \t]*$", src, re.M | re.S))
    if not imports:
        raise SystemExit(f"no import lines found to anchor on in {APP}")
    end = imports[-1].end()
    return src[:end] + "\n" + line + src[end:]


def patch_app():
    src = open(APP, encoding="utf-8").read()
    if "sprite-lore/SpriteLorePage" in src and 'path="/sprite-lore"' in src:
        return "App.tsx: already patched"
    backup(APP)
    src = add_import(src, APP_IMPORT, r"^import \{ AICreationStudio \}[^\n]*\n")
    if 'path="/sprite-lore"' not in src:
        m = re.search(r'^([ \t]*)<Route path="/ai-create"[^\n]*\n', src, re.M)
        if m:
            src = src[:m.end()] + f'{m.group(1)}<Route path="/sprite-lore" element={{<SpriteLorePage />}} />\n' + src[m.end():]
        else:
            m = re.search(r"^([ \t]*)</Routes>", src, re.M)
            if not m:
                raise SystemExit("App.tsx: no <Routes> block found")
            ind = m.group(1) + "  "
            src = src[:m.start()] + f'{ind}<Route path="/sprite-lore" element={{<SpriteLorePage />}} />\n' + src[m.start():]
    open(APP, "w", encoding="utf-8").write(src)
    return "App.tsx: route /sprite-lore added"


def patch_header():
    if not os.path.isfile(HEADER):
        return "VaultHeader.tsx not found: open the page at #/sprite-lore"
    src = open(HEADER, encoding="utf-8").read()
    if "<SpriteLoreNavLink" in src:
        return "VaultHeader.tsx: already patched"
    m = re.search(r'\n([ \t]*)<a\s+href="#/ai-create"\s+className="hidden lg:inline-flex', src)
    if not m:
        return "VaultHeader.tsx: AI Create button not found; link not added (page is at #/sprite-lore)"
    backup(HEADER)
    src = src[:m.start()] + f"\n{m.group(1)}<SpriteLoreNavLink />" + src[m.start():]
    src = add_import(src, HDR_IMPORT)
    open(HEADER, "w", encoding="utf-8").write(src)
    return "VaultHeader.tsx: Sprite Lore button added next to AI Create"


def unpatch():
    """Drops only the lines this script added, so later CharacterVault edits survive."""
    out = []
    for path, marks in ((APP, (APP_IMPORT, 'path="/sprite-lore"')),
                        (HEADER, (HDR_IMPORT, "<SpriteLoreNavLink />"))):
        if not os.path.isfile(path):
            continue
        src = open(path, encoding="utf-8").read()
        lines = src.split("\n")
        kept = [ln for ln in lines if not any(m in ln for m in marks)]
        if len(kept) != len(lines):
            open(path, "w", encoding="utf-8").write("\n".join(kept))
            out.append(f"{os.path.basename(path)}: Sprite Lore lines removed")
    page = os.path.join(CV, "src", "pages", "sprite-lore")
    gone = []
    for f in PAGE_FILES:
        fp = os.path.join(page, f)
        if os.path.isfile(fp):
            os.remove(fp)
            gone.append(f)
    if gone:
        out.append(f"src/pages/sprite-lore: removed {', '.join(gone)}")
    if os.path.isdir(page) and not os.listdir(page):
        os.rmdir(page)
    return out or ["nothing to remove"]


if __name__ == "__main__":
    if not os.path.isfile(APP):
        raise SystemExit(f"CharacterVault not found at {CV}")
    if REMOVE:
        print("\n".join(unpatch()))
    else:
        print(patch_app())
        print(patch_header())
