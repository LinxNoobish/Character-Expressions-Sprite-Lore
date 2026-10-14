#!/usr/bin/env python3
"""
spritelore_server.py - local sidecar for the CharacterVault "Sprite Lore" page.

Serves sprite folders, reads/writes the st_expr PNG chunk (same format as
tag_sprites.py / write_lore.py, via exprlore_common.py), and talks to the
OpenAI-compatible backends (KoboldCpp) for VLM tagging and lore generation,
and runs your anime2img.py for photoreal image prompts (spritelore_imgprompt.py).

    python3 spritelore_server.py            # http://127.0.0.1:5181

Files kept next to this script:
    spritelore_defaults.json        shipped defaults, never written
    spritelore_settings.json        your edited settings
    spritelore_settings.prev.json   the settings as they were before the last save
    spritelore_guides.json          per-character lore focus tags + notes
"""

import base64
import copy
import hashlib
import shutil
import datetime as dt
import io
import json
import os
import re
import socket
import sys
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from exprlore_common import (  # noqa: E402
    PNGError, ST_LABELS, cfg_get, extract_json, file_pixel_hash, find_st_settings, local_config,
    find_st_settings_home, label_from_filename, read_card, read_chunks,
    read_meta, read_st_custom_expressions, write_meta,
)

import spritelore_imgprompt as imgp  # noqa: E402
import spritelore_tmerge as tmerge  # noqa: E402

try:
    from PIL import Image
except ImportError:
    print("Pillow is required:  pip install pillow", file=sys.stderr)
    sys.exit(1)

HOST = "127.0.0.1"
PORT = int(cfg_get("port", 5181))
DEFAULTS_FILE = os.path.join(HERE, "spritelore_defaults.json")
SETTINGS_FILE = os.path.join(HERE, "spritelore_settings.json")
SETTINGS_PREV = os.path.join(HERE, "spritelore_settings.prev.json")
GUIDES_FILE = os.path.join(HERE, "spritelore_guides.json")
PROFILES_FILE = os.path.join(HERE, "spritelore_profiles.json")
PROFILES_PREV = os.path.join(HERE, "spritelore_profiles.prev.json")
RENAME_LOG_DIR = os.path.join(HERE, "rename_logs")
HISTORY_FILE = os.path.join(HERE, "spritelore_template_history.json")
PROFILE_KEYS = ("templates", "activeTemplate", "taggerTemplate", "intensity", "directions",
                "defaultRotation", "emotionRules", "tagGroups", "lore")
# system+user hashes of earlier shipped lore templates: a saved template that
# still matches one of these was never edited and is upgraded to the current
# default text automatically.
OLD_DEFAULT_TEMPLATES = {"3091a76bae2efba7", "960de15d1fbb7e10", "5e3263ac431007f0", "f38b63309e6156da"}
ST_CHAR_DIRS = [d for d in dict.fromkeys(
    [cfg_get("st_characters", ""), "~/SillyTavern/data/default-user/characters"]) if d]
IMAGE_EXTS = (".png", ".webp", ".jpg", ".jpeg", ".gif")
ORIGIN_RX = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
MAX_TOKENS_CAP = 4096
THINK_RX = re.compile(r"<think>.*?</think>", re.S | re.I)

_settings_lock = threading.Lock()
_file_locks = {}
_file_locks_guard = threading.Lock()
_thumb_cache = {}
_thumb_lock = threading.Lock()
THUMB_CACHE_MAX = 3000


def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def file_lock(path):
    key = os.path.realpath(path)
    with _file_locks_guard:
        if key not in _file_locks:
            _file_locks[key] = threading.Lock()
        return _file_locks[key]


def write_json_atomic(path, obj):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


# ------------------------------------------------------------------ settings --
def load_defaults():
    """Shipped defaults with this machine's paths and URLs from the
    installer's config laid over them."""
    with open(DEFAULTS_FILE, "r", encoding="utf-8") as f:
        d = json.load(f)
    loc = local_config()
    roots = [loc.get("st_characters")] + list(loc.get("sprite_roots") or [])
    roots = [r for r in dict.fromkeys(roots) if r]
    d["roots"] = roots or ["~/SillyTavern/data/default-user/characters"]
    for b in d["backends"]:
        url = loc.get("vision_url") if b.get("role") == "vlm" else loc.get("lore_url")
        if url:
            b["url"] = url
    if loc.get("comfy_input"):
        d["imgprompt"]["comfyInput"] = loc["comfy_input"]
    if loc.get("anime2img_dir"):
        d["imgprompt"]["anime2imgDir"] = loc["anime2img_dir"]
    return d


def _tmpl_hash(t):
    return hashlib.sha256((t.get("system", "") + "\n" + t.get("user", "")).encode()).hexdigest()[:16]


def complete(user, defaults, keys=None):
    """Fill keys missing from saved settings/profiles with the shipped
    defaults and upgrade lore templates that were never edited."""
    for k, v in defaults.items():
        if (keys is None or k in keys) and k not in user:
            user[k] = copy.deepcopy(v)
    if isinstance(user.get("lore"), dict):
        for k, v in defaults["lore"].items():
            user["lore"].setdefault(k, v)
    if isinstance(user.get("imgprompt"), dict):
        for k, v in defaults["imgprompt"].items():
            user["imgprompt"].setdefault(k, v)
    if isinstance(user.get("intensity"), dict):
        for k, v in defaults["intensity"].items():
            user["intensity"].setdefault(k, copy.deepcopy(v))
    if isinstance(user.get("emotionRules"), dict):
        user["emotionRules"].setdefault("default", copy.deepcopy(defaults["emotionRules"]["default"]))
    if isinstance(user.get("templates"), list):
        fresh = {t["id"]: t for t in defaults["templates"]}
        for i, t in enumerate(user["templates"]):
            if t.get("id") in fresh and _tmpl_hash(t) in OLD_DEFAULT_TEMPLATES:
                user["templates"][i] = copy.deepcopy(fresh[t["id"]])
    return user


def load_settings():
    defaults = load_defaults()
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            user = json.load(f)
    except (FileNotFoundError, ValueError):
        user = None
    if not isinstance(user, dict):
        return copy.deepcopy(defaults)
    return complete(user, defaults)


def load_profiles():
    try:
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    defaults = load_defaults()
    return {k: complete(v, defaults, PROFILE_KEYS) for k, v in d.items() if isinstance(v, dict)}


def profile_name_ok(name):
    name = (name or "").strip()
    if not name or len(name) > 60 or name.lower() == "default":
        raise ValueError("profile name must be 1-60 characters and not 'Default'")
    return name


def save_profile(name, data, rename_from=None):
    name = profile_name_ok(name)
    with _settings_lock:
        try:
            with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                cur = json.load(f)
        except (FileNotFoundError, ValueError):
            cur = {}
        if os.path.exists(PROFILES_FILE):
            shutil.copy2(PROFILES_FILE, PROFILES_PREV)
        if rename_from and rename_from in cur and rename_from != name:
            cur.pop(rename_from)
        cur[name] = {k: copy.deepcopy(data[k]) for k in PROFILE_KEYS if k in data}
        cur[name]["saved"] = now_iso()
        write_json_atomic(PROFILES_FILE, cur)
    return name


def delete_profile(name):
    with _settings_lock:
        try:
            with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                cur = json.load(f)
        except (FileNotFoundError, ValueError):
            cur = {}
        if name not in cur:
            raise FileNotFoundError(f"no profile {name}")
        shutil.copy2(PROFILES_FILE, PROFILES_PREV)
        cur.pop(name)
        write_json_atomic(PROFILES_FILE, cur)


