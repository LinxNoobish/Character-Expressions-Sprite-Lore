#!/usr/bin/env python3
"""
exprlore_common.py - shared helpers for the expression lore pack pipeline.

- Lossless PNG text-chunk read/write (no pixel re-encode)
- SillyTavern character card reading (ccv3 / chara chunks)
- OpenAI-compatible chat calls with JSON extraction + retry
- Sprite folder discovery

Run directly to inspect a sprite's embedded metadata:
    python exprlore_common.py path/to/sprite.png [more.png ...]

Paths and backend URLs written by the Sprite Lore installer are read from
~/.config/spritelore/spritelore_local.json (or $SPRITELORE_CONFIG); every
script falls back to standard locations when that file doesn't exist.
"""

import base64
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import urllib.error
import urllib.request
import zlib

PNG_SIG = b"\x89PNG\r\n\x1a\n"
META_KEYWORD = "st_expr"


def config_path():
    env = os.environ.get("SPRITELORE_CONFIG")
    if env:
        return os.path.expanduser(env)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "spritelore", "spritelore_local.json")


def local_config():
    """Paths and URLs resolved by the installer; {} when not installed."""
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def cfg_get(key, default=None):
    v = local_config().get(key)
    return default if v in (None, "", []) else v
SCHEMA_VERSION = 1

IMAGE_EXTS = (".png", ".webp", ".jpg", ".jpeg", ".gif")

# SillyTavern's default classifier label set.
ST_LABELS = [
    "admiration", "amusement", "anger", "annoyance", "approval", "caring",
    "confusion", "curiosity", "desire", "disappointment", "disapproval",
    "disgust", "embarrassment", "excitement", "fear", "gratitude", "grief",
    "joy", "love", "nervousness", "optimism", "pride", "realization",
    "relief", "remorse", "sadness", "surprise", "neutral",
]


# --------------------------------------------------------------------------
# PNG chunk handling
# --------------------------------------------------------------------------

class PNGError(Exception):
    pass


def read_chunks(data):
    """Return list of (type_bytes, body_bytes). Stops at IEND."""
    if not data.startswith(PNG_SIG):
        raise PNGError("not a PNG file")
    pos = len(PNG_SIG)
    chunks = []
    n = len(data)
    while pos < n:
        if pos + 8 > n:
            raise PNGError("truncated chunk header")
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise PNGError("truncated chunk body")
        chunks.append((ctype, body))
        pos += 12 + length
        if ctype == b"IEND":
            break
    if not chunks or chunks[-1][0] != b"IEND":
        raise PNGError("missing IEND")
    return chunks


