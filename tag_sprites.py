#!/usr/bin/env python3
"""
tag_sprites.py - Stage 1: tag SillyTavern expression sprites with a VLM.

For every sprite in a character's expression folder:
  * sends the image to an OpenAI-compatible vision endpoint
  * gets back {emotion, confidence, tags, description}
  * embeds that as JSON in a PNG tEXt chunk ("st_expr"), pixels untouched
  * optionally renames the file so its label matches the VLM's emotion

Examples
  # one character, preview only
  python tag_sprites.py "~/SillyTavern/data/default-user/characters/Tessa" --dry-run

  # tag + rename everything under the characters folder
  python tag_sprites.py --characters-root ~/SillyTavern/data/default-user/characters --rename

Defaults for the backend URL come from the Sprite Lore config when installed.

Re-running is cheap: images already tagged (same pixels) are not sent to the
VLM again unless --force, so you can tag first and decide on --rename later.
"""

import argparse
import datetime as dt
import json
import os
import sys
import base64
import io
import re

from exprlore_common import (
    MAX_FAILS, FAIL_LOGS, failure_count, record_failure, clear_failure, load_failures,
    is_backend_down, cfg_get,
    ST_LABELS, PNGError, chat_json, file_pixel_hash, label_from_filename,
    list_images, load_labels, add_label_args, read_meta, resolve_folders, write_meta,
)

try:
    from PIL import Image
except ImportError:
    print("Pillow is required:  pip install pillow", file=sys.stderr)
    sys.exit(1)


SYSTEM_PROMPT = (
    "You are an image tagger for character art used in a roleplay app. You "
    "write tag-based image-generation prompts describing exactly what is in "
    "the image you are shown. Non-human anatomy (horns, tails, wings, extra "
    "limbs, scales, animal ears) is part of the character design: tag it "
    "neutrally. You respond with a single JSON object and nothing else."
)

TAG_CATEGORIES = ("appearance", "expression", "action", "objects", "setting")


def build_user_prompt(char_name, labels, hint_label):
    hint = (f'\nThe file is currently labeled "{hint_label}"; this may be wrong.'
            if hint_label else "")
    return (
        f'This image shows the character "{char_name}".{hint}\n'
        "Look closely at THIS image and tag it the way you would write a "
        "booru-style image-generation prompt: short lowercase tags such as "
        "'purple hair', 'twintails', 'hand on hip', 'holding mug', "
        "'looking at viewer', 'sweat drop'. Report the actual colors you see.\n\n"
        "Return JSON with exactly these keys:\n"
        "{\n"
        '  "emotion": "<one label from the allowed list>",\n'
        '  "confidence": <number 0-1>,\n'
        '  "appearance": ["hair color and style, eye color, skin, body type, '
        'species features, each clothing item with its color, accessories"],\n'
        '  "expression": ["facial expression, eyes, eyebrows, mouth, blush, '
        'tears, other face details"],\n'
        '  "action": ["what the character is doing: pose, gesture, hand '
        'positions, body orientation, movement"],\n'
        '  "objects": ["props held or nearby, visual effects such as '
        'sparkles, hearts, sweat drops, motion lines"],\n'
        '  "setting": ["background, lighting, framing such as upper body or '
        'full body"],\n'
        '  "description": "<one sentence: who is doing what, feeling what>"\n'
        "}\n\n"
        f"Allowed emotion labels: {', '.join(labels)}\n"
        "Pick the most specific label that fits the face and body language "
        "together (e.g. pride, amusement and excitement are different from "
        "plain joy). Use empty lists for categories with nothing to tag. "
        "Do not invent names, backstory or anything not visible."
    )


def image_to_data_url(path, max_side, bg):
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


def make_validator(labels):
    def validate(obj):
        emo = str(obj.get("emotion", "")).strip().lower()
        if emo not in labels:
            raise ValueError(f'"emotion" must be one of the allowed labels, got "{emo}"')
        try:
            conf = float(obj.get("confidence", 0))
        except (TypeError, ValueError):
            raise ValueError('"confidence" must be a number between 0 and 1')
        conf = max(0.0, min(1.0, conf))
        cats = {c: _clean_list(obj.get(c)) for c in TAG_CATEGORIES}
        if not cats["expression"] or not cats["action"]:
            raise ValueError('"expression" and "action" must be non-empty lists of tags')
        if not cats["appearance"]:
            raise ValueError('"appearance" must list hair, eyes and clothing tags')
        # flat tag list, prompt order: appearance, expression, action, objects, setting
        flat, seen = [], set()
        for c in TAG_CATEGORIES:
            for t in cats[c]:
                if t not in seen:
                    seen.add(t)
                    flat.append(t)
        desc = str(obj.get("description", "")).strip()
        return {"emotion": emo, "confidence": round(conf, 3), "tags": flat,
                "categories": cats, "description": desc}
    return validate