def template_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("versions", [])
    except (FileNotFoundError, ValueError):
        return []


def upgrade_report(templates, choices=None):
    """Per template: merge your edits onto the current shipped default."""
    hist = template_history()
    current = load_defaults()["templates"]
    return [tmerge.upgrade_template(t, hist, current, (choices or {}).get(t.get("id"), {}))
            for t in templates]


def effective(settings, profile):
    """Global settings with a character's profile laid over the instruction
    sections. Backends, folders and image-prompt setup always stay global."""
    if not profile:
        return settings
    p = load_profiles().get(profile)
    if not p:
        return settings
    out = dict(settings)
    for k in PROFILE_KEYS:
        if k in p:
            out[k] = p[k]
    return out


def effective_for(settings, folder):
    return effective(settings, guide_for(folder).get("profile"))


def save_settings(new):
    with _settings_lock:
        if os.path.exists(SETTINGS_FILE):
            os.replace(SETTINGS_FILE, SETTINGS_PREV)
        write_json_atomic(SETTINGS_FILE, new)


def backend_by_id(settings, bid, role):
    bes = settings.get("backends", [])
    want = bid or settings.get("activeVlm" if role == "vlm" else "activeLlm")
    for b in bes:
        if b.get("id") == want:
            return b
    for b in bes:
        if b.get("role") in (role, "both"):
            return b
    raise ValueError(f"no {role.upper()} backend configured")