def build_chunk(ctype, body):
    crc = zlib.crc32(ctype + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", crc)


def _parse_text(body):
    key, _, val = body.partition(b"\x00")
    return key.decode("latin-1"), val.decode("latin-1")


def get_text_chunk(data, keyword):
    """Return value of the last tEXt chunk with this keyword, or None."""
    found = None
    for ctype, body in read_chunks(data):
        if ctype == b"tEXt":
            k, v = _parse_text(body)
            if k == keyword:
                found = v
    return found


def pixel_hash(data):
    """sha256 over IDAT payloads - changes only if the image itself changes."""
    h = hashlib.sha256()
    for ctype, body in read_chunks(data):
        if ctype == b"IDAT":
            h.update(body)
    return h.hexdigest()


def atomic_write_bytes(path, data):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".exprlore_", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except FileNotFoundError:
            pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def set_text_chunk_bytes(data, keyword, value):
    """Return new PNG bytes with any existing `keyword` tEXt replaced."""
    out = [PNG_SIG]
    for ctype, body in read_chunks(data):
        if ctype == b"tEXt" and _parse_text(body)[0] == keyword:
            continue
        if ctype == b"IEND":
            payload = keyword.encode("latin-1") + b"\x00" + value.encode("latin-1")
            out.append(build_chunk(b"tEXt", payload))
        out.append(build_chunk(ctype, body))
    return b"".join(out)


def read_meta(path):
    """Return the embedded expression metadata dict, or None."""
    with open(path, "rb") as f:
        data = f.read()
    raw = get_text_chunk(data, META_KEYWORD)
    if raw is None:
        return None
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        return None


def write_meta(path, meta):
    """Embed metadata (base64 JSON in a tEXt chunk). Pixels untouched."""
    with open(path, "rb") as f:
        data = f.read()
    meta = dict(meta)
    meta["v"] = SCHEMA_VERSION
    encoded = base64.b64encode(
        json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    atomic_write_bytes(path, set_text_chunk_bytes(data, META_KEYWORD, encoded))


def file_pixel_hash(path):
    with open(path, "rb") as f:
        return pixel_hash(f.read())


# --------------------------------------------------------------------------
# Character cards
# --------------------------------------------------------------------------

def read_card(path):
    """Read a SillyTavern card PNG (ccv3 preferred, then chara) or .json.
    Returns a flat dict with name/description/personality/scenario."""
    if path.lower().endswith(".json"):
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    else:
        with open(path, "rb") as f:
            data = f.read()
        raw = get_text_chunk(data, "ccv3") or get_text_chunk(data, "chara")
        if raw is None:
            raise PNGError(f"no character card data in {path}")
        obj = json.loads(base64.b64decode(raw).decode("utf-8"))
    d = obj.get("data", obj) if isinstance(obj, dict) else {}
    return {
        "name": d.get("name") or obj.get("name") or "",
        "description": d.get("description") or "",
        "personality": d.get("personality") or "",
        "scenario": d.get("scenario") or "",
    }


def find_card_for_folder(folder):
    """ST layout: characters/<Name>/ sprites, characters/<Name>.png card."""
    folder = os.path.abspath(folder.rstrip("/"))
    base = folder
    for ext in (".png", ".json"):
        if os.path.isfile(base + ext):
            return base + ext
    return None


# --------------------------------------------------------------------------
# Folders / labels
# --------------------------------------------------------------------------

def list_images(folder):
    out = []
    for e in sorted(os.scandir(folder), key=lambda e: e.name.lower()):
        if not e.is_file():
            continue
        if e.name.startswith(".exprlore_"):
            continue
        if e.name.lower().endswith(IMAGE_EXTS):
            out.append(e.path)
    return out


def discover_sprite_folders(root):
    """Every subfolder of the characters root that contains images."""
    out = []
    for e in sorted(os.scandir(root), key=lambda e: e.name.lower()):
        if e.is_dir() and not e.name.startswith("."):
            if list_images(e.path):
                out.append(e.path)
    return out


def resolve_folders(paths, characters_root):
    folders = []
    if characters_root:
        folders.extend(discover_sprite_folders(characters_root))
    for p in paths or []:
        if os.path.isdir(p):
            folders.append(p)
        else:
            print(f"!! not a folder, skipped: {p}", file=sys.stderr)
    seen, uniq = set(), []
    for f in folders:
        k = os.path.realpath(f)
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def label_from_filename(filename, labels):
    """'joy.png' -> joy, 'joy-2.png' -> joy, 'joy-smug.png' -> joy.
    Unknown prefixes return the full stem."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    low = stem.lower()
    if low in labels:
        return low
    head = low.split("-", 1)[0]
    if head in labels:
        return head
    return low


def parse_labels(arg):
    if not arg:
        return list(ST_LABELS)
    if os.path.isfile(arg):
        with open(arg, "r", encoding="utf-8") as f:
            items = re.split(r"[,\n]", f.read())
    else:
        items = arg.split(",")
    return [i.strip().lower() for i in items if i.strip()]


def find_st_settings(folders):
    """Walk up from each sprite folder (without resolving symlinks) looking
    for SillyTavern's user settings.json: .../data/<user>/settings.json,
    which sits next to the characters/ folder."""
    for folder in folders:
        d = os.path.abspath(folder)
        for _ in range(6):
            cand = os.path.join(d, "settings.json")
            if os.path.isfile(cand) and os.path.isdir(os.path.join(d, "characters")):
                return cand
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return None


# Fixed locations checked when the sprite path isn't inside ST's data dir
# (e.g. a sprite library on another drive symlinked into ST).
ST_SETTINGS_PATHS = [
    "~/SillyTavern/data/default-user/settings.json",
    "~/sillytavern/data/default-user/settings.json",
    "~/Sillytavern/data/default-user/settings.json",
    "~/SillyTavern-Launcher/SillyTavern/data/default-user/settings.json",
]


def find_st_settings_home():
    configured = cfg_get("st_settings")
    if configured and os.path.isfile(os.path.expanduser(configured)):
        return os.path.expanduser(configured)
    for p in ST_SETTINGS_PATHS:
        p = os.path.expanduser(p)
        if os.path.isfile(p):
            return p
    # any ST install one or two levels under home (forks, renamed folders)
    import glob
    home = os.path.expanduser("~")
    for pat in ("*/data/default-user/settings.json", "*/*/data/default-user/settings.json"):
        hits = sorted(glob.glob(os.path.join(home, pat)), key=os.path.getmtime, reverse=True)
        if hits:
            return hits[0]
    return None


def read_st_custom_expressions(settings_path):
    """Custom expression labels from ST settings.json
    (extension_settings.expressions.custom). Returns list or None."""
    with open(settings_path, "r", encoding="utf-8") as f:
        s = json.load(f)
    expr = (s.get("extension_settings") or {}).get("expressions") or {}
    custom = expr.get("custom")
    if not isinstance(custom, list):
        return None
    return [str(c).strip().lower() for c in custom if str(c).strip()]


def load_labels(args, folders):
    """Resolve the label set:
      --labels given          -> exactly that list
      otherwise               -> ST's 28 defaults (unless --no-default-labels)
                                 + custom expressions from ST settings.json
    ST settings: --st-settings, else auto-found by walking up from folders."""
    if args.labels:
        labels = parse_labels(args.labels)
        print(f"labels: {len(labels)} from --labels")
        return labels
    labels = [] if args.no_default_labels else list(ST_LABELS)
    path = args.st_settings or find_st_settings(folders) or find_st_settings_home()
    if path:
        try:
            custom = read_st_custom_expressions(path)
        except Exception as e:
            print(f"!! could not read ST settings {path}: {e}", file=sys.stderr)
            custom = None
        if custom is None:
            print(f"labels: no custom expression list found in {path}")
        else:
            new = [c for c in custom if c not in labels]
            labels += new
            print(f"labels: {len(labels)} total, custom from ST "
                  f"({path}): {', '.join(custom) if custom else '(none)'}")
    else:
        print("labels: ST settings.json not found (use --st-settings); "
              "using defaults only")
    if not labels:
        raise SystemExit("no labels: custom list empty and defaults disabled")
    return labels


def add_label_args(ap):
    ap.add_argument("--labels",
                    help="comma list or file of labels; overrides everything else")
    ap.add_argument("--st-settings",
                    help="path to SillyTavern data/<user>/settings.json "
                         "(auto-found when the sprite path is inside ST's data dir)")
    ap.add_argument("--no-default-labels", action="store_true",
                    help="use only your ST custom expressions, not the 28 defaults")


# --------------------------------------------------------------------------
# LLM / VLM calls
# --------------------------------------------------------------------------

def normalize_endpoint(url):
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if not re.search(r"/v\d+$", url):
        url += "/v1"
    return url + "/chat/completions"


def chat_full(url, messages, model=None, api_key=None, max_tokens=512,
              temperature=0.4, json_mode=False, timeout=900):
    payload = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if model:
        payload["model"] = model
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        normalize_endpoint(url), data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {e.code} from backend: {body}") from None
    try:
        ch = resp["choices"][0]
        return (ch["message"].get("content") or ""), ch.get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise RuntimeError(f"unexpected backend response: {str(resp)[:500]}")


def chat(url, messages, **kw):
    """Content only (see chat_full for finish_reason)."""
    return chat_full(url, messages, **kw)[0]


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def extract_json(text):
    """Pull the first JSON object out of model output (handles think tags,
    code fences, chatter before/after)."""
    text = _THINK_RE.sub("", text or "")
    text = re.sub(r"```(?:json)?", "", text)
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON object found in model output")


class ModelOutputError(ValueError):
    def __init__(self, msg, raw=""):
        super().__init__(msg)
        self.raw = raw


MAX_TOKENS_CAP = 4096


def chat_json(url, messages, validate, retries=2, **kw):
    """Call, parse, validate. validate(obj) returns cleaned obj or raises
    ValueError with a message that is fed back to the model.
    If the reply was cut off at max_tokens, the next attempt starts fresh
    with double the token budget instead of asking the model to fix it."""
    msgs = list(messages)
    max_tokens = kw.pop("max_tokens", 512)
    last_err, content, finish = None, "", None
    for attempt in range(retries + 1):
        content, finish = chat_full(url, msgs, max_tokens=max_tokens, **kw)
        try:
            return validate(extract_json(content))
        except ValueError as e:
            last_err = e
            if finish == "length" and max_tokens < MAX_TOKENS_CAP:
                max_tokens = min(MAX_TOKENS_CAP, max_tokens * 2)
                last_err = ValueError(f"reply cut off at token limit; retrying with {max_tokens}")
                msgs = list(messages)
                continue
            msgs = msgs + [
                {"role": "assistant", "content": content},
                {"role": "user", "content":
                    f"That response was invalid: {e}. "
                    "Reply again with ONLY the corrected JSON object."},
            ]
    cut = " (reply was cut off at the token limit)" if finish == "length" else ""
    snippet = re.sub(r"\s+", " ", _THINK_RE.sub("", content or "")).strip()[:160]
    raise ModelOutputError(
        f"model output invalid after {retries + 1} attempts{cut}: {last_err}"
        + (f' | last reply: "{snippet}"' if snippet else " | last reply was empty"),
        raw=content or "")


# --------------------------------------------------------------------------
# Failure log: images that keep failing are skipped after MAX_FAILS runs
# --------------------------------------------------------------------------

MAX_FAILS = 3
_HERE = os.path.dirname(os.path.abspath(__file__))
# one file per stage, so the tagger and lore writer can run at the same time
FAIL_LOGS = {"tag": os.path.join(_HERE, "exprlore_failures_tag.json"),
             "lore": os.path.join(_HERE, "exprlore_failures_lore.json")}
FAIL_LOG = FAIL_LOGS["tag"]   # shown in messages; both files sit side by side


def _load_stage(stage):
    try:
        with open(FAIL_LOGS[stage], "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save_stage(stage, d):
    path = FAIL_LOGS[stage]
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def _fail_key(path):
    return os.path.realpath(path)


def load_failures():
    """{realpath: {stage: entry}} merged across both stage files."""
    out = {}
    for stage in FAIL_LOGS:
        for k, v in _load_stage(stage).items():
            out.setdefault(k, {})[stage] = v
    return out


def failure_count(path, stage, _cache=None):
    if _cache is not None:
        return _cache.get(_fail_key(path), {}).get(stage, {}).get("count", 0)
    return _load_stage(stage).get(_fail_key(path), {}).get("count", 0)


def record_failure(path, stage, err):
    import datetime as _dt
    d = _load_stage(stage)
    e = d.setdefault(_fail_key(path), {"count": 0})
    e["count"] += 1
    e["last_error"] = str(err)[:500]
    e["last_raw"] = (getattr(err, "raw", "") or "")[:3000]
    e["ts"] = _dt.datetime.now().isoformat(timespec="seconds")
    _save_stage(stage, d)
    return e["count"]


def clear_failure(path, stage):
    d = _load_stage(stage)
    if d.pop(_fail_key(path), None) is not None:
        _save_stage(stage, d)


def is_backend_down(err):
    """Connection-level errors mean every following image would fail too."""
    import socket
    if isinstance(err, (ConnectionError, TimeoutError, socket.timeout)):
        return True
    if isinstance(err, urllib.error.URLError) and not isinstance(err, urllib.error.HTTPError):
        return True
    return False


# --------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for p in sys.argv[1:]:
        try:
            m = read_meta(p)
        except Exception as e:
            print(f"{p}: error: {e}")
            continue
        print(f"== {p}")
        print(json.dumps(m, indent=2, ensure_ascii=False) if m else "(no expression metadata)")
