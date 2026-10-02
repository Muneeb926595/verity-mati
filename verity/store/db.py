"""SQLite fact store: the source of truth. WAL mode, per-file fact replacement, FTS5 for lexical search,
float32 vectors stored as blobs (brute-force cosine in numpy — the sqlite-vec role for small repos)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Iterable

from ..pathkinds import is_secondary_path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS file (path TEXT PRIMARY KEY, lang TEXT, sha TEXT, loc INT, fidelity TEXT, errors INT,
    stubs TEXT, profile TEXT, indexed_at REAL, size INT, mtime REAL);
CREATE TABLE IF NOT EXISTS symbol (key TEXT PRIMARY KEY, kind TEXT, name TEXT, qual TEXT, sig TEXT, type TEXT,
    file TEXT, line INT, end_line INT, col INT, container TEXT, is_def INT, static INT, extern INT, doc TEXT,
    lang TEXT, cfg TEXT DEFAULT 'default', stub INT DEFAULT 0, idx INT);
CREATE INDEX IF NOT EXISTS sym_name ON symbol(name);
CREATE INDEX IF NOT EXISTS sym_file ON symbol(file);
CREATE INDEX IF NOT EXISTS sym_container ON symbol(container);
CREATE TABLE IF NOT EXISTS occ (sym TEXT, file TEXT, line INT, col INT, role TEXT, fn TEXT, via TEXT, guards TEXT,
    cfg TEXT DEFAULT 'default', UNIQUE(sym, file, line, col, role));
CREATE INDEX IF NOT EXISTS occ_sym ON occ(sym, role);
CREATE INDEX IF NOT EXISTS occ_file ON occ(file);
CREATE INDEX IF NOT EXISTS occ_fn ON occ(fn);
CREATE TABLE IF NOT EXISTS call (caller TEXT, callee TEXT, file TEXT, line INT, col INT, kind TEXT, res TEXT,
    via TEXT, args TEXT, guards TEXT, cfg TEXT DEFAULT 'default', derived INT DEFAULT 0,
    UNIQUE(caller, callee, file, line, col, via));
CREATE INDEX IF NOT EXISTS call_caller ON call(caller);
CREATE INDEX IF NOT EXISTS call_callee ON call(callee);
CREATE INDEX IF NOT EXISTS call_file ON call(file);
CREATE TABLE IF NOT EXISTS flow (src TEXT, dst TEXT, kind TEXT, file TEXT, line INT, fn TEXT, expr TEXT,
    cfg TEXT DEFAULT 'default', derived INT DEFAULT 0, site INT, UNIQUE(src, dst, file, line, kind));
CREATE INDEX IF NOT EXISTS flow_src ON flow(src);
CREATE INDEX IF NOT EXISTS flow_dst ON flow(dst);
CREATE INDEX IF NOT EXISTS flow_file ON flow(file);
CREATE TABLE IF NOT EXISTS fnaddr (fn TEXT, dst TEXT, file TEXT, line INT, kind TEXT, UNIQUE(fn, dst, file, line));
CREATE TABLE IF NOT EXISTS branch (fn TEXT, file TEXT, line INT, end_line INT, kind TEXT, cond TEXT, reads TEXT,
    then_s INT, then_e INT, else_s INT, else_e INT, cfg TEXT DEFAULT 'default', UNIQUE(fn, file, line, kind));
CREATE INDEX IF NOT EXISTS branch_fn ON branch(fn);
CREATE TABLE IF NOT EXISTS constv (sym TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS entry (sym TEXT, kind TEXT, detail TEXT, res TEXT, derived INT DEFAULT 0, UNIQUE(sym, kind));
CREATE TABLE IF NOT EXISTS io (sym TEXT, dir TEXT, api TEXT, file TEXT, line INT, fn TEXT, res TEXT,
    derived INT DEFAULT 0, UNIQUE(sym, dir, file, line));
CREATE TABLE IF NOT EXISTS inherit (sub TEXT, base TEXT, kind TEXT, UNIQUE(sub, base, kind));
CREATE TABLE IF NOT EXISTS instantiate (type TEXT, file TEXT, line INT, fn TEXT);
CREATE TABLE IF NOT EXISTS subscribe (event TEXT, handler TEXT, file TEXT, line INT, fn TEXT, op TEXT);
CREATE TABLE IF NOT EXISTS include_edge (file TEXT, target TEXT, resolved INT, stub INT, UNIQUE(file, target));
CREATE TABLE IF NOT EXISTS diag (file TEXT, line INT, severity TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS config_key (key TEXT, sym TEXT, file TEXT, line INT, via TEXT);
CREATE TABLE IF NOT EXISTS freemacro (name TEXT PRIMARY KEY, guarded_lines INT, files TEXT, indexed_variant INT);
CREATE TABLE IF NOT EXISTS stub_symbol (name TEXT PRIMARY KEY, kind TEXT, files TEXT);
CREATE TABLE IF NOT EXISTS card (sym TEXT PRIMARY KEY, text TEXT, hash TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS card_fts USING fts5(sym UNINDEXED, name, words, body, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS vec (sym TEXT PRIMARY KEY, model TEXT, dim INT, v BLOB, hash TEXT);
CREATE TABLE IF NOT EXISTS summary (sym TEXT PRIMARY KEY, text TEXT, model TEXT, input_hash TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS run (id TEXT PRIMARY KEY, created REAL, scenario TEXT, manifest TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS run_event (run TEXT, seq INT, kind TEXT, sym TEXT, file TEXT, line INT, value TEXT);
CREATE TABLE IF NOT EXISTS answer (id TEXT PRIMARY KEY, question TEXT, created REAL, result TEXT, deps TEXT);
"""