def load_guides():
    try:
        with open(GUIDES_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def guide_for(folder):
    g = load_guides().get(os.path.realpath(folder)) or {}
    return {"tags": list(g.get("tags") or []), "notes": str(g.get("notes") or ""),
            "instruction": str(g.get("instruction") or ""), "profile": str(g.get("profile") or "")}


def save_guide(folder, **fields):
    with _settings_lock:
        d = load_guides()
        key = os.path.realpath(folder)
        cur = d.get(key) or {}
        for k in ("tags", "notes", "instruction", "profile"):
            if k in fields and fields[k] is not None:
                cur[k] = fields[k]
        cur["ts"] = now_iso()
        d[key] = cur
        write_json_atomic(GUIDES_FILE, d)


# --------------------------------------------------------------------- paths --
def roots(settings):
    out = []
    for r in settings.get("roots") or []:
        p = os.path.abspath(os.path.expanduser(r))
        if os.path.isdir(p):
            out.append(p)
    return out


def check_path(settings, path, must_exist=True):
    if not path:
        raise ValueError("path missing")
    p = os.path.abspath(os.path.expanduser(path))
    real = os.path.realpath(p)
    for r in roots(settings):
        rr = os.path.realpath(r)
        if real == rr or real.startswith(rr + os.sep):
            if must_exist and not os.path.exists(p):
                raise FileNotFoundError(p)
            return p
    raise PermissionError(f"outside the configured roots: {path}")


def list_images(folder):
    out = []
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
    except OSError:
        return out
    for e in entries:
        if e.name.startswith(".exprlore_") or e.name.startswith("."):
            continue
        if e.is_file() and e.name.lower().endswith(IMAGE_EXTS):
            out.append(e.path)
    return out


def find_card(folder):
    base = os.path.abspath(folder.rstrip("/"))
    name = os.path.basename(base)
    cands = [base + ".png", base + ".json"]
    for d in ST_CHAR_DIRS:
        d = os.path.expanduser(d)
        cands += [os.path.join(d, name + ".png"), os.path.join(d, name + ".json")]
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def labels_for(folder):
    labels = list(ST_LABELS)
    path = find_st_settings([folder]) or find_st_settings_home()
    custom = []
    if path:
        try:
            custom = read_st_custom_expressions(path) or []
        except Exception:
            custom = []
    for c in custom:
        if c not in labels:
            labels.append(c)
    return labels, custom


# ---------------------------------------------------------------------- scan --
def image_record(path, labels):
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    st = os.stat(path)
    rec = {"name": name, "path": path, "ext": ext, "isPng": ext == ".png",
           "size": st.st_size, "mtime": int(st.st_mtime),
           "label": label_from_filename(name, labels),
           "meta": None, "tagged": False, "hasLore": False, "stale": False,
           "error": None}
    if ext != ".png":
        return rec
    try:
        meta = read_meta(path)
        px = file_pixel_hash(path)
    except (PNGError, OSError) as e:
        rec["error"] = str(e)
        return rec
    rec["meta"] = meta
    if meta:
        rec["tagged"] = bool(meta.get("vlm")) and meta.get("pxhash") == px
        lore = meta.get("lore")
        if lore and lore.get("text"):
            rec["hasLore"] = True
            rec["stale"] = bool(lore.get("stale")) or (
                bool(lore.get("pxhash")) and lore.get("pxhash") != px) or (
                bool(lore.get("label")) and lore.get("label") != rec["label"])
        if meta.get("vlm") and meta.get("pxhash") != px:
            rec["stale"] = True
    rec["pxhash"] = px
    return rec


def ladder_ids(settings):
    return [x["id"] for x in settings["intensity"]["ladder"]]


def spread_for(settings, n):
    ids = ladder_ids(settings)
    if not ids:
        return ["moderate"] * n
    if n <= 1:
        single = settings["intensity"].get("single")
        return [single if single in ids else ids[len(ids) // 2]]
    table = settings["intensity"].get("spread") or {}
    row = table.get(str(n))
    if isinstance(row, list) and len(row) == n and all(r in ids for r in row):
        return list(row)
    L = len(ids)
    return [ids[round(i * (L - 1) / (n - 1))] for i in range(n)]


def rule_for(settings, label):
    rules = settings.get("emotionRules") or {}
    base = rules.get("default") or {"rule": "", "tags": [], "directions": []}
    r = rules.get(label)
    if not r:
        return {"rule": base.get("rule", ""), "tags": list(base.get("tags") or []),
                "directions": list(base.get("directions") or []), "custom": False}
    return {"rule": r.get("rule") or base.get("rule", ""),
            "tags": list(r.get("tags") or []),
            "directions": list(r.get("directions") or []), "custom": True}


def compute_plan(settings, images):
    """Assign each image an intensity degree and a direction.
    Images sharing an emotion label are spread across the degree ladder
    (ordered by the VLM intensity estimate when present, else by name) and
    rotated through that emotion's directions, so no two read the same.
    Saved per-image overrides (meta.plan) win."""
    dir_ids = [d["id"] for d in settings.get("directions") or []]
    groups = {}
    for im in images:
        groups.setdefault(im["label"], []).append(im)
    order_by = settings["intensity"].get("orderBy", "vlm")

    def natural(name):
        stem = os.path.splitext(name.lower())[0]
        m = re.match(r"^(.*?)-(\d+)$", stem)
        return (m.group(1), int(m.group(2))) if m else (stem, 0)

    def vlm_int(im):
        vi = ((im.get("meta") or {}).get("vlm") or {}).get("intensity")
        return vi if isinstance(vi, (int, float)) else None

    for label, items in groups.items():
        if order_by == "vlm" and all(vlm_int(im) is not None for im in items):
            items.sort(key=lambda im: (vlm_int(im), natural(im["name"])))
        else:
            items.sort(key=lambda im: natural(im["name"]))
        degrees = spread_for(settings, len(items))
        rule = rule_for(settings, label)
        rot = [d for d in (rule["directions"] or settings.get("defaultRotation") or [])
               if d in dir_ids] or (["none"] if "none" in dir_ids else dir_ids[:1])
        for i, im in enumerate(items):
            saved = ((im.get("meta") or {}).get("plan") or {})
            auto_i, auto_d = degrees[i], rot[i % len(rot)]
            im["plan"] = {
                "intensity": saved.get("intensity") or auto_i,
                "direction": saved.get("direction") or auto_d,
                "autoIntensity": auto_i, "autoDirection": auto_d,
                "intensityLocked": bool(saved.get("intensity")),
                "directionLocked": bool(saved.get("direction")),
                "rank": i + 1, "groupSize": len(items),
            }
    return groups


def name_kept(im):
    """True when the user chose to keep this sprite's name over the tagger's
    current answer. Renaming the file or a different tagger answer ends it."""
    meta = im.get("meta") or {}
    k = meta.get("keepName")
    emo = (meta.get("vlm") or {}).get("emotion")
    return isinstance(k, dict) and k.get("label") == im.get("label") and k.get("rejected") == emo


def suggestion(im, labels, min_conf):
    vlm = (im.get("meta") or {}).get("vlm") or {}
    emo = vlm.get("emotion")
    if (im.get("isPng") and im.get("tagged") and emo in labels and emo != im["label"]
            and float(vlm.get("confidence") or 0) >= min_conf and not name_kept(im)):
        return emo
    return None


def do_keep_names(settings, folder, paths=None, clear=False):
    """Reject the tagger's names: mark sprites so their current names stay and
    nothing highlights them. clear=True brings the suggestions back."""
    labels, _ = labels_for(folder)
    want = {os.path.realpath(p) for p in paths} if paths else None
    done = []
    for p in list_images(folder):
        if not p.lower().endswith(".png"):
            continue
        if want is not None and os.path.realpath(p) not in want:
            continue
        rec = image_record(p, labels)
        meta = rec.get("meta")
        if not meta:
            continue
        emo = (meta.get("vlm") or {}).get("emotion")
        if clear:
            if "keepName" not in meta:
                continue
        elif not emo or emo == rec["label"] or name_kept(rec):
            continue
        with file_lock(p):
            meta = read_meta(p) or {}
            if clear:
                meta.pop("keepName", None)
            else:
                meta["keepName"] = {"label": rec["label"], "rejected": emo,
                                    "ts": dt.datetime.now().isoformat(timespec="seconds")}
            write_meta(p, meta)
        done.append(os.path.basename(p))
    return {"changed": done}


def scan_folder(settings, folder):
    labels, custom = labels_for(folder)
    guide = guide_for(folder)
    profiles = load_profiles()
    settings = effective(settings, guide["profile"] if guide["profile"] in profiles else "")
    images = [image_record(p, labels) for p in list_images(folder)]
    min_conf = float(settings["lore"].get("renameMinConfidence", 0.6))
    for im in images:
        im["suggest"] = suggestion(im, labels, min_conf)
        im["kept"] = name_kept(im)
    compute_plan(settings, images)
    card_path = find_card(folder)
    card = None
    if card_path:
        try:
            card = read_card(card_path)
            card["path"] = card_path
        except Exception as e:
            card = {"name": "", "path": card_path, "error": str(e)}
    return {"folder": folder, "name": os.path.basename(folder.rstrip("/")),
            "card": card, "labels": labels, "customLabels": custom,
            "guide": guide, "profile": guide["profile"] if guide["profile"] in profiles else "",
            "profiles": sorted(profiles),
            "effective": {k: settings[k] for k in PROFILE_KEYS}, "images": images}


def list_folders(settings):
    seen, out = set(), []
    for r in roots(settings):
        try:
            entries = sorted(os.scandir(r), key=lambda e: e.name.lower())
        except OSError:
            continue
        for e in entries:
            if e.name.startswith(".") or not e.is_dir():
                continue
            real = os.path.realpath(e.path)
            if real in seen:
                continue
            imgs = list_images(e.path)
            if not imgs:
                continue
            seen.add(real)
            out.append({"name": e.name, "path": e.path, "root": r,
                        "images": len(imgs), "card": bool(find_card(e.path))})
    return out


# ---------------------------------------------------------------- PNG chunks --
def text_chunks(path):
    if not path.lower().endswith(".png"):
        return []
    with open(path, "rb") as f:
        data = f.read()
    out = []
    for ctype, body in read_chunks(data):
        t = ctype.decode("latin-1")
        if t not in ("tEXt", "iTXt", "zTXt"):
            continue
        key, _, rest = body.partition(b"\x00")
        key = key.decode("latin-1", "replace")
        try:
            if t == "tEXt":
                val = rest.decode("latin-1")
            elif t == "zTXt":
                val = zlib.decompress(rest[1:]).decode("latin-1")
            else:
                flag, rest2 = rest[0], rest[2:]
                _lang, _, rest2 = rest2.partition(b"\x00")
                _tk, _, rest2 = rest2.partition(b"\x00")
                val = (zlib.decompress(rest2) if flag else rest2).decode("utf-8", "replace")
        except Exception as e:
            val = f"(undecodable: {e})"
        decoded = None
        if key in ("st_expr", "chara", "ccv3"):
            try:
                decoded = json.loads(base64.b64decode(val).decode("utf-8"))
            except Exception:
                decoded = None
        elif key in ("imgprompt", "prompt", "workflow"):
            try:
                decoded = json.loads(val)
            except Exception:
                decoded = None
        out.append({"type": t, "key": key, "length": len(val),
                    "value": val if len(val) <= 20000 else val[:20000] + " ...",
                    "json": decoded})
    return out


# -------------------------------------------------------------------- images --
def thumbnail_bytes(path, w):
    st = os.stat(path)
    key = (os.path.realpath(path), st.st_mtime_ns, w)
    with _thumb_lock:
        hit = _thumb_cache.get(key)
    if hit:
        return hit
    with Image.open(path) as im:
        im.seek(0)
        im = im.convert("RGBA")
        im.thumbnail((w, w * 2), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=False, compress_level=3)
    data = buf.getvalue()
    with _thumb_lock:
        if len(_thumb_cache) > THUMB_CACHE_MAX:
            _thumb_cache.clear()
        _thumb_cache[key] = data
    return data


def image_data_url(path, max_side=1024, bg=(128, 128, 128)):
    with Image.open(path) as im:
        im.seek(0)
        im = im.convert("RGBA")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        canvas = Image.new("RGBA", im.size, bg + (255,))
        canvas.alpha_composite(im)
        buf = io.BytesIO()
        canvas.convert("RGB").save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ------------------------------------------------------------------- backend --
def endpoint(url, tail="/chat/completions"):
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    if not re.search(r"/v\d+$", url):
        url += "/v1"
    return url + tail


def http_json(url, payload=None, api_key="", timeout=900):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"HTTP {e.code} from {url}: {body}") from None
    except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
        raise ConnectionError(f"cannot reach {url}: {getattr(e, 'reason', e)}") from None


def chat(backend, messages, max_tokens, temperature, schema=None):
    payload = {"messages": messages, "max_tokens": max_tokens,
               "temperature": temperature, "stream": False}
    if backend.get("model"):
        payload["model"] = backend["model"]
    mode = backend.get("jsonMode") or "off"
    if mode == "json_object":
        payload["response_format"] = {"type": "json_object"}
    elif mode == "schema" and schema:
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "spritelore", "strict": True, "schema": schema}}
    resp = http_json(endpoint(backend["url"]), payload, backend.get("apiKey", ""),
                     int(backend.get("timeout") or 900))
    try:
        ch = resp["choices"][0]
        return (ch["message"].get("content") or ""), ch.get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise RuntimeError(f"unexpected backend response: {str(resp)[:500]}")


