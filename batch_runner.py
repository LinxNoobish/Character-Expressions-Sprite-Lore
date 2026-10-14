#!/usr/bin/env python3
"""
batch_runner.py - run the expression lore pipeline over the character
library in segments of ~150 images, with an audit index.

    python3 batch_runner.py                 # tag the next segment AND, at the same
                                            # time, write lore for every folder that
                                            # is already fully tagged
    python3 batch_runner.py run --stage tag # tag the next segment only
    python3 batch_runner.py run --stage lore# lore for all tagged folders only
    python3 batch_runner.py run --count 3   # three cycles back to back
    python3 batch_runner.py run --segment 4 # work on segment 4 only
    python3 batch_runner.py status          # show the segment table
    python3 batch_runner.py audit           # rescan everything, rebuild segments

Each default run is one pipeline step: the VLM tags segment N while the LLM
writes lore for segments tagged earlier. The last segment's lore is written
by the next run.

The index is written next to this script:
    exprlore_index.json   machine-readable (used by this script)
    exprlore_index.txt    human-readable table

Segments are fixed at audit time: folders are taken alphabetically and
grouped to about 150 images (a segment under 100 always takes the next whole
folder; a folder over 150 images is its own segment), so the numbers don't
shift as folders finish. Re-run `audit` after adding new characters.
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

from exprlore_common import (
    PNGError, find_card_for_folder, list_images, pixel_hash,
    discover_sprite_folders, META_KEYWORD, get_text_chunk,
    MAX_FAILS, failure_count, load_failures, cfg_get,
)
import base64

# ---- setup (from the Sprite Lore config when installed) ----------------------
CHARACTERS_ROOT = os.path.expanduser(
    (cfg_get("sprite_roots") or [cfg_get("st_characters",
     "~/SillyTavern/data/default-user/characters")])[0])
VLM_URL = cfg_get("vision_url", "http://127.0.0.1:5001/v1")
LLM_URL = cfg_get("lore_url", "http://127.0.0.1:5001/v1")
SEGMENT_TARGET = 150   # aim for this many images per segment
SEGMENT_MIN = 100      # below this, keep adding the next whole folder even past the target
# ----------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_JSON = os.path.join(HERE, "exprlore_index.json")
INDEX_TXT = os.path.join(HERE, "exprlore_index.txt")


def audit_folder(folder):
    """Scan one sprite folder and report how far through the pipeline it is."""
    imgs = list_images(folder)
    rec = {"name": os.path.basename(folder.rstrip("/")), "path": folder,
           "images": len(imgs), "png": 0, "convertible": 0, "gif": 0,
           "tagged": 0, "stale": 0, "lore": 0, "bad": 0,
           "tag_failed": 0, "lore_failed": 0,
           "card": bool(find_card_for_folder(folder))}
    fails = load_failures()
    for p in imgs:
        ext = os.path.splitext(p)[1].lower()
        if ext == ".gif":
            rec["gif"] += 1
            continue
        if ext != ".png":
            rec["convertible"] += 1
            continue
        rec["png"] += 1
        try:
            with open(p, "rb") as f:
                data = f.read()
            raw = get_text_chunk(data, META_KEYWORD)
            meta = json.loads(base64.b64decode(raw)) if raw else {}
            px = pixel_hash(data)
        except (PNGError, ValueError, OSError):
            rec["bad"] += 1
            continue
        if meta.get("vlm") and meta.get("pxhash") == px:
            rec["tagged"] += 1
            lore = meta.get("lore")
            if lore and not lore.get("stale") and lore.get("pxhash") == px:
                rec["lore"] += 1
            elif failure_count(p, "lore", fails) >= MAX_FAILS:
                rec["lore_failed"] += 1
        else:
            if meta.get("vlm"):
                rec["stale"] += 1
            if failure_count(p, "tag", fails) >= MAX_FAILS:
                rec["tag_failed"] += 1
    workable = rec["png"] + rec["convertible"] - rec["bad"]
    if workable == 0:
        status = "empty"
    elif rec["tagged"] + rec["tag_failed"] < workable:
        status = "untagged" if rec["tagged"] == 0 else "partial-tags"
    elif not rec["card"]:
        status = "no-card"          # tagged, but lore can't be written
    elif rec["lore"] + rec["lore_failed"] < rec["tagged"]:
        status = "needs-lore"
    else:
        status = "done"
    rec["status"] = status
    return rec


FINISHED = {"done", "empty", "no-card"}


def workable(rec):
    return max(0, rec["png"] + rec["convertible"] - rec["bad"])


def segment_state(folders):
    if all(f["status"] in FINISHED for f in folders):
        return "complete"
    if all(f["status"] == "untagged" for f in folders):
        return "pending"
    return "in-progress"


def build_index(root):
    folders = discover_sprite_folders(root)
    print(f"auditing {len(folders)} folders under {root} ...")
    recs = []
    for i, f in enumerate(folders, 1):
        recs.append(audit_folder(f))
        if i % 25 == 0:
            print(f"  {i}/{len(folders)}")
    segs = []

    def close(chunk):
        if chunk:
            segs.append({"segment": len(segs) + 1, "folders": chunk,
                         "images": sum(workable(f) for f in chunk),
                         "state": segment_state(chunk), "last_run": None})

    chunk, total = [], 0
    for r in recs:
        n = workable(r)
        if n > SEGMENT_TARGET:              # big folder: its own run
            close(chunk)
            chunk, total = [], 0
            close([r])
            continue
        if chunk and total + n > SEGMENT_TARGET and total >= SEGMENT_MIN:
            close(chunk)
            chunk, total = [], 0
        chunk.append(r)                     # under 100: round up to this folder's last image
        total += n
    close(chunk)
    return {"root": root, "segment_target": SEGMENT_TARGET,
            "audited": dt.datetime.now().isoformat(timespec="seconds"),
            "segments": segs}


def save_index(idx):
    for s in idx["segments"]:
        s["state"] = segment_state(s["folders"])
    with open(INDEX_JSON, "w", encoding="utf-8") as f:
        json.dump(idx, f, indent=1)
    with open(INDEX_TXT, "w", encoding="utf-8") as f:
        f.write(render_table(idx, full=True))


def load_index():
    if not os.path.exists(INDEX_JSON):
        return None
    with open(INDEX_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def render_table(idx, full=False):
    out = [f"Expression lore index  root: {idx['root']}  audited: {idx['audited']}", ""]
    tot = {"complete": 0, "in-progress": 0, "pending": 0}
    for s in idx["segments"]:
        tot[s["state"]] = tot.get(s["state"], 0) + 1
        names = ", ".join(f["name"] for f in s["folders"])
        ran = f"  last run {s['last_run']}" if s.get("last_run") else ""
        imgs = s.get("images", sum(workable(f) for f in s["folders"]))
        out.append(f"Segment {s['segment']:>3}  [{s['state']}]  "
                   f"{len(s['folders'])} folder(s), {imgs} images{ran}")
        if full:
            for f in s["folders"]:
                extra = []
                if f["convertible"]:
                    extra.append(f"{f['convertible']} jpg/webp")
                if f["gif"]:
                    extra.append(f"{f['gif']} gif skipped")
                if f["stale"]:
                    extra.append(f"{f['stale']} stale")
                if f["bad"]:
                    extra.append(f"{f['bad']} unreadable")
                if f.get("tag_failed"):
                    extra.append(f"{f['tag_failed']} gave up tagging")
                if f.get("lore_failed"):
                    extra.append(f"{f['lore_failed']} gave up lore")
                if not f["card"]:
                    extra.append("NO CARD")
                out.append(f"    {f['name']:<28} {f['status']:<13} "
                           f"tagged {f['tagged']}/{f['png'] + f['convertible'] - f['bad']}  "
                           f"lore {f['lore']}/{f['tagged']}"
                           + (f"   ({', '.join(extra)})" if extra else ""))
        else:
            out.append(f"    {names}")
    out += ["", f"{len(idx['segments'])} segments: {tot['complete']} complete, "
                f"{tot['in-progress']} in progress, {tot['pending']} pending"]
    return "\n".join(out) + "\n"


TAG_NEEDED = {"untagged", "partial-tags"}


def all_folders(idx):
    for seg in idx["segments"]:
        for i, f in enumerate(seg["folders"]):
            yield seg, i, f


def refresh(idx, only_unfinished=True):
    """Re-audit folders so statuses reflect what's on disk now."""
    for seg, i, f in all_folders(idx):
        if not only_unfinished or f["status"] not in FINISHED:
            seg["folders"][i] = audit_folder(f["path"])


