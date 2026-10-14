#!/usr/bin/env python3
"""
anime2img.py - turn anime / illustrated / 3D character images into
photorealistic image-gen prompts embedded in a copy of the image.

anime2img_dirs.txt pairs folders with prompt templates:

    ~/SillyTavern/data/default-user/characters = exact
    ~/Pictures/references                      = exact, creative

How one image is processed (ONE vision-model call per template):

  1. The image goes to the KoboldCpp vision model with a JSON schema
     (response_format json_schema -> KoboldCpp turns it into a grammar).
     The model can only produce that form: a free "notes" field first, the
     age as a number inside the template's range, one field per part of the
     photo (face, eyes, hair, outfit, expression, pose, setting, lighting,
     camera ...), a short "avoid" tag list, and exactly N variations that
     hold ONLY the fields they change. Every string has a length cap, so the
     reply always terminates.
  2. This script cleans every field (anime / 2D / 3D / shader terms from
     anime2img_style_terms.txt removed or swapped, "not X" chunks moved to
     the negative) and builds the prompts itself:
        base      = opener ("photograph of a 22 year old young woman") +
                    the fields in template order
        variation = the base with its changed fields swapped in
     so the base text is never re-typed by the model.

A PNG copy is written per template to
ComfyUI/input/imgprompt/<template>/<Folder>_001.png with:

  iTXt "imgprompt"   full JSON (notes, ages, fields, base, variations, cleanup)
  iTXt "parameters"  A1111-style base prompt, readable by other tools

Originals are never modified. When an image fails, a copy of it and every
raw model reply (with the reason each was rejected) go to the failed folder
(failed_dir in the Sprite Lore config, else ./failed next to this script)
as <Folder>_001_<template>.png / .txt; they are removed once it succeeds.

Paths and the vision backend URL come from the Sprite Lore config
(~/.config/spritelore/spritelore_local.json) when it exists; without it the
standard locations below are used.

  python3 anime2img.py                        # everything in anime2img_dirs.txt
  python3 anime2img.py --force                # redo images already done
  python3 anime2img.py --only mature          # one template only
  python3 anime2img.py SOME/FOLDER -t creative
  python3 anime2img.py --clean "anime style, 1girl, 3d render, red hair"
                                              # test the cleanup on a prompt

Already-processed images are skipped unless the source image, the template
or the style terms file changed.
"""

import argparse
import base64
import datetime as dt
import hashlib
import io
import json
import os
import re
import struct
import sys
import urllib.error
import urllib.request
import zlib

try:
    from PIL import Image
except ImportError:
    print("Pillow is required:  pip install pillow", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------- defaults --
def _local_config():
    path = os.environ.get("SPRITELORE_CONFIG") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "spritelore", "spritelore_local.json")
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


LOCAL = _local_config()


def _chat_url(u):
    u = u.rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    return (u if re.search(r"/v\d+$", u) else u + "/v1") + "/chat/completions"


# Vision backend: the first of these that answers the vision check is used
# (configured vision URL first, then the KoboldCpp default port).
KOBOLD_URLS = list(dict.fromkeys(
    ([_chat_url(LOCAL["vision_url"])] if LOCAL.get("vision_url") else [])
    + ["http://127.0.0.1:5001/v1/chat/completions"]))
KOBOLD_URL = KOBOLD_URLS[0]
# Qwen3-VL Instruct recommended samplers (Qwen model card / Unsloth guide:
# temp 0.7, top_p 0.8, top_k 20, presence_penalty 1.5, repetition 1.0).
# Templates set their own temperature and can override any of these.
SAMPLER_DEFAULTS = {"top_p": 0.8, "top_k": 20, "min_p": 0.0,
                    "presence_penalty": 1.5, "rep_pen": 1.0}
ST_CHAR_ROOT = os.path.expanduser(LOCAL.get("st_characters")
                                  or "~/SillyTavern/data/default-user/characters")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DIRS_FILE = os.path.join(SCRIPT_DIR, "anime2img_dirs.txt")
TERMS_FILE = os.path.join(SCRIPT_DIR, "anime2img_style_terms.txt")
DEFAULT_TEMPLATE = "exact"
OUT_SUBDIR = "imgprompt"            # inside ComfyUI/input/
META_KEY = "imgprompt"
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
ATTEMPTS = 2        # with the schema enforced, a second try is the exception
TOKEN_CAP = 8192
# Images the model couldn't produce valid prompts for: a PNG copy of the
# source plus a .txt with every raw model reply and why it was rejected.
FAILED_DIR = os.path.expanduser(LOCAL.get("failed_dir") or os.path.join(SCRIPT_DIR, "failed"))
COMFY_CANDIDATES = [
    LOCAL.get("comfyui", ""), os.environ.get("COMFYUI_DIR", ""),
    "~/ComfyUI", "~/comfyui", "~/AI/ComfyUI", "~/ai/ComfyUI", "~/apps/ComfyUI",
    "~/Programs/ComfyUI", "~/ComfyUI_windows_portable/ComfyUI", "/opt/ComfyUI",
]
SEARCH_ROOTS = [("~", 4)]

# Per-field length caps in characters, by detail level: (min, max) for a
# required field. The grammar enforces them, so a reply cannot run away.
FIELD_LEN = {"low": (8, 140), "medium": (20, 240), "high": (40, 380)}
NOTES_LEN = (60, 900)       # free reasoning written before the fields
LABEL_LEN = 30
CHANGE_LEN = 140
AVOID_ITEMS, AVOID_LEN = 8, 40
VAR_AVOID_ITEMS = 4


# ------------------------------------------------------------- comfy path --
def is_comfy(d):
    return (os.path.isfile(os.path.join(d, "main.py"))
            and os.path.isdir(os.path.join(d, "custom_nodes"))
            and os.path.isdir(os.path.join(d, "comfy")))


def find_comfy():
    for c in COMFY_CANDIDATES:
        if c and is_comfy(os.path.expanduser(c)):
            return os.path.abspath(os.path.expanduser(c))
    for root, depth in SEARCH_ROOTS:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        base = root.rstrip("/").count("/")
        for d, subs, _ in os.walk(root):
            if d.count("/") - base >= depth:
                subs[:] = []
                continue
            subs[:] = [s for s in subs if not s.startswith(".")
                       and s not in ("node_modules", "venv", ".venv", "models")]
            if is_comfy(d):
                return d
    return None