def chat_json(backend, messages, validate, retries=2, max_tokens=600,
              temperature=0.7, schema=None):
    msgs = list(messages)
    last_err, content, finish = None, "", None
    for _ in range(retries + 1):
        content, finish = chat(backend, msgs, max_tokens, temperature, schema)
        try:
            return validate(extract_json(content)), content
        except ValueError as e:
            last_err = e
            if finish == "length" and max_tokens < MAX_TOKENS_CAP:
                max_tokens = min(MAX_TOKENS_CAP, max_tokens * 2)
                msgs = list(messages)
                continue
            msgs = msgs + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"That response was invalid: {e}. "
                 "Reply again with ONLY the corrected JSON object."}]
    snippet = re.sub(r"\s+", " ", THINK_RX.sub("", content or "")).strip()[:300]
    raise ValueError(f"model output invalid after {retries + 1} attempts: {last_err}"
                     + (f' | last reply: "{snippet}"' if snippet else " | empty reply"))


def test_backend(backend):
    out = {"ok": False, "models": [], "vision": None, "error": None}
    try:
        resp = http_json(endpoint(backend["url"], "/models"), None,
                         backend.get("apiKey", ""), 20)
        out["models"] = [m.get("id") for m in resp.get("data", []) if m.get("id")]
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)
        return out
    if backend.get("role") in ("vlm", "both"):
        answers = {}
        for color, rgb in (("red", (220, 20, 20)), ("blue", (20, 40, 220))):
            buf = io.BytesIO()
            Image.new("RGB", (256, 256), rgb).save(buf, "PNG")
            uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
            msgs = [{"role": "user", "content": [
                {"type": "text", "text": "What single color fills this image? Answer with one word."},
                {"type": "image_url", "image_url": {"url": uri}}]}]
            try:
                answers[color] = THINK_RX.sub("", chat(dict(backend, jsonMode="off"),
                                                       msgs, 300, 0.0)[0]).strip()
            except Exception as e:
                out["error"] = f"vision check failed: {e}"
                return out
        ok = all(c in a.lower() for c, a in answers.items())
        out["vision"] = {"ok": ok, "answers": answers}
    return out


# ------------------------------------------------------------------- tagging --
TAG_CATEGORIES = ("appearance", "expression", "action", "objects", "setting")
DIRECTED = ("viewer", "self", "away", "object", "unclear")


def fill(text, mapping):
    for k, v in mapping.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def _clean_list(v):
    if isinstance(v, str):
        v = v.split(",")
    if not isinstance(v, list):
        return []
    out, seen = [], set()
    for t in v:
        t = str(t).strip().strip(".").lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:20]


def tag_validator(labels):
    def validate(obj):
        emo = str(obj.get("emotion", "")).strip().lower()
        if emo not in labels:
            raise ValueError(f'"emotion" must be one of the allowed labels, got "{emo}"')
        try:
            conf = max(0.0, min(1.0, float(obj.get("confidence", 0))))
        except (TypeError, ValueError):
            raise ValueError('"confidence" must be a number between 0 and 1')
        try:
            inten = int(round(float(obj.get("intensity", 3))))
        except (TypeError, ValueError):
            inten = 3
        inten = max(1, min(5, inten))
        da = str(obj.get("directed_at", "unclear")).strip().lower()
        da = da if da in DIRECTED else "unclear"
        cats = {c: _clean_list(obj.get(c)) for c in TAG_CATEGORIES}
        if not cats["expression"] or not cats["action"]:
            raise ValueError('"expression" and "action" must be non-empty lists of tags')
        if not cats["appearance"]:
            raise ValueError('"appearance" must list hair, eyes and clothing tags')
        flat, seen = [], set()
        for c in TAG_CATEGORIES:
            for t in cats[c]:
                if t not in seen:
                    seen.add(t)
                    flat.append(t)
        return {"emotion": emo, "confidence": round(conf, 3), "intensity": inten,
                "directed_at": da, "tags": flat, "categories": cats,
                "description": str(obj.get("description", "")).strip()}
    return validate


def do_tag(settings, path, backend_id=None, hint=False):
    if not path.lower().endswith(".png"):
        path = do_convert(path)
    folder = os.path.dirname(path)
    labels, _ = labels_for(folder)
    backend = backend_by_id(settings, backend_id, "vlm")
    settings = effective_for(settings, folder)
    char = os.path.basename(folder.rstrip("/"))
    name = os.path.basename(path)
    cur = label_from_filename(name, labels)
    tt = settings["taggerTemplate"]
    m = {"CHAR": char, "LABELS": ", ".join(labels),
         "HINT": f'\nThe file is currently labeled "{cur}"; this may be wrong.' if hint else ""}
    messages = [{"role": "system", "content": fill(tt["system"], m)},
                {"role": "user", "content": [
                    {"type": "text", "text": fill(tt["user"], m)},
                    {"type": "image_url", "image_url": {"url": image_data_url(path)}}]}]
    result, raw = chat_json(backend, messages, tag_validator(labels),
                            retries=int(settings["lore"].get("retries", 2)),
                            max_tokens=int(backend.get("maxTokens") or 900),
                            temperature=float(backend.get("temperature") or 0.1))
    with file_lock(path):
        meta = read_meta(path) or {}
        px = file_pixel_hash(path)
        meta["pxhash"] = px
        meta.setdefault("orig_name", name)
        meta["char"] = char
        meta["label"] = cur
        meta["vlm"] = dict(result, model=backend.get("model") or backend.get("name", ""),
                           ts=now_iso())
        lore = meta.get("lore")
        if lore and lore.get("pxhash") and lore.get("pxhash") != px:
            lore["stale"] = True
        write_meta(path, meta)
    kept = name_kept({"meta": meta, "label": cur})
    return {"vlm": meta["vlm"], "raw": raw, "mismatch": result["emotion"] != cur and not kept, "path": path}


def do_rename(settings, path, new_label):
    folder = os.path.dirname(path)
    labels, _ = labels_for(folder)
    new_label = re.sub(r"[^a-z0-9_ -]", "", (new_label or "").strip().lower()).replace(" ", "_")
    if not new_label:
        raise ValueError("empty label")
    ext = os.path.splitext(path)[1].lower()
    existing = {n.lower() for n in os.listdir(folder)}
    cand, n = f"{new_label}{ext}", 1
    while cand.lower() in existing:
        cand = f"{new_label}-{n}{ext}"
        n += 1
    return _move(path, os.path.join(folder, cand), labels)


def do_rename_exact(settings, path, new_name):
    """Rename to exactly the name typed (extension kept). Refuses a name that
    another file already uses instead of adding a suffix."""
    folder = os.path.dirname(path)
    labels, _ = labels_for(folder)
    ext = os.path.splitext(path)[1]
    name = (new_name or "").strip()
    if name.lower().endswith(ext.lower()):
        name = name[: -len(ext)].rstrip()
    if not name:
        raise ValueError("empty name")
    if name.startswith(".") or any(c in name for c in '/\\\0') or len(name) > 200:
        raise ValueError(f"not a usable file name: {name!r}")
    cand = name + ext
    if cand == os.path.basename(path):
        return path
    for other in os.listdir(folder):
        if other.lower() == cand.lower() and other != os.path.basename(path):
            raise FileExistsError(f"{other} already exists in this folder")
    return _move(path, os.path.join(folder, cand), labels)


