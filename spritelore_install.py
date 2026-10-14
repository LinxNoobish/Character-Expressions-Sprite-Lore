#!/usr/bin/env python3
"""
Sprite Lore installer. Run it from the unzipped folder (all files flat):

    python3 spritelore_install.py              install / update
    python3 spritelore_install.py --dry-run    only show what was found and what would happen
    python3 spritelore_install.py --uninstall  same as spritelore_uninstall.py / uninstall.sh:
                                               removes only what the installer placed

Every file the installer writes, every folder it creates and every file it
renames is recorded in ~/.config/spritelore/install_manifest.json. A file that
was already there the first time is copied to <file>.spritelore-bak before it
is replaced, so the uninstaller can put it back.

Paths come from config.ini, then config.local.ini (same folder, wins), then
the values saved by the last install, then auto-detection. Everything found
is written to ~/.config/spritelore/spritelore_local.json for the server,
anime2img and the command-line tools.

What it installs:
  Sprite Lore server + data      <install_dir>  (settings/profiles/guides kept)
  standalone page                http://127.0.0.1:<port>/
  anime2img (bundled copy)       <install_dir>/anime2img (your templates kept)
  SillyTavern extension          <SillyTavern>/public/scripts/extensions/third-party/SpriteLore
  ComfyUI nodes                  <ComfyUI>/custom_nodes/comfyui-imgprompt, comfyui-vlcaption
  CharacterVault page (optional) <CharacterVault>/src/pages/sprite-lore + route
  systemd user service           spritelore.service (Linux)
"""

import argparse
import configparser
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
IS_WIN = platform.system() == "Windows"
HOME = os.path.expanduser("~")

CORE_FILES = [
    "spritelore_uninstall.py", "spritelore_server.py", "spritelore_imgprompt.py", "spritelore_tmerge.py",
    "spritelore_template_history.json", "spritelore_defaults.json", "spritelore_patch.py",
    "exprlore_common.py", "spritelore_ui.html",
    "tag_sprites.py", "write_lore.py", "batch_runner.py",
]
A2I_CODE = ["anime2img.py"]
A2I_USER_FILES = ["anime2img_exact.txt", "anime2img_creative.txt", "anime2img_style_terms.txt"]
CV_FILES = ["SpriteLoreApi.ts", "SpriteLorePage.tsx", "SpriteLoreSettings.tsx", "SpriteLoreTags.tsx",
            "SpriteLoreNavLink.tsx", "SpriteLoreUpgrade.tsx"]
ST_FILES = {"st_spritelore_index.js": "index.js", "st_spritelore_manifest.json": "manifest.json",
            "st_spritelore_style.css": "style.css"}
COMFY_IMGPROMPT = {"comfy_imgprompt_nodes.py": "__init__.py", "comfy_imgprompt.js": "js/imgprompt.js"}
COMFY_VLCAPTION = {"comfy_vlcaption_nodes.py": "__init__.py",
                   "comfy_vlcaption_requirements.txt": "requirements.txt",
                   "comfy_vlcaption_README.md": "README.md",
                   "comfy_vlcaption_example_zimage_turbo.json":
                       "example_workflows/vlcaption_example_zimage_turbo.json"}
# node ids our two packs register; another copy registering them is a duplicate
OUR_NODE_IDS = ["ImgPromptLoader", "ImgPromptBatch", "QwenVLCaptionZIT", "OrientationLatentZIT",
                "QwenVLCaption", "OrientationLatent"]

ST_CANDIDATES = ["~/SillyTavern", "~/sillytavern", "~/Documents/SillyTavern",
                 "~/SillyTavern-Launcher/SillyTavern", "~/ST/SillyTavern", "/opt/SillyTavern"]
COMFY_CANDIDATES = ["~/ComfyUI", "~/comfyui", "~/comfy/ComfyUI", "~/Documents/ComfyUI",
                    "~/ComfyUI_windows_portable/ComfyUI", "~/AI/ComfyUI", "/opt/ComfyUI"]