# --------------------------------------------------------------- template --
def load_template(path):
    if not os.path.isfile(path):
        sys.exit(f"Template not found: {path} (keep it next to anime2img.py)")
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    sections, cur = {}, None
    for line in raw.splitlines(keepends=True):
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"^===\s*(.+?)\s*===\s*$", line)
        if m:
            cur = m.group(1).lower()
            sections[cur] = []
        elif cur is not None:
            sections[cur].append(line)
    sections = {k: "".join(v).strip() for k, v in sections.items()}
    settings = {}
    for ln in sections.get("settings", "").splitlines():
        ln = ln.split("#", 1)[0].strip()
        if ":" in ln:
            k, v = ln.split(":", 1)
            settings[k.strip().lower()] = v.strip()
    for need in ("system", "user", "variations"):
        if need not in sections:
            sys.exit(f"Template is missing the === {need} === section")
    return sections, settings, hashlib.sha256(raw.encode()).hexdigest()[:16]


def fill(text, mapping):
    for k, v in mapping.items():
        text = text.replace("{{" + k + "}}", v)
    return text


# ------------------------------------------------------------------ PNG IO --
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def iter_chunks(data):
    if data[:8] != PNG_SIG:
        raise ValueError("not a PNG")
    pos = 8
    while pos + 8 <= len(data):
        ln, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + ln]
        yield typ, body, data[pos:pos + 12 + ln]
        pos += 12 + ln
        if typ == b"IEND":
            break


def chunk(typ, body):
    return (struct.pack(">I", len(body)) + typ + body
            + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF))


def itxt(key, text):
    return chunk(b"iTXt", key.encode("latin-1") + b"\x00\x00\x00\x00\x00"
                 + text.encode("utf-8"))


def text_key(typ, body):
    if typ in (b"tEXt", b"iTXt", b"zTXt"):
        return body.split(b"\x00", 1)[0].decode("latin-1", "replace")
    return None


def text_value(typ, body):
    key, rest = body.split(b"\x00", 1)
    if typ == b"tEXt":
        return rest.decode("latin-1")
    if typ == b"zTXt":
        return zlib.decompress(rest[1:]).decode("latin-1")
    flag, _method = rest[0], rest[1]
    rest = rest[2:]
    _lang, rest = rest.split(b"\x00", 1)
    _tkey, rest = rest.split(b"\x00", 1)
    return (zlib.decompress(rest) if flag else rest).decode("utf-8")


def read_meta(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        for typ, body, _ in iter_chunks(data):
            if text_key(typ, body) == META_KEY:
                return json.loads(text_value(typ, body))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def png_bytes(src_path):
    """Source as PNG bytes; PNGs are passed through untouched (pixels and
    existing chunks kept), other formats are converted."""
    with open(src_path, "rb") as f:
        data = f.read()
    if data[:8] == PNG_SIG:
        return data
    with Image.open(io.BytesIO(data)) as im:
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()


def embed(png, entries):
    """entries: {key: text}. Replaces existing text chunks with those keys."""
    out, placed = [PNG_SIG], False
    for typ, body, raw in iter_chunks(png):
        if text_key(typ, body) in entries:
            continue
        if not placed and typ in (b"IDAT", b"IEND"):   # before pixel data, so
            for k, v in entries.items():               # readers see it without
                out.append(itxt(k, v))                 # decoding the image
            placed = True
        out.append(raw)
    return b"".join(out)


# ---------------------------------------------------------------- backend --
def image_data_uri(path, max_side):
    from PIL import ImageOps
    with Image.open(path) as im:
        im.load()
        im = ImageOps.exif_transpose(im)      # upright camera/phone photos
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (128, 128, 128, 255))
            im = Image.alpha_composite(bg, im)
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side), Image.LANCZOS)   # long side, aspect kept
        buf = io.BytesIO()
        im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()