def _move(path, dst, labels):
    ext = os.path.splitext(path)[1].lower()
    cand = os.path.basename(dst)
    with file_lock(path):
        if os.path.basename(path).lower() == cand.lower():
            tmp = os.path.join(os.path.dirname(path), f".exprlore_mv_{os.getpid()}{ext}")
            os.rename(path, tmp)
            os.rename(tmp, dst)
        else:
            os.rename(path, dst)
    if ext == ".png":
        with file_lock(dst):
            meta = read_meta(dst) or {}
            meta.setdefault("orig_name", os.path.basename(path))
            meta["label"] = label_from_filename(cand, labels)
            write_meta(dst, meta)
    return dst


def plan_suggested(settings, folder, paths=None):
    """[(path, new_name, emotion, confidence)] for sprites whose tagged emotion
    differs from their file label. Sprites that keep their names reserve them
    first; movers get label.png, label-1.png ... like tag_sprites.py --rename."""
    settings = effective_for(settings, folder)
    labels, _ = labels_for(folder)
    min_conf = float(settings["lore"].get("renameMinConfidence", 0.6))
    want = {os.path.realpath(p) for p in paths} if paths else None
    movers, reserved = [], {n.lower() for n in os.listdir(folder)}
    for p in list_images(folder):
        if not p.lower().endswith(".png"):
            continue
        if want is not None and os.path.realpath(p) not in want:
            continue
        rec = image_record(p, labels)
        emo = suggestion(rec, labels, min_conf)
        if emo:
            movers.append((p, emo, float(rec["meta"]["vlm"].get("confidence") or 0)))
            reserved.discard(os.path.basename(p).lower())
    plan = []
    for p, emo, conf in movers:
        cand, n = f"{emo}.png", 1
        while cand.lower() in reserved:
            cand = f"{emo}-{n}.png"
            n += 1
        reserved.add(cand.lower())
        plan.append((p, cand, emo, conf))
    return plan


def do_rename_suggested(settings, folder, paths=None):
    plan = plan_suggested(settings, folder, paths)
    if not plan:
        return {"moves": [], "log": None}
    labels, _ = labels_for(folder)
    staged = []
    for i, (p, cand, emo, conf) in enumerate(plan):
        tmp = os.path.join(folder, f".exprlore_mv_{os.getpid()}_{i}.png")
        with file_lock(p):
            os.rename(p, tmp)
        staged.append((tmp, p, cand, emo, conf))
    moves = []
    for tmp, p, cand, emo, conf in staged:
        dst = os.path.join(folder, cand)
        os.rename(tmp, dst)
        with file_lock(dst):
            meta = read_meta(dst) or {}
            meta.setdefault("orig_name", os.path.basename(p))
            meta["label"] = label_from_filename(cand, labels)
            write_meta(dst, meta)
        moves.append({"folder": os.path.abspath(folder), "from": os.path.basename(p),
                      "to": cand, "emotion": emo, "confidence": conf})
    os.makedirs(RENAME_LOG_DIR, exist_ok=True)
    log = os.path.join(RENAME_LOG_DIR, f"exprlore_renames_{dt.datetime.now():%Y%m%d_%H%M%S_%f}.json")
    with open(log, "w", encoding="utf-8") as f:
        json.dump(moves, f, indent=2)
    return {"moves": moves, "log": log}


def last_rename_log(folder):
    if not os.path.isdir(RENAME_LOG_DIR):
        return None
    folder = os.path.abspath(folder)
    for f in sorted(os.listdir(RENAME_LOG_DIR), reverse=True):
        if not f.endswith(".json") or f.endswith(".undone.json"):
            continue
        p = os.path.join(RENAME_LOG_DIR, f)
        try:
            with open(p, encoding="utf-8") as fh:
                recs = json.load(fh)
        except (OSError, ValueError):
            continue
        if recs and all(r.get("folder") == folder for r in recs):
            return p
    return None


def undo_renames(folder):
    log = last_rename_log(folder)
    if not log:
        raise FileNotFoundError("no renames to undo for this folder")
    with open(log, encoding="utf-8") as f:
        recs = json.load(f)
    labels, _ = labels_for(folder)
    staged, skipped = [], []
    for i, r in enumerate(recs):
        src = os.path.join(r["folder"], r["to"])
        if not os.path.exists(src):
            skipped.append(f"{r['to']} is gone")
            continue
        tmp = os.path.join(r["folder"], f".exprlore_undo_{os.getpid()}_{i}.png")
        os.rename(src, tmp)
        staged.append((tmp, r))
    restored = []
    for tmp, r in staged:
        dst = os.path.join(r["folder"], r["from"])
        if os.path.exists(dst):
            os.rename(tmp, os.path.join(r["folder"], r["to"]))
            skipped.append(f"{r['from']} exists, kept {r['to']}")
            continue
        os.rename(tmp, dst)
        with file_lock(dst):
            meta = read_meta(dst) or {}
            meta["label"] = label_from_filename(r["from"], labels)
            write_meta(dst, meta)
        restored.append({"from": r["to"], "to": r["from"]})
    os.replace(log, log[:-5] + ".undone.json")
    return {"restored": restored, "skipped": skipped}


ORIGINALS_DIR = ".spritelore_originals"
CONVERTIBLE = (".webp", ".jpg", ".jpeg", ".gif")


def do_convert(path):
    """webp/jpg/gif -> PNG next to it (first frame for gif). The original is
    moved to <folder>/.spritelore_originals/, which SillyTavern ignores.
    If <name>.png already exists the new file gets the next free -N suffix,
    which SillyTavern treats as another sprite of the same expression."""
    folder = os.path.dirname(path)
    stem, ext = os.path.splitext(os.path.basename(path))
    if ext.lower() not in CONVERTIBLE:
        raise ValueError(f"{os.path.basename(path)} is not webp/jpg/gif")
    m = re.match(r"^(.*?)-(\d+)$", stem)
    head = m.group(1) if m else stem
    existing = {n.lower() for n in os.listdir(folder)}
    cand, n = f"{stem}.png", 1
    while cand.lower() in existing:
        cand = f"{head}-{n}.png"
        n += 1
    new = os.path.join(folder, cand)
    with file_lock(path):
        with Image.open(path) as im:
            im.seek(0)
            im.convert("RGBA").save(new + ".tmp", "PNG")
        os.replace(new + ".tmp", new)
        keep = os.path.join(folder, ORIGINALS_DIR)
        os.makedirs(keep, exist_ok=True)
        dst = os.path.join(keep, os.path.basename(path))
        if os.path.exists(dst):
            dst = os.path.join(keep, f"{stem}.{int(dt.datetime.now().timestamp())}{ext}")
        os.replace(path, dst)
    return new


def do_convert_folder(folder, include_gif=False):
    exts = CONVERTIBLE if include_gif else tuple(e for e in CONVERTIBLE if e != ".gif")
    done, errors, skipped = [], [], []
    for p in list_images(folder):
        ext = os.path.splitext(p)[1].lower()
        if ext == ".png":
            continue
        if ext not in exts:
            skipped.append(os.path.basename(p))
            continue
        try:
            done.append({"from": os.path.basename(p), "to": os.path.basename(do_convert(p))})
        except Exception as e:
            errors.append(f"{os.path.basename(p)}: {e}")
    return {"converted": done, "skipped": skipped, "errors": errors,
            "originals": os.path.join(folder, ORIGINALS_DIR) if done else None}


# ---------------------------------------------------------------------- lore --
def card_block(card, max_chars):
    if not card:
        return "(no character card found next to this folder)"
    name = card.get("name") or ""

    def clip(s):
        s = (s or "").replace("{{char}}", name).replace("<BOT>", name).strip()
        return s if len(s) <= max_chars else s[:max_chars].rsplit(" ", 1)[0] + " ..."
    parts = [f"Name: {name}"]
    for key in ("description", "personality", "scenario"):
        if card.get(key):
            parts.append(f"{key.capitalize()}:\n{clip(card[key])}")
    return "\n\n".join(parts)


def visual_block(vlm):
    if not vlm:
        return "Visual tags: (image not tagged yet; rely on the emotion and card)"
    cats = vlm.get("categories")
    lines = []
    if cats:
        lines = [f"{c.capitalize()}: {', '.join(cats[c])}" for c in TAG_CATEGORIES if cats.get(c)]
    else:
        lines = [f"Visual tags: {', '.join(vlm.get('tags', []))}"]
    if vlm.get("description"):
        lines.append(f"Visual description: {vlm['description']}")
    return "\n".join(lines)


def lore_validator(max_chars, n_kw):
    def validate(obj):
        lore = str(obj.get("lore", "")).strip()
        if not lore:
            raise ValueError('"lore" must be a non-empty string')
        if len(lore) > max_chars:
            raise ValueError(f'"lore" is too long ({len(lore)} chars); keep it under {max_chars}')
        kws = obj.get("keywords")
        if not isinstance(kws, list):
            raise ValueError(f'"keywords" must be a list of {n_kw} strings')
        kws = [str(k).strip() for k in kws if str(k).strip()]
        if len(kws) != n_kw:
            raise ValueError(f'"keywords" must contain exactly {n_kw} items, got {len(kws)}')
        return {"lore": lore, "keywords": kws}
    return validate


def lore_schema(max_chars, n_kw):
    return {"type": "object", "additionalProperties": False,
            "required": ["lore", "keywords"],
            "properties": {
                "lore": {"type": "string", "minLength": 20, "maxLength": max_chars},
                "keywords": {"type": "array", "minItems": n_kw, "maxItems": n_kw,
                             "items": {"type": "string", "minLength": 2, "maxLength": 40}}}}


def by_id(items, iid):
    for x in items or []:
        if x.get("id") == iid:
            return x
    return None


def build_lore_prompt(settings, ctx, path, req):
    """-> (messages, info) for one image. req keys: templateId, intensity,
    direction, tags, pending {path: text}."""
    img = next((i for i in ctx["images"] if os.path.realpath(i["path"]) == os.path.realpath(path)), None)
    if img is None:
        raise FileNotFoundError(path)
    lore_cfg = settings["lore"]
    tmpl = by_id(settings["templates"], req.get("templateId")) \
        or by_id(settings["templates"], settings.get("activeTemplate")) \
        or settings["templates"][0]
    card = ctx["card"]
    char = (card or {}).get("name") or ctx["name"]
    plan = dict(img["plan"])
    if req.get("intensity"):
        plan["intensity"] = req["intensity"]
    if req.get("direction"):
        plan["direction"] = req["direction"]
    ladder = by_id(settings["intensity"]["ladder"], plan["intensity"]) or \
        {"id": plan["intensity"], "name": plan["intensity"], "desc": ""}
    direction = by_id(settings["directions"], plan["direction"]) or \
        {"id": plan["direction"], "name": plan["direction"], "desc": ""}
    label = img["label"]
    rule = rule_for(settings, label)
    meta = img.get("meta") or {}
    pending = req.get("pending") or {}

    def lore_text(im):
        p = pending.get(im["path"])
        if p:
            return p
        lo = (im.get("meta") or {}).get("lore") or {}
        return lo.get("text") or ""

    sib_lines = []
    if lore_cfg.get("siblingAware", True):
        for im in ctx["images"]:
            if im is img or im["label"] != label:
                continue
            ip = im["plan"]
            lo = (im.get("meta") or {}).get("lore") or {}
            si = lo.get("intensity") if lo.get("text") and im["path"] not in pending else None
            sd = lo.get("direction") if si else None
            si, sd = si or ip["intensity"], sd or ip["direction"]
            dn = (by_id(settings["directions"], sd) or {}).get("name", sd)
            sib_lines.append(f"- {si}, aimed at {dn}: {lore_text(im) or '(not written yet)'}")
    siblings = ""
    if sib_lines:
        siblings = (f"OTHER {label.upper()} SPRITES OF THIS CHARACTER (each is a different "
                    "degree or target; make this entry clearly distinct from them):\n"
                    + "\n".join(sib_lines) + "\n")

    prior_lines = []
    for im in ctx["images"]:
        if im is img or im["label"] == label:
            continue
        t = lore_text(im)
        if t:
            kws = ((im.get("meta") or {}).get("lore") or {}).get("keywords") or []
            prior_lines.append(t + (f"  [keywords: {', '.join(kws)}]" if kws else ""))
    prior_lines = prior_lines[-int(lore_cfg.get("priorMax", 20)):]
    prior = ""
    if prior_lines:
        prior = ("ENTRIES ALREADY WRITTEN FOR THIS CHARACTER (do not repeat these facts or "
                 "keywords):\n" + "\n".join(f"- {p}" for p in prior_lines) + "\n")

    guide = ctx["guide"]
    focus_tags = list(dict.fromkeys((guide.get("tags") or []) + (req.get("tags") or [])))
    emo_tags = rule["tags"]
    focus = ""
    if focus_tags:
        focus += "LORE FOCUS (steer the entry toward these themes): " + ", ".join(focus_tags) + "\n"
    if emo_tags:
        focus += f"{label.upper()} TAGS (use at least one): " + ", ".join(emo_tags) + "\n"
    notes = ""
    if guide.get("notes", "").strip():
        notes = "AUTHOR NOTES FOR THIS CHARACTER\n" + guide["notes"].strip() + "\n"
    instruction = ""
    if guide.get("instruction", "").strip():
        instruction = ("CUSTOM INSTRUCTION FOR THIS CHARACTER (follow it over the general rules)\n"
                       + guide["instruction"].strip() + "\n")

    n_kw = int(lore_cfg.get("keywords", 4))
    mapping = {
        "CARD": card_block(card, int(lore_cfg.get("cardChars", 6000))),
        "NOTES": notes, "INSTRUCTION": instruction, "IMAGE": img["name"], "EMOTION": label,
        "INTENSITY": ladder.get("name") or ladder["id"],
        "INTENSITY_DESC": ladder.get("desc", ""),
        "RANK": str(plan["rank"]), "GROUP_SIZE": str(plan["groupSize"]),
        "DIRECTION": direction.get("name") or direction["id"],
        "DIRECTION_DESC": direction.get("desc", ""),
        "VISUAL": visual_block(meta.get("vlm")),
        "EMOTION_RULE": rule["rule"], "SIBLINGS": siblings, "FOCUS": focus,
        "PRIOR": prior, "LORE_WORDS": str(lore_cfg.get("words", 80)),
        "KEYWORD_COUNT": str(n_kw),
        "KEYWORD_SLOTS": ", ".join(['"..."'] * n_kw),
        "CHAR": char,
    }

    def render(t):
        t = fill(t, mapping)
        for _ in range(2):
            t = fill(t, mapping)
        t = t.replace("{{char}}", char)
        return re.sub(r"\n{3,}", "\n\n", t).strip()

    user_t = tmpl["user"]
    for key in ("NOTES", "INSTRUCTION"):
        if mapping[key] and "{{" + key + "}}" not in user_t:
            cut = user_t.rfind("Return JSON")
            block = "{{" + key + "}}\n"
            user_t = user_t[:cut] + block + user_t[cut:] if cut >= 0 else user_t + "\n\n" + block
    tmpl = dict(tmpl, user=user_t)
    messages = [{"role": "system", "content": render(tmpl["system"])},
                {"role": "user", "content": render(tmpl["user"])}]
    info = {"plan": plan, "label": label, "template": tmpl["id"],
            "tags": focus_tags, "emotionTags": emo_tags,
            "temperature": tmpl.get("temperature")}
    return messages, info, img