def _solid_png(rgb, size=256):
    buf = io.BytesIO()
    Image.new("RGB", (size, size), rgb).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def vision_selfcheck(args):
    """Send two solid-color images. If the answers don't match the colors,
    the backend is not actually receiving images (e.g. no mmproj loaded)."""
    from exprlore_common import chat
    answers = {}
    for color, rgb in (("red", (220, 20, 20)), ("blue", (20, 40, 220))):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "What single color fills this image? Answer with one word."},
            {"type": "image_url", "image_url": {"url": _solid_png(rgb)}},
        ]}]
        try:
            answers[color] = chat(args.vlm_url, msgs, model=args.vlm_model,
                                  api_key=args.api_key, max_tokens=300,
                                  temperature=0.0, timeout=args.timeout)
        except Exception as e:
            raise SystemExit(f"vision check: backend call failed: {e}")
    ok = all(c in re.sub(r"<think>.*?</think>", "", a, flags=re.S | re.I).lower()
             for c, a in answers.items())
    if ok:
        print("vision check: OK (backend is receiving images)")
        return
    raise SystemExit(
        "vision check FAILED - the backend is not seeing images.\n"
        f"  red image  -> {answers['red'][:80]!r}\n"
        f"  blue image -> {answers['blue'][:80]!r}\n"
        "  Most likely the mmproj file isn't loaded (KoboldCpp: Loaded Files ->\n"
        "  Vision mmproj, or --mmproj), or it doesn't match the model.\n"
        "  Use --skip-vision-check to run anyway.")


def collapse_warning(entries):
    """Warn when different images got near-identical tags."""
    fresh = [set(e["result"]["tags"]) for e in entries if e["src"] == "vlm"]
    if len(fresh) < 4:
        return
    sims = []
    for i in range(len(fresh)):
        for j in range(i + 1, len(fresh)):
            a, b = fresh[i], fresh[j]
            sims.append(len(a & b) / max(1, len(a | b)))
    avg = sum(sims) / len(sims)
    if avg > 0.7:
        print(f" !! WARNING: tags are {avg:.0%} identical across different images."
              " The VLM is probably not seeing them - results are guesses.")


def convert_to_png(path, dry_run):
    """Convert webp/jpg to PNG in place (original removed)."""
    new = os.path.splitext(path)[0] + ".png"
    if os.path.exists(new):
        print(f"   !! cannot convert, {os.path.basename(new)} already exists")
        return None
    if dry_run:
        print(f"   would convert -> {os.path.basename(new)}")
        return None
    with Image.open(path) as im:
        im.convert("RGBA").save(new, "PNG")
    os.unlink(path)
    print(f"   converted -> {os.path.basename(new)}")
    return new


def plan_names(entries, labels, do_rename, min_conf):
    """entries: list of dicts with path, cur_label, result.
    Sets entry['target'] (basename). Files not renamed keep their names."""
    reserved = set()
    movers = []
    for e in entries:
        res = e.get("result")
        want = None
        keep = (e.get("meta") or {}).get("keepName") or {}
        kept = isinstance(keep, dict) and keep.get("label") == e["cur_label"] \
            and res is not None and keep.get("rejected") == res.get("emotion")
        if do_rename and res and res["confidence"] >= min_conf \
                and res["emotion"] != e["cur_label"] and not kept:   # "Keep current name" in Sprite Lore
            want = res["emotion"]
        if want:
            e["want"] = want
            movers.append(e)
        else:
            e["target"] = os.path.basename(e["path"])
            reserved.add(e["target"].lower())
    for e in movers:
        label = e["want"]
        cand = f"{label}.png"
        n = 1
        while cand.lower() in reserved:
            cand = f"{label}-{n}.png"
            n += 1
        reserved.add(cand.lower())
        e["target"] = cand