def reaudit(idx, paths):
    paths = set(paths)
    for seg, i, f in all_folders(idx):
        if f["path"] in paths:
            seg["folders"][i] = audit_folder(f["path"])


def _pump(proc, tag):
    import threading

    def run():
        for line in proc.stdout:
            sys.stdout.write(f"[{tag}] {line}")
            sys.stdout.flush()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def launch(script, folders, extra, tag):
    cmd = [sys.executable, "-u", os.path.join(HERE, script)] + folders + extra
    print(f"[{tag}] >>> {script} on {len(folders)} folder(s): "
          + ", ".join(os.path.basename(f.rstrip('/')) for f in folders))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    return proc, _pump(proc, tag)


def pick_tag_segment(idx, args):
    segs = idx["segments"]
    if args.segment:
        seg = next((s for s in segs if s["segment"] == args.segment), None)
        if seg is None:
            sys.exit(f"no segment {args.segment} (have 1-{len(segs)})")
        return seg
    return next((s for s in segs
                 if any(f["status"] in TAG_NEEDED for f in s["folders"])), None)


def cycle(idx, args):
    """One step: tag the next segment while the LLM writes lore for every
    folder that is already fully tagged. Returns False when nothing was done
    or the tagger failed."""
    refresh(idx)
    tag_seg = pick_tag_segment(idx, args) if args.stage in ("tag", "both") else None
    tag_folders = []
    if tag_seg:
        tag_folders = [f["path"] for f in tag_seg["folders"]
                       if f["status"] in TAG_NEEDED or args.force]
    lore_folders = []
    if args.stage in ("lore", "both"):
        if args.segment:
            pool = pick_tag_segment(idx, args)["folders"]
        else:
            pool = [f for _, _, f in all_folders(idx)]
        lore_folders = [f["path"] for f in pool
                        if (f["status"] == "needs-lore"
                            or (args.force and args.segment and f["card"] and f["tagged"]))
                        and f["path"] not in tag_folders]

    if not tag_folders and not lore_folders:
        print("Nothing left to do for this stage. "
              "Run `audit` after adding characters.")
        return False

    force = ["--force"] if args.force else []
    if tag_seg and tag_folders:
        print(f"\n######## tagging segment {tag_seg['segment']} "
              f"({len(tag_folders)} folders) ########")
    if lore_folders:
        print(f"######## lore for {len(lore_folders)} tagged folder(s) ########")

    procs = []
    if tag_folders:
        procs.append(("tag", launch("tag_sprites.py", tag_folders,
                     ["--vlm-url", args.vlm_url]
                     + (["--rename"] if args.rename else []) + force, "tag ")))
    if lore_folders:
        procs.append(("lore", launch("write_lore.py", lore_folders,
                     ["--llm-url", args.llm_url] + force, "lore")))
    ok = True
    for name, (proc, pump) in procs:
        rc = proc.wait()
        pump.join()
        if rc != 0:
            print(f"!! {name} stage exited with code {rc}")
            if name == "tag":
                ok = False

    reaudit(idx, tag_folders + lore_folders)
    now = dt.datetime.now().isoformat(timespec="seconds")
    if tag_seg:
        tag_seg["last_run"] = now
    save_index(idx)

    print("\n---- after this run ----")
    for seg, _, f in all_folders(idx):
        if f["path"] in tag_folders or f["path"] in lore_folders:
            print(f"    seg {seg['segment']:>3}  {f['name']:<28} {f['status']}")
    return ok