def do_generate(settings, req):
    path = check_path(settings, req.get("path"))
    if not path.lower().endswith(".png"):
        raise ValueError("only PNG sprites can hold lore (convert it first)")
    ctx = scan_folder(settings, os.path.dirname(path))
    backend = None if req.get("previewOnly") else backend_by_id(settings, req.get("backendId"), "llm")
    settings = effective_for(settings, os.path.dirname(path))
    messages, info, img = build_lore_prompt(settings, ctx, path, req)
    if req.get("previewOnly"):
        return {"messages": messages, **info}
    lore_cfg = settings["lore"]
    n_kw = int(lore_cfg.get("keywords", 4))
    max_chars = int(lore_cfg.get("maxChars", 900))
    temp = info["temperature"] if info["temperature"] is not None else backend.get("temperature", 0.8)
    out, raw = chat_json(backend, messages, lore_validator(max_chars, n_kw),
                         retries=int(lore_cfg.get("retries", 2)),
                         max_tokens=int(backend.get("maxTokens") or 700),
                         temperature=float(temp), schema=lore_schema(max_chars, n_kw))
    lore = {"text": out["lore"], "keywords": out["keywords"], "label": info["label"],
            "intensity": info["plan"]["intensity"], "direction": info["plan"]["direction"],
            "template": info["template"], "profile": ctx.get("profile", ""), "tags": info["tags"],
            "model": backend.get("model") or backend.get("name", ""), "ts": now_iso(),
            "src": "generated", "reviewed": False}
    result = {"lore": lore, "raw": raw, "messages": messages, **info, "written": False}
    if req.get("write"):
        result["meta"] = write_lore(path, lore)
        result["written"] = True
    return result


def do_imgprompt(settings, req):
    path = check_path(settings, req.get("path"))
    cfg = settings.get("imgprompt") or {}
    tname = req.get("template") or cfg.get("activeTemplate") or "exact"
    backend = backend_by_id(settings, req.get("backendId") or cfg.get("backend") or None, "vlm")
    prev = None
    if path.lower().endswith(".png"):
        prev = ((read_meta(path) or {}).get("imgprompts") or {}).get(tname)
    summary = imgp.run(cfg, path, tname, endpoint(backend["url"]), bool(req.get("force")), prev)
    if path.lower().endswith(".png") and not (summary.get("cached") and summary == prev):
        with file_lock(path):
            meta = read_meta(path) or {}
            meta.setdefault("pxhash", file_pixel_hash(path))
            meta.setdefault("orig_name", os.path.basename(path))
            prompts = meta.get("imgprompts") if isinstance(meta.get("imgprompts"), dict) else {}
            prompts[tname] = summary
            meta["imgprompts"] = prompts
            write_meta(path, meta)
    return summary


def write_lore(path, lore):
    with file_lock(path):
        meta = read_meta(path) or {}
        px = file_pixel_hash(path)
        meta.setdefault("pxhash", px)
        meta.setdefault("orig_name", os.path.basename(path))
        meta.setdefault("char", os.path.basename(os.path.dirname(path).rstrip("/")))
        lore = dict(lore)
        lore["pxhash"] = px
        lore.pop("stale", None)
        meta["lore"] = lore
        write_meta(path, meta)
        return meta


def do_save_meta(settings, req):
    path = check_path(settings, req.get("path"))
    if not path.lower().endswith(".png"):
        raise ValueError("only PNG sprites can hold metadata (convert it first)")
    meta = req.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("meta must be an object")
    with file_lock(path):
        px = file_pixel_hash(path)
        meta.setdefault("pxhash", px)
        meta.setdefault("orig_name", os.path.basename(path))
        lore = meta.get("lore")
        if isinstance(lore, dict) and req.get("touchLore"):
            if not str(lore.get("text", "")).strip():
                meta.pop("lore", None)
            else:
                lore["pxhash"] = px
                lore.pop("stale", None)
                lore["ts"] = now_iso()
                lore.setdefault("src", "manual")
        write_meta(path, meta)
    return read_meta(path)


def do_save_plan(settings, req):
    path = check_path(settings, req.get("path"))
    if not path.lower().endswith(".png"):
        raise ValueError("only PNG sprites can hold metadata")
    with file_lock(path):
        meta = read_meta(path) or {}
        meta.setdefault("pxhash", file_pixel_hash(path))
        plan = meta.get("plan") or {}
        for k in ("intensity", "direction"):
            if k in req:
                if req[k]:
                    plan[k] = req[k]
                else:
                    plan.pop(k, None)
        if plan:
            meta["plan"] = plan
        else:
            meta.pop("plan", None)
        write_meta(path, meta)
    return meta


