import sqlite3

from verity.store.db import FactStore


def test_definition_beats_declaration_and_keeps_doc(tmp_path):
    st = FactStore(tmp_path / "i.db")
    st.insert_records([{"t": "symbol", "key": "c:@F@f", "kind": "function", "name": "f", "file": "a.h", "line": 1,
                        "is_def": False, "doc": "does f"}])
    st.insert_records([{"t": "symbol", "key": "c:@F@f", "kind": "function", "name": "f", "file": "a.c", "line": 9,
                        "is_def": True}])
    r = st.one("SELECT file, line, is_def, doc FROM symbol WHERE key='c:@F@f'")
    assert (r["file"], r["line"], r["is_def"], r["doc"]) == ("a.c", 9, 1, "does f")
    # a later declaration never replaces the definition
    st.insert_records([{"t": "symbol", "key": "c:@F@f", "kind": "function", "name": "f", "file": "b.h", "line": 3,
                        "is_def": False}])
    assert st.one("SELECT file FROM symbol WHERE key='c:@F@f'")["file"] == "a.c"


def test_per_file_replacement(tmp_path):
    st = FactStore(tmp_path / "i.db")
    st.insert_records([{"t": "file", "path": "a.c", "lang": "c", "sha": "1"},
                       {"t": "call", "from": "c:@F@a", "to": "c:@F@b", "file": "a.c", "line": 3, "col": 1, "kind": "direct",
                        "res": "exact"},
                       {"t": "call", "from": "c:@F@x", "to": "c:@F@y", "file": "x.c", "line": 3, "col": 1, "kind": "direct",
                        "res": "exact"}])
    st.delete_file_facts(["a.c"])
    assert st.one("SELECT count(*) n FROM call")["n"] == 1
    assert st.one("SELECT count(*) n FROM file")["n"] == 0


def test_corrupted_index_is_recovered(tmp_path):
    p = tmp_path / "i.db"
    p.write_bytes(b"this is not a sqlite database" * 100)
    st = FactStore(p)
    assert st.one("SELECT count(*) n FROM symbol")["n"] == 0
    assert "corrupt" in st.meta_get("recovered_from")


def test_aliases_map_record_fields(tmp_path):
    st = FactStore(tmp_path / "i.db")
    st.insert_records([{"t": "occ", "sym": "s", "file": "a.c", "line": 1, "col": 1, "role": "read", "in": "c:@F@f"},
                       {"t": "symbol", "key": "k#param:x", "kind": "param", "name": "x", "file": "a.cs", "line": 1, "pos": 2}])
    assert st.one("SELECT fn FROM occ")["fn"] == "c:@F@f"
    assert st.one("SELECT idx FROM symbol")["idx"] == 2