def execute_renames(folder, entries):
    """Two-phase rename so swaps (joy<->anger) never collide."""
    moves = [e for e in entries if e["target"] != os.path.basename(e["path"])]
    staged = []
    for i, e in enumerate(moves):
        tmp = os.path.join(folder, f".exprlore_mv_{os.getpid()}_{i}.png")
        os.rename(e["path"], tmp)
        staged.append((tmp, e))
    for tmp, e in staged:
        final = os.path.join(folder, e["target"])
        os.rename(tmp, final)
        e["path"] = final
    return moves


def undo_renames(log_path):
    with open(log_path, "r", encoding="utf-8") as f:
        recs = json.load(f)
    staged = []
    for i, r in enumerate(recs):
        src = os.path.join(r["folder"], r["to"])
        if not os.path.exists(src):
            print(f" !! missing, skipped: {src}")
            continue
        tmp = os.path.join(r["folder"], f".exprlore_undo_{os.getpid()}_{i}.png")
        os.rename(src, tmp)
        staged.append((tmp, r))
    for tmp, r in staged:
        dst = os.path.join(r["folder"], r["from"])
        if os.path.exists(dst):
            print(f" !! {r['from']} exists, left as {os.path.basename(tmp)}")
            continue
        os.rename(tmp, dst)
        print(f" - {r['to']} -> {r['from']}")
    print(f"Undo done ({len(staged)} file(s)). Embedded tags are kept.")


def process_folder(folder, args, labels, log):
    char_name = os.path.basename(os.path.abspath(folder).rstrip("/"))
    print(f"\n=== {char_name}  ({folder})")
    validate = make_validator(labels)
    entries = []
    fails = load_failures()

    for path in list_images(folder):
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        if ext == ".gif":
            print(f" - {name}: skipped (gif; animation would be lost)")
            continue
        if ext != ".png":
            if not args.convert:
                print(f" - {name}: skipped (not PNG; --no-convert set)")
                continue
            path = convert_to_png(path, args.dry_run)
            if not path:
                continue
            name = os.path.basename(path)

        try:
            meta = read_meta(path) or {}
            pxhash = file_pixel_hash(path)
        except PNGError as e:
            print(f" - {name}: skipped ({e})")
            continue

        cur_label = label_from_filename(name, labels)
        prev = meta.get("vlm")
        if prev and meta.get("pxhash") == pxhash and not args.force:
            result = {k: prev.get(k) for k in ("emotion", "confidence", "tags", "categories", "description")}
            src = "cached"
        elif not args.force and failure_count(path, "tag", fails) >= MAX_FAILS:
            print(f" - {name}: skipped (failed {MAX_FAILS} runs; details in "
                  f"{os.path.basename(FAIL_LOGS['tag'])}; --force retries it)")
            continue
        else:
            try:
                data_url = image_to_data_url(path, args.max_side, args.bg)
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": build_user_prompt(
                            char_name, labels, cur_label if args.hint_label else None)},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ]},
                ]
                result = chat_json(
                    args.vlm_url, messages, validate, retries=args.retries,
                    model=args.vlm_model, api_key=args.api_key,
                    max_tokens=args.max_tokens, temperature=args.temperature,
                    json_mode=args.json_mode, timeout=args.timeout)
                src = "vlm"
                clear_failure(path, "tag")
            except Exception as e:
                if is_backend_down(e):
                    raise SystemExit(f" !! backend unreachable ({e}); stopping run")
                n = record_failure(path, "tag", e)
                more = "giving up on it" if n >= MAX_FAILS else f"will retry next run ({n}/{MAX_FAILS})"
                print(f" - {name}: FAILED, {more}\n     {e}")
                continue

        entries.append({"path": path, "cur_label": cur_label, "result": result,
                        "meta": meta, "pxhash": pxhash, "src": src})

    collapse_warning(entries)
    plan_names(entries, labels, args.rename, args.min_confidence)

    now = dt.datetime.now().isoformat(timespec="seconds")
    for e in entries:
        r, name = e["result"], os.path.basename(e["path"])
        arrow = f" -> {e['target']}" if e["target"] != name else ""
        flag = ""
        if r["emotion"] != e["cur_label"] and not arrow:
            flag = "  [mismatch]" if r["confidence"] >= args.min_confidence else "  [mismatch, low conf]"
        print(f" - {name}{arrow}  ({r['emotion']} {r['confidence']:.2f}, {e['src']}){flag}")
        cats = r.get("categories")
        if cats:
            for c in TAG_CATEGORIES:
                if cats.get(c):
                    print(f"     {c + ':':12} {', '.join(cats[c])}")
        else:
            print(f"     tags: {', '.join(r['tags'])}")
        if r.get("description"):
            print(f"     {'desc:':12} {r['description']}")

        if args.dry_run:
            continue
        meta = e["meta"]
        meta["pxhash"] = e["pxhash"]
        meta.setdefault("orig_name", name)
        meta["char"] = char_name
        meta["label"] = label_from_filename(e["target"], labels)
        if e["src"] == "vlm":
            meta["vlm"] = dict(r, model=args.vlm_model or "", ts=now)
            # pixels re-tagged -> any existing lore is stale
            if "lore" in meta and meta.get("lore", {}).get("pxhash") != e["pxhash"]:
                meta["lore"]["stale"] = True
        write_meta(e["path"], meta)

    if not args.dry_run:
        for e in entries:
            e["src_name"] = os.path.basename(e["path"])
        for e in execute_renames(folder, entries):
            r = e["result"]
            log.append({"folder": os.path.abspath(folder), "from": e["src_name"],
                        "to": e["target"], "emotion": r["emotion"],
                        "confidence": r["confidence"]})
    return entries


def main():
    ap = argparse.ArgumentParser(
        description="Stage 1: VLM tagging of SillyTavern expression sprites.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("folders", nargs="*", help="character expression folder(s)")
    ap.add_argument("--characters-root", help="process every sprite folder under this dir")
    ap.add_argument("--vlm-url", default=cfg_get("vision_url", "http://127.0.0.1:5001/v1"),
                    help="OpenAI-compatible base URL (default: %(default)s)")
    ap.add_argument("--vlm-model", default="", help="model name to send (optional)")
    ap.add_argument("--api-key", default=os.environ.get("EXPRLORE_API_KEY", ""))
    add_label_args(ap)
    ap.add_argument("--rename", action="store_true",
                    help="rename files whose VLM emotion differs from their label")
    ap.add_argument("--min-confidence", type=float, default=0.6,
                    help="only rename at or above this confidence (default: %(default)s)")
    ap.add_argument("--hint-label", action="store_true",
                    help="tell the VLM the current filename label (biases toward it)")
    ap.add_argument("--no-convert", dest="convert", action="store_false",
                    help="don't convert webp/jpg sprites to PNG (converted by default)")
    ap.add_argument("--force", action="store_true", help="re-tag even if already tagged")
    ap.add_argument("--dry-run", action="store_true", help="call VLM, write nothing")
    ap.add_argument("--max-side", type=int, default=1024, help="downscale for VLM (px)")
    ap.add_argument("--bg", default="128,128,128", help="RGB backdrop for transparency")
    ap.add_argument("--max-tokens", type=int, default=800)
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--json-mode", action="store_true",
                    help="send response_format json_object (if backend supports it)")
    ap.add_argument("--log", help="rename log path (default: ./exprlore_renames_<time>.json)")
    ap.add_argument("--skip-vision-check", action="store_true",
                    help="skip the startup test that the backend actually sees images")
    ap.add_argument("--undo", metavar="LOG", help="reverse the renames in a rename log and exit")
    args = ap.parse_args()

    if args.undo:
        undo_renames(args.undo)
        return

    try:
        args.bg = tuple(int(x) for x in args.bg.split(","))
        assert len(args.bg) == 3
    except Exception:
        ap.error("--bg must be R,G,B")
    folders = resolve_folders(args.folders, args.characters_root)
    if not folders:
        ap.error("no sprite folders given/found")
    labels = load_labels(args, [args.characters_root] if args.characters_root else folders)

    if not args.skip_vision_check:
        vision_selfcheck(args)

    log = []
    for folder in folders:
        process_folder(folder, args, labels, log)

    if log and not args.dry_run:
        path = args.log or f"exprlore_renames_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2)
        print(f"\nRenamed {len(log)} file(s); log: {path}")
    elif args.dry_run:
        print("\nDry run: nothing written.")


if __name__ == "__main__":
    main()