def chat(messages, temperature, max_tokens, samplers=None, schema=None):
    payload = {"model": "koboldcpp", "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens}
    payload.update(samplers or SAMPLER_DEFAULTS)
    if schema:   # KoboldCpp converts this to a GBNF grammar (koboldcpp.py)
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "anime2img", "strict": True, "schema": schema}}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(KOBOLD_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        resp = json.load(r)
    try:
        ch = resp["choices"][0]
        return (ch["message"].get("content") or ""), ch.get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise RuntimeError(f"unexpected backend response: {str(resp)[:400]}")


def strip_think(text):
    """Drop reasoning blocks. The chat template of Thinking models often
    injects the opening <think> itself, so only </think> shows up."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    elif "<think>" in text:              # reasoning never finished
        text = text.split("<think>", 1)[0]
    return text


def extract_json(text):
    text = strip_think(text)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found in your reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                frag = text[start:i + 1]
                try:
                    return json.loads(frag)
                except json.JSONDecodeError:
                    return json.loads(re.sub(r",\s*([}\]])", r"\1", frag))
    raise ValueError("JSON object in your reply is not closed")


def _close_json(frag):
    """Close an unterminated JSON fragment: open string, then every open
    { / [. Returns the parsed object or None."""
    stack, in_str, esc = [], False, False
    for c in frag:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[":
            stack.append(c)
        elif c in "}]" and stack:
            stack.pop()
    if esc:
        frag = frag[:-1]
    if in_str:
        frag += '"'
    frag = re.sub(r"[,:\s]+$", "", frag)
    frag += "".join("}" if b == "{" else "]" for b in reversed(stack))
    try:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", frag))
    except json.JSONDecodeError:
        return None


def salvage_json(text):
    """Best-effort parse of a reply that hit max_tokens (usually a model
    stuck repeating its negative list). Cuts back to earlier commas until
    the closed fragment parses; whatever was finished survives."""
    text = strip_think(text)
    start = text.find("{")
    if start < 0:
        return None
    frag = text[start:]
    for _ in range(80):
        obj = _close_json(frag)
        if isinstance(obj, dict):
            return obj
        cut = frag.rfind(",")
        if cut <= 0:
            return None
        frag = frag[:cut]
    return None


def as_text(v):
    if isinstance(v, list):
        v = ", ".join(str(x).strip() for x in v if str(x).strip())
    return str(v or "").strip()



# ------------------------------------------------------------ style terms --
STOP = set("""a an the and or of in on with as by at to for from is are was be
being been she he her his him they them their it its this that these those
style styles styled art artwork aesthetic look looks looking like type kind
drawn rendered depicted done made features feature design image picture
version also very inspired esque based influenced form manner fashion
mode format medium quality effect effects detailed highly no not without
using via through into onto""".split())
TRAIL = re.compile(r"(?:\s+|^)(?:a|an|the|in|with|as|like|of|by|and|or|using|via|"
                   r"through|into|onto|from|to|for|at|on|is|are|was|no|not|without|free|zero|any|"
                   r"drawn|rendered|depicted|"
                   r"illustrated|shown|done|made|styled|presented|created)\s*$", re.I)


def term_rx(term):
    parts = [re.escape(p) for p in re.split(r"[\s_-]+", term.strip().lower()) if p]
    return re.compile(r"(?<![a-z0-9])" + r"[\s_-]*".join(parts) + r"(?![a-z0-9])", re.I)


def norm_chunk(c):
    c = c.strip().strip("()[]{}").strip()
    c = re.sub(r":\s*[\d.]+$", "", c).strip()
    return re.sub(r"[\s_-]+", " ", c.lower())


class StyleTerms:
    def __init__(self, path):
        if not os.path.isfile(path):
            sys.exit(f"Style terms file not found: {path}")
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        self.sha = hashlib.sha256(raw.encode()).hexdigest()[:16]
        sec, cur = {}, None
        for line in raw.splitlines():
            st = line.strip()
            if not st or st.startswith("#"):
                continue
            m = re.match(r"^===\s*(.+?)\s*===$", st)
            if m:
                cur = m.group(1).lower()
                sec.setdefault(cur, [])
            elif cur:
                sec[cur].append(st)
        rules = []
        self.quiet = set()
        for name in ("remove", "remove_only"):
            for line in sec.get(name, []):
                for t in line.split(","):
                    if t.strip():
                        rules.append((t.strip().lower(), ""))
                        if name == "remove_only":
                            self.quiet.add(t.strip().lower())
        for line in sec.get("replace", []):
            if "=>" not in line:
                sys.exit(f"style terms: replace line needs '=>': {line}")
            src, dst = line.split("=>", 1)
            for t in src.split(","):
                if t.strip():
                    rules.append((t.strip().lower(), dst.strip()))
        seen, self.rules = set(), []
        for t, d in sorted(rules, key=lambda r: len(r[0]), reverse=True):
            if t not in seen:
                seen.add(t)
                self.rules.append((term_rx(t), d, t))
        self.always = [t.strip() for line in sec.get("always_negative", [])
                       for t in line.split(",") if t.strip()]
        self.protect = {norm_chunk(t) for line in sec.get("protect", [])
                        for t in line.split(",") if t.strip()}
        for _, d, t in self.rules:   # an equivalent must not be a style term
            if d and any(rx.search(d) for rx, dd, tt in self.rules if not dd):
                print(f"warning: replacement for '{t}' ('{d}') contains a "
                      "style term and will be stripped again")

    def clean_positive(self, text, extra=()):
        """-> (cleaned text, [terms found]). extra: more terms to remove
        (the template's force_negative)."""
        rules = self.rules + [(term_rx(t), "", t.lower()) for t in extra]
        found = []
        parts = re.split(r"(\.(?=\s|$)|[,;\n])", text)
        out = []
        for i in range(0, len(parts), 2):
            seg = parts[i]
            delim = parts[i + 1] if i + 1 < len(parts) else ""
            new = seg
            for rx, rep, term in rules:
                if rx.search(new):
                    found.append(term)
                    new = rx.sub(rep, new)
            if new != seg:
                new = re.sub(r"\b(?:in|with|as|like)\s+(?:an?\s+|the\s+)?"
                             r"(?:style|look|aesthetic)(?:\s+of)?\s*$", "", new.strip(), flags=re.I)
                new = re.sub(r"\b(?:in the style of|in a style of|style of|drawn in|"
                             r"rendered in|done in|depicted in|made in)\s*$", "", new, flags=re.I)
                new = re.sub(r"\(\s*(?::\s*[\d.]+)?\s*\)|\[\s*\]|\{\s*\}", "", new)
                prev = None
                while prev != new:          # drop dangling "an", "with", ...
                    prev = new
                    new = TRAIL.sub("", new).strip()
                words = [w for w in re.findall(r"[a-z0-9']+", new.lower())
                         if w not in STOP]
                if not words:
                    new = ""
            out.append((new.strip(), delim))
        txt = ""
        for seg, delim in out:
            if seg:
                txt += seg + (delim if delim != "\n" else ",") + " "
        txt = re.sub(r"\s+", " ", txt)
        txt = re.sub(r"\s+([,;])", r"\1", txt)
        txt = re.sub(r"([,;])(?:\s*[,;])+", r"\1", txt)
        txt = txt.strip().rstrip(",;").strip()
        uniq = []
        for f in found:
            if f not in uniq and f not in self.quiet:
                uniq.append(f)
        return txt, uniq

    def clean_negative(self, text, found, forced=(), positive=""):
        chunks = []
        for c in re.split(r"[,;\n]", text):
            c = re.sub(r"^(?:not|no|never|without|avoid(?:ing)?)\s+", "",
                       c.strip(), flags=re.I).strip(" .")
            if re.fullmatch(r"(?:not|no|never|without|avoid\w*)?", c, re.I):
                continue                      # bare negator left by a cut-off
            if norm_chunk(c) not in {norm_chunk(x) for x in chunks}:
                chunks.append(c)
        chunks = chunks[:NEG_CAP]     # runaway lists: keep the first ones
        have, out, dropped = set(), [], []
        for c in chunks + list(found) + list(forced) + self.always:
            n = norm_chunk(c)
            if not n or n in have:
                continue
            if n in self.protect or (positive and term_rx(n).search(positive)):
                dropped.append(c)
                continue
            have.add(n)
            out.append(c)
        return ", ".join(out), dropped



NEGATION = re.compile(r"(^|[,;.])\s*(?:not|never|avoid|avoiding)\s+"
                      r"((?:[^\s,;.]+\s*){1,5})(?=[,;.]|$)", re.I)


def move_negations(text):
    """'..., not blurry, not old woman, ...' in a positive -> moved out
    (they'd be read as things to draw). Only short chunks (<=5 words), so a
    real sentence starting with 'Not' survives."""
    moved = []

    def sub(m):
        moved.append(m.group(2).strip())
        return m.group(1)
    new = NEGATION.sub(sub, text)
    if not moved:
        return text, []
    new = re.sub(r"\s*([,;])(?:\s*[,;])+", r"\1", new)
    new = re.sub(r"([.;])\s*,", r"\1", new)
    new = re.sub(r"^\s*[,;]\s*|\s*[,;]\s*$", "", new)
    return re.sub(r"\s{2,}", " ", new).strip(), moved


def dedupe_chunks(text):
    """Drop exact repeats of a sentence or comma chunk (repetition loops)."""
    parts = re.split(r"(\.(?=\s|$)|[,;])", text)
    seen, out = set(), []
    for i in range(0, len(parts), 2):
        seg, delim = parts[i], parts[i + 1] if i + 1 < len(parts) else ""
        key = re.sub(r"\W+", " ", seg.lower()).strip()
        if key and key in seen:
            continue
        seen.add(key)
        out.append(seg + delim)
    return "".join(out).strip()




# --------------------------------------------------------- prompt building --
NEG_CAP = 40   # max tags kept from the default + model negatives (before forced)


def new_log():
    return {"removed": [], "negative_dropped": []}


def add_tags(text, tags):
    low = text.lower()
    extra = [t for t in tags if t.lower() not in low]
    if not extra:
        return text
    text = text.rstrip(", ")
    sep = " " if text.endswith((".", "!", "?")) else ", "
    return text + sep + ", ".join(extra)


def clean_field(text, cfg, log):
    """One field -> (clean text, terms that belong in the negative)."""
    text = as_text(text)
    if not text:
        return "", []
    text, moved = move_negations(dedupe_chunks(text))
    if moved:
        nm = log.setdefault("negations_moved", [])
        nm.extend(m for m in moved if m not in nm)
    found = []
    terms = cfg.get("terms")
    if terms:
        text, found = terms.clean_positive(text, cfg.get("force_neg", []))
    for b in cfg["banned"]:                  # strip, don't reject
        rx = re.compile(rf"(?<![a-z]){re.escape(b)}(?![a-z])", re.I)
        if rx.search(text):
            text = rx.sub("", text)
            bl = log.setdefault("banned_removed", [])
            if b not in bl:
                bl.append(b)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,.;])", r"\1", text).strip(" ,;")
    for t in moved + found:
        if t not in log["removed"]:
            log["removed"].append(t)
    return text, moved + found


def sentence(s):
    s = s.strip().rstrip(",;").strip()
    if not s:
        return ""
    s = s[0].upper() + s[1:]
    return s if s[-1] in ".!?" else s + "."


def compose(opener, parts, style):
    parts = [p for p in parts if p]
    if style == "tags":
        return ", ".join([opener] + [p.rstrip(". ") for p in parts])
    if style == "mixed":
        return sentence(opener) + " " + ", ".join(p.rstrip(". ") for p in parts)
    return " ".join([sentence(opener)] + [sentence(p) for p in parts])


def opener(age, noun):
    if age is None:
        return f"photograph of a {noun}"
    art = "an" if str(age).startswith("8") or age in (11, 18) else "a"
    return f"photograph of {art} {age} year old {noun}"


def build_negative(cfg, avoid, found, positive):
    terms = cfg.get("terms")
    tags = [a for a in (as_text(x) for x in avoid or []) if a]
    text = ", ".join([cfg["default_neg"]] + tags) if cfg["default_neg"] else ", ".join(tags)
    if terms:
        return terms.clean_negative(text, found, cfg.get("force_neg", []), positive)
    return add_tags(text, list(found) + list(cfg.get("force_neg", []))), []


def read_age(obj, cfg, log):
    lo, hi = cfg["age_lo"], cfg["age_hi"]
    v = obj.get("age", obj.get("ages"))
    if isinstance(v, list):
        v = v[0] if v else None
    try:
        age = int(round(float(str(v).strip())))
    except (TypeError, ValueError):
        age = None
    if age is None:
        if cfg["age_range"]:
            age = round((lo + hi) / 2)
            log["age_filled"] = f"{age} (middle of {lo}-{hi})"
        return age
    if age < lo:
        raise ValueError(f'"age" {age} is below {lo}; give an age between {lo} '
                         f'and {hi} and describe the person at that age')
    if age > hi:
        log["age_clamped"] = f"{age} -> {hi}"
        age = hi
    return age


def build(obj, t):
    """Model form -> base + variations, all text composed here."""
    cfg = t.cfg
    if not isinstance(obj, dict):
        raise ValueError("reply must be a JSON object")
    missing = [f for f, opt in t.fields if not opt and f not in obj]
    if missing:
        raise ValueError(f"fields missing from the form: {', '.join(missing)}")
    log = new_log()
    age = read_age(obj, cfg, log)

    if t.subject_noun:
        sp, _ = clean_field(obj.get("species"), cfg, log)
        words = t.subject_noun.split()
        noun = (" ".join(words[:-1] + [sp, words[-1]])
                if sp and sp.lower() not in ("human", "none", "n/a") else t.subject_noun)
    else:
        noun, _ = clean_field(obj.get("subject"), cfg, log)
        noun = noun or "person"
    head = opener(age, noun)

    fields, found_by = {}, {}
    for f, _opt in t.fields:
        fields[f], found_by[f] = clean_field(obj.get(f), cfg, log)
    empty = [f for f, opt in t.fields if not opt and not fields[f]]
    if empty:
        log["empty_after_cleanup"] = empty

    def make(fs, avoid, found):
        pos = add_tags(compose(head, [fs[f] for f, _ in t.fields], t.style),
                       cfg.get("force_pos", []))
        neg, dropped = build_negative(cfg, avoid, found, pos)
        for d in dropped:
            if d not in log["negative_dropped"]:
                log["negative_dropped"].append(d)
        return pos, neg

    base_found = [x for f in fields for x in found_by[f]]
    base_avoid = obj.get("avoid") if isinstance(obj.get("avoid"), list) else []
    bpos, bneg = make(fields, base_avoid, base_found)
    base = {"positive": bpos, "negative": bneg, "fields": dict(fields)}

    variations, seen = [], {bpos.lower()}
    raw_vars = obj.get("variations") if isinstance(obj.get("variations"), list) else []
    for i, v in enumerate(raw_vars[:t.n], 1):
        if not isinstance(v, dict):
            continue
        fs, changed, vfound = dict(fields), {}, list(base_found)
        for f in t.vary:
            val, fnd = clean_field(v.get(f), cfg, log)
            if val and val.lower() != fields[f].lower():
                fs[f] = changed[f] = val
                vfound += fnd
        if not changed:
            log.setdefault("variations_dropped", []).append(f"{i}: changed nothing")
            continue
        vavoid = [a for a in (v.get("avoid") or []) if isinstance(a, str)]
        pos, neg = make(fs, base_avoid + vavoid, vfound)
        if pos.lower() in seen:
            log.setdefault("variations_dropped", []).append(f"{i}: duplicate")
            continue
        seen.add(pos.lower())
        label = re.sub(r"[^a-z0-9 _-]", "", as_text(v.get("label")).lower())[:40].strip()
        variations.append({"label": label or f"variation {i}",
                           "change": as_text(v.get("change")),
                           "positive": pos, "negative": neg, "fields": changed})
    if len(variations) < t.n:
        log["variations_short"] = f"{len(variations)} of {t.n}"
    return {"ages": [age] if age is not None else [], "base": base,
            "variations": variations, "cleanup": log,
            "observations": {"notes": as_text(obj.get("notes"))},
            "opener": head}


def ask(messages, schema, validator, cfg, trace=None, stage=""):
    """One constrained call; a retry only if the reply is unusable.
    trace receives one record per model call for the failure dump."""
    msgs = list(messages)
    max_tokens = cfg["max_tokens"]
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        rec = {"stage": stage, "attempt": attempt, "max_tokens": max_tokens,
               "finish": None, "raw": "", "error": None}
        if trace is not None:
            trace.append(rec)
        try:
            content, finish = chat(msgs, cfg["temperature"], max_tokens,
                                   cfg.get("samplers"), schema)
        except urllib.error.HTTPError as e:   # e.g. prompt over context size
            try:
                body = e.read().decode("utf-8", "replace")[:4000]
            except Exception:
                body = ""
            rec["error"] = f"backend HTTP {e.code}: {body or e.reason}"
            raise RuntimeError(f"{stage}: backend returned HTTP {e.code}")
        rec["raw"], rec["finish"] = content, finish
        try:
            obj = extract_json(content)
        except (ValueError, json.JSONDecodeError) as e:
            obj = salvage_json(content) if finish == "length" else None
            if obj is None:
                last = (f"reply cut off at max_tokens={max_tokens}" if finish == "length"
                        else f"not valid JSON: {e}")
                rec["error"] = last
                msgs = list(messages) + [{"role": "user", "content":
                    "Your previous reply was not a complete JSON object. Reply "
                    "with only the filled-in form, keeping every field short."}]
                max_tokens = min(max_tokens * 2, TOKEN_CAP)
                continue
            rec["note"] = "reply was cut off; the finished part was used"
        try:
            return validator(obj)
        except ValueError as e:
            last = str(e)
            rec["error"] = last
            msgs = list(messages) + [
                {"role": "assistant", "content": content[:8000]},
                {"role": "user", "content":
                    f"That reply was invalid: {e}. Reply with only the "
                    "corrected, complete JSON object."}]
    raise RuntimeError(f"{stage}: model output invalid after {ATTEMPTS} "
                       f"attempts: {last}")


def run_template(t, src, char, label, trace=None):
    return ask(t.messages(src, char, label), t.schema, lambda o: build(o, t),
               t.cfg, trace, "image -> form")


def vision_check():
    im = Image.new("RGB", (64, 64), (220, 20, 20))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    msg = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": uri}},
        {"type": "text", "text": "What single color fills this image? "
                                 "Answer with one word."}]}]
    content, _ = chat(msg, 0.0, 1024)
    content = strip_think(content)
    return "red" in content.lower(), content.strip()[:80]



