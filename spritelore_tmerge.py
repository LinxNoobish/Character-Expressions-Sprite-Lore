"""
spritelore_tmerge.py - carry your template edits over to a new default.

Three texts per field (system / user prompt):
    base  the shipped default your template started from
    mine  your edited template
    new   the current shipped default

1. base -> new is diffed line by line. Unchanged lines map exactly; lines
   the new default rewrote are matched to their closest new line by shared
   key words ({{PLACEHOLDERS}} count double) and by position.
2. base -> mine gives your edits: changed lines, deleted lines, added lines.
3. Each edit is replayed on the new text at the mapped place:
     - a line you changed that the new default left alone: your line is used
     - a line both changed: your word-level changes are applied to the new
       line; if that fails, the line is split into sentences and each
       sentence you changed is matched to the nearest sentence anywhere in
       the new text (key words + position) and merged there; a sentence
       edit that overlaps the new default's own wording -> conflict
     - a line you deleted: deleted from the new text (conflict if the new
       default rewrote it)
     - lines you added: inserted after the mapped line that preceded them
       (before the next one if that is all that maps), skipped if the new
       text already has the same line
     - your change to a line the new default removed -> conflict
   Every conflict has an id; choices {id: "mine" | "new"} pick a side.
   Unresolved conflicts default to "mine" (your edit wins).
"""

import difflib
import re

STOP = set("""a an the and or of in on to for with by at as is are be it its this that
these those from into than then them they their there not no do does did was were
has have had you your one each any all can will would should may might must only
also more most very just what when how who which where""".split())
TOKEN_RX = re.compile(r"\{\{[^}]+\}\}|\w+|\s+|[^\w\s]")
WORD_RX = re.compile(r"\{\{[^}]+\}\}|[A-Za-z][A-Za-z0-9_'-]+")


def keywords(line):
    out = []
    for w in WORD_RX.findall(line):
        if w.startswith("{{"):
            out += [w.lower(), w.lower()]
        elif len(w) >= 3 and w.lower() not in STOP:
            out.append(w.lower())
    return out


def similarity(a, b):
    if a == b:
        return 1.0
    ka, kb = set(keywords(a)), set(keywords(b))
    jac = len(ka & kb) / len(ka | kb) if (ka or kb) else 0.0
    ph_a = {w for w in ka if w.startswith("{{")}
    ph_b = {w for w in kb if w.startswith("{{")}
    if ph_a and ph_a == ph_b:
        jac = max(jac, 0.6)
    ratio = difflib.SequenceMatcher(None, a.strip(), b.strip(), autojunk=False).ratio()
    return 0.6 * jac + 0.4 * ratio


def map_lines(base, new, threshold=0.38):
    """base index -> new index (or None)."""
    m = {}
    sm = difflib.SequenceMatcher(None, base, new, autojunk=False)
    used = set()
    pending = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                m[i1 + k] = j1 + k
                used.add(j1 + k)
        elif tag in ("replace", "delete"):
            pending.append((i1, i2, j1, j2))
    for i1, i2, j1, j2 in pending:
        for i in range(i1, i2):
            if not base[i].strip():
                continue
            best, bj = 0.0, None
            for j in range(len(new)):
                if j in used or not new[j].strip():
                    continue
                inside = j1 <= j < j2
                dist = 0 if inside else min(abs(j - j1), abs(j - j2)) / max(1, len(new))
                sc = similarity(base[i], new[j]) - 0.25 * dist + (0.05 if inside else 0)
                if sc > best:
                    best, bj = sc, j
            if bj is not None and best >= threshold:
                m[i] = bj
                used.add(bj)
    return m


def pair_block(olines, ulines):
    """Pair lines of a replaced block in order by similarity.
    -> list of (o_index or None, u_index or None)."""
    pairs, uj = [], 0
    for oi, o in enumerate(olines):
        best, bj = 0.0, None
        for j in range(uj, len(ulines)):
            sc = similarity(o, ulines[j])
            if sc > best:
                best, bj = sc, j
        if bj is not None and best >= 0.3:
            for j in range(uj, bj):
                pairs.append((None, j))
            pairs.append((oi, bj))
            uj = bj + 1
        else:
            pairs.append((oi, None))
    for j in range(uj, len(ulines)):
        pairs.append((None, j))
    return pairs


def merge_tokens(o, u, n):
    """Word-level replay of o->u onto n. Returns merged string or None."""
    to, tu, tn = TOKEN_RX.findall(o), TOKEN_RX.findall(u), TOKEN_RX.findall(n)
    m = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, to, tn, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                m[i1 + k] = j1 + k
    edits = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, to, tu, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if i1 < i2:
            idx = [m.get(i) for i in range(i1, i2)]
            if None in idx or idx != list(range(idx[0], idx[0] + len(idx))):
                return None
            edits.append((idx[0], idx[-1] + 1, tu[j1:j2]))
        else:
            before = m.get(i1 - 1) if i1 > 0 else -1
            after = m.get(i1) if i1 < len(to) else len(tn)
            if before is None and after is None:
                return None
            pos = before + 1 if before is not None else after
            if after is not None and before is not None and after != before + 1:
                return None
            edits.append((pos, pos, tu[j1:j2]))
    out = list(tn)
    for s, e, rep in sorted(edits, key=lambda x: x[0], reverse=True):
        out[s:e] = rep
    return "".join(out)


SENT_RX = re.compile(r"(?<=[.!?:;])\s+(?=\S)")


def sentences(line):
    return [x for x in SENT_RX.split(line) if x.strip()]


def merge_field(base_text, mine_text, new_text, field, choices=None):
    """-> (merged_text, changes). changes: list of dicts for the review UI."""
    choices = choices or {}
    base, mine, new = base_text.split("\n"), mine_text.split("\n"), new_text.split("\n")
    bmap = map_lines(base, new)
    cur = list(new)              # working copy of the new text, line by line
    gone = set()                 # new lines deleted
    ins_after = {}               # new index (-1 = top) -> [lines]
    changes = []

    def decide(cid, default="mine"):
        return choices.get(cid, default)

    def anchor_after(oi):
        k = oi
        while k >= 0:
            if k in bmap:
                return bmap[k]
            k -= 1
        return -1

    def anchor_before(oi):
        k = oi
        while k is not None and k < len(base):
            if k in bmap:
                return bmap[k]
            k += 1
        return None

    def note(cid, kind, status, mine_s="", new_s="", base_s="", **kw):
        d = {"id": cid, "field": field, "kind": kind, "status": status,
             "mine": mine_s, "new": new_s, "base": base_s}
        d.update(kw)
        changes.append(d)

    def add_insert(oi_prev, oi_next, lines, cid):
        keep = [ln for ln in lines if not (ln.strip() and any(similarity(ln, x) >= 0.92 for x in cur))]
        if not [ln for ln in keep if ln.strip()]:
            note(cid, "insert", "already", "\n".join(lines))
            return
        at = anchor_after(oi_prev) if oi_prev >= 0 else -1
        if at < 0 and oi_next is not None:
            nb = anchor_before(oi_next)
            at = nb - 1 if nb is not None else -1
        ins_after.setdefault(at, []).extend(keep)
        where = cur[at] if 0 <= at < len(cur) else "(top)"
        note(cid, "insert", "applied", "\n".join(keep), where=where.strip()[:120])

    def find_sentence(so, prefer_j):
        best, hit = 0.0, None
        for j, line in enumerate(cur):
            if j in gone or not line.strip():
                continue
            for sn in sentences(line):
                sc = similarity(so, sn) + (0.05 if j == prefer_j else 0)
                if sc > best:
                    best, hit = sc, (j, sn)
        return hit if best >= 0.3 else None

    def context_patch(so, su, prefer_j):
        """Each word-level edit of so->su is located in the new text by the
        words around it (4, then 3, 2, 1 words of context each side) and
        applied there. -> (ok, [changed line indexes])."""
        to, tu = TOKEN_RX.findall(so), TOKEN_RX.findall(su)
        edits = []
        for op in difflib.SequenceMatcher(None, to, tu, autojunk=False).get_opcodes():
            if op[0] == "equal":
                continue
            if edits:
                p = edits[-1]
                gap = [x for x in to[p[2]:op[1]] if not x.isspace()]
                if len(gap) <= 1:
                    edits[-1] = ("replace", p[1], op[2], p[3], op[4])
                    continue
            edits.append(op)
        touched = []
        for _tag, i1, i2, j1, j2 in reversed(edits):
            done = False
            for ctx in (4, 3, 2, 1):
                left, k, n = [], i1 - 1, 0
                while k >= 0 and n < ctx:
                    left.insert(0, to[k])
                    n += 0 if to[k].isspace() else 1
                    k -= 1
                right, k, n = [], i2, 0
                while k < len(to) and n < ctx:
                    right.append(to[k])
                    n += 0 if to[k].isspace() else 1
                    k += 1
                words = [x for x in left + to[i1:i2] + right if not x.isspace()]
                if len(words) < 2:
                    continue

                def pat(toks):
                    return "".join(r"\s+" if x.isspace() else re.escape(x) for x in toks)
                rx = re.compile("(" + pat(left) + ")" + pat(to[i1:i2]) + "(" + pat(right) + ")")
                hits = [(j, m) for j, line in enumerate(cur) if j not in gone
                        for m in rx.finditer(line)]
                if not hits:
                    continue
                if len(hits) > 1:
                    near = [h for h in hits if h[0] == prefer_j]
                    if len(near) != 1:
                        continue
                    hits = near
                j, m = hits[0]
                cur[j] = cur[j][:m.start()] + m.group(1) + "".join(tu[j1:j2]) + m.group(2) + cur[j][m.end():]
                touched.append(j)
                done = True
                break
            if not done:
                return False, touched
        return True, touched

    def sentence_merge(oi, o_line, u_line, prefer_j):
        """Replays sentence-level edits of one changed line anywhere in the
        new text. -> True when every edit found a place."""
        so_l, su_l = sentences(o_line), sentences(u_line)
        all_ok = True
        prev_target = None
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, so_l, su_l, autojunk=False).get_opcodes():
            if tag == "equal":
                hit = find_sentence(so_l[i2 - 1], prefer_j)
                prev_target = hit or prev_target
                continue
            for po, pu in pair_block(so_l[i1:i2], su_l[j1:j2]):
                cid = f"{field}:s:{oi}:{i1 + (po if po is not None else 0)}:{j1 + (pu if pu is not None else 0)}"
                if po is None:
                    su = su_l[j1 + pu]
                    if find_sentence(su, prefer_j) and similarity(su, find_sentence(su, prefer_j)[1]) >= 0.92:
                        note(cid, "insert", "already", su)
                        continue
                    if prev_target:
                        j, sn = prev_target
                        cur[j] = cur[j].replace(sn, sn + " " + su, 1)
                        prev_target = (j, sn + " " + su)
                        note(cid, "insert", "applied", su, where=sn[:120])
                    else:
                        at = prefer_j if prefer_j is not None else anchor_after(oi - 1)
                        ins_after.setdefault(at, []).append(su)
                        note(cid, "insert", "applied", su, where="(own line)")
                    continue
                so = so_l[i1 + po]
                hit = find_sentence(so, prefer_j)
                if pu is None:
                    if hit and similarity(so, hit[1]) >= 0.92:
                        j, sn = hit
                        cur[j] = re.sub(r"\s{2,}", " ", cur[j].replace(sn, "", 1)).strip()
                        note(cid, "delete", "applied", "", sn, so)
                    else:
                        note(cid, "delete", "already", "", "", so)
                    continue
                su = su_l[j1 + pu]
                if hit is None:
                    pick = decide(cid)
                    if pick == "mine":
                        at = prefer_j if prefer_j is not None else anchor_after(oi - 1)
                        ins_after.setdefault(at, []).append(su)
                    note(cid, "modify", "conflict", su, "(no matching sentence in the new default)", so,
                         choice=pick)
                    all_ok = False
                    continue
                j, sn = hit
                if sn == so:
                    cur[j] = cur[j].replace(sn, su, 1)
                    note(cid, "modify", "applied", su, sn, so)
                    prev_target = (j, su)
                    continue
                merged = merge_tokens(so, su, sn)
                if merged is not None:
                    cur[j] = cur[j].replace(sn, merged, 1)
                    note(cid, "modify", "merged", su, sn, so, result=merged)
                    prev_target = (j, merged)
                    continue
                snapshot = list(cur)
                ok, touched = context_patch(so, su, j)
                if ok and touched:
                    note(cid, "modify", "merged", su, sn, so,
                         result=" / ".join(cur[t] for t in sorted(set(touched)))[:400])
                    continue
                cur[:] = snapshot
                pick = decide(cid)
                close = similarity(so, sn) >= 0.6
                if pick == "mine":
                    if close:
                        cur[j] = cur[j].replace(sn, su, 1)
                    else:
                        ins_after.setdefault(j, []).append(su)
                note(cid, "modify", "conflict", su, sn, so, choice=pick,
                     how="replaces the new sentence" if close else "added as its own line after it")
                prev_target = (j, su if (pick == "mine" and close) else sn)
                all_ok = False
        return all_ok

    ops = difflib.SequenceMatcher(None, base, mine, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        if tag == "insert":
            add_insert(i1 - 1, i1 if i1 < len(base) else None, mine[j1:j2], f"{field}:ins:{i1}:{j1}")
            continue
        pairs = pair_block(base[i1:i2], mine[j1:j2])
        last_o, run = i1 - 1, []
        for po, pu in pairs:
            if po is None:
                run.append(mine[j1 + pu])
                continue
            if run:
                add_insert(last_o, i1 + po, run, f"{field}:ins:{i1 + po}:{j1}")
                run = []
            oi = i1 + po
            last_o = oi
            o_line = base[oi]
            j = bmap.get(oi)
            if pu is None:
                cid = f"{field}:del:{oi}"
                if j is None:
                    note(cid, "delete", "already", "", "", o_line)
                elif new[j] == o_line:
                    gone.add(j)
                    note(cid, "delete", "applied", "", new[j], o_line)
                else:
                    pick = decide(cid)
                    if pick == "mine":
                        gone.add(j)
                    note(cid, "delete", "conflict", "(deleted)", new[j], o_line, choice=pick)
                continue
            u_line = mine[j1 + pu]
            cid = f"{field}:mod:{oi}"
            if j is not None and new[j] == o_line:
                cur[j] = u_line
                note(cid, "modify", "applied", u_line, new[j], o_line)
                continue
            merged = merge_tokens(o_line, u_line, new[j]) if j is not None else None
            if merged is not None:
                cur[j] = merged
                note(cid, "modify", "merged", u_line, new[j], o_line, result=merged)
                continue
            sentence_merge(oi, o_line, u_line, j)
        if run:
            add_insert(last_o, i2 if i2 < len(base) else None, run, f"{field}:ins:{i2}:{j2}")

    out = list(ins_after.get(-1, []))
    for j, line in enumerate(cur):
        if j not in gone:
            out.append(line)
        out.extend(ins_after.get(j, []))
    return "\n".join(out), changes


def best_base(t, history, fields=("system", "user")):
    """The shipped template (any version, any id) this one most likely
    started from. -> (version, template) or (None, None)."""
    text = "\n".join(t.get(f, "") for f in fields)
    best, pick = 0.0, (None, None)
    for ver in history:
        for h in ver.get("templates", []):
            htext = "\n".join(h.get(f, "") for f in fields)
            sc = difflib.SequenceMatcher(None, text, htext, autojunk=False).quick_ratio()
            if sc < best - 0.05:
                continue
            sc = difflib.SequenceMatcher(None, text.split("\n"), htext.split("\n"), autojunk=False).ratio() * 0.5 \
                + difflib.SequenceMatcher(None, text, htext, autojunk=False).ratio() * 0.5
            if h.get("id") == t.get("id"):
                sc += 0.05
            if sc > best or (sc == best and ver["version"] > (pick[0] or 0)):
                best, pick = sc, (ver["version"], h)
    return pick if best >= 0.45 else (None, None)


def upgrade_template(t, history, current, choices=None):
    """-> report dict. current: list of current default templates."""
    choices = choices or {}
    rep = {"id": t.get("id"), "name": t.get("name"), "status": "none", "changes": [],
           "merged": None, "base": None}
    cur_by_id = {c["id"]: c for c in current}
    if any(c.get("system") == t.get("system") and c.get("user") == t.get("user") for c in current):
        rep["status"] = "current"
        return rep
    ver, base = best_base(t, history)
    if base is None:
        rep["status"] = "custom"
        return rep
    target = cur_by_id.get(base["id"])
    rep["base"] = f"v{ver}:{base['id']}"
    if target is None:
        rep["status"] = "no-successor"
        return rep
    if target.get("system") == base.get("system") and target.get("user") == base.get("user"):
        rep["status"] = "current"
        return rep
    merged = dict(t)
    for f in ("system", "user"):
        text, ch = merge_field(base.get(f, ""), t.get(f, ""), target.get(f, ""), f, choices)
        merged[f] = text
        rep["changes"] += ch
    rep["merged"] = merged
    rep["status"] = "conflicts" if any(c["status"] == "conflict" for c in rep["changes"]) else "clean"
    return rep
