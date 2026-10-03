"""learned.db: what people taught Verity about one project. Kept apart from index.db, which is rebuildable: a full
re-index, a deleted index or a corrupted one never touches this file.

Tables
  correction    one per "the right place is here"; anchored to the line's code text, active | stale | removed
  glossary      a word people use -> the identifiers the code uses for it; suggested | accepted | rejected
  confirmation  "the answer was right": kept as a test case, never changes answers
  event         an append-only log of every change (who did what, when), for the Corrections panel and audits
  meta          schema version, the index state the anchors were last checked against, the next correction number
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS correction (
  id TEXT PRIMARY KEY,                 -- c-0007
  seq INTEGER,
  created REAL, updated REAL, author TEXT, note TEXT,
  answer_id TEXT,                      -- answer.id in index.db (may be gone after an index reset; nothing depends on it)
  question TEXT, qnorm TEXT, intent TEXT,
  topic TEXT,                          -- the router's topic words, JSON list
  qvec TEXT,                           -- the question's meaning vector (JSON list), when an embedding model was used
  wrong TEXT,                          -- JSON {fn, file, line, side, label}: what ranked first
  kind TEXT,                           -- pick (one of the offered alternatives) | line (a line picked in the source)
  target_fn TEXT, target_name TEXT, target_file TEXT, target_line INTEGER,
  branch_line INTEGER, side TEXT, target_cond TEXT, target_label TEXT,
  anchor TEXT,                         -- sha1 of the target line's code (whitespace-normalised)
  branch_anchor TEXT,                  -- sha1 of the branch line's code, when the target is inside a branch
  rel_line INTEGER,                    -- target line - first line of its function (to follow a moved function body)
  status TEXT,                         -- active | stale | removed
  status_reason TEXT,
  conflict TEXT,                       -- JSON list of correction ids that disagree with this one on the same question
  replaces TEXT,                       -- the correction this one replaced (the user corrected a corrected answer)
  uses INTEGER DEFAULT 0, last_used REAL,
  source TEXT                          -- local | import
);
CREATE TABLE IF NOT EXISTS glossary (
  term TEXT PRIMARY KEY, identifiers TEXT,   -- "bus-off" -> ["CO_CAN_ERRTX_BUS_OFF"]
  source TEXT,                               -- the correction id it came from, or "manual" / "import"
  status TEXT,                               -- suggested | accepted | rejected
  created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS confirmation (
  id TEXT PRIMARY KEY, answer_id TEXT, created REAL, question TEXT, intent TEXT, topic TEXT,
  target_fn TEXT, target_name TEXT, target_file TEXT, target_line INTEGER, must_mention TEXT, note TEXT,
  status TEXT                                -- active | removed
);
CREATE TABLE IF NOT EXISTS event (ts REAL, action TEXT, ref TEXT, detail TEXT);
"""

CORRECTION_JSON = ("topic", "qvec", "wrong", "conflict")


class LearnError(Exception):
    """A request the learning store refuses (bad pick, unknown id, line not in the project). The message is for the
    person who asked."""