A2I_OWN_SPOTS = ["~/anime2img", "~/Documents/anime2img", "~/Desktop/anime2img"]
CV_CANDIDATES = ["~/CharacterVault", "~/Documents/CharacterVault", "~/src/CharacterVault"]

SERVICE = "spritelore.service"
UNIT_PATH = os.path.join(HOME, ".config", "systemd", "user", SERVICE)


# ------------------------------------------------------------------ helpers

def x(p):
    return os.path.abspath(os.path.expanduser(p)) if p else ""


def say(msg=""):
    print(msg, flush=True)


def config_json_path():
    env = os.environ.get("SPRITELORE_CONFIG")
    if env:
        return x(env)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    return os.path.join(base, "spritelore", "spritelore_local.json")


def is_sillytavern(d):
    return bool(d) and os.path.isfile(os.path.join(d, "server.js")) \
        and os.path.isdir(os.path.join(d, "public", "scripts", "extensions"))


def is_comfyui(d):
    return bool(d) and os.path.isfile(os.path.join(d, "main.py")) \
        and os.path.isdir(os.path.join(d, "custom_nodes")) and os.path.isdir(os.path.join(d, "comfy"))


def is_charactervault(d):
    if not d or not os.path.isfile(os.path.join(d, "src", "App.tsx")):
        return False
    try:
        with open(os.path.join(d, "package.json"), encoding="utf-8") as f:
            return "vault" in json.load(f).get("name", "").lower()
    except (OSError, ValueError):
        return True


def shallow_find(test, depth=3):
    """First folder under home (max `depth` levels, hidden/system folders skipped) passing test."""
    skip = {"node_modules", ".git", ".cache", ".local", ".npm", ".venv", "venv", "__pycache__",
            "snap", ".var", ".mozilla", ".steam", "Library", "AppData", "proc"}
    level = [HOME]
    for _ in range(depth):
        nxt = []
        for d in level:
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for n in names:
                if n.startswith(".") or n in skip:
                    continue
                p = os.path.join(d, n)
                if not os.path.isdir(p) or os.path.islink(p):
                    continue
                if test(p):
                    return p
                nxt.append(p)
        level = nxt
    return ""


def detect(name, configured, previous, candidates, test):
    """(path, how) - configured value wins, then the last install, then standard spots, then a shallow search."""
    if configured:
        p = x(configured)
        return (p, "config") if test(p) else (p, "config, but it doesn't look right")
    if previous and test(x(previous)):
        return x(previous), "last install"
    for c in candidates:
        if test(x(c)):
            return x(c), "found"
    p = shallow_find(test)
    return (p, "found by search") if p else ("", "not found")


def manifest_path():
    return os.path.join(os.path.dirname(config_json_path()), "install_manifest.json")


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(path):
    try:
        with open(path, "rb") as f:
            return sha256_bytes(f.read())
    except OSError:
        return None