def main():
    ap = argparse.ArgumentParser(
        description="Segmented batch runner for the expression lore pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("command", nargs="?", default="run", choices=["run", "status", "audit"])
    ap.add_argument("--segment", type=int, help="work on this segment number only")
    ap.add_argument("--count", type=int, default=1,
                    help="number of cycles to run back to back (default 1)")
    ap.add_argument("--stage", choices=["both", "tag", "lore"], default="both",
                    help="both = tag next segment + lore in parallel (default)")
    ap.add_argument("--rename", action="store_true", help="pass --rename to tagging")
    ap.add_argument("--force", action="store_true", help="redo tags/lore for the folders run")
    ap.add_argument("--root", default=CHARACTERS_ROOT)
    ap.add_argument("--vlm-url", default=VLM_URL)
    ap.add_argument("--llm-url", default=LLM_URL)
    args = ap.parse_args()

    idx = load_index()
    if args.command == "audit" or idx is None or idx.get("root") != args.root:
        idx = build_index(args.root)
        save_index(idx)
        print(f"index written: {INDEX_TXT}")
        if args.command == "audit":
            print(render_table(idx))
            return

    if args.command == "status":
        refresh(idx)
        save_index(idx)
        print(render_table(idx, full=True))
        return

    for n in range(args.count):
        if args.count > 1:
            print(f"\n================ cycle {n + 1}/{args.count} ================")
        if not cycle(idx, args):
            break
        if args.segment:
            break
    print(f"\nindex updated: {INDEX_TXT}")


if __name__ == "__main__":
    main()