class LearnStore:
    """learned.db for one project. Thread-safe; every change is one transaction."""

    def __init__(self, path: str | os.PathLike):
        self.path = os.fspath(path)
        self._lock = threading.RLock()
        self.recovered_from: str | None = None
        self.conn = self._open()

    # ------------------------------------------------------------------------------------------ connection
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.execute("SELECT count(*) FROM correction").fetchone()
        return conn

    def _open(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = self._connect()
        except sqlite3.OperationalError as e:
            # locked by another process, no permission, disk error: the file is not damaged, so never move it aside
            raise LearnError(f"This project's corrections could not be opened ({e}). Close other Verity windows on "
                             "this project and try again.") from None
        except sqlite3.DatabaseError:
            # Unlike the index, corrections cannot be rebuilt: keep the damaged file (it may be recoverable by hand),
            # start a fresh one, and say so wherever corrections are listed.
            bad = f"{self.path}.corrupt-{int(time.time())}"
            try:
                os.replace(self.path, bad)
            except OSError:
                bad = ""
            for ext in ("-wal", "-shm"):
                try:
                    os.replace(self.path + ext, bad + ext) if bad else os.remove(self.path + ext)
                except OSError:
                    pass
            conn = self._connect()
            self.recovered_from = bad or None
        with self._lock:
            if conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone() is None:
                conn.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
            if self.recovered_from:
                conn.execute("INSERT OR REPLACE INTO meta VALUES('recovered_from', ?)", (self.recovered_from,))
        rec = conn.execute("SELECT value FROM meta WHERE key='recovered_from'").fetchone()
        if rec and not self.recovered_from:
            self.recovered_from = rec["value"]
        return conn

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------------------------------ helpers
    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(args)).fetchone()

    @contextlib.contextmanager
    def tx(self):
        """One write transaction (all or nothing), also across processes (the app and the CLI share the file)."""
        with self._lock:
            if self.conn.in_transaction:
                yield
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def meta_get(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        if r is None:
            return default
        try:
            return json.loads(r["value"])
        except (TypeError, ValueError):
            return r["value"]

    def meta_set(self, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, json.dumps(value)))

    def log(self, action: str, ref: str = "", detail: Any = None) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO event VALUES(?,?,?,?)",
                              (time.time(), action, ref, json.dumps(detail, default=str) if detail is not None else None))

    def events(self, limit: int = 200) -> list[dict]:
        out = []
        for r in self.q("SELECT * FROM event ORDER BY ts DESC LIMIT ?", (limit,)):
            d = dict(r)
            try:
                d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            except ValueError:
                pass
            out.append(d)
        return out

    # ------------------------------------------------------------------------------------------ corrections
    def next_id(self) -> tuple[str, int]:
        with self._lock:
            row = self.conn.execute("SELECT max(seq) AS m FROM correction").fetchone()
            seq = max(int(row["m"] or 0), int(self.meta_get("next_seq", 1)) - 1) + 1
            self.meta_set("next_seq", seq + 1)
        return f"c-{seq:04d}", seq

    def add_correction(self, rec: dict) -> dict:
        """Insert a correction. Without an id, the next one (c-0007) is taken in the same transaction, so two
        processes adding at once never collide."""
        cols = [r["name"] for r in self.q("PRAGMA table_info(correction)")]
        rec = dict(rec)
        with self.tx():
            if not rec.get("id"):
                rec["id"], rec["seq"] = self.next_id()
            row = {c: rec.get(c) for c in cols}
            for c in CORRECTION_JSON:
                if row.get(c) is not None and not isinstance(row[c], str):
                    row[c] = json.dumps(row[c])
            self.conn.execute(f"INSERT INTO correction({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                              [row[c] for c in cols])
            self.log(rec.get("_event") or "correct", rec["id"],
                     {"question": rec.get("question"), "target": rec.get("target_label")})
        return self.correction(rec["id"])

    def correction(self, cid: str) -> dict | None:
        r = self.one("SELECT * FROM correction WHERE id=?", (cid,))
        return _row(r) if r else None

    def corrections(self, status: str | None = None) -> list[dict]:
        if status and status != "all":
            rows = self.q("SELECT * FROM correction WHERE status=? ORDER BY seq DESC", (status,))
        else:
            rows = self.q("SELECT * FROM correction ORDER BY seq DESC")
        return [_row(r) for r in rows]

    def update_correction(self, cid: str, **fields: Any) -> dict:
        if not fields:
            return self.correction(cid) or {}
        for c in CORRECTION_JSON:
            if c in fields and fields[c] is not None and not isinstance(fields[c], str):
                fields[c] = json.dumps(fields[c])
        fields["updated"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE correction SET {sets} WHERE id=?", [*fields.values(), cid])
        return self.correction(cid) or {}

    def update_correction_if(self, cid: str, expect: str, **fields: Any) -> bool:
        """Update only while the correction still has this status (a background re-check must not overwrite an Undo
        made meanwhile). True when it was updated."""
        for c in CORRECTION_JSON:
            if c in fields and fields[c] is not None and not isinstance(fields[c], str):
                fields[c] = json.dumps(fields[c])
        fields["updated"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            cur = self.conn.execute(f"UPDATE correction SET {sets} WHERE id=? AND status=?", [*fields.values(), cid, expect])
            return cur.rowcount > 0

    def mark_used(self, ids: list[str]) -> None:
        if not ids:
            return
        now = time.time()
        with self._lock:
            self.conn.executemany("UPDATE correction SET uses=coalesce(uses,0)+1, last_used=? WHERE id=?",
                                  [(now, i) for i in ids])

    # ------------------------------------------------------------------------------------------ glossary
    def glossary(self, status: str | None = None) -> list[dict]:
        rows = self.q("SELECT * FROM glossary" + (" WHERE status=?" if status else "") + " ORDER BY term",
                      (status,) if status else ())
        out = []
        for r in rows:
            d = dict(r)
            d["identifiers"] = json.loads(d["identifiers"] or "[]")
            out.append(d)
        return out

    def glossary_term(self, term: str) -> dict | None:
        r = self.one("SELECT * FROM glossary WHERE term=?", (term,))
        if not r:
            return None
        d = dict(r)
        d["identifiers"] = json.loads(d["identifiers"] or "[]")
        return d

    def put_glossary(self, term: str, identifiers: list[str], source: str, status: str) -> dict:
        now = time.time()
        old = self.glossary_term(term)
        with self._lock:
            if old:
                ids = list(dict.fromkeys(list(old["identifiers"]) + list(identifiers))) if status == old["status"] \
                    else list(dict.fromkeys(identifiers or old["identifiers"]))
                self.conn.execute("UPDATE glossary SET identifiers=?, source=?, status=?, updated=? WHERE term=?",
                                  (json.dumps(ids), source or old["source"], status, now, term))
            else:
                self.conn.execute("INSERT INTO glossary VALUES(?,?,?,?,?,?)",
                                  (term, json.dumps(list(dict.fromkeys(identifiers))), source, status, now, now))
        return self.glossary_term(term) or {}

    def delete_glossary(self, term: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM glossary WHERE term=?", (term,))

    # ------------------------------------------------------------------------------------------ confirmations
    def add_confirmation(self, rec: dict) -> dict:
        cols = [r["name"] for r in self.q("PRAGMA table_info(confirmation)")]
        row = {c: rec.get(c) for c in cols}
        for c in ("topic", "must_mention"):
            if row.get(c) is not None and not isinstance(row[c], str):
                row[c] = json.dumps(row[c])
        with self._lock:
            self.conn.execute(f"INSERT OR REPLACE INTO confirmation({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                              [row[c] for c in cols])
        self.log("confirm", rec["id"], {"question": rec.get("question")})
        return self.confirmation(rec["id"]) or {}

    def confirmation(self, kid: str) -> dict | None:
        r = self.one("SELECT * FROM confirmation WHERE id=?", (kid,))
        if not r:
            return None
        d = dict(r)
        for c in ("topic", "must_mention"):
            d[c] = json.loads(d[c] or "[]")
        return d

    def confirmations(self, status: str | None = "active") -> list[dict]:
        rows = self.q("SELECT * FROM confirmation" + (" WHERE status=?" if status and status != "all" else "") +
                      " ORDER BY created DESC", (status,) if status and status != "all" else ())
        out = []
        for r in rows:
            d = dict(r)
            for c in ("topic", "must_mention"):
                d[c] = json.loads(d[c] or "[]")
            out.append(d)
        return out

    def set_confirmation_status(self, kid: str, status: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE confirmation SET status=? WHERE id=?", (status, kid))


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for c in CORRECTION_JSON:
        v = d.get(c)
        if isinstance(v, str) and v:
            try:
                d[c] = json.loads(v)
            except ValueError:
                pass
        elif v is None and c in ("topic", "conflict"):
            d[c] = []
    return d