class Manifest:
    """What this installer placed, so the uninstaller removes exactly that.

    files[path] = {"original": "none" | "backup" | "identical", "backup": path|None, "sha": sha256}
      none       the file didn't exist before Sprite Lore -> uninstall deletes it
      backup     a different file was there -> it was saved as <file>.spritelore-bak
      identical  the same content was already there -> uninstall leaves it
    dirs     folders the installer created (removed only if empty)
    trees    folders the installer created and filled wholesale (the venv)
    renamed  [from, to] renames of old duplicate ComfyUI nodes (put back)
    patched  CharacterVault checkouts whose App.tsx / VaultHeader.tsx got the route and button
    service  systemd unit path
    """

    def __init__(self, dry):
        self.dry = dry
        self.path = manifest_path()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.d = json.load(f)
        except (OSError, ValueError):
            self.d = {}
        for k, v in (("version", 1), ("files", {}), ("dirs", []), ("trees", []),
                     ("renamed", []), ("patched", []), ("service", "")):
            self.d.setdefault(k, v)

    def save(self):
        if self.dry:
            return
        folder = os.path.dirname(self.path)
        p = folder
        while not os.path.exists(p):          # ~/.config/spritelore made by us
            if p not in self.d["dirs"]:
                self.d["dirs"].append(p)
            if os.path.dirname(p) == p:
                break
            p = os.path.dirname(p)
        os.makedirs(folder, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, indent=1)
        os.replace(tmp, self.path)

    def ensure_dir(self, d):
        d = os.path.abspath(d)
        missing, p = [], d
        while not os.path.exists(p):
            missing.append(p)
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        if self.dry or not missing:
            return
        os.makedirs(d, exist_ok=True)
        for m in missing:
            if m not in self.d["dirs"]:
                self.d["dirs"].append(m)
        self.save()

    def tracked(self, path):
        return os.path.abspath(path) in self.d["files"]

    def put(self, dst, data, mode=0o644, overwrite=True):
        """Write bytes to dst, recording it. Returns a short status word."""
        dst = os.path.abspath(dst)
        existed = os.path.exists(dst)
        entry = self.d["files"].get(dst)
        if existed:
            with open(dst, "rb") as f:
                cur = f.read()
            if cur == data:
                if entry is None and not self.dry:
                    self.d["files"][dst] = {"original": "identical", "backup": None, "sha": sha256_bytes(data)}
                    self.save()
                return "same"
            if not overwrite and (entry is None or entry.get("sha") != sha256_bytes(cur)):
                return "kept yours"     # not ours, or edited since we placed it
        if self.dry:
            return "updated" if existed else "new"
        if entry is None:
            entry = {"original": "none", "backup": None}
            if existed:
                bak, n = dst + ".spritelore-bak", 1
                while os.path.exists(bak):
                    n += 1
                    bak = f"{dst}.spritelore-bak{n}"
                shutil.copy2(dst, bak)
                entry = {"original": "backup", "backup": bak}
        self.ensure_dir(os.path.dirname(dst))
        if existed and not os.access(dst, os.W_OK):
            os.chmod(dst, 0o644)
        with open(dst, "wb") as f:
            f.write(data)
        os.chmod(dst, mode)
        entry["sha"] = sha256_bytes(data)
        self.d["files"][dst] = entry
        self.save()
        return "updated" if existed else "new"

    def adopt(self, path):
        """A file some other step created for us (CharacterVault patch backups)."""
        path = os.path.abspath(path)
        if os.path.isfile(path) and path not in self.d["files"] and not self.dry:
            self.d["files"][path] = {"original": "none", "backup": None, "sha": sha256_file(path)}
            self.save()

    def add(self, key, value):
        if not self.dry and value not in self.d[key]:
            self.d[key].append(value)
            self.save()


M = None   # the Manifest for this run


def copy(src, dst, dry, overwrite=True):
    """Copy one pack file through the manifest; returns a short status word."""
    with open(src, "rb") as f:
        data = f.read()
    return M.put(dst, data, 0o755 if dst.endswith(".sh") else 0o644, overwrite)


def run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 600), **kw)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def venv_python(venv):
    return os.path.join(venv, "Scripts", "python.exe") if IS_WIN else os.path.join(venv, "bin", "python")


# ------------------------------------------------------------------ config

def read_ini(extra):
    cp = configparser.ConfigParser(interpolation=None)
    files = [os.path.join(HERE, "config.ini"), os.path.join(HERE, "config.local.ini")] + ([extra] if extra else [])
    used = cp.read([f for f in files if os.path.isfile(f)], encoding="utf-8")
    flat = {}
    for sec in cp.sections():
        for k, v in cp.items(sec):
            flat[k] = v.strip()
    return flat, used


def yes(v, default=True):
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "yes", "true", "on")