# ------------------------------------------------------------------- jobs --
def safe(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "image"


def file_label(name):
    stem = os.path.splitext(os.path.basename(name))[0]
    return re.sub(r"-\d+$", "", stem)


def folder_parts(path, root):
    """Folder names an image came from, used for its new name.
    ST root:            characters/Tessa/joy.png        -> [Tessa]
                        characters/Tessa/alt/joy.png    -> [Tessa, alt]
                        characters/Tessa.png (avatar)   -> [Tessa]
    any other folder:   Outfits/joy.png                 -> [Outfits]
                        Outfits/Tessa/joy.png           -> [Outfits, Tessa]
    a single image:     its parent folder"""
    d = os.path.dirname(os.path.abspath(path))
    if os.path.isfile(root):
        return [os.path.basename(d)]
    rel = os.path.relpath(d, root)
    parts = [] if rel == "." else rel.split(os.sep)
    if os.path.realpath(root) != os.path.realpath(ST_CHAR_ROOT):
        parts = [os.path.basename(root.rstrip("/"))] + parts
    return parts or [os.path.splitext(os.path.basename(path))[0]]


def load_dirs(path):
    """[(folder, [templates])] from anime2img_dirs.txt; creates a default."""
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{ST_CHAR_ROOT} = {DEFAULT_TEMPLATE}\n")
        print(f"Created {path} (edit it to pick folders and templates)")
    entries = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                sys.exit(f"{os.path.basename(path)} line {n}: expected "
                         f"'folder = template', got: {line}")
            folder, tmpls = line.rsplit("=", 1)
            folder = os.path.abspath(os.path.expanduser(folder.strip().strip('"')))
            names = [t.strip() for t in tmpls.split(",") if t.strip()]
            if not names:
                sys.exit(f"{os.path.basename(path)} line {n}: no template named")
            entries.append((folder, names))
    return entries


def collect(entries, skip_dirs):
    """entries: [(folder or image, [templates])].
    Returns [(image, folder parts, [templates])]. An image under several
    listed folders gets the most specific (deepest) folder's templates;
    symlinked duplicates are removed."""
    skip = [os.path.realpath(d) for d in skip_dirs]
    owners = sorted(((os.path.realpath(r), r, t) for r, t in entries),
                    key=lambda e: len(e[0]), reverse=True)

    def owner(img):
        """(real root, templates) of the deepest listed folder holding img,
        compared by real path so symlinked folders resolve correctly."""
        d = os.path.realpath(os.path.dirname(os.path.abspath(img)))
        for real, root, tmpls in owners:
            if os.path.isfile(root):
                if real == os.path.realpath(img):
                    return real, tmpls
            elif d == real or d.startswith(real + os.sep):
                return real, tmpls
        return None, None

    found = []
    for root, _ in entries:
        if os.path.isfile(root):
            if root.lower().endswith(IMG_EXT):
                found.append(root)
            continue
        if not os.path.isdir(root):
            try:
                os.makedirs(root)
                print(f"created empty folder (drop images in it): {root}")
            except OSError as e:
                print(f"warning: folder not found and can't be created, skipped: {root} ({e})")
            continue
        for d, subs, files in os.walk(root, followlinks=True):
            rd = os.path.realpath(d)
            if any(rd == s or rd.startswith(s + os.sep) for s in skip):
                subs[:] = []
                continue
            subs[:] = sorted(x for x in subs if not x.startswith("."))
            found += [os.path.join(d, f) for f in sorted(files)
                      if f.lower().endswith(IMG_EXT)]
    seen, jobs = set(), []
    for img in found:
        r = os.path.realpath(img)
        if r in seen:
            continue
        seen.add(r)
        root, tmpls = owner(img)
        if root:
            jobs.append((img, folder_parts(r, root), tmpls))
            continue
        # reached through a symlink that points outside every listed folder:
        # name it by the path it was found under
        for rr, t in sorted(entries, key=lambda e: len(e[0]), reverse=True):
            if os.path.abspath(img).startswith(rr.rstrip("/") + os.sep):
                jobs.append((img, folder_parts(img, rr), t))
                break
    return jobs


def failure_paths(stem, tname):
    b = os.path.join(FAILED_DIR, safe(f"{stem}_{tname}"))
    return b + ".png", b + ".txt"


def dump_failure(src, stem, tname, rel, err, trace):
    """Flat files in FAILED_DIR: <Folder>_001_<template>.png (copy of the
    source) and .txt (every raw reply in order, with the rejection reason)."""
    os.makedirs(FAILED_DIR, exist_ok=True)
    png_path, txt_path = failure_paths(stem, tname)
    try:
        with open(png_path, "wb") as f:
            f.write(png_bytes(src))
    except Exception as e:
        png_path = f"(couldn't copy image: {e})"
    lines = [f"source:   {os.path.abspath(src)}",
             f"folder:   {rel}",
             f"template: {tname}",
             f"time:     {dt.datetime.now().isoformat(timespec='seconds')}",
             f"backend:  {KOBOLD_URL}",
             f"result:   FAILED - {err}", ""]
    if not trace:
        lines.append("(no model reply was received)")
    for r in trace:
        status = ("ACCEPTED" + (f" ({r['note']})" if r.get("note") else "")
                  if r["error"] is None else f"REJECTED: {r['error']}")
        lines += ["=" * 78,
                  f"{r['stage']}  attempt {r['attempt']}/{ATTEMPTS}  "
                  f"finish_reason={r['finish']}  max_tokens={r['max_tokens']}  "
                  f"reply_chars={len(r['raw'] or '')}",
                  status, "-" * 78, r["raw"] or "(empty reply)", ""]
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return txt_path


def clear_failure(stem, tname):
    for p in failure_paths(stem, tname):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


class Namer:
    """Names outputs <Folder>_001.png, <Folder>_002.png ... A source keeps
    its number across runs (looked up from the metadata already in the
    output folder); files from older naming schemes are renamed to it."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.used, self.by_src = set(), {}
        if os.path.isdir(out_dir):
            for f in os.listdir(out_dir):
                if f.lower().endswith(".png"):
                    self.used.add(f)
                    m = read_meta(os.path.join(out_dir, f))
                    if m and m.get("source"):
                        self.by_src[os.path.realpath(m["source"])] = f

    def name(self, src, prefix, dry_run=False):
        real = os.path.realpath(src)
        cur = self.by_src.get(real)
        pat = re.compile(rf"^{re.escape(prefix)}_(\d+)\.png$")
        if cur and pat.match(cur):
            return cur
        nums = [int(m.group(1)) for f in self.used if (m := pat.match(f))]
        new = f"{prefix}_{(max(nums) + 1 if nums else 1):03d}.png"
        if cur and not dry_run:
            os.replace(os.path.join(self.out_dir, cur),
                       os.path.join(self.out_dir, new))
            self.used.discard(cur)
            print(f" ~ renamed {cur} -> {new}")
        self.used.add(new)
        self.by_src[real] = new
        return new


def split_list(v, lower=False):
    out = [x.strip() for x in v.split(",") if x.strip()]
    return [x.lower() for x in out] if lower else out


def parse_range(v):
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", v or "")
    if not v.strip():
        return None
    if not m:
        sys.exit(f"bad age_range '{v}', expected e.g. 30-40")
    lo, hi = sorted((int(m.group(1)), int(m.group(2))))
    return lo, hi




def parse_fields(text, where):
    """=== fields === lines 'name: instruction' (name? = may be empty)."""
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"^([a-z][a-z0-9_]*)(\?)?\s*:\s*(.+)$", ln)
        if not m:
            sys.exit(f"{where}: bad fields line (want 'name: instruction'): {ln}")
        out.append((m.group(1), bool(m.group(2)), m.group(3).strip()))
    if not out:
        sys.exit(f"{where}: === fields === is empty")
    return out


def s_(lo, hi):
    return {"type": "string", "minLength": lo, "maxLength": hi}


class Template:
    def __init__(self, name):
        self.name = name
        path = name if name.endswith(".txt") else os.path.join(
            SCRIPT_DIR, f"anime2img_{name}.txt")
        if not os.path.isfile(path):
            sys.exit(f'Template "{name}" not found: {path}')
        self.sections, st, self.sha = load_template(path)
        where = os.path.basename(path)
        for need in ("fields",):
            if need not in self.sections:
                sys.exit(f"{where} has no === {need} === section (old format? "
                         "use the templates shipped with this anime2img.py)")
        self.detail = st.get("detail", "high").lower()
        if self.detail not in FIELD_LEN:
            sys.exit(f"{where}: detail must be low, medium or high")
        self.style = st.get("style", "natural").lower()
        self.n = int(st.get("variations", 4))
        self.max_side = int(st.get("max_side", 512))
        self.output = safe(st.get("output", name.replace(".txt", "")))
        self.subject_noun = st.get("subject_noun", "").strip()
        f3 = parse_fields(self.sections["fields"], where)
        self.fields = [(f, opt) for f, opt, _ in f3]
        self.field_help = {f: h for f, _, h in f3}
        self.vary = split_list(st.get("vary", "expression, pose"), lower=True)
        bad = [v for v in self.vary if v not in self.field_help]
        if bad:
            sys.exit(f"{where}: vary names unknown field(s): {', '.join(bad)}")
        rng = parse_range(st.get("age_range", ""))
        self.cfg = {
            "temperature": float(st.get("temperature", 0.7)),
            "samplers": {k: (int(st[k]) if k == "top_k" else float(st[k]))
                         if k in st else v for k, v in SAMPLER_DEFAULTS.items()},
            "default_neg": st.get("default_negative", ""),
            "banned": split_list(st.get("banned_phrases", ""), lower=True),
            "force_pos": split_list(st.get("force_positive", "")),
            "force_neg": split_list(st.get("force_negative", "")),
            "age_range": rng,
            "age_lo": rng[0] if rng else 1, "age_hi": rng[1] if rng else 100,
        }
        self.schema = self.build_schema()
        # every string is capped, so the longest legal reply is known:
        # ~3 chars per token is conservative for English prose
        self.cfg["max_tokens"] = min(TOKEN_CAP, max(
            int(st.get("max_tokens", 0) or 0), self.max_chars // 3 + 300))
        for key in (f"detail.{self.detail}", f"style.{self.style}"):
            if key not in self.sections:
                sys.exit(f'{where} has no === {key} === section')

    def build_schema(self):
        mn, mx = FIELD_LEN[self.detail]
        props = {"notes": s_(*NOTES_LEN),
                 "age": {"type": "integer", "minimum": self.cfg["age_lo"],
                         "maximum": self.cfg["age_hi"]}}
        if self.subject_noun:
            props["species"] = s_(0, 40)
        else:
            props["subject"] = s_(3, 60)
        for f, opt in self.fields:
            props[f] = s_(0 if opt else mn, mx)
        props["avoid"] = {"type": "array", "items": s_(2, AVOID_LEN),
                          "maxItems": AVOID_ITEMS}
        vp = {"label": s_(2, LABEL_LEN), "change": s_(5, CHANGE_LEN)}
        for f in self.vary:
            vp[f] = s_(0, mx)
        vp["avoid"] = {"type": "array", "items": s_(2, AVOID_LEN),
                       "maxItems": VAR_AVOID_ITEMS}
        props["variations"] = {
            "type": "array", "minItems": self.n, "maxItems": self.n,
            "items": {"type": "object", "properties": vp,
                      "required": list(vp), "additionalProperties": False}}
        self.max_chars = (NOTES_LEN[1] + 60 + len(self.fields) * mx
                          + AVOID_ITEMS * (AVOID_LEN + 4)
                          + self.n * (LABEL_LEN + CHANGE_LEN + len(self.vary) * mx
                                      + VAR_AVOID_ITEMS * (AVOID_LEN + 4) + 120)
                          + 30 * len(props))
        return {"type": "object", "properties": props,
                "required": list(props), "additionalProperties": False}

    def form_text(self):
        """The field-by-field instructions, generated from the template so
        the prompt and the enforced schema can never disagree."""
        lo, hi = self.cfg["age_lo"], self.cfg["age_hi"]
        L = ['Fill in this form, in this order (the reply is only the JSON form):',
             '"notes": first, in plain words, what you see (art style, apparent '
             'age, species, outfit, scene) and how each stylized trait becomes '
             'real. This is your working space; it is not used in the prompt.']
        if self.cfg["age_range"]:
            L.append(f'"age": one whole number from {lo} to {hi}.')
        else:
            L.append('"age": the apparent age as one whole number.')
        if self.subject_noun:
            L.append(f'"species": "" for a human; otherwise 1-4 words for what '
                     f'she is, placed before the noun (e.g. "fox-eared", "horned '
                     f'demon"). The prompt opens with "photograph of a <age> year '
                     f'old {self.subject_noun}" automatically; do not repeat it.')
        else:
            L.append('"subject": the subject noun without the age, 1-6 words '
                     '(e.g. "woman", "man", "fox-eared woman"). The prompt opens '
                     'with "photograph of a <age> year old <subject>" '
                     'automatically; do not repeat the age anywhere else.')
        for f, opt in self.fields:
            L.append(f'"{f}": {self.field_help[f]}' + (' ("" if none)' if opt else ""))
        L.append(f'"avoid": up to {AVOID_ITEMS} short tags naming mistakes an image '
                 'generator is likely to make with THIS image (e.g. "brown hair" '
                 'when the hair is red, "missing horns"). Plain nouns, never '
                 '"not" or "no".')
        vf = ", ".join(f'"{f}"' for f in self.vary)
        L.append(f'"variations": exactly {self.n} entries. Each has "label" (1-3 '
                 f'lowercase words), "change" (one short sentence), {vf} written '
                 'exactly like the base fields of the same name ("" keeps the base '
                 f'one), and "avoid" (up to {VAR_AVOID_ITEMS} tags that stop the base '
                 'version bleeding through, e.g. "smile" when the new expression '
                 'is angry). Everything else about the photo stays as in the base '
                 'and is filled in by the program.')
        return "\n".join(L)

    def mapping(self, char, label=""):
        lo, hi = self.cfg["age_lo"], self.cfg["age_hi"]
        return {
            "CHAR_LINE": f' of the character "{char}"' if char else "",
            "HINT": f'\nThe file is named "{label}", which may hint at the '
                    "expression shown." if label else "",
            "N": str(self.n), "AGE_MIN": str(lo), "AGE_MAX": str(hi),
            "STYLE_RULES": self.sections[f"style.{self.style}"],
            "DETAIL_RULES": self.sections[f"detail.{self.detail}"],
            "VARIATION_RULES": fill(self.sections.get("variations", ""),
                                    {"N": str(self.n), "AGE_MIN": str(lo),
                                     "AGE_MAX": str(hi)}),
            "FORM": self.form_text(),
            "SUBJECT_NOUN": self.subject_noun,
        }

    def messages(self, src, char, label):
        m = self.mapping(char, label)
        user = fill(self.sections["user"], m)
        if "{{FORM}}" not in self.sections["user"]:
            user += "\n\n" + m["FORM"]
        return [
            {"role": "system", "content": fill(self.sections["system"], m)},
            {"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": image_data_uri(src, self.max_side)}},
                {"type": "text", "text": user}]},
        ]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", help="folders or images (default: "
                    "everything listed in anime2img_dirs.txt)")
    ap.add_argument("-t", "--template", action="append",
                    help="template(s) for the targets given on the command "
                         f"line (default: {DEFAULT_TEMPLATE})")
    ap.add_argument("--only", help="only run this template from the dirs file")
    ap.add_argument("--force", action="store_true", help="redo finished images")
    ap.add_argument("--limit", type=int, default=0, help="stop after N images")
    ap.add_argument("--out", help="base output folder "
                    "(default ComfyUI/input/imgprompt)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompts, write nothing")
    ap.add_argument("--no-check", action="store_true", help="skip vision check")
    ap.add_argument("--clean", metavar="PROMPT",
                    help="run the style cleanup on PROMPT, print the result, exit")
    args = ap.parse_args()

    terms = StyleTerms(TERMS_FILE)
    if args.clean is not None:
        pos, found = terms.clean_positive(args.clean)
        neg, dropped = terms.clean_negative("", found, (), pos)
        print(f"positive: {pos}\nremoved:  {', '.join(found) or '-'}\nnegative: {neg}")
        return

    if args.targets:
        tm = args.template or [DEFAULT_TEMPLATE]
        entries = [(os.path.abspath(os.path.expanduser(t)), tm) for t in args.targets]
    else:
        entries = load_dirs(DIRS_FILE)
        if args.only:
            entries = [(d, [t for t in ts if t == args.only]) for d, ts in entries]
            entries = [(d, ts) for d, ts in entries if ts]
    if not entries:
        sys.exit("Nothing to do: no folders listed.")

    templates = {}
    for _, names in entries:
        for nm in names:
            if nm not in templates:
                templates[nm] = Template(nm)
                templates[nm].cfg["terms"] = terms

    if args.out:
        out_base = os.path.abspath(os.path.expanduser(args.out))
    elif LOCAL.get("comfy_input"):
        out_base = os.path.abspath(os.path.expanduser(LOCAL["comfy_input"]))
    else:
        comfy = find_comfy()
        if not comfy:
            sys.exit("Couldn't find ComfyUI. Set COMFYUI_DIR=/path/to/ComfyUI "
                     "or pass --out /path/to/ComfyUI/input/imgprompt")
        out_base = os.path.join(comfy, "input", OUT_SUBDIR)

    jobs = collect(entries, [out_base])
    if not jobs:
        sys.exit("No images found in: " + ", ".join(e[0] for e in entries))
    for d, names in entries:
        print(f"  {d}  ->  {', '.join(names)}")
    for t in templates.values():
        print(f"  [{t.name}] detail={t.detail} style={t.style} "
              f"variations={t.n} vary={'/'.join(t.vary)} "
              f"max_tokens={t.cfg['max_tokens']} -> {os.path.join(out_base, t.output)}")
    print(f"{len(jobs)} image(s)")

    if not args.no_check:
        global KOBOLD_URL
        notes = []
        for url in KOBOLD_URLS:
            KOBOLD_URL = url
            try:
                ok, said = vision_check()
            except (urllib.error.URLError, OSError) as e:
                notes.append(f"{url}: not reachable")
                continue
            if ok:
                break
            notes.append(f"{url}: no vision (answered {said!r})")
        else:
            sys.exit("No vision backend found.\n  " + "\n  ".join(notes) +
                     "\nStart a vision model (KoboldCpp: load the model with its "
                     "mmproj) and set vision_url in the Sprite Lore config.ini.")
        print(f"vision backend: {KOBOLD_URL}")

    namers = {}
    done = skipped = failed = 0
    for src, parts, tnames in jobs:
        if args.limit and done >= args.limit:
            break
        with open(src, "rb") as f:
            src_sha = hashlib.sha256(f.read()).hexdigest()
        char = parts[0]
        rel = f"{'/'.join(parts)}/{os.path.basename(src)}"
        for tname in tnames:
            if args.limit and done >= args.limit:
                break
            t = templates[tname]
            out_dir = os.path.join(out_base, t.output)
            if out_dir not in namers:
                if not args.dry_run:
                    os.makedirs(out_dir, exist_ok=True)
                namers[out_dir] = Namer(out_dir)
            out_name = namers[out_dir].name(src, safe("_".join(parts)), args.dry_run)
            out_path = os.path.join(out_dir, out_name)
            old = read_meta(out_path)
            if (old and not args.force and old.get("src_sha") == src_sha
                    and old.get("template_sha") == t.sha
                    and old.get("terms_sha") == terms.sha):
                skipped += 1
                continue
            stem = os.path.splitext(out_name)[0]
            trace = []
            try:
                result = run_template(t, src, char, file_label(src), trace)
            except urllib.error.HTTPError as e:     # outside ask(): treat per image
                failed += 1
                log = dump_failure(src, stem, tname, rel, f"HTTP {e.code}", trace)
                print(f" - {rel} [{tname}]: FAILED (HTTP {e.code}) -> {log}")
                continue
            except urllib.error.URLError as e:
                if trace:
                    dump_failure(src, stem, tname, rel, f"connection lost: {e}", trace)
                sys.exit(f"Lost KoboldCpp at {KOBOLD_URL}: {e}")
            except RuntimeError as e:
                failed += 1
                log = dump_failure(src, stem, tname, rel, str(e), trace)
                print(f" - {rel} [{tname}]: FAILED ({e})\n     raw replies: {log}")
                continue
            clear_failure(stem, tname)
            meta = {"v": 3, "tool": "anime2img", "template": tname,
                    "char": char, "folder": "/".join(parts),
                    "orig_name": os.path.basename(src),
                    "source": os.path.abspath(src), "src_sha": src_sha,
                    "template_sha": t.sha, "terms_sha": terms.sha, "detail": t.detail, "style": t.style,
                    "generated": dt.datetime.now().isoformat(timespec="seconds"),
                    **result}
            if args.dry_run:
                print(f"\n=== {rel} [{tname}]\n{json.dumps(meta, indent=2)}")
            else:
                params = (f"{result['base']['positive']}\n"
                          f"Negative prompt: {result['base']['negative']}")
                png = embed(png_bytes(src), {
                    META_KEY: json.dumps(meta, ensure_ascii=False),
                    "parameters": params})
                tmp = out_path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(png)
                os.replace(tmp, out_path)
                ages = "/".join(str(a) for a in result["ages"]) or "-"
                labels = ", ".join(v["label"] for v in result["variations"])
                cl = result["cleanup"]["removed"]
                cl = f"  cleaned: {', '.join(cl)}" if cl else ""
                print(f" + {rel} [{tname}] -> {t.output}/{out_name}  "
                      f"age {ages}  [{labels}]{cl}")
            done += 1

    print(f"\ndone {done}, skipped {skipped} (already current), failed {failed}")
    if failed:
        print(f"failed images and their raw model replies: {FAILED_DIR}")


if __name__ == "__main__":
    main()