# ---------------------------------------------------------------------- HTTP --
class Handler(BaseHTTPRequestHandler):
    server_version = "spritelore/1"

    def log_message(self, fmt, *args):
        if os.environ.get("SPRITELORE_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _origin_ok(self):
        o = self.headers.get("Origin")
        return o is None or bool(ORIGIN_RX.match(o)) or o == "null"

    def _cors(self):
        o = self.headers.get("Origin")
        if o and (ORIGIN_RX.match(o) or o == "null"):
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")

    def _send(self, code, body, ctype="application/json", cache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_OPTIONS(self):
        if not self._origin_ok():
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def _route(self, method):
        if not self._origin_ok():
            return self._send(403, {"error": "origin not allowed"})
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            settings = load_settings()
            r = (method, u.path)
            if method == "GET" and u.path in ("/", "/index.html", "/spritelore_ui.html"):
                ui = os.path.join(HERE, "spritelore_ui.html")
                if not os.path.isfile(ui):
                    return self._send(404, "spritelore_ui.html is missing next to the server; re-run the installer.",
                                      "text/plain; charset=utf-8")
                with open(ui, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if r == ("GET", "/api/health"):
                return self._send(200, {"ok": True, "version": 1, "dir": HERE})
            if r == ("GET", "/api/settings"):
                return self._send(200, {"settings": settings, "defaults": load_defaults(),
                                        "hasPrev": os.path.exists(SETTINGS_PREV)})
            if r == ("PUT", "/api/settings"):
                new = self._body().get("settings")
                if not isinstance(new, dict) or "templates" not in new:
                    raise ValueError("settings object missing")
                save_settings(new)
                return self._send(200, {"settings": load_settings()})
            if r == ("POST", "/api/settings/reset"):
                section = self._body().get("section")
                d = load_defaults()
                if section:
                    if section not in d:
                        raise ValueError(f"unknown section {section}")
                    settings[section] = copy.deepcopy(d[section])
                    if section == "backends":
                        settings["activeVlm"], settings["activeLlm"] = d["activeVlm"], d["activeLlm"]
                    if section == "templates":
                        settings["activeTemplate"] = d["activeTemplate"]
                else:
                    settings = copy.deepcopy(d)
                save_settings(settings)
                return self._send(200, {"settings": load_settings()})
            if r == ("POST", "/api/settings/undo"):
                if not os.path.exists(SETTINGS_PREV):
                    raise FileNotFoundError("no previous settings saved")
                with _settings_lock:
                    cur = SETTINGS_FILE + ".swap"
                    if os.path.exists(SETTINGS_FILE):
                        os.replace(SETTINGS_FILE, cur)
                    os.replace(SETTINGS_PREV, SETTINGS_FILE)
                    if os.path.exists(cur):
                        os.replace(cur, SETTINGS_PREV)
                return self._send(200, {"settings": load_settings()})
            if r == ("GET", "/api/folders"):
                return self._send(200, {"roots": roots(settings), "folders": list_folders(settings)})
            if r == ("GET", "/api/folder"):
                folder = check_path(settings, q.get("path"))
                if not os.path.isdir(folder):
                    raise NotADirectoryError(folder)
                return self._send(200, scan_folder(settings, folder))
            if r == ("GET", "/api/image"):
                path = check_path(settings, q.get("path"))
                w = int(q.get("w") or 0)
                if w > 0:
                    return self._send(200, thumbnail_bytes(path, min(w, 2048)), "image/png", cache=True)
                ext = os.path.splitext(path)[1].lower().lstrip(".")
                ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, f"image/{ext}")
                with open(path, "rb") as f:
                    return self._send(200, f.read(), ctype, cache=True)
            if r == ("GET", "/api/chunks"):
                path = check_path(settings, q.get("path"))
                return self._send(200, {"chunks": text_chunks(path)})
            if r == ("PUT", "/api/meta"):
                b = self._body()
                return self._send(200, {"meta": do_save_meta(settings, b)})
            if r == ("PUT", "/api/plan"):
                return self._send(200, {"meta": do_save_plan(settings, self._body())})
            if r == ("PUT", "/api/guide"):
                b = self._body()
                folder = check_path(settings, b.get("folder"))
                fields = {}
                if "tags" in b:
                    fields["tags"] = [str(t).strip() for t in (b.get("tags") or []) if str(t).strip()]
                for k in ("notes", "instruction", "profile"):
                    if k in b:
                        fields[k] = str(b.get(k) or "")
                if fields.get("profile") and fields["profile"] not in load_profiles():
                    raise ValueError(f"no profile {fields['profile']}")
                save_guide(folder, **fields)
                return self._send(200, {"guide": guide_for(folder)})
            if r == ("POST", "/api/templates/upgrade"):
                b = self._body()
                templates = b.get("templates")
                if not isinstance(templates, list):
                    raise ValueError("templates list missing")
                return self._send(200, {"reports": upgrade_report(templates, b.get("choices") or {})})
            if r == ("GET", "/api/profiles"):
                return self._send(200, {"profiles": load_profiles()})
            if r == ("PUT", "/api/profile"):
                b = self._body()
                name = save_profile(b.get("name"), b.get("data") or {}, b.get("renameFrom"))
                if b.get("renameFrom") and b["renameFrom"] != name:
                    with _settings_lock:
                        g = load_guides()
                        for v in g.values():
                            if v.get("profile") == b["renameFrom"]:
                                v["profile"] = name
                        write_json_atomic(GUIDES_FILE, g)
                return self._send(200, {"name": name, "profiles": load_profiles()})
            if r == ("POST", "/api/profile/delete"):
                delete_profile(self._body().get("name", ""))
                return self._send(200, {"profiles": load_profiles()})
            if r == ("POST", "/api/rename-suggested"):
                b = self._body()
                folder = check_path(settings, b.get("folder"))
                paths = [check_path(settings, p) for p in (b.get("paths") or [])] or None
                if b.get("dryRun"):
                    plan = plan_suggested(settings, folder, paths)
                    return self._send(200, {"moves": [{"from": os.path.basename(p), "to": c, "emotion": e,
                                                       "confidence": cf} for p, c, e, cf in plan]})
                return self._send(200, do_rename_suggested(settings, folder, paths))
            if r == ("POST", "/api/keep-names"):
                b = self._body()
                folder = check_path(settings, b.get("folder"))
                paths = [check_path(settings, p) for p in (b.get("paths") or [])] or None
                return self._send(200, do_keep_names(settings, folder, paths, bool(b.get("clear"))))
            if r == ("POST", "/api/rename-undo"):
                b = self._body()
                folder = check_path(settings, b.get("folder"))
                return self._send(200, undo_renames(folder))
            if r == ("GET", "/api/rename-undo"):
                folder = check_path(settings, q.get("folder"))
                return self._send(200, {"available": bool(last_rename_log(folder))})
            if r == ("POST", "/api/tag"):
                b = self._body()
                path = check_path(settings, b.get("path"))
                return self._send(200, do_tag(settings, path, b.get("backendId"), bool(b.get("hint"))))
            if r == ("POST", "/api/generate"):
                return self._send(200, do_generate(settings, self._body()))
            if r == ("POST", "/api/lore"):
                b = self._body()
                path = check_path(settings, b.get("path"))
                return self._send(200, {"meta": write_lore(path, b.get("lore") or {})})
            if r == ("POST", "/api/rename"):
                b = self._body()
                path = check_path(settings, b.get("path"))
                if b.get("name") is not None:
                    return self._send(200, {"path": do_rename_exact(settings, path, b.get("name"))})
                return self._send(200, {"path": do_rename(settings, path, b.get("label"))})
            if r == ("POST", "/api/convert"):
                b = self._body()
                path = check_path(settings, b.get("path"))
                return self._send(200, {"path": do_convert(path)})
            if r == ("POST", "/api/convert-folder"):
                b = self._body()
                folder = check_path(settings, b.get("folder"))
                if not os.path.isdir(folder):
                    raise NotADirectoryError(folder)
                return self._send(200, do_convert_folder(folder, bool(b.get("includeGif"))))
            if r == ("GET", "/api/imgprompt/status"):
                return self._send(200, imgp.status(settings.get("imgprompt") or {}))
            if r == ("PUT", "/api/imgprompt/template"):
                b = self._body()
                st = imgp.status(settings.get("imgprompt") or {})
                if not st["dir"]:
                    raise FileNotFoundError(st["error"])
                imgp.save_template(st["dir"], b.get("name", ""), b.get("text", ""), bool(b.get("create")))
                return self._send(200, imgp.status(settings.get("imgprompt") or {}))
            if r == ("POST", "/api/imgprompt/restore"):
                st = imgp.status(settings.get("imgprompt") or {})
                if not st["dir"]:
                    raise FileNotFoundError(st["error"])
                imgp.restore_template(st["dir"], self._body().get("name", ""))
                return self._send(200, imgp.status(settings.get("imgprompt") or {}))
            if r == ("POST", "/api/imgprompt/run"):
                return self._send(200, do_imgprompt(settings, self._body()))
            if r == ("POST", "/api/backend/test"):
                return self._send(200, test_backend(self._body().get("backend") or {}))
            return self._send(404, {"error": f"no route {method} {u.path}"})
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except (FileNotFoundError, NotADirectoryError) as e:
            return self._send(404, {"error": f"not found: {e}"})
        except ConnectionError as e:
            return self._send(502, {"error": str(e)})
        except (ValueError, FileExistsError, RuntimeError, PNGError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    load_defaults()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"spritelore server on http://{HOST}:{PORT}  (data: {HERE})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