def resolve(ini, prev):
    """All paths/values for this install, with how each was found."""
    r, how = {}, {}
    r["install_dir"] = x(ini.get("install_dir") or prev.get("install_dir") or "~/.local/share/spritelore")
    how["install_dir"] = "config" if ini.get("install_dir") else ("last install" if prev.get("install_dir") else "default")

    r["sillytavern"], how["sillytavern"] = detect("SillyTavern", ini.get("sillytavern"), prev.get("sillytavern"),
                                                  ST_CANDIDATES, is_sillytavern)
    r["st_user"] = ini.get("st_user") or prev.get("st_user") or "default-user"
    if r["sillytavern"] and is_sillytavern(r["sillytavern"]):
        data = os.path.join(r["sillytavern"], "data", r["st_user"])
        r["st_characters"] = os.path.join(data, "characters")
        r["st_settings"] = os.path.join(data, "settings.json")
    else:
        r["st_characters"] = x(prev.get("st_characters", ""))
        r["st_settings"] = x(prev.get("st_settings", ""))

    r["comfyui"], how["comfyui"] = detect("ComfyUI", ini.get("comfyui"), prev.get("comfyui"),
                                          COMFY_CANDIDATES, is_comfyui)
    if ini.get("comfy_input"):
        r["comfy_input"], how["comfy_input"] = x(ini["comfy_input"]), "config"
    elif r["comfyui"] and is_comfyui(r["comfyui"]):
        r["comfy_input"], how["comfy_input"] = os.path.join(r["comfyui"], "input"), "ComfyUI"
    else:
        r["comfy_input"], how["comfy_input"] = x(prev.get("comfy_input", "")), "last install"

    if ini.get("charactervault"):
        r["charactervault"], how["charactervault"] = detect("CharacterVault", ini.get("charactervault"), "",
                                                            [], is_charactervault)
    elif prev.get("charactervault") and is_charactervault(x(prev["charactervault"])):
        r["charactervault"], how["charactervault"] = x(prev["charactervault"]), "last install"
    else:
        hit = next((x(c) for c in CV_CANDIDATES if is_charactervault(x(c))), "")
        r["charactervault"], how["charactervault"] = hit, ("found" if hit else "not set (optional)")

    a2i = ini.get("anime2img_dir") or prev.get("anime2img_dir") or ""
    how["anime2img_dir"] = "config" if ini.get("anime2img_dir") else "last install"
    if not a2i:   # an anime2img you already set up in one of the usual spots
        a2i = next((x(c) for c in A2I_OWN_SPOTS if os.path.isfile(os.path.join(x(c), "anime2img.py"))), "")
        how["anime2img_dir"] = "found your copy"
    r["anime2img_dir"] = x(a2i) if a2i else os.path.join(r["install_dir"], "anime2img")
    r["anime2img_bundled"] = not a2i or x(a2i) == os.path.join(r["install_dir"], "anime2img")
    if r["anime2img_bundled"]:
        how["anime2img_dir"] = "bundled copy"
    r["failed_dir"] = x(ini.get("failed_dir") or prev.get("failed_dir") or os.path.join(r["anime2img_dir"], "failed"))

    roots = ini.get("sprite_roots")
    if roots is None or roots == "":
        roots = prev.get("sprite_roots") or []
    if isinstance(roots, str):
        roots = [p.strip() for p in roots.split(";") if p.strip()]
    r["sprite_roots"] = [x(p) for p in roots]

    r["vision_url"] = (ini.get("vision_url") or prev.get("vision_url") or "http://127.0.0.1:5001/v1").rstrip("/")
    r["lore_url"] = (ini.get("lore_url") or prev.get("lore_url") or "http://127.0.0.1:5001/v1").rstrip("/")
    try:
        r["port"] = int(ini.get("port") or prev.get("port") or 5181)
    except ValueError:
        r["port"] = 5181
    return r, how


# ------------------------------------------------------------------ steps

