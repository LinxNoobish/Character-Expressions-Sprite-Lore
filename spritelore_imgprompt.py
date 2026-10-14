"""
spritelore_imgprompt.py - runs your anime2img.py (photoreal image-gen prompts)
from the Sprite Lore page.

anime2img.py is run through its own command line (python3 anime2img.py
<image> -t <template> --out <folder>), the same way you run it by hand, so
any version of it works; nothing inside it is called directly. The vision
backend chosen on the page is passed in by setting its KOBOLD_URL /
KOBOLD_URLS before main() runs. Your anime2img_<template>.txt files and
anime2img_style_terms.txt are used as they are. The folder used is the one
set in config.ini (anime2img_dir) or Settings -> Image prompts, else the copy
bundled with Sprite Lore. Output is the same as running it by hand: a PNG
copy per template in
ComfyUI/input/imgprompt/<output>/<Character>_NNN.png carrying the "imgprompt"
and "parameters" chunks, ready for the Load Image + Embedded Prompt node.
A short summary (base + variations + where the copy went) is also stored in
the sprite's own st_expr chunk under "imgprompts", pixels untouched.

Editing a template from the page keeps the first version as
anime2img_<template>.txt.orig, which "Restore original" puts back.
"""

import datetime as dt
import glob
import hashlib
import importlib.util
import re
import struct
import subprocess
import zlib
import json
import os
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLED = os.path.join(HERE, "anime2img")
COMFY_CANDIDATES = ["~/ComfyUI", "~/comfyui", "~/AI/ComfyUI", "~/ai/ComfyUI", "~/apps/ComfyUI",
                    "~/Programs/ComfyUI", "/opt/ComfyUI"]
# Checked in order when no folder is configured; the copy bundled with the
# pack comes first, then common spots in the home folder.
CANDIDATES = [BUNDLED, "~/anime2img", "~/Documents/anime2img", "~/Desktop/anime2img"]
SEARCH_ROOTS = [("~", 4)]
SKIP_DIRS = {"node_modules", "venv", ".venv", "models", ".git", "__pycache__",
             "output", "input", ".cache", ".local", "site-packages", "Characters"}

_lock = threading.Lock()
_found = {"ts": 0, "all": []}
_module = {"path": None, "mtime": None, "mod": None}
_help = {"path": None, "mtime": None, "flags": set()}
RUN_TIMEOUT = 3600

RUNNER = r"""
import importlib.util, os, runpy, sys
a2i, url = sys.argv[1], sys.argv[2]
script = os.path.join(a2i, "anime2img.py")
sys.path.insert(0, a2i)
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("anime2img", script)
mod = importlib.util.module_from_spec(spec)
sys.modules["anime2img"] = mod
spec.loader.exec_module(mod)
if url:
    if hasattr(mod, "KOBOLD_URL"):
        mod.KOBOLD_URL = url
    if hasattr(mod, "KOBOLD_URLS"):
        mod.KOBOLD_URLS = [url]
sys.argv = [script] + sys.argv[3:]
if callable(getattr(mod, "main", None)):
    mod.main()
else:
    runpy.run_path(script, run_name="__main__")
"""


def _is_dir(d):
    return (os.path.isfile(os.path.join(d, "anime2img.py"))
            and os.path.isfile(os.path.join(d, "anime2img_style_terms.txt")))


def candidates(refresh=False):
    """Folders holding anime2img.py + anime2img_style_terms.txt: the fixed
    CANDIDATES in order (bundled copy first); the home folder is only walked
    when none of those exists. Cached for a minute."""
    if not refresh and time.time() - _found["ts"] < 60:
        return [c for c in _found["all"] if _is_dir(c["dir"])]
    seen, out = set(), []

    def add(d):
        real = os.path.realpath(d)
        if real in seen or not _is_dir(d):
            return
        seen.add(real)
        out.append({"dir": d, "mtime": int(os.path.getmtime(os.path.join(d, "anime2img.py")))})

    for c in CANDIDATES:
        add(os.path.expanduser(c))
    if not out:
        for root, depth in SEARCH_ROOTS:
            root = os.path.expanduser(root)
            if not os.path.isdir(root):
                continue
            base = root.rstrip("/").count("/")
            for d, subs, files in os.walk(root):
                if d.count("/") - base >= depth:
                    subs[:] = []
                else:
                    subs[:] = [x for x in subs if not x.startswith(".") and x not in SKIP_DIRS]
                if "anime2img.py" in files and "anime2img_style_terms.txt" in files:
                    add(d)
    _found.update(ts=time.time(), all=out)
    return out


