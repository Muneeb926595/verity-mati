"""Where a correction points, and keeping it pointed there as the code changes.

A correction's target is a line of real, indexed code: the function it is in and, when the line is a check or inside
one, that check (branch) and which side of it. The target line's code is fingerprinted; after every index the
fingerprint is looked up again inside the same function:

- found at the same line            nothing changes
- found elsewhere in the function   the correction follows it (the function grew, or moved down the file)
- not found / function gone         the correction turns stale: listed for review, never applied
- a stale correction whose exact code is back (an edit was undone) becomes active again
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .store import LearnStore
from .text import FN_KINDS, anchor_of, norm_code, short, sym_display

LOOP_KINDS = ("while", "do", "for")
BRANCH_KINDS = ("if", "ternary", "switch", "catch", "early_exit") + LOOP_KINDS


def _lines(root: Path, rel: str) -> list[str]:
    try:
        return (Path(root) / rel).read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return []


def _line(lines: list[str], n: int | None) -> str:
    return lines[n - 1].rstrip() if n and 0 < n <= len(lines) else ""


def enclosing_function(fs, file: str, line: int) -> dict | None:
    """The innermost function definition (of the default build first) whose body contains the line."""
    rows = fs.q("SELECT * FROM symbol WHERE file=? AND is_def=1 AND kind IN (?,?,?) AND line<=? AND "
                "coalesce(end_line, line)>=? ORDER BY line DESC", (file, *FN_KINDS, line, line))
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: (str(r["cfg"] or "default").startswith("+"), -(r["line"] or 0),
                                       (r["end_line"] or r["line"]) - (r["line"] or 0)))
    return dict(rows[0])


def enclosing_branch(fs, fn: str, file: str, line: int) -> tuple[dict | None, str | None]:
    """The innermost check of the function that the line is, or is inside: (branch, side) where side is
    then | else | case. A line outside every check gives (None, None)."""
    best = None
    for r in fs.q("SELECT * FROM branch WHERE fn=? AND file=?", (fn, file)):
        b = dict(r)
        if b["kind"] not in BRANCH_KINDS or not b.get("cond") and b["kind"] != "catch":
            continue
        side = None
        if b["line"] == line:
            side = "then"
        elif b.get("then_s") and b["then_s"] <= line <= (b.get("then_e") or b["then_s"]):
            side = "then"
        elif b.get("else_s") and b["else_s"] <= line <= (b.get("else_e") or b["else_s"]):
            side = "else"
        if side is None:
            continue
        if b["kind"] == "switch" and line != b["line"]:
            side = "case"
        # innermost: the latest-starting check, then the one with the smallest body
        span = (b.get("else_e") or b.get("then_e") or b["line"]) - b["line"]
        k = (b["line"], -span)
        if best is None or k > best[0]:
            best = (k, b, side)
    return (best[1], best[2]) if best else (None, None)


def resolve_line(fs, root: Path, file: str, line: int) -> dict[str, Any]:
    """A line picked by a person, as a correction target. Raises ValueError with a plain message when it is not a line
    of the indexed project's code."""
    from .store import LearnError
    file = (file or "").replace("\\", "/")
    while file.startswith("./"):
        file = file[2:]
    if not file:
        raise LearnError("Pick a file and a line.")
    row = fs.one("SELECT sha FROM file WHERE path=?", (file,))
    if not row:
        raise LearnError(f"{file} is not part of the indexed project (index it first, or pick a line in a project file).")
    if not file_unchanged(root, file, row["sha"]):
        raise LearnError(f"{file} changed since the project was last indexed. Update the index first, so the line is "
                         "matched to the code as it is now.")
    lines = _lines(root, file)
    try:
        line = int(line)
    except (TypeError, ValueError):
        raise LearnError("The line must be a number.") from None
    if not (1 <= line <= len(lines)):
        raise LearnError(f"{file} has {len(lines)} lines; line {line} does not exist.")
    text = _line(lines, line)
    if not norm_code(text):
        raise LearnError(f"Line {line} of {file} is empty; pick a line of code.")
    fn = enclosing_function(fs, file, line)
    if not fn:
        raise LearnError(f"Line {line} of {file} is not inside a function; pick a line inside the function that "
                         "answers the question.")
    br, side = enclosing_branch(fs, fn["key"], file, line)
    disp = sym_display(fn)
    if br:
        cond = br.get("cond") or ""
        if side == "else":
            label = f"{disp} when `{short(cond, 120)}` is not true ({file}:{br['line']}): `{short(norm_code(text), 100)}`" \
                if line != br["line"] else f"{disp} checks `{short(cond, 120)}` ({file}:{br['line']}), other side"
        elif line == br["line"]:
            label = f"{disp} checks `{short(cond, 140)}` ({file}:{br['line']})"
        else:
            label = f"{disp}, when `{short(cond, 120)}` ({file}:{br['line']}): `{short(norm_code(text), 100)}` (line {line})"
    else:
        label = f"{disp} at {file}:{line}: `{short(norm_code(text), 120)}`"
    return {"target_fn": fn["key"], "target_name": disp[:-2] if disp.endswith("()") else disp,
            "target_file": file, "target_line": line, "branch_line": br["line"] if br else None,
            "side": side, "target_cond": (br or {}).get("cond"), "target_label": label,
            "anchor": anchor_of(text), "branch_anchor": anchor_of(_line(lines, br["line"])) if br else None,
            "rel_line": line - (fn["line"] or line), "fn_line": fn["line"], "fn_end": fn["end_line"],
            "kind_of_branch": (br or {}).get("kind")}