TABLE_COLUMNS = {
    "symbol": ["key", "kind", "name", "qual", "sig", "type", "file", "line", "end_line", "col", "container",
               "is_def", "static", "extern", "doc", "lang", "cfg", "stub", "idx"],
    "occ": ["sym", "file", "line", "col", "role", "fn", "via", "guards", "cfg"],
    "call": ["caller", "callee", "file", "line", "col", "kind", "res", "via", "args", "guards", "cfg", "derived"],
    "flow": ["src", "dst", "kind", "file", "line", "fn", "expr", "cfg", "derived", "site"],
    "fnaddr": ["fn", "dst", "file", "line", "kind"],
    "branch": ["fn", "file", "line", "end_line", "kind", "cond", "reads", "then_s", "then_e", "else_s", "else_e", "cfg"],
    "constv": ["sym", "value"],
    "entry": ["sym", "kind", "detail", "res", "derived"],
    "io": ["sym", "dir", "api", "file", "line", "fn", "res", "derived"],
    "inherit": ["sub", "base", "kind"],
    "instantiate": ["type", "file", "line", "fn"],
    "subscribe": ["event", "handler", "file", "line", "fn", "op"],
    "include_edge": ["file", "target", "resolved", "stub"],
    "diag": ["file", "line", "severity", "msg"],
    "config_key": ["key", "sym", "file", "line", "via"],
}
# record type ("t") -> table
RECORD_TABLE = {"symbol": "symbol", "occ": "occ", "call": "call", "flow": "flow", "fnaddr": "fnaddr",
                "branch": "branch", "const": "constv", "entry": "entry", "io": "io", "inherit": "inherit",
                "instantiate": "instantiate", "subscribe": "subscribe", "include": "include_edge", "diag": "diag",
                "config_key": "config_key"}
# record field aliases: FACT_SCHEMA name -> column name
ALIASES = {"occ": {"fn": "in"}, "call": {"caller": "from", "callee": "to"}, "symbol": {"idx": "pos"}}
FILE_SCOPED = ["occ", "call", "flow", "fnaddr", "branch", "io", "instantiate", "subscribe", "include_edge", "diag",
               "config_key"]


