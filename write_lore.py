#!/usr/bin/env python3
"""
write_lore.py - Stage 2: write a lore variation + 4 scene keywords per sprite.

Separate run from tag_sprites.py. For each sprite that stage 1 tagged:
  * reads the character card (characters/<Name>.png next to the folder)
  * sends card + sprite's emotion label + VLM tags to an LLM backend
  * gets back {"lore": "...", "keywords": [4 items]}
  * embeds it into the same st_expr PNG chunk (pixels untouched)

Entries already written for the same character are passed along as
"do not repeat" context so 40 sprites don't yield the same 3 facts
(disable with --no-dedupe).

Examples
  python write_lore.py "~/SillyTavern/data/default-user/characters/Tessa" --dry-run
  python write_lore.py --characters-root ~/SillyTavern/data/default-user/characters --llm-url http://127.0.0.1:5001/v1

Defaults for the backend URL come from the Sprite Lore config when installed.
"""

import argparse
import datetime as dt
import os
import sys

from exprlore_common import (
    MAX_FAILS, FAIL_LOGS, failure_count, record_failure, clear_failure, load_failures,
    is_backend_down, cfg_get,
    PNGError, chat_json, file_pixel_hash, find_card_for_folder,
    label_from_filename, list_images, load_labels, add_label_args, read_card, read_meta,
    resolve_folders, write_meta,
)

SYSTEM_PROMPT = (
    "You write short lore entries for a roleplay character, grounded in "
    "their character card. You respond with a single JSON object and "
    "nothing else."
)


def card_block(card, max_chars):
    def clip(s):
        s = (s or "").replace("{{char}}", card["name"]).replace("<BOT>", card["name"]).strip()
        return s if len(s) <= max_chars else s[:max_chars].rsplit(" ", 1)[0] + " ..."
    parts = [f"Name: {card['name']}"]
    for key in ("description", "personality", "scenario"):
        if card.get(key):
            parts.append(f"{key.capitalize()}:\n{clip(card[key])}")
    return "\n\n".join(parts)


def visual_block(vlm):
    cats = vlm.get("categories")
    if cats:
        lines = [f"{c.capitalize()}: {', '.join(cats[c])}"
                 for c in ("appearance", "expression", "action", "objects", "setting")
                 if cats.get(c)]
        return "\n".join(lines)
    return f"Visual tags: {', '.join(vlm.get('tags', []))}"


def build_user_prompt(card, card_text, image_name, label, vlm, prior, lore_words):
    name = card["name"]
    prior_block = ""
    if prior:
        lines = "\n".join(f"- {p}" for p in prior)
        prior_block = (
            "\n\nENTRIES ALREADY WRITTEN FOR THIS CHARACTER "
            "(do not repeat these facts or keywords):\n" + lines)
    return (
        f"CHARACTER CARD\n{card_text}\n\n"
        f"EXPRESSION SPRITE\n"
        f"Image name: {image_name}\n"
        f"Emotion: {label}\n"
        f"{visual_block(vlm)}\n"
        f"Visual description: {vlm.get('description', '')}"
        f"{prior_block}\n\n"
        "TASK\n"
        f"Write a short lore variation about {name} that this expression could "
        f"reveal: a habit, memory, belief, relationship, quirk, or reaction tied "
        f"to feeling {label}. Use the visible action, objects and appearance for specifics. Stay consistent "
        "with the card; add new details only where the card is silent and never "
        "contradict it. Third person, present tense, 2-4 sentences, under "
        f"{lore_words} words. Refer to the user as {{{{user}}}} if needed.\n"
        "Then give exactly 4 keywords (1-3 words each): concrete objects, places, "
        "topics, or actions to weave into the next scene.\n\n"
        'Return JSON: {"lore": "...", "keywords": ["...", "...", "...", "..."]}'
    )


def make_validator(max_chars):
    def validate(obj):
        lore = str(obj.get("lore", "")).strip()
        if not lore:
            raise ValueError('"lore" must be a non-empty string')
        if len(lore) > max_chars:
            raise ValueError(f'"lore" is too long ({len(lore)} chars); keep it under {max_chars}')
        kws = obj.get("keywords")
        if not isinstance(kws, list):
            raise ValueError('"keywords" must be a list of 4 strings')
        kws = [str(k).strip() for k in kws if str(k).strip()]
        if len(kws) != 4:
            raise ValueError(f'"keywords" must contain exactly 4 items, got {len(kws)}')
        return {"lore": lore, "keywords": kws}
    return validate