def file_unchanged(root: Path, rel: str, sha: str | None) -> bool:
    """The file on disk is the one that was indexed (the index stores its sha256)."""
    if not sha:
        return True
    try:
        return hashlib.sha256((Path(root) / rel).read_bytes()).hexdigest() == sha
    except OSError:
        return False


def index_mark(fs) -> str | None:
    """Changes whenever indexed code changes: a completed index, and also files re-indexed by an index that was then
    cancelled or failed (their rows are kept)."""
    done = fs.meta_get("indexed_at")
    r = fs.one("SELECT max(indexed_at) AS m, count(*) AS n FROM file")
    last, n = (r["m"], r["n"]) if r else (None, 0)
    if done is None and last is None:
        return None
    return f"{done}|{last}|{n}"


def _find(lines: list[str], anchor: str | None, start: int, end: int, near: int) -> int | None:
    """The line of the function [start, end] with this fingerprint closest to `near`."""
    if not anchor:
        return None
    hits = [n for n in range(max(1, start), min(len(lines), end) + 1) if anchor_of(lines[n - 1]) == anchor]
    if not hits:
        return None
    return min(hits, key=lambda n: (abs(n - near), n))


def locate(fs, root: Path, c: dict) -> tuple[bool, dict[str, Any], str]:
    """Where a correction's target is in the indexed code now: (found, changed fields, reason if not found)."""
    fn = fs.one("SELECT * FROM symbol WHERE key=? AND is_def=1 ORDER BY line LIMIT 1", (c["target_fn"],))
    if fn is None and c.get("target_name"):
        # a static C function's key can change with its file; same name in the same file is the same function
        fn = fs.one("SELECT * FROM symbol WHERE name=? AND file=? AND is_def=1 AND kind IN (?,?,?) LIMIT 1",
                    (c["target_name"].split(".")[-1], c.get("target_file"), *FN_KINDS))
    if fn is None:
        return False, {}, f"the function {c.get('target_name') or c['target_fn']}() no longer exists"
    fn = dict(fn)
    file = fn["file"]
    lines = _lines(root, file)
    if not lines:
        return False, {}, f"{file} could not be read"
    start, end = fn["line"] or 1, fn["end_line"] or fn["line"] or 1
    changes: dict[str, Any] = {}
    if fn["key"] != c["target_fn"]:
        changes["target_fn"] = fn["key"]
    if file != c.get("target_file"):
        changes["target_file"] = file
    want = start + int(c.get("rel_line") or 0)
    tl = c.get("target_line")
    if not (tl and start <= tl <= end and anchor_of(_line(lines, tl)) == c.get("anchor")):
        tl = _find(lines, c.get("anchor"), start, end, want)
        if tl is None:
            return False, {}, f"the code at {c.get('target_file')}:{c.get('target_line')} changed or was removed"
        changes["target_line"] = tl
    if tl - start != c.get("rel_line"):
        changes["rel_line"] = tl - start
    bl = c.get("branch_line")
    if bl:
        if not (start <= bl <= end and anchor_of(_line(lines, bl)) == c.get("branch_anchor")):
            bl = _find(lines, c.get("branch_anchor"), start, end, tl if c.get("side") != "else" else bl)
            if bl is None:
                return False, {}, f"the check at {c.get('target_file')}:{c.get('branch_line')} changed or was removed"
            changes["branch_line"] = bl
        if not fs.one("SELECT 1 FROM branch WHERE fn=? AND file=? AND line=?", (fn["key"], file, bl)):
            return False, {}, f"line {bl} of {file} is no longer a check"
    return True, changes, ""


def reanchor(ls: LearnStore, fs, root: Path, force: bool = False) -> dict[str, list[str]]:
    """Check every correction against the current index (once per index, unless forced). Returns what changed."""
    mark = index_mark(fs)
    out: dict[str, list[str]] = {"moved": [], "stale": [], "revived": []}
    if mark is None or (not force and ls.meta_get("anchored_mark") == mark):
        return out
    for c in ls.corrections():
        if c["status"] not in ("active", "stale"):
            continue
        try:
            ok, changes, why = locate(fs, root, c)
        except Exception as e:  # noqa: BLE001 — one odd record must not stop the others
            ok, changes, why = False, {}, f"could not be checked ({type(e).__name__})"
        # every update is conditional on the status read above: an Undo made meanwhile is never overwritten
        if ok:
            fields = dict(changes)
            if c["status"] == "stale":
                fields.update(status="active", status_reason="the code it points at is back")
                if ls.update_correction_if(c["id"], "stale", **fields):
                    out["revived"].append(c["id"])
                    ls.log("revive", c["id"], fields)
            elif changes.get("target_line") or changes.get("branch_line"):
                moved = changes.get("target_line") or c["target_line"]
                fields["status_reason"] = f"followed the code from line {c['target_line']} to line {moved}"
                if ls.update_correction_if(c["id"], "active", **fields):
                    out["moved"].append(c["id"])
                    ls.log("move", c["id"], fields)
            elif fields:
                ls.update_correction_if(c["id"], c["status"], **fields)
        elif c["status"] == "active":
            if ls.update_correction_if(c["id"], "active", status="stale", status_reason=why):
                out["stale"].append(c["id"])
                ls.log("stale", c["id"], {"reason": why})
    ls.meta_set("anchored_mark", mark)
    return out