class FactStore:
    def __init__(self, path: str | os.PathLike):
        self.path = os.fspath(path)
        self._lock = threading.RLock()
        self.conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA)
            conn.execute("SELECT count(*) FROM symbol").fetchone()
        except sqlite3.DatabaseError:
            # Corrupted index: move it aside and start fresh (the index is always rebuildable).
            conn.close()
            bad = self.path + f".corrupt-{int(time.time())}"
            os.replace(self.path, bad)
            for ext in ("-wal", "-shm"):
                if os.path.exists(self.path + ext):
                    os.remove(self.path + ext)
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR REPLACE INTO meta VALUES('recovered_from', ?)", (bad,))
        return conn

    # ---- basic helpers -------------------------------------------------------------------------------
    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(args)).fetchone()

    def exec(self, sql: str, args: Iterable[Any] = ()) -> None:
        with self._lock:
            self.conn.execute(sql, tuple(args))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        rows = [tuple(r) for r in rows]
        if not rows:
            return
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def meta_get(self, k: str, default: Any = None) -> Any:
        r = self.one("SELECT v FROM meta WHERE k=?", (k,))
        if not r:
            return default
        try:
            return json.loads(r["v"])
        except Exception:
            return r["v"]

    def meta_set(self, k: str, v: Any) -> None:
        self.exec("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v)))

    # ---- writes --------------------------------------------------------------------------------------
    def delete_file_facts(self, paths: Iterable[str]) -> None:
        paths = list(paths)
        if not paths:
            return
        with self._lock:
            cur = self.conn
            for p in paths:
                for t in FILE_SCOPED:
                    cur.execute(f"DELETE FROM {t} WHERE file=?", (p,))
                cur.execute("DELETE FROM symbol WHERE file=?", (p,))
                cur.execute("DELETE FROM file WHERE path=?", (p,))

    def insert_records(self, records: Iterable[dict[str, Any]]) -> dict[str, int]:
        """Insert extractor records (FACT_SCHEMA.md). Symbols keep the definition over a declaration."""
        counts: dict[str, int] = {}
        buckets: dict[str, list[tuple]] = {}
        files: list[tuple] = []
        for r in records:
            t = r.get("t")
            if t == "file":
                files.append((r["path"], r.get("lang"), r.get("sha"), r.get("loc", 0), r.get("fidelity", "F3"),
                              r.get("errors", 0), json.dumps(r.get("stubs") or []), r.get("profile", ""),
                              time.time(), r.get("size", 0), r.get("mtime", 0.0)))
                continue
            table = RECORD_TABLE.get(t)
            if not table:
                continue
            cols = TABLE_COLUMNS[table]
            alias = ALIASES.get(table, {})
            row = []
            for c in cols:
                v = r.get(c) if c not in alias else (r.get(alias[c]) if r.get(alias[c]) is not None else r.get(c))
                if c == "sym" and table == "constv":
                    v = r.get("sym")
                if c in ("guards", "args", "reads", "stubs") and v is not None and not isinstance(v, str):
                    v = json.dumps(v)
                if c in ("then_s", "then_e", "else_s", "else_e"):
                    rng = r.get("then" if c.startswith("then") else "else")
                    v = (rng[0] if c.endswith("_s") else rng[1]) if rng else None
                if c in ("is_def", "static", "extern", "resolved", "stub") and isinstance(v, bool):
                    v = int(v)
                if c == "cfg" and v is None:
                    v = "default"
                if c == "derived" and v is None:
                    v = 0
                row.append(v)
            buckets.setdefault(table, []).append(tuple(row))
        with self._lock:
            cur = self.conn
            cur.execute("BEGIN")
            try:
                if files:
                    cur.executemany("INSERT OR REPLACE INTO file VALUES(?,?,?,?,?,?,?,?,?,?,?)", files)
                for table, rows in buckets.items():
                    cols = TABLE_COLUMNS[table]
                    ph = ",".join("?" * len(cols))
                    if table == "symbol":
                        # a definition replaces a declaration; a declaration never replaces a definition
                        di = cols.index("doc")
                        fi, dfi = cols.index("file"), cols.index("is_def")
                        for row in rows:
                            key = row[0]
                            ex = cur.execute("SELECT is_def, doc, file FROM symbol WHERE key=?", (key,)).fetchone()
                            if ex is None:
                                cur.execute(f"INSERT INTO symbol({','.join(cols)}) VALUES({ph})", row)
                            elif ex[0] and row[dfi] and is_secondary_path(ex[2]) and not is_secondary_path(row[fi]):
                                # defined twice: the program's own definition wins over a test / example / model copy
                                # (and keeps its own comment, not the copy's)
                                cur.execute(f"INSERT OR REPLACE INTO symbol({','.join(cols)}) VALUES({ph})", row)
                            elif not ex[0]:
                                # a definition replaces a declaration, and a newer declaration refreshes an older one
                                # (external API symbols have no file of their own, so nothing else would refresh them)
                                if not row[di] and ex[1]:
                                    row = row[:di] + (ex[1],) + row[di + 1:]
                                cur.execute(f"INSERT OR REPLACE INTO symbol({','.join(cols)}) VALUES({ph})", row)
                            elif row[di] and not ex[1]:
                                cur.execute("UPDATE symbol SET doc=? WHERE key=?", (row[di], key))
                    else:
                        verb = "INSERT OR REPLACE" if table == "constv" else "INSERT OR IGNORE"
                        cur.executemany(f"{verb} INTO {table}({','.join(cols)}) VALUES({ph})", rows)
                    counts[table] = counts.get(table, 0) + len(rows)
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return counts

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass
