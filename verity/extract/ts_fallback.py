"""Fidelity F4: syntax-only extraction with tree-sitter when the compiler front end cannot process a file (or the C#
compiler worker is unavailable). Emits the same records; calls are matched by name later (res="name"), so every
claim built on them is at most INFERENCE."""
from __future__ import annotations

from pathlib import Path

from .recon import read_text

_PARSERS: dict[str, object] = {}


def _parser(lang: str):
    if lang not in _PARSERS:
        import tree_sitter as ts
        if lang == "c":
            import tree_sitter_c as tsl
        else:
            import tree_sitter_c_sharp as tsl
        _PARSERS[lang] = ts.Parser(ts.Language(tsl.language()))
    return _PARSERS[lang]


def _txt(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", "replace")


def _ident_of_declarator(src: bytes, n):
    """Walk down C declarators (pointer/function/array/parenthesized) to the identifier."""
    while n is not None:
        if n.type in ("identifier", "field_identifier", "type_identifier"):
            return n
        nxt = n.child_by_field_name("declarator")
        if nxt is None:
            for ch in n.children:
                if ch.type in ("identifier", "field_identifier", "parenthesized_declarator", "pointer_declarator",
                               "function_declarator", "array_declarator"):
                    nxt = ch
                    break
        n = nxt
    return None


def extract_c(root: Path, rel: str) -> list[dict]:
    src_text = read_text(Path(root) / rel)
    src = src_text.encode("utf-8", "replace")
    tree = _parser("c").parse(src)
    out: list[dict] = []
    file_static = rel.rsplit("/", 1)[-1]

    def sym(kind, name, node, container=None, static=False, sig=""):
        key = (f"c:{file_static}@F@{name}" if static else f"c:@F@{name}") if kind == "function" else \
            (f"c:{file_static}@{name}" if static else f"c:@{name}")
        if kind == "field" and container:
            key = f"{container}@FI@{name}"
        out.append({"t": "symbol", "key": key, "kind": kind, "name": name, "qual": name, "sig": sig, "type": sig,
                    "file": rel, "line": node.start_point[0] + 1, "end_line": node.end_point[0] + 1,
                    "col": node.start_point[1] + 1, "container": container, "is_def": True, "static": static,
                    "extern": False, "lang": "c", "cfg": "default"})
        return key

    def walk_calls(n, fkey):
        stack = [n]
        while stack:
            x = stack.pop()
            if x.type == "call_expression":
                fn = x.child_by_field_name("function")
                if fn is not None and fn.type == "identifier":
                    args = x.child_by_field_name("arguments")
                    out.append({"t": "call", "from": fkey, "to": f"name:{_txt(src, fn)}", "file": rel,
                                "line": x.start_point[0] + 1, "col": x.start_point[1] + 1, "kind": "direct",
                                "res": "name", "args": [_txt(src, a)[:120] for a in (args.named_children if args else [])]})
            stack.extend(x.children)

    for n in tree.root_node.children:
        static = any(_txt(src, c) == "static" for c in n.children if c.type == "storage_class_specifier")
        if n.type == "function_definition":
            d = n.child_by_field_name("declarator")
            ident = _ident_of_declarator(src, d)
            if ident is None:
                continue
            name = _txt(src, ident)
            fkey = sym("function", name, n, static=static, sig=_txt(src, d)[:200])
            body = n.child_by_field_name("body")
            if body is not None:
                walk_calls(body, fkey)
        elif n.type == "declaration":
            for d in n.children_by_field_name("declarator"):
                ident = _ident_of_declarator(src, d)
                if ident is None:
                    continue
                is_fn = d.type == "function_declarator" or any(c.type == "function_declarator" for c in d.children)
                if not is_fn:
                    sym("global", _txt(src, ident), n, static=static, sig=_txt(src, n)[:200])
        elif n.type in ("type_definition", "struct_specifier"):
            spec = n.child_by_field_name("type") if n.type == "type_definition" else n
            names = [_txt(src, d) for d in n.children_by_field_name("declarator")] if n.type == "type_definition" else []
            if spec is not None and spec.type == "struct_specifier":
                nm = spec.child_by_field_name("name")
                sname = _txt(src, nm) if nm is not None else (names[0] if names else "anon")
                skey = f"c:@S@{sname}" if nm is not None else f"c:@SA@{sname}"
                out.append({"t": "symbol", "key": skey, "kind": "struct", "name": sname, "qual": sname, "sig": "",
                            "type": "", "file": rel, "line": n.start_point[0] + 1, "end_line": n.end_point[0] + 1,
                            "is_def": True, "lang": "c", "cfg": "default"})
                body = spec.child_by_field_name("body")
                for f in (body.named_children if body is not None else []):
                    if f.type == "field_declaration":
                        for d in f.children_by_field_name("declarator"):
                            ident = _ident_of_declarator(src, d)
                            if ident is not None:
                                sym("field", _txt(src, ident), f, container=skey, sig=_txt(src, f)[:160])
    return out


def extract_csharp(root: Path, rels: list[str]) -> list[dict]:
    """Syntax-only C#: types, methods, fields and name-matched invocations."""
    out: list[dict] = []
    for rel in rels:
        src = read_text(Path(root) / rel).encode("utf-8", "replace")
        tree = _parser("cs").parse(src)
        ns_stack: list[str] = []

        def visit(n, ctx: list[str], fkey: str | None):
            t = n.type
            if t in ("namespace_declaration", "file_scoped_namespace_declaration"):
                nm = n.child_by_field_name("name")
                ctx = ctx + [_txt(src, nm)] if nm is not None else ctx
            elif t in ("class_declaration", "struct_declaration", "interface_declaration", "record_declaration",
                       "enum_declaration"):
                nm = n.child_by_field_name("name")
                if nm is not None:
                    ctx = ctx + [_txt(src, nm)]
                    q = ".".join(ctx)
                    out.append({"t": "symbol", "key": f"T:{q}", "kind": t.split("_")[0], "name": _txt(src, nm),
                                "qual": q, "sig": "", "type": "", "file": rel, "line": n.start_point[0] + 1,
                                "end_line": n.end_point[0] + 1, "is_def": True, "lang": "csharp", "cfg": "default"})
            elif t in ("method_declaration", "constructor_declaration"):
                nm = n.child_by_field_name("name")
                if nm is not None:
                    q = ".".join(ctx + [_txt(src, nm)])
                    params = n.child_by_field_name("parameters")
                    fkey = f"M:{q}"
                    out.append({"t": "symbol", "key": fkey, "kind": "method", "name": _txt(src, nm), "qual": q,
                                "sig": _txt(src, params)[:200] if params is not None else "", "type": "", "file": rel,
                                "line": n.start_point[0] + 1, "end_line": n.end_point[0] + 1, "container": f"T:{'.'.join(ctx)}",
                                "is_def": True, "lang": "csharp", "cfg": "default"})
            elif t == "field_declaration" or t == "property_declaration":
                for d in n.named_children:
                    if d.type == "variable_declaration":
                        for v in d.named_children:
                            if v.type == "variable_declarator":
                                nm = v.child_by_field_name("name") or (v.named_children[0] if v.named_children else None)
                                if nm is not None:
                                    q = ".".join(ctx + [_txt(src, nm)])
                                    out.append({"t": "symbol", "key": f"F:{q}", "kind": "field", "name": _txt(src, nm),
                                                "qual": q, "sig": _txt(src, n)[:160], "type": "", "file": rel,
                                                "line": n.start_point[0] + 1, "end_line": n.end_point[0] + 1,
                                                "container": f"T:{'.'.join(ctx)}", "is_def": True, "lang": "csharp",
                                                "cfg": "default"})
                if t == "property_declaration":
                    nm = n.child_by_field_name("name")
                    if nm is not None:
                        q = ".".join(ctx + [_txt(src, nm)])
                        out.append({"t": "symbol", "key": f"P:{q}", "kind": "property", "name": _txt(src, nm),
                                    "qual": q, "sig": _txt(src, n).split("{")[0][:160], "type": "", "file": rel,
                                    "line": n.start_point[0] + 1, "end_line": n.end_point[0] + 1,
                                    "container": f"T:{'.'.join(ctx)}", "is_def": True, "lang": "csharp", "cfg": "default"})
            elif t == "invocation_expression" and fkey:
                fn = n.child_by_field_name("function")
                if fn is not None:
                    name = _txt(src, fn).split(".")[-1].split("<")[0]
                    out.append({"t": "call", "from": fkey, "to": f"name:{name}", "file": rel,
                                "line": n.start_point[0] + 1, "col": n.start_point[1] + 1, "kind": "direct",
                                "res": "name", "args": []})
            for ch in n.children:
                visit(ch, ctx, fkey)

        visit(tree.root_node, ns_stack, None)
    return out