def process_folder(folder, args, labels):
    char_dir = os.path.basename(os.path.abspath(folder).rstrip("/"))
    card_path = args.card or find_card_for_folder(folder)
    print(f"\n=== {char_dir}  ({folder})")
    if not card_path:
        print(f" !! no card found at {os.path.abspath(folder).rstrip('/')}.png - use --card")
        return
    try:
        card = read_card(card_path)
    except Exception as e:
        print(f" !! cannot read card {card_path}: {e}")
        return
    card["name"] = card["name"] or char_dir
    card_text = card_block(card, args.card_chars)
    print(f" card: {card_path}  (name: {card['name']})")

    sprites = []
    for path in list_images(folder):
        if not path.lower().endswith(".png"):
            continue
        try:
            meta = read_meta(path)
        except PNGError:
            continue
        sprites.append((path, meta))

    prior = []
    if not args.no_dedupe:
        for _, m in sprites:
            lore = (m or {}).get("lore")
            if lore and not lore.get("stale") and not args.force:
                prior.append(f"{lore['text']}  [keywords: {', '.join(lore['keywords'])}]")

    validate = make_validator(args.lore_chars)
    now = dt.datetime.now().isoformat(timespec="seconds")
    done = skipped = failed = 0
    fails = load_failures()

    for path, meta in sprites:
        name = os.path.basename(path)
        if not meta or "vlm" not in meta:
            print(f" - {name}: no stage-1 tags, run tag_sprites.py first")
            skipped += 1
            continue
        pxhash = file_pixel_hash(path)
        if meta.get("pxhash") != pxhash:
            print(f" - {name}: image changed since tagging, re-run tag_sprites.py")
            skipped += 1
            continue
        lore = meta.get("lore")
        if lore and not lore.get("stale") and not args.force:
            skipped += 1
            continue

        if not args.force and failure_count(path, "lore", fails) >= MAX_FAILS:
            print(f" - {name}: skipped (failed {MAX_FAILS} runs; details in "
                  f"{os.path.basename(FAIL_LOGS['lore'])}; --force retries it)")
            skipped += 1
            continue

        label = label_from_filename(name, labels)
        ctx = prior[-args.dedupe_max:] if not args.no_dedupe else []
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(
                card, card_text, name, label, meta["vlm"], ctx, args.lore_words)},
        ]
        try:
            out = chat_json(
                args.llm_url, messages, validate, retries=args.retries,
                model=args.llm_model, api_key=args.api_key,
                max_tokens=args.max_tokens, temperature=args.temperature,
                json_mode=args.json_mode, timeout=args.timeout)
        except Exception as e:
            if is_backend_down(e):
                raise SystemExit(f" !! backend unreachable ({e}); stopping run")
            n = record_failure(path, "lore", e)
            more = "giving up on it" if n >= MAX_FAILS else f"will retry next run ({n}/{MAX_FAILS})"
            print(f" - {name}: FAILED, {more}\n     {e}")
            failed += 1
            continue
        clear_failure(path, "lore")

        print(f" - {name} ({label})")
        print(f"     lore: {out['lore']}")
        print(f"     keywords: {', '.join(out['keywords'])}")
        prior.append(f"{out['lore']}  [keywords: {', '.join(out['keywords'])}]")
        done += 1

        if args.dry_run:
            continue
        meta["lore"] = {
            "text": out["lore"], "keywords": out["keywords"], "label": label,
            "model": args.llm_model or "", "ts": now, "pxhash": pxhash,
            "src": "generated", "reviewed": False,
        }
        write_meta(path, meta)

    print(f" summary: {done} written, {skipped} skipped, {failed} failed"
          + (" (dry run)" if args.dry_run else ""))


def main():
    ap = argparse.ArgumentParser(
        description="Stage 2: LLM lore + keywords for tagged expression sprites.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("folders", nargs="*", help="character expression folder(s)")
    ap.add_argument("--characters-root", help="process every sprite folder under this dir")
    ap.add_argument("--card", help="card PNG/JSON (only with a single folder)")
    ap.add_argument("--llm-url", default=cfg_get("lore_url", "http://127.0.0.1:5001/v1"),
                    help="OpenAI-compatible base URL (default: %(default)s)")
    ap.add_argument("--llm-model", default="", help="model name to send (optional)")
    ap.add_argument("--api-key", default=os.environ.get("EXPRLORE_API_KEY", ""))
    add_label_args(ap)
    ap.add_argument("--force", action="store_true", help="regenerate existing lore")
    ap.add_argument("--dry-run", action="store_true", help="call LLM, write nothing")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="don't pass earlier entries as do-not-repeat context")
    ap.add_argument("--dedupe-max", type=int, default=20,
                    help="max earlier entries passed as context (default: %(default)s)")
    ap.add_argument("--card-chars", type=int, default=6000,
                    help="truncate each card field to this many chars")
    ap.add_argument("--lore-words", type=int, default=80, help="target max words for lore")
    ap.add_argument("--lore-chars", type=int, default=900, help="hard reject above this")
    ap.add_argument("--max-tokens", type=int, default=600)
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--json-mode", action="store_true",
                    help="send response_format json_object (if backend supports it)")
    args = ap.parse_args()

    folders = resolve_folders(args.folders, args.characters_root)
    if not folders:
        ap.error("no sprite folders given/found")
    labels = load_labels(args, [args.characters_root] if args.characters_root else folders)
    if args.card and len(folders) != 1:
        ap.error("--card only works with exactly one folder")
    for folder in folders:
        process_folder(folder, args, labels)


if __name__ == "__main__":
    main()