def find_dir(configured=""):
    if configured:
        d = os.path.abspath(os.path.expanduser(configured))
        return d if _is_dir(d) else None
    c = candidates()
    return c[0]["dir"] if c else None


def load_module(a2i_dir):
    """In-process import, only used to check a template before it is saved."""
    path = os.path.join(a2i_dir, "anime2img.py")
    mtime = os.path.getmtime(path)
    if _module["mod"] is not None and _module["path"] == path and _module["mtime"] == mtime:
        return _module["mod"]
    spec = importlib.util.spec_from_file_location("anime2img_check", path)
    mod = importlib.util.module_from_spec(spec)
    keep = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    except SystemExit as e:
        raise ValueError(f"anime2img.py could not load: {e}") from None
    finally:
        sys.dont_write_bytecode = keep
    _module.update(path=path, mtime=mtime, mod=mod)
    return mod


def cli_flags(a2i_dir):
    path = os.path.join(a2i_dir, "anime2img.py")
    mtime = os.path.getmtime(path)
    if _help["path"] == path and _help["mtime"] == mtime:
        return _help["flags"]
    try:
        r = subprocess.run([sys.executable, path, "--help"], cwd=a2i_dir, capture_output=True,
                           text=True, timeout=60, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        text = r.stdout + r.stderr
    except Exception:
        text = ""
    flags = set(re.findall(r"(?<![\w-])(--?[a-zA-Z][\w-]*)", text))
    _help.update(path=path, mtime=mtime, flags=flags)
    return flags


def _template_path(a2i_dir, name):
    if not name or "/" in name or name.startswith("."):
        raise ValueError(f"bad template name: {name!r}")
    return os.path.join(a2i_dir, f"anime2img_{name}.txt")


def _settings_of(text):
    """The === settings === block as a dict (for the template list)."""
    out, inside = {}, False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("==="):
            inside = s.strip("= ").lower() == "settings"
            continue
        if inside and ":" in s and not s.startswith("#"):
            k, v = s.split("#", 1)[0].split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _sections(text):
    return {m.group(1).strip().lower() for m in re.finditer(r"^===\s*(.+?)\s*===\s*$", text, re.M)}


def list_templates(a2i_dir):
    out = []
    for p in sorted(glob.glob(os.path.join(a2i_dir, "anime2img_*.txt"))):
        name = os.path.basename(p)[len("anime2img_"):-len(".txt")]
        if name in ("style_terms", "dirs") or name.endswith(".tmpcheck"):
            continue
        with open(p, encoding="utf-8") as f:
            text = f.read()
        if "system" not in _sections(text):
            continue
        st = _settings_of(text)
        out.append({"name": name, "text": text, "hasOrig": os.path.exists(p + ".orig"),
                    "output": st.get("output", name), "detail": st.get("detail", "high"),
                    "style": st.get("style", "natural"), "variations": st.get("variations", "4"),
                    "vary": st.get("vary", "expression, pose"), "ageRange": st.get("age_range", "")})
    return out


def validate_text(a2i_dir, name, text):
    missing = [s for s in ("system", "user") if s not in _sections(text)]
    if missing:
        raise ValueError(f"template not valid: missing === {missing[0]} === section")
    try:
        mod = load_module(a2i_dir)
    except Exception:
        return
    if not hasattr(mod, "Template"):
        return
    tmp = os.path.join(a2i_dir, f"anime2img_{name}.tmpcheck.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        mod.Template(tmp)
    except SystemExit as e:
        raise ValueError(f"template not valid: {e}") from None
    except Exception as e:
        raise ValueError(f"template not valid: {type(e).__name__}: {e}") from None
    finally:
        os.unlink(tmp)


def save_template(a2i_dir, name, text, create=False):
    path = _template_path(a2i_dir, name)
    if create and os.path.exists(path):
        raise FileExistsError(f"template {name} already exists")
    if not create and not os.path.exists(path):
        raise FileNotFoundError(path)
    validate_text(a2i_dir, name, text)
    if os.path.exists(path) and not os.path.exists(path + ".orig"):
        shutil.copy2(path, path + ".orig")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def restore_template(a2i_dir, name):
    path = _template_path(a2i_dir, name)
    if not os.path.exists(path + ".orig"):
        raise FileNotFoundError(f"no original saved for {name}")
    shutil.copy2(path + ".orig", path)


def _comfy_input():
    for c in [os.environ.get("COMFYUI_DIR", "")] + COMFY_CANDIDATES:
        d = os.path.expanduser(c) if c else ""
        if d and os.path.isfile(os.path.join(d, "main.py")) and os.path.isdir(os.path.join(d, "input")):
            return os.path.join(d, "input", "imgprompt")
    return ""


def output_base(cfg):
    want = (cfg or {}).get("comfyInput") or _comfy_input()
    want = os.path.expanduser(want).rstrip("/") if want else ""
    if want and os.path.isdir(os.path.dirname(want)):
        return want
    raise FileNotFoundError("ComfyUI input folder not found; set comfyui in config.ini and "
                            "re-run the installer, or set it in Settings -> Image prompts")


def status(cfg):
    configured = (cfg or {}).get("anime2imgDir", "")
    d = find_dir(configured)
    out = {"dir": d, "found": bool(d), "templates": [], "output": None, "error": None,
           "candidates": [] if configured else candidates()}
    if not d:
        out["error"] = (f"no anime2img.py + anime2img_style_terms.txt in {configured}" if configured
                        else "anime2img.py with anime2img_style_terms.txt not found; set its folder "
                             "in Settings -> Image prompts")
        return out
    try:
        out["templates"] = list_templates(d)
        out["output"] = output_base(cfg)
    except Exception as e:
        out["error"] = str(e)
    return out


# ------------------------------------------------------------- PNG reading --
def _png_text(path, keys):
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return {}
    out, pos = {}, 8
    while pos + 8 <= len(data):
        ln, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ in (b"tEXt", b"iTXt", b"zTXt"):
            key, _, rest = body.partition(b"\x00")
            key = key.decode("latin-1", "replace")
            if key not in keys:
                continue
            try:
                if typ == b"tEXt":
                    out[key] = rest.decode("latin-1")
                elif typ == b"zTXt":
                    out[key] = zlib.decompress(rest[1:]).decode("latin-1")
                else:
                    flag, rest = rest[0], rest[2:]
                    _lang, _, rest = rest.partition(b"\x00")
                    _tk, _, rest = rest.partition(b"\x00")
                    out[key] = (zlib.decompress(rest) if flag else rest).decode("utf-8")
            except Exception:
                pass
        if typ == b"IEND":
            break
    return out


def read_output(path):
    t = _png_text(path, ("imgprompt", "parameters"))
    if t.get("imgprompt"):
        try:
            return json.loads(t["imgprompt"])
        except ValueError:
            pass
    if t.get("parameters"):
        pos, _, neg = t["parameters"].partition("\nNegative prompt:")
        return {"base": {"positive": pos.strip(), "negative": neg.split("\nSteps:", 1)[0].strip()},
                "variations": []}
    return None


def _safe(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "image"


def find_output(out_dir, src, char):
    """Newest PNG in out_dir whose embedded source is src."""
    if not os.path.isdir(out_dir):
        return None, None
    real, name = os.path.realpath(src), os.path.basename(src)
    best = None
    prefix = _safe(char).lower()
    for f in os.listdir(out_dir):
        if not f.lower().endswith(".png") or not f.lower().startswith(prefix):
            continue
        p = os.path.join(out_dir, f)
        try:
            m = read_output(p)
        except OSError:
            continue
        if not m:
            continue
        hit = (m.get("source") and os.path.realpath(m["source"]) == real) or (
            not m.get("source") and m.get("orig_name") == name)
        if hit:
            mt = os.path.getmtime(p)
            if best is None or mt > best[0]:
                best = (mt, p, m)
    return (best[1], best[2]) if best else (None, None)


# --------------------------------------------------------------------- run --
def _fingerprint(a2i_dir, tname):
    h = hashlib.sha256()
    for f in (f"anime2img_{tname}.txt", "anime2img_style_terms.txt"):
        try:
            with open(os.path.join(a2i_dir, f), "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
    return h.hexdigest()[:16]


def is_current(prev, a2i_dir, tname, src_px):
    return bool(prev and src_px and prev.get("src_pxhash") == src_px
                and prev.get("fingerprint") == _fingerprint(a2i_dir, tname)
                and prev.get("out") and os.path.isfile(prev["out"]))


def run(cfg, path, tname, chat_url, force=False, prev=None):
    """One sprite through one template via anime2img's command line.
    Returns the summary stored in the sprite's st_expr."""
    d = find_dir((cfg or {}).get("anime2imgDir", ""))
    if not d:
        raise FileNotFoundError("anime2img.py not found; set its folder in Settings -> Image prompts")
    with open(path, "rb") as f:
        src_px = _pixels(f.read())
    if not force and is_current(prev, d, tname, src_px):
        return dict(prev, cached=True)
    base = output_base(cfg)
    tmpl = next((t for t in list_templates(d) if t["name"] == tname), None)
    if tmpl is None:
        raise FileNotFoundError(f"template anime2img_{tname}.txt not found in {d}")
    out_dir = os.path.join(base, _safe(tmpl["output"]))
    char = os.path.basename(os.path.dirname(os.path.abspath(path)).rstrip("/"))
    flags = cli_flags(d)
    args = [os.path.abspath(path)]
    if "-t" in flags or "--template" in flags:
        args += ["-t", tname]
    if "--out" in flags:
        args += ["--out", base]
    if force and "--force" in flags:
        args.append("--force")
    if chat_url and "--no-check" in flags:
        args.append("--no-check")
    with _lock:
        started = time.time()
        try:
            r = subprocess.run([sys.executable, "-u", "-c", RUNNER, d, chat_url or ""] + args,
                               cwd=d, capture_output=True, text=True, timeout=RUN_TIMEOUT,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        except subprocess.TimeoutExpired:
            raise ValueError(f"anime2img took longer than {RUN_TIMEOUT // 60} min; stopped") from None
    log = (r.stdout or "") + (r.stderr or "")
    lines = [ln.strip() for ln in log.splitlines() if ln.strip()]
    failed = [ln for ln in lines if "FAILED" in ln or "raw replies:" in ln]
    if failed:
        raise ValueError(" | ".join(failed)[:900])
    if r.returncode != 0:
        tail = " / ".join(lines[-2:])
        raise ValueError(f"anime2img stopped: {tail[:800] or f'exit code {r.returncode}, no output'}")
    out_path = None
    for ln in log.splitlines():
        m = re.match(r"^\s*\+\s.*?->\s*(\S+\.png)", ln)
        if m:
            cand = m.group(1)
            cand = cand if os.path.isabs(cand) else os.path.join(base, cand)
            if os.path.isfile(cand):
                out_path = cand
    meta = read_output(out_path) if out_path else None
    if meta is None:
        out_path, meta = find_output(out_dir, path, char)
    if meta is None:
        tail = " / ".join(ln.strip() for ln in log.strip().splitlines()[-6:] if ln.strip())
        raise ValueError(f"anime2img exited with code {r.returncode} and wrote no prompt"
                         + (f": {tail[:700]}" if tail else ""))
    cached = os.path.getmtime(out_path) < started
    if force and cached:
        tail = " / ".join(lines[-4:])
        raise ValueError(f"anime2img did not rewrite the prompt: {tail[:600]}")
    s = summary(tname, out_path, meta, cached)
    s.update(src_pxhash=src_px, fingerprint=_fingerprint(d, tname), a2i=d)
    return s


def _pixels(data):
    """Hash of the PNG pixel data only, so embedding lore/tags into the sprite
    (which changes the file bytes) doesn't make its image prompt look stale."""
    try:
        from exprlore_common import pixel_hash
        return pixel_hash(data)
    except Exception:
        return hashlib.sha256(data).hexdigest()


def summary(tname, out_path, meta, cached):
    def text(v):
        return ", ".join(map(str, v)) if isinstance(v, list) else str(v or "")
    return {
        "template": tname, "out": out_path, "cached": cached,
        "ts": meta.get("generated") or dt.datetime.now().isoformat(timespec="seconds"),
        "ages": meta.get("ages") or [],
        "base": {"positive": text((meta.get("base") or {}).get("positive")),
                 "negative": text((meta.get("base") or {}).get("negative"))},
        "variations": [{"label": text(v.get("label")), "change": text(v.get("change")),
                        "positive": text(v.get("positive")), "negative": text(v.get("negative"))}
                       for v in meta.get("variations") or [] if isinstance(v, dict)],
        "removed": ((meta.get("cleanup") or {}).get("removed") or []),
        "notes": text((meta.get("observations") or {}).get("notes")),
    }