def step_python(r, dry):
    """Python that has Pillow, for the server and anime2img."""
    if run([sys.executable, "-c", "import PIL"]).returncode == 0:
        return sys.executable, "system Python has Pillow"
    venv = os.path.join(r["install_dir"], "venv")
    py = venv_python(venv)
    if os.path.isfile(py) and run([py, "-c", "import PIL"]).returncode == 0:
        return py, "venv with Pillow"
    if dry:
        return py, "would create a venv with Pillow"
    fresh = not os.path.exists(venv)
    res = run([sys.executable, "-m", "venv", "--system-site-packages", venv])
    if fresh and os.path.isdir(venv):
        M.add("trees", os.path.abspath(venv))
    if res.returncode == 0:
        res = run([py, "-m", "pip", "install", "--quiet", "pillow"])
        if res.returncode == 0:
            return py, "venv created, Pillow installed"
    res2 = run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "pillow"])
    if res2.returncode == 0:
        return sys.executable, "Pillow installed for your user"
    hint = ("sudo dnf install python3-pillow   or   sudo apt install python3-pil python3-venv"
            if not IS_WIN else "py -3 -m pip install pillow")
    raise SystemExit(f"Could not install Pillow automatically ({(res.stderr or res2.stderr).strip()[-300:]}).\n"
                     f"Install it with:  {hint}\nthen run the installer again.")


def step_core(r, dry, out):
    d = r["install_dir"]
    for f in CORE_FILES:
        out.append(f"  {f:38} {copy(os.path.join(HERE, f), os.path.join(d, f), dry)}")
    a = r["anime2img_dir"]
    if r["anime2img_bundled"]:
        for f in A2I_CODE:
            out.append(f"  anime2img/{f:28} {copy(os.path.join(HERE, f), os.path.join(a, f), dry)}")
        for f in A2I_USER_FILES:
            out.append(f"  anime2img/{f:28} {copy(os.path.join(HERE, f), os.path.join(a, f), dry, overwrite=False)}")
    else:
        out.append(f"  anime2img: using your folder {a} (not changed)")
        if not os.path.isfile(os.path.join(a, "anime2img.py")):
            out.append(f"  !! no anime2img.py in {a}; image prompts won't run until it's there "
                       f"(or clear anime2img_dir to use the bundled copy)")
    M.ensure_dir(r["failed_dir"])
    server = os.path.join(d, "spritelore_server.py")
    unin = os.path.join(d, "spritelore_uninstall.py")
    if IS_WIN:
        launchers = {"start_spritelore.bat": f'@echo off\r\n"{r["python"]}" "{server}"\r\n',
                     "uninstall_spritelore.bat": f'@echo off\r\n"{r["python"]}" "{unin}" %*\r\npause\r\n'}
    else:
        launchers = {"start_spritelore.sh": f'#!/bin/sh\nexec "{r["python"]}" "{server}" "$@"\n',
                     "uninstall_spritelore.sh": f'#!/bin/sh\nexec "{r["python"]}" "{unin}" "$@"\n'}
    for name, text in launchers.items():
        st = M.put(os.path.join(d, name), text.encode("utf-8"), 0o755)
        out.append(f"  {name:38} {st}")


def write_local_json(r, dry):
    keys = ["install_dir", "sillytavern", "st_user", "st_characters", "st_settings", "comfyui", "comfy_input",
            "charactervault", "anime2img_dir", "failed_dir", "sprite_roots", "vision_url", "lore_url", "port"]
    data = {k: r[k] for k in keys if r.get(k) not in (None, "")}
    p = config_json_path()
    M.put(p, json.dumps(data, indent=2).encode("utf-8"))
    return p


def step_sillytavern(r, dry, out):
    st = r["sillytavern"]
    dst = os.path.join(st, "public", "scripts", "extensions", "third-party", "SpriteLore")
    for src, name in ST_FILES.items():
        out.append(f"  SpriteLore/{name:27} {copy(os.path.join(HERE, src), os.path.join(dst, name), dry)}")
    out.append(f"  -> {dst}")
    out.append("  reload SillyTavern in the browser; settings are under Extensions -> Sprite Lore")


def comfy_python(comfy):
    for p in ("venv/bin/python", ".venv/bin/python", "venv/Scripts/python.exe", ".venv/Scripts/python.exe",
              "../python_embeded/python.exe", "../python_embedded/python.exe"):
        q = os.path.normpath(os.path.join(comfy, p))
        if os.path.isfile(q):
            return q
    return ""


def disable(path, dry):
    target = path + ".disabled"
    n = 1
    while os.path.exists(target):
        n += 1
        target = f"{path}.disabled{n}"
    if not dry:
        os.rename(path, target)
        M.add("renamed", [os.path.abspath(path), os.path.abspath(target)])
    return target


def step_comfyui(r, dry, out):
    comfy = r["comfyui"]
    cn = os.path.join(comfy, "custom_nodes")
    ours = {"comfyui-imgprompt", "comfyui-vlcaption"}
    # older single-file / differently named copies that register the same node ids
    for name in sorted(os.listdir(cn)):
        p = os.path.join(cn, name)
        if name in ours or name.endswith(".disabled") or ".disabled" in name:
            continue
        code = p if name.endswith(".py") else os.path.join(p, "__init__.py") if os.path.isdir(p) else ""
        if not code or not os.path.isfile(code):
            continue
        try:
            src = open(code, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if name == "zit_caption_nodes.py" or any(f'"{nid}"' in src and "NODE_CLASS_MAPPINGS" in src
                                                 for nid in OUR_NODE_IDS):
            out.append(f"  old copy {name} -> {os.path.basename(disable(p, dry))} (same nodes, would load twice)")
    base = os.path.join(cn, "comfyui-imgprompt")
    for src, rel in COMFY_IMGPROMPT.items():
        out.append(f"  comfyui-imgprompt/{rel:20} {copy(os.path.join(HERE, src), os.path.join(base, rel), dry)}")
    base = os.path.join(cn, "comfyui-vlcaption")
    for src, rel in COMFY_VLCAPTION.items():
        out.append(f"  comfyui-vlcaption/{rel:20} {copy(os.path.join(HERE, src), os.path.join(base, rel), dry)}")
    wf = os.path.join(comfy, "user", r.get("comfy_user", "default"), "workflows")
    if os.path.isdir(os.path.dirname(wf)):
        st = copy(os.path.join(HERE, "comfy_vlcaption_example_zimage_turbo.json"),
                  os.path.join(wf, "vlcaption_example_zimage_turbo.json"), dry, overwrite=False)
        out.append(f"  workflows/vlcaption_example_zimage_turbo.json {st}")
    if r["comfy_input"]:
        M.ensure_dir(r["comfy_input"])

    py = comfy_python(comfy)
    req = os.path.join(base, "requirements.txt")
    if py:
        res = run([py, "-c", "import transformers, huggingface_hub; print(transformers.__version__)"], timeout=120)
        ver = res.stdout.strip().splitlines()[-1] if res.returncode == 0 and res.stdout.strip() else ""
        if not ver:
            out.append(f"  !! Qwen VL Caption needs transformers in ComfyUI's Python:\n"
                       f"     \"{py}\" -m pip install -r \"{req}\"")
        else:
            try:
                major, minor = (int(v) for v in ver.split(".")[:2])
            except ValueError:
                major, minor = 0, 0
            note = "" if (major, minor) >= (4, 57) else \
                (" (fine for Qwen2.5-VL; Qwen3-VL needs >= 4.57)" if (major, minor) >= (4, 49) else " - too old, needs >= 4.49")
            out.append(f"  ComfyUI Python: transformers {ver}{note}")
    else:
        out.append("  ComfyUI's Python not found next to it; for Qwen VL Caption run in that environment:\n"
                   f"     pip install -r \"{req}\"")
    out.append("  restart ComfyUI to load the nodes")


def step_charactervault(r, dry, out):
    cv = r["charactervault"]
    page = os.path.join(cv, "src", "pages", "sprite-lore")
    for f in CV_FILES:
        if f == "SpriteLoreApi.ts":   # default server URL follows the configured port
            src = open(os.path.join(HERE, f), encoding="utf-8").read().replace(
                "http://127.0.0.1:5181", f"http://127.0.0.1:{r['port']}")
            dst = os.path.join(page, f)
            cur = open(dst, encoding="utf-8").read() if os.path.isfile(dst) else None
            st = M.put(dst, src.encode("utf-8"))
        else:
            st = copy(os.path.join(HERE, f), os.path.join(page, f), dry)
        out.append(f"  sprite-lore/{f:26} {st}")
    baks = [os.path.join(cv, f + ".spritelore.bak") for f in ("src/App.tsx", "src/components/vault/VaultHeader.tsx")]
    had = {b for b in baks if os.path.exists(b)}
    if not dry:
        res = run([r["python"], os.path.join(HERE, "spritelore_patch.py"), cv])
        for line in (res.stdout + res.stderr).strip().splitlines():
            out.append(f"  {line}")
        if res.returncode == 0:
            M.add("patched", os.path.abspath(cv))
            for b in baks:
                if b not in had:
                    M.adopt(b)
    out.append("  rebuild / restart CharacterVault (npm run build, or npm run dev) to see the page")


def systemd_ok():
    if IS_WIN or not shutil.which("systemctl"):
        return False
    return run(["systemctl", "--user", "show-environment"], timeout=20).returncode == 0


def step_service(r, dry, out):
    if not systemd_ok():
        out.append("  no systemd user session; start the server with "
                   + os.path.join(r["install_dir"], "start_spritelore.bat" if IS_WIN else "start_spritelore.sh"))
        return False
    unit = f"""[Unit]
Description=Sprite Lore server (port {r['port']})
After=network.target

[Service]
ExecStart="{r['python']}" "{os.path.join(r['install_dir'], 'spritelore_server.py')}"
WorkingDirectory={r['install_dir']}
Environment=SPRITELORE_CONFIG={config_json_path()}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""
    if dry:
        out.append(f"  would write {UNIT_PATH} and (re)start {SERVICE}")
        return False
    M.put(UNIT_PATH, unit.encode("utf-8"))
    if not M.dry:
        M.d["service"] = UNIT_PATH
        M.save()
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", SERVICE])
    res = run(["systemctl", "--user", "restart", SERVICE])
    out.append(f"  {SERVICE} enabled (starts at login)" if res.returncode == 0
               else f"  !! could not start {SERVICE}: {res.stderr.strip()}")
    return res.returncode == 0


def health(port, wait=8.0):
    end = time.time() + wait
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                return json.load(resp)
        except Exception:
            time.sleep(0.4)
    return None


def imgprompt_status(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/imgprompt/status", timeout=10) as resp:
            return json.load(resp)
    except Exception:
        return None


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="Install Sprite Lore (server, SillyTavern extension, ComfyUI nodes).")
    ap.add_argument("--dry-run", action="store_true", help="show what was found and what would happen")
    ap.add_argument("--uninstall", action="store_true", help="run spritelore_uninstall.py (removes only what was placed)")
    ap.add_argument("--config", help="extra .ini read after config.ini and config.local.ini")
    ap.add_argument("--no-service", action="store_true", help="don't install/start the systemd service")
    args = ap.parse_args()

    missing = [f for f in CORE_FILES + A2I_CODE + A2I_USER_FILES + CV_FILES + list(ST_FILES)
               + list(COMFY_IMGPROMPT) + list(COMFY_VLCAPTION) if not os.path.isfile(os.path.join(HERE, f))]
    prev = {}
    try:
        with open(config_json_path(), encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        pass

    if args.uninstall:
        cmd = [sys.executable, os.path.join(HERE, "spritelore_uninstall.py")] + (["--dry-run"] if args.dry_run else [])
        raise SystemExit(subprocess.call(cmd))
    if missing:
        raise SystemExit("These files are missing next to the installer (unzip everything into one folder):\n  "
                         + "\n  ".join(missing))

    ini, used = read_ini(args.config)
    r, how = resolve(ini, prev)
    dry = args.dry_run
    global M
    M = Manifest(dry)
    say("Sprite Lore installer" + ("  (dry run, nothing is changed)" if dry else ""))
    say("config: " + (", ".join(os.path.basename(u) for u in used) or "none, auto-detecting"))
    say("")
    for k, label in (("install_dir", "Sprite Lore data"), ("sillytavern", "SillyTavern"),
                     ("comfyui", "ComfyUI"), ("comfy_input", "ComfyUI input"),
                     ("charactervault", "CharacterVault"), ("anime2img_dir", "anime2img")):
        say(f"  {label:17} {r[k] or '-':55} ({how.get(k, '')})")
    if r["st_characters"]:
        say(f"  {'sprites':17} {r['st_characters']}")
    for p in r["sprite_roots"]:
        say(f"  {'extra sprites':17} {p}{'' if os.path.isdir(p) else '  (missing!)'}")
    say(f"  {'vision backend':17} {r['vision_url']}")
    say(f"  {'lore backend':17} {r['lore_url']}")
    say("")

    r["python"], pymsg = step_python(r, dry)
    say(f"Python: {r['python']} ({pymsg})")

    out = []
    step_core(r, dry, out)
    say(f"\nServer -> {r['install_dir']}")
    say("\n".join(out))
    p = write_local_json(r, dry)
    say(f"  paths saved to {p}")

    want_st = yes(ini.get("sillytavern_extension"))
    st_ok = is_sillytavern(r["sillytavern"])
    say("\nSillyTavern extension")
    if not want_st:
        say("  skipped (config)")
    elif st_ok:
        out = []
        step_sillytavern(r, dry, out)
        say("\n".join(out))
    else:
        say(f"  !! SillyTavern not found{' at ' + r['sillytavern'] if r['sillytavern'] else ''}. "
            f"Set sillytavern = <folder> in config.local.ini and run again.")

    want_comfy = yes(ini.get("comfyui_nodes"))
    say("\nComfyUI nodes")
    if not want_comfy:
        say("  skipped (config)")
    elif is_comfyui(r["comfyui"]):
        out = []
        step_comfyui(r, dry, out)
        say("\n".join(out))
    else:
        say(f"  !! ComfyUI not found{' at ' + r['comfyui'] if r['comfyui'] else ''}. "
            f"Set comfyui = <folder> in config.local.ini and run again.")

    say("\nCharacterVault page")
    if not yes(ini.get("charactervault_page")):
        say("  skipped (config)")
    elif r["charactervault"] and is_charactervault(r["charactervault"]):
        out = []
        step_charactervault(r, dry, out)
        say("\n".join(out))
    elif r["charactervault"]:
        say(f"  !! {r['charactervault']} is not a CharacterVault checkout (no src/App.tsx)")
    else:
        say("  not set; Sprite Lore runs on its own page (charactervault = <folder> in config.local.ini to add it)")

    say("\nServer")
    out = []
    started = False
    if yes(ini.get("service")) and not args.no_service:
        started = step_service(r, dry, out)
    else:
        out.append("  service skipped; start it with " + os.path.join(
            r["install_dir"], "start_spritelore.bat" if IS_WIN else "start_spritelore.sh"))
    say("\n".join(out))
    if started:
        h = health(r["port"])
        if h:
            say(f"  running: open http://127.0.0.1:{r['port']}/")
            s = imgprompt_status(r["port"])
            if s and s.get("found"):
                names = ", ".join(t["name"] for t in s.get("templates", [])) or "(none)"
                say(f"  image prompts: {s['dir']} -> {s.get('output')}  templates: {names}")
            elif s:
                say(f"  !! image prompts: {s.get('error')}")
        else:
            say(f"  !! no answer on port {r['port']}; see: journalctl --user -u {SERVICE} -n 30")
    if missing_sources := [p for p in r["sprite_roots"] if not os.path.isdir(p)]:
        say("\nNote: these sprite folders don't exist: " + ", ".join(missing_sources))
    if not dry:
        say(f"\nEverything placed is listed in {M.path}; uninstall_spritelore.sh in {r['install_dir']} "
            f"(or uninstall.sh here) removes exactly that.")
    say("\nDone." if not dry else "\nDry run finished; run without --dry-run to install.")


if __name__ == "__main__":
    main()
