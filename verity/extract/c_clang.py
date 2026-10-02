"""C extractor on libclang (design §13): symbols, references by role, call sites (direct / through function
pointers), value flow, function-address flows (for table and callback resolution), branches and path guards,
constants, macros and entry points. Emits FACT_SCHEMA.md records."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

import clang.cindex as ci

from .recon import CProfile, add_stub_declarations, read_text, sha256_file

# Operator kinds straight from the AST (libclang >= 17). Reading tokens instead gives the macro name for operators that
# come from a macro expansion (`STATS_INC(x)` -> `++x`), so those are only the fallback.
ERRNO_KEY = "c:@errno"
_UNOP = {1: "++post", 2: "--post", 3: "++", 4: "--", 5: "&", 6: "*", 7: "+", 8: "-", 9: "~", 10: "!"}
_BINOP = {3: "*", 4: "/", 5: "%", 6: "+", 7: "-", 8: "<<", 9: ">>", 10: "<=>", 11: "<", 12: ">", 13: "<=", 14: ">=",
          15: "==", 16: "!=", 17: "&", 18: "^", 19: "|", 20: "&&", 21: "||", 22: "=", 23: "*=", 24: "/=", 25: "%=",
          26: "+=", 27: "-=", 28: "<<=", 29: ">>=", 30: "&=", 31: "^=", 32: "|=", 33: ","}


def _op_kind_fn(name: str):
    try:
        f = getattr(ci.conf.lib, name)
        f.argtypes = [ci.Cursor]
        f.restype = __import__("ctypes").c_int
        return f
    except Exception:  # noqa: BLE001 — older libclang: fall back to tokens
        return None

CK = ci.CursorKind
KEEP_GOING = 0x200          # CXTranslationUnit_KeepGoing: continue after fatal errors (missing headers)
DETAILED = ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD

EXPR_WRAPPERS = {CK.UNEXPOSED_EXPR, CK.PAREN_EXPR, CK.CSTYLE_CAST_EXPR}
LOOP_KINDS = {CK.WHILE_STMT, CK.FOR_STMT, CK.DO_STMT}
EXIT_KINDS = {CK.RETURN_STMT, CK.BREAK_STMT, CK.CONTINUE_STMT, CK.GOTO_STMT}
COMPOUND_OPS = {"+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}


class TUResult(dict):
    pass


def clip(text: str, n: int) -> str:
    """Shorten code text without cutting an identifier in half (a cut name would look like a real one)."""
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    cut = text[:n]
    m = re.search(r"[A-Za-z0-9_]+$", cut)
    if m and n < len(text) and re.match(r"[A-Za-z0-9_]", text[n]):
        cut = cut[: m.start()]
    return cut.rstrip() + " …"


class CExtractor:
    """Extracts one translation unit at a time. `emit` receives fact records."""

    def __init__(self, root: Path, profile: CProfile, emit: Callable[[dict], None], known_names: set[str],
                 cfg: str = "default", extra_defines: dict[str, str] | None = None):
        self.root = Path(root).resolve()
        self.profile = profile
        self.emit = emit
        self.known_names = known_names
        self.cfg = cfg
        self.extra_defines = extra_defines or {}
        self.index = ci.Index.create()
        self._unop_kind = _op_kind_fn("clang_getCursorUnaryOperatorKind")
        self._binop_kind = _op_kind_fn("clang_getCursorBinaryOperatorKind")
        self._src: dict[str, bytes] = {}
        self._param_cache: dict[tuple, list] = {}
        self._fn_ranges: dict[str, list[tuple[int, int, str]]] = {}
        self._sites: dict[str, int] = {}      # "<callee>#ret" -> line of the call that produced it (call-site context)
        self.stub_dir = os.path.realpath(profile.stub_dir)
        self.prelude = os.path.realpath(profile.prelude_path)
        self.tu_prelude_dir = os.path.join(os.path.dirname(self.prelude), "tu")
        os.makedirs(self.tu_prelude_dir, exist_ok=True)

    # ------------------------------------------------------------------------------------------ helpers
    def _headers_text(self) -> str:
        """Text of the whole project (headers and sources): how undeclared names are used ANYWHERE guides the stub
        loop, so every file synthesises the same stand-in layout for the same register block."""
        if getattr(self, "_htext", None) is None:
            parts, total = [], 0
            for p in sorted(list(self.root.rglob("*.h")) + list(self.root.rglob("*.c"))):
                if any(x in p.parts for x in (".git", "node_modules", "build", "out")):
                    continue
                t = read_text(p)
                total += len(t)
                if total > 6_000_000:
                    break
                parts.append(t)
            self._htext = "\n".join(parts)
        return self._htext

    def _synthetic(self, real: str) -> bool:
        return real == self.prelude or real.startswith(self.stub_dir + os.sep) or \
            real.startswith(self.tu_prelude_dir + os.sep) or os.path.basename(real).startswith("__vr_wrap__")

    def rel(self, f: str | None) -> str | None:
        if not f:
            return None
        real = os.path.realpath(f)
        if self._synthetic(real):
            return None
        try:
            return Path(real).relative_to(self.root).as_posix()
        except ValueError:
            return None

    def in_repo(self, c: ci.Cursor) -> bool:
        loc = c.location
        return bool(loc.file) and self.rel(loc.file.name) is not None

    def is_stub(self, c: ci.Cursor) -> bool:
        loc = c.location
        if not loc.file:
            return False
        return self._synthetic(os.path.realpath(loc.file.name))

    def text(self, c: ci.Cursor) -> str:
        ext = c.extent
        f = ext.start.file
        if not f:
            return ""
        name = f.name
        if name not in self._src:
            try:
                self._src[name] = Path(name).read_bytes()
            except OSError:
                self._src[name] = b""
        data = self._src[name]
        txt = data[ext.start.offset: ext.end.offset].decode("utf-8", "replace")
        if "#" in txt and "\n" in txt:
            # `if (a && b \n#if FEATURE\n && c \n#endif\n)`: a directive is never part of the expression
            txt = "\n".join(ln for ln in txt.split("\n")
                            if not re.match(r"\s*#\s*(if|ifdef|ifndef|elif|else|endif|define|undef|pragma|error|warning|"
                                            r"include|line)\b", ln))
        return txt

    @staticmethod
    def children(c: ci.Cursor) -> list[ci.Cursor]:
        return list(c.get_children())

    def unwrap(self, c: ci.Cursor) -> ci.Cursor:
        while c.kind in EXPR_WRAPPERS:
            ch = [x for x in c.get_children() if x.kind != CK.TYPE_REF]
            if len(ch) != 1:
                break
            c = ch[0]
        return c

    def binop(self, c: ci.Cursor) -> str:
        if self._binop_kind is not None:
            op = _BINOP.get(self._binop_kind(c))
            if op:
                return op
        ch = self.children(c)
        if len(ch) < 2:
            return "?"
        end = ch[0].extent.end.offset
        for t in c.get_tokens():
            if t.extent.start.offset >= end and t.kind == ci.TokenKind.PUNCTUATION:
                return t.spelling
        return "?"

    def unop(self, c: ci.Cursor) -> str:
        if self._unop_kind is not None:
            op = _UNOP.get(self._unop_kind(c))
            if op:
                return op
        toks = [t.spelling for t in c.get_tokens()]
        if not toks:
            return "?"
        if toks[0] in ("++", "--", "&", "*", "-", "!", "~", "+"):
            return toks[0]
        if toks[-1] in ("++", "--"):
            return toks[-1] + "post"
        return toks[0]

    def fn_params(self, fn: ci.Cursor) -> list:
        loc = fn.location
        ck = (fn.get_usr(), loc.file.name if loc.file else "", loc.offset)
        if ck not in self._param_cache:
            self._param_cache[ck] = list(fn.get_arguments() or [])
        return self._param_cache[ck]

    def key(self, c: ci.Cursor | None) -> str | None:
        """Stable symbol key (USR-based; params/locals are keyed by their function)."""
        if c is None:
            return None
        k = c.kind
        if k == CK.PARM_DECL:
            fn = c.semantic_parent
            if fn is not None and fn.kind == CK.FUNCTION_DECL:
                for i, p in enumerate(self.fn_params(fn)):
                    if p == c or (p.location.offset == c.location.offset and p.spelling == c.spelling):
                        return f"{fn.get_usr()}#param:{i}"
                return f"{fn.get_usr()}#param:{c.spelling}"
            return None
        if k == CK.VAR_DECL:
            sp = c.semantic_parent
            if sp is not None and sp.kind == CK.FUNCTION_DECL:
                return f"{sp.get_usr()}#local:{c.spelling}@{c.location.line}"
            return c.get_usr() or None
        if k == CK.MACRO_DEFINITION:
            return f"c:macro@{c.spelling}"
        usr = c.get_usr()
        return usr or None

    def struct_name(self, c: ci.Cursor) -> str:
        sp = c.spelling or ""
        if not sp or "unnamed" in sp or "anonymous" in sp:
            t = c.type.spelling or ""
            return t.replace("struct ", "").replace("union ", "") if t and "unnamed" not in t else "anon"
        return sp

    def guard_for(self, cond: ci.Cursor, polarity: bool, kind: str) -> dict:
        return {"cond": clip(self.text(cond), 240), "polarity": polarity, "line": cond.location.line,
                "kind": kind, "reads": sorted(self.reads_in(cond))}

    def is_errno(self, c: ci.Cursor | None) -> bool:
        """`errno` as written in the source: the C library expands it to `(*__error())`, `(*_errno())` or
        `(*__errno_location())`, which would otherwise read as a call instead of the error variable."""
        if c is None or c.kind not in (CK.UNARY_OPERATOR, CK.PAREN_EXPR, CK.UNEXPOSED_EXPR):
            return False
        try:
            ext = c.extent
            if ext.end.offset - ext.start.offset != 5:
                return False
            return self.text(c) == "errno"
        except Exception:  # noqa: BLE001
            return False

    def errno_key(self) -> str:
        if not getattr(self, "_errno_emitted", False):
            self._errno_emitted = True
            self.emit({"t": "symbol", "key": ERRNO_KEY, "kind": "global", "name": "errno", "qual": "errno", "sig": "int",
                       "type": "int", "file": None, "line": 0, "end_line": 0, "is_def": False, "extern": True,
                       "lang": "c", "cfg": self.cfg})
        return ERRNO_KEY

    def reads_in(self, c: ci.Cursor) -> set[str]:
        out: set[str] = set()
        for n in c.walk_preorder():
            if n.kind == CK.UNARY_OPERATOR and self.is_errno(n):
                out.add(self.errno_key())
            if n.kind in (CK.DECL_REF_EXPR, CK.MEMBER_REF_EXPR):
                r = n.referenced
                if r is not None and r.kind in (CK.VAR_DECL, CK.PARM_DECL, CK.FIELD_DECL, CK.ENUM_CONSTANT_DECL):
                    k = self.key(r)
                    if k:
                        out.add(k)
        return out

    # ---------------------------------------------------------------------------------------- parsing
    def build_args(self, rel: str, prelude: str | None = None) -> tuple[list[str], str]:
        prof = self.profile
        prelude = prelude or prof.prelude_path
        if prof.compile_db and rel in prof.compile_db:
            args = ["-x", "c", "-ferror-limit=0", "-Wno-everything"] + prof.compile_db[rel] + prof.sysroot_args
            args += ["-U_FORTIFY_SOURCE", "-D_FORTIFY_SOURCE=0"]
            for k, v in self.extra_defines.items():
                args.append(f"-D{k}={v}" if v not in (None, "") else f"-D{k}")
            return args + ["-I" + prof.stub_dir, "-include", prelude], "F1"
        args = list(prof.args) + list(prof.sysroot_args) + ["-U_FORTIFY_SOURCE", "-D_FORTIFY_SOURCE=0"]
        for d in prof.include_dirs:
            args.append("-I" + d)
        args.append("-I" + prof.stub_dir)
        for k, v in {**prof.defines, **self.extra_defines}.items():
            args.append(f"-D{k}={v}" if v not in (None, "") else f"-D{k}")
        args += ["-include", prelude]
        return args, "reconstructed"

    def parse(self, rel: str, prelude: str | None = None) -> tuple[ci.TranslationUnit | None, str, list[str]]:
        path = self.root / rel
        args, mode = self.build_args(rel, prelude)
        unsaved = None
        pre = [p for p in (getattr(self.profile, "header_preamble", None) or [])
               if os.path.realpath(p) != os.path.realpath(str(path))]
        if rel.endswith((".h", ".hh", ".hpp")) and pre:
            # a header nothing includes: parse it after the project's usual first include, in a virtual wrapper
            wrapper = self.root / ("__vr_wrap__" + re.sub(r"[^A-Za-z0-9_]", "_", rel) + ".c")
            content = "".join(f'#include "{p}"\n' for p in pre) + f'#include "{path}"\n'
            try:
                tu = self.index.parse(str(wrapper), args=args, unsaved_files=[(str(wrapper), content)],
                                      options=DETAILED | KEEP_GOING)
            except ci.TranslationUnitLoadError as e:
                return None, mode, [f"parse failed: {e}"]
            diags = [f"{d.location.line}: {d.spelling}" for d in tu.diagnostics if d.severity >= 3]
            return tu, mode, diags
        if self.profile.rewrites:
            txt = read_text(path)
            new = txt
            for pat, rep in self.profile.rewrites:
                new = re.sub(pat, lambda m: " " * len(m.group(0)) if rep == " " else rep, new)
            if new != txt:
                unsaved = [(str(path), new)]
        try:
            tu = self.index.parse(str(path), args=args, unsaved_files=unsaved, options=DETAILED | KEEP_GOING)
        except ci.TranslationUnitLoadError as e:
            return None, mode, [f"parse failed: {e}"]
        diags = [f"{d.location.line}: {d.spelling}" for d in tu.diagnostics if d.severity >= 3]
        return tu, mode, diags

    def extract(self, rel: str, stub_rounds: int = 4) -> dict[str, Any]:
        """Parse (with the stub loop) and emit facts for one TU. Returns per-file stats."""
        tu, mode, diags = self.parse(rel)
        stubbed: list[str] = []
        src_text = read_text(self.root / rel) + "\n" + self._headers_text()
        known = set(self.known_names)
        tu_prelude = None
        for _ in range(stub_rounds):
            if tu is None or not diags:
                break
            if tu_prelude is None:
                safe = re.sub(r"[^A-Za-z0-9_.-]", "_", rel) + f".{self.cfg.replace('+', 'p')}.h"
                tu_prelude = os.path.join(self.tu_prelude_dir, safe)
                with open(self.profile.prelude_path) as src, open(tu_prelude, "w") as dst:
                    dst.write(src.read())
            added = add_stub_declarations(tu_prelude, diags, src_text, known)
            if not added:
                break
            stubbed += added
            tu, mode, diags = self.parse(rel, tu_prelude)
        if tu is None:
            return {"ok": False, "errors": len(diags), "diags": diags, "mode": mode, "stubbed": stubbed}
        self._src.clear()
        self._fn_ranges = {}
        self._ext_emitted: set[str] = set()
        uses_stub_header = False
        for inc in tu.get_includes():
            try:
                real = os.path.realpath(inc.include.name)
            except Exception:
                continue
            src = self.rel(inc.source.name) if inc.source else None
            if real.startswith(self.stub_dir):
                uses_stub_header = True
                if src:
                    self.emit({"t": "include", "file": src, "target": os.path.relpath(real, self.stub_dir),
                               "resolved": False, "stub": True})
            elif src and inc.depth == 1 or (src and self.rel(inc.include.name)):
                tgt = self.rel(inc.include.name)
                if src:
                    self.emit({"t": "include", "file": src, "target": tgt or os.path.basename(inc.include.name),
                               "resolved": True, "stub": False})
        self.walk_tu(tu, rel)
        errors = len(diags)
        for d in diags[:50]:
            ln, _, msg = d.partition(": ")
            self.emit({"t": "diag", "file": rel, "line": int(ln) if ln.isdigit() else 0, "severity": "error", "msg": msg})
        if mode == "F1":
            fidelity = "F1" if errors == 0 else "F3"
        else:
            fidelity = "F2" if (errors == 0 and not uses_stub_header and not stubbed) else "F3"
        return {"ok": True, "errors": errors, "diags": diags, "mode": mode, "fidelity": fidelity,
                "stubbed": stubbed, "stub_header": uses_stub_header}

    # ------------------------------------------------------------------------------------------ walk
    def walk_tu(self, tu: ci.TranslationUnit, main_rel: str) -> None:
        macro_uses: list[tuple[str, str, int, int]] = []
        for c in tu.cursor.get_children():
            if c.kind == CK.MACRO_INSTANTIATION:
                if self.in_repo(c) and c.referenced is not None:
                    macro_uses.append((f"c:macro@{c.spelling}", self.rel(c.location.file.name), c.location.line,
                                       c.location.column))
                continue
            if c.kind == CK.INCLUSION_DIRECTIVE:
                continue
            if self.is_stub(c):
                self.emit_stub_symbol(c)
                continue
            if not self.in_repo(c):
                continue
            self.visit_decl(c, None)
        # macro uses → occurrences with their enclosing function (by line range)
        for key, f, line, col in macro_uses:
            fn = self._enclosing_fn(f, line)
            self.emit({"t": "occ", "sym": key, "file": f, "line": line, "col": col, "role": "macro", "in": fn,
                       "cfg": self.cfg})

    def _enclosing_fn(self, f: str, line: int) -> str | None:
        for s, e, k in self._fn_ranges.get(f, []):
            if s <= line <= e:
                return k
        return None

    def emit_stub_symbol(self, c: ci.Cursor) -> None:
        if c.kind in (CK.FUNCTION_DECL, CK.VAR_DECL, CK.TYPEDEF_DECL, CK.STRUCT_DECL):
            k = self.key(c)
            if k:
                self.emit({"t": "symbol", "key": k, "kind": "function" if c.kind == CK.FUNCTION_DECL else
                           ("global" if c.kind == CK.VAR_DECL else "struct"), "name": c.spelling, "qual": c.spelling,
                           "sig": c.type.spelling, "type": c.type.spelling, "file": "<stub>", "line": 0, "end_line": 0,
                           "is_def": False, "stub": 1, "extern": True, "lang": "c", "cfg": self.cfg})

    def sym_record(self, c: ci.Cursor, kind: str, name: str, qual: str, container: str | None = None,
                   idx: int | None = None) -> None:
        k = self.key(c)
        if not k:
            return
        f = self.rel(c.location.file.name) if c.location.file else None
        doc = c.brief_comment or None
        if not doc and kind in ("function", "global", "struct", "field", "macro"):
            doc = self.leading_comment(c)
        self.emit({"t": "symbol", "key": k, "kind": kind, "name": name, "qual": qual,
                   "sig": (c.displayname if kind == "function" else c.type.spelling) or "",
                   "type": c.type.spelling or "", "file": f, "line": c.extent.start.line, "end_line": c.extent.end.line,
                   "col": c.location.column, "container": container,
                   "is_def": bool(c.is_definition()) if kind in ("function", "global", "struct") else True,
                   "static": c.storage_class == ci.StorageClass.STATIC if kind in ("function", "global") else False,
                   "extern": c.storage_class == ci.StorageClass.EXTERN if kind in ("function", "global") else False,
                   "doc": doc, "lang": "c", "cfg": self.cfg, "idx": idx})

    def leading_comment(self, c: ci.Cursor) -> str | None:
        t = self._leading_comment(c)
        if t and not re.search(r"[A-Za-z]{2,}", t):
            return None                  # separator lines (/*-----*/, /*=====*/) are not documentation
        return t

    def _leading_comment(self, c: ci.Cursor) -> str | None:
        f = c.extent.start.file
        if not f:
            return None
        name = f.name
        if name not in self._src:
            try:
                self._src[name] = Path(name).read_bytes()
            except OSError:
                return None
        data = self._src[name][: c.extent.start.offset].decode("utf-8", "replace").rstrip()
        # `/* banner */  #if OS_TASK_CREATE_EN > 0u  INT8U OSTaskCreate (…)`: a conditional between them is not a gap
        while True:
            last_nl = data.rfind("\n")
            last = data[last_nl + 1:].strip()
            if last_nl < 0 or not re.match(r"#\s*(if|ifdef|ifndef|elif|else|endif)\b", last):
                break
            data = data[:last_nl].rstrip()
        # trailing comment on the same line as a field/global: take the rest of the line after the decl
        after = self._src[name][c.extent.end.offset: c.extent.end.offset + 200].decode("utf-8", "replace").split("\n")[0]
        m = re.search(r"/\*(.*?)\*/|//(.*)", after)
        if m:
            return " ".join((m.group(1) or m.group(2) or "").split()) or None
        if data.endswith("*/"):
            start = data.rfind("/*")
            line_start = data.rfind("\n", 0, start) + 1
            if start >= 0 and not data[line_start:start].strip():
                body = data[start + 2: -2]
                if data.count("\n", start) <= 8:
                    return " ".join(body.replace("*", " ").split()) or None
                # a long banner above a function ("Description : … Arguments : … Returns : …"), not a licence header
                if data.count("\n", start) <= 80 and not re.search(r"copyright|licen[cs]e|spdx|permission is hereby",
                                                                     body, re.I):
                    text = " ".join(re.sub(r"[*=\-#]{3,}", " ", body).replace("*", " ").split())
                    m = re.search(r"\b(Description|Summary|Purpose|Function|@brief|\\brief)\s*:?\s*(.+)", text)
                    if m:
                        text = re.split(r"\b(Arguments?|Parameters?|Params?|Returns?|Notes?|Caller\(s\)|@param|"
                                        r"\\param)\s*:", m.group(2))[0]
                    return text.strip()[:500] or None
        lines = data.split("\n")
        coms = []
        for ln in reversed(lines):
            s = ln.strip()
            if s.startswith("//"):
                coms.insert(0, s[2:].strip())
            else:
                break
        return " ".join(coms) or None

    def visit_decl(self, c: ci.Cursor, container: str | None) -> None:
        k = c.kind
        if k == CK.FUNCTION_DECL:
            self.visit_function(c)
        elif k == CK.VAR_DECL:
            name = c.spelling
            self.sym_record(c, "global", name, name)
            self.visit_initializer(c)
        elif k in (CK.STRUCT_DECL, CK.UNION_DECL):
            sname = self.struct_name(c)
            if c.is_definition():
                self.sym_record(c, "struct" if k == CK.STRUCT_DECL else "union", sname, sname)
            for ch in c.get_children():
                if ch.kind == CK.FIELD_DECL:
                    self.sym_record(ch, "field", ch.spelling, f"{sname}.{ch.spelling}", container=self.key(c))
                elif ch.kind in (CK.STRUCT_DECL, CK.UNION_DECL):
                    self.visit_decl(ch, self.key(c))
        elif k == CK.TYPEDEF_DECL:
            self.sym_record(c, "typedef", c.spelling, c.spelling)
            for ch in c.get_children():
                if ch.kind in (CK.STRUCT_DECL, CK.UNION_DECL, CK.ENUM_DECL):
                    self.visit_decl(ch, None)
        elif k == CK.ENUM_DECL:
            ename = self.struct_name(c)
            self.sym_record(c, "enum", ename, ename)
            for ch in c.get_children():
                if ch.kind == CK.ENUM_CONSTANT_DECL:
                    self.sym_record(ch, "enum_const", ch.spelling, ch.spelling, container=self.key(c))
                    kk = self.key(ch)
                    if kk:
                        self.emit({"t": "const", "sym": kk, "value": str(ch.enum_value)})
        elif k == CK.MACRO_DEFINITION:
            if c.spelling.startswith("_") and c.spelling.endswith("_H"):
                return
            toks = [t.spelling for t in c.get_tokens()]
            body = " ".join(toks[1:]) if len(toks) > 1 else ""
            is_func = len(toks) > 1 and toks[1] == "(" and self.text(c).startswith(c.spelling + "(")
            self.emit({"t": "symbol", "key": f"c:macro@{c.spelling}", "kind": "macro", "name": c.spelling,
                       "qual": c.spelling, "sig": ("function-like" if is_func else "object-like"), "type": clip(body, 200),
                       "file": self.rel(c.location.file.name), "line": c.location.line, "end_line": c.extent.end.line,
                       "col": c.location.column, "is_def": True, "doc": self.leading_comment(c), "lang": "c",
                       "cfg": self.cfg})
            if not is_func and body:
                val = body.strip()
                # tokens are joined with spaces: `#define EC_SLAVECOUNTEXCEEDED -4` arrives as "- 4"
                if re.fullmatch(r"\(?\s*[-+]?\s*[0-9.]+[uUlLfF]*\s*\)?", val) or re.fullmatch(r'"[^"]*"', val):
                    v = val.strip("() ")
                    self.emit({"t": "const", "sym": f"c:macro@{c.spelling}",
                               "value": re.sub(r"^([-+])\s+", r"\1", v) if not v.startswith('"') else v})

    def visit_initializer(self, var: ci.Cursor) -> None:
        vkey = self.key(var)
        inits = [ch for ch in var.get_children() if ch.kind not in (CK.TYPE_REF, CK.PARM_DECL)]
        if not inits or not vkey:
            return
        init = inits[-1]
        f = self.rel(var.location.file.name)
        if self.unwrap(init).kind == CK.INIT_LIST_EXPR:
            self.visit_init_list(self.unwrap(init), vkey, var.type, f, var)
        else:
            for src in self.values(init):
                if src.startswith("fnaddr:"):
                    self.emit({"t": "fnaddr", "fn": src[7:], "dst": vkey, "file": f, "line": init.location.line,
                               "kind": "init"})
                else:
                    self.emit({"t": "flow", "src": src, "dst": vkey, "kind": "init", "file": f,
                               "line": init.location.line, "fn": None, "expr": clip(self.text(var), 200), "cfg": self.cfg})

    def _struct_fields(self, t: ci.Type) -> list[ci.Cursor]:
        t = t.get_canonical()
        if t.kind == ci.TypeKind.CONSTANTARRAY or t.kind == ci.TypeKind.INCOMPLETEARRAY:
            t = t.element_type.get_canonical()
        decl = t.get_declaration()
        return [ch for ch in decl.get_children() if ch.kind == CK.FIELD_DECL] if decl is not None else []

    def visit_init_list(self, lst: ci.Cursor, vkey: str, vtype: ci.Type, f: str | None, var: ci.Cursor,
                        row: list[int] | None = None) -> None:
        elems = [ch for ch in lst.get_children()]
        canon = vtype.get_canonical()
        is_array = canon.kind in (ci.TypeKind.CONSTANTARRAY, ci.TypeKind.INCOMPLETEARRAY)
        fields = self._struct_fields(vtype) if not is_array else []
        for i, e in enumerate(elems):
            u = self.unwrap(e)
            if is_array:
                if u.kind == CK.INIT_LIST_EXPR:
                    self.visit_init_list(u, vkey, canon.element_type, f, var, row=[i])
                    continue
                target = vkey
                field = None
            else:
                field = fields[i] if i < len(fields) else None
                ch = [x for x in e.get_children()]
                if e.kind == CK.UNEXPOSED_EXPR and ch and ch[0].kind == CK.MEMBER_REF:
                    field = ch[0].referenced
                    u = self.unwrap(ch[1]) if len(ch) > 1 else u
                target = self.key(field) if field is not None else vkey
                if u.kind == CK.INIT_LIST_EXPR and field is not None:
                    self.visit_init_list(u, vkey, field.type, f, var, row=row)
                    continue
            for src in self.values(u):
                if src.startswith("fnaddr:"):
                    self.emit({"t": "fnaddr", "fn": src[7:], "dst": target, "file": f, "line": u.location.line,
                               "kind": "table"})
                    if target != vkey:
                        self.emit({"t": "fnaddr", "fn": src[7:], "dst": vkey, "file": f, "line": u.location.line,
                                   "kind": "table"})
                else:
                    self.emit({"t": "flow", "src": src, "dst": target, "kind": "init", "file": f,
                               "line": u.location.line, "fn": None, "expr": clip(self.text(u), 120), "cfg": self.cfg})
            if row is not None and field is not None:
                self.emit({"t": "const", "sym": f"{vkey}#row{row[0]}.{field.spelling}", "value": clip(self.text(u), 120)})

    # ------------------------------------------------------------------------------------ functions
    def visit_function(self, fn: ci.Cursor) -> None:
        fkey = self.key(fn)
        if not fkey:
            return
        self.sym_record(fn, "function", fn.spelling, fn.spelling)
        for i, p in enumerate(self.fn_params(fn)):
            if fn.is_definition():
                self.emit({"t": "symbol", "key": f"{fkey}#param:{i}", "kind": "param", "name": p.spelling or f"arg{i}",
                           "qual": f"{fn.spelling}.{p.spelling or f'arg{i}'}", "sig": p.type.get_canonical().spelling,
                           "type": p.type.spelling, "file": self.rel(p.location.file.name) if p.location.file else None,
                           "line": p.location.line, "end_line": p.location.line, "col": p.location.column,
                           "container": fkey, "is_def": True, "lang": "c", "cfg": self.cfg, "idx": i})
        if not fn.is_definition():
            return
        f = self.rel(fn.location.file.name)
        self._fn_ranges.setdefault(f, []).append((fn.extent.start.line, fn.extent.end.line, fkey))
        if fn.spelling in ("main", "wmain", "WinMain", "wWinMain", "DllMain"):
            self.emit({"t": "entry", "sym": fkey, "kind": "main", "detail": "program entry point", "res": "exact"})
        if re.search(r"(_IRQHandler|_Handler|_ISR|_isr|_irq)$", fn.spelling) or fn.spelling.startswith(("ISR_", "isr_")):
            self.emit({"t": "entry", "sym": fkey, "kind": "isr", "detail": "interrupt handler (naming convention)",
                       "res": "pattern"})
        body = [ch for ch in fn.get_children() if ch.kind == CK.COMPOUND_STMT]
        if body:
            self.ctx_fn = fkey
            self.ctx_file = f
            self.visit_stmt(body[0], [])

    def occ(self, sym: str | None, c: ci.Cursor, role: str, guards: list[dict], via: str | None = None) -> None:
        if not sym:
            return
        self.emit({"t": "occ", "sym": sym, "file": self.rel(c.location.file.name) if c.location.file else self.ctx_file,
                   "line": c.location.line, "col": c.location.column, "role": role, "in": self.ctx_fn, "via": via,
                   "guards": guards or None, "cfg": self.cfg})

    def branch(self, c: ci.Cursor, kind: str, cond: ci.Cursor | None, then: ci.Cursor | None,
               els: ci.Cursor | None) -> None:
        self.emit({"t": "branch", "fn": self.ctx_fn, "file": self.ctx_file, "line": c.location.line,
                   "end_line": c.extent.end.line, "kind": kind,
                   "cond": clip(self.text(cond), 240) if cond is not None else "",
                   "reads": sorted(self.reads_in(cond)) if cond is not None else [],
                   "then": [then.extent.start.line, then.extent.end.line] if then is not None else None,
                   "else": [els.extent.start.line, els.extent.end.line] if els is not None else None,
                   "cfg": self.cfg})

    @staticmethod
    def ends_with_exit(stmt: ci.Cursor) -> bool:
        if stmt.kind in EXIT_KINDS:
            return True
        if stmt.kind == CK.COMPOUND_STMT:
            ch = list(stmt.get_children())
            return bool(ch) and ch[-1].kind in EXIT_KINDS
        return False

    def visit_stmt(self, c: ci.Cursor, guards: list[dict], in_switch: str | None = None) -> None:
        k = c.kind
        if k == CK.COMPOUND_STMT:
            extra: list[dict] = []
            case_guard: dict | None = None
            for ch in c.get_children():
                if in_switch is not None and ch.kind in (CK.CASE_STMT, CK.DEFAULT_STMT):
                    case_guard = None
                    if ch.kind == CK.CASE_STMT:
                        val = list(ch.get_children())
                        case_guard = {"cond": f"{in_switch} == {self.text(val[0]).strip()}" if val else in_switch,
                                      "polarity": True, "line": ch.location.line, "kind": "case", "reads": []}
                    else:
                        case_guard = {"cond": f"{in_switch} (default)", "polarity": True, "line": ch.location.line,
                                      "kind": "case", "reads": []}
                g = guards + extra + ([case_guard] if case_guard else [])
                self.visit_stmt(ch, g, None)
                if ch.kind == CK.IF_STMT:
                    parts = list(ch.get_children())
                    if len(parts) == 2 and self.ends_with_exit(parts[1]):
                        extra = extra + [self.guard_for(parts[0], False, "early_exit")]
            return
        if k == CK.IF_STMT:
            parts = list(c.get_children())
            cond = parts[0] if parts else None
            then = parts[1] if len(parts) > 1 else None
            els = parts[2] if len(parts) > 2 else None
            self.branch(c, "if", cond, then, els)
            if cond is not None:
                self.visit_expr(cond, guards)
                if then is not None:
                    self.visit_stmt(then, guards + [self.guard_for(cond, True, "if")])
                if els is not None:
                    self.visit_stmt(els, guards + [self.guard_for(cond, False, "else")])
            return
        if k in LOOP_KINDS:
            parts = list(c.get_children())
            body = parts[-1] if k != CK.DO_STMT else (parts[0] if parts else None)
            header = self.text(c)
            hdr = header.split("{")[0] if k != CK.DO_STMT else "do-while"
            g = {"cond": clip(hdr, 200), "polarity": True, "line": c.location.line, "kind": "loop",
                 "reads": sorted(set().union(*[self.reads_in(p) for p in parts if p is not body]) if len(parts) > 1 else set())}
            # the loop's own condition: `while (c)`, `do … while (c)`, `for (…; c; …)`
            lcond = None
            if k == CK.WHILE_STMT and len(parts) >= 2:
                lcond = parts[0]
            elif k == CK.DO_STMT and len(parts) >= 2:
                lcond = parts[-1]
            if k == CK.FOR_STMT:
                m = re.match(r"^\s*for\s*\((.*?);(.*?);", header, re.S)
                ftxt = " ".join(m.group(2).split()) if m else ""
                self.emit({"t": "branch", "fn": self.ctx_fn, "file": self.ctx_file, "line": c.location.line,
                           "end_line": c.extent.end.line, "kind": "for", "cond": clip(ftxt, 240),
                           "reads": sorted(set().union(*[self.reads_in(p) for p in parts if p is not body]))
                           if len(parts) > 1 else [],
                           "then": [body.extent.start.line, body.extent.end.line] if body is not None else None,
                           "else": None, "cfg": self.cfg})
            else:
                self.branch(c, {CK.WHILE_STMT: "while", CK.DO_STMT: "do"}[k], lcond, body, None)
            for p in parts:
                if p is body:
                    continue
                if p.kind == CK.DECL_STMT:
                    self.visit_stmt(p, guards)
                else:
                    self.visit_expr(p, guards)
            if body is not None:
                self.visit_stmt(body, guards + [g])
            return
        if k == CK.SWITCH_STMT:
            parts = list(c.get_children())
            cond = parts[0] if parts else None
            self.branch(c, "switch", cond, parts[-1] if len(parts) > 1 else None, None)
            if cond is not None:
                self.visit_expr(cond, guards)
                ctext = " ".join(self.text(cond).split())
                if len(parts) > 1:
                    self.visit_stmt(parts[-1], guards, in_switch=ctext)
            return
        if k in (CK.CASE_STMT, CK.DEFAULT_STMT):
            parts = list(c.get_children())
            for p in parts[1:] if k == CK.CASE_STMT else parts:
                self.visit_stmt(p, guards)
            return
        if k == CK.RETURN_STMT:
            parts = list(c.get_children())
            if parts:
                for src in self.values(parts[0]):
                    if src.startswith("fnaddr:"):
                        self.emit({"t": "fnaddr", "fn": src[7:], "dst": f"{self.ctx_fn}#ret", "file": self.ctx_file,
                                   "line": c.location.line, "kind": "assign"})
                    else:
                        self.emit({"t": "flow", "src": src, "dst": f"{self.ctx_fn}#ret", "kind": "ret",
                                   "file": self.ctx_file, "line": c.location.line, "fn": self.ctx_fn,
                                   "expr": clip(self.text(c), 200), "cfg": self.cfg,
                                   "site": self._sites.get(src) if src.endswith("#ret") else None})
                self.visit_expr(parts[0], guards)
            self.occ(f"{self.ctx_fn}#ret", c, "write", guards)
            return
        if k == CK.DECL_STMT:
            for v in c.get_children():
                if v.kind == CK.VAR_DECL:
                    self.visit_local(v, guards)
            return
        if k in (CK.LABEL_STMT,):
            for p in c.get_children():
                self.visit_stmt(p, guards)
            return
        if k in (CK.BREAK_STMT, CK.CONTINUE_STMT, CK.GOTO_STMT, CK.NULL_STMT):
            return
        self.visit_expr(c, guards)

    def visit_local(self, v: ci.Cursor, guards: list[dict]) -> None:
        lkey = self.key(v)
        if not lkey:
            return
        fname = self.ctx_fn.split("@F@")[-1] if self.ctx_fn else ""
        self.emit({"t": "symbol", "key": lkey, "kind": "local", "name": v.spelling, "qual": f"{fname}.{v.spelling}",
                   "sig": v.type.spelling, "type": v.type.spelling, "file": self.ctx_file, "line": v.location.line,
                   "end_line": v.location.line, "col": v.location.column, "container": self.ctx_fn, "is_def": True,
                   "static": v.storage_class == ci.StorageClass.STATIC, "lang": "c", "cfg": self.cfg})
        inits = [ch for ch in v.get_children() if ch.kind not in (CK.TYPE_REF,)]
        if inits:
            init = inits[-1]
            if self.unwrap(init).kind == CK.INIT_LIST_EXPR:
                self.visit_init_list(self.unwrap(init), lkey, v.type, self.ctx_file, v)
            else:
                self.assign_flows([lkey], init, "init", v, guards)
                self.occ(lkey, v, "write", guards)
                self.visit_expr(init, guards)

    # --------------------------------------------------------------------------------- expressions
    def values(self, e: ci.Cursor) -> set[str]:
        """What flows out of evaluating expression `e`: symbol reads, call results, function addresses."""
        out: set[str] = set()
        stack = [e]
        while stack:
            n = stack.pop()
            k = n.kind
            if k in (CK.UNARY_OPERATOR, CK.PAREN_EXPR) and self.is_errno(n):
                out.add(self.errno_key())
                continue
            if k == CK.CALL_EXPR:
                ref = n.referenced
                if ref is not None and ref.kind in (CK.FUNCTION_DECL, CK.VAR_DECL, CK.FIELD_DECL, CK.PARM_DECL):
                    rk = f"{self.key(ref)}#ret"
                    out.add(rk)
                    self._sites[rk] = n.location.line
                continue       # arguments flow into the callee, not directly into our target
            if k == CK.DECL_REF_EXPR:
                ref = n.referenced
                if ref is not None:
                    if ref.kind == CK.FUNCTION_DECL:
                        out.add(f"fnaddr:{self.key(ref)}")
                    elif ref.kind in (CK.VAR_DECL, CK.PARM_DECL, CK.ENUM_CONSTANT_DECL):
                        kk = self.key(ref)
                        if kk:
                            out.add(kk)
                continue
            if k == CK.MEMBER_REF_EXPR:
                ref = n.referenced
                if ref is not None and ref.kind == CK.FIELD_DECL:
                    kk = self.key(ref)
                    if kk:
                        out.add(kk)
                continue          # field-sensitive: the field node, not the whole base object
            if k == CK.ARRAY_SUBSCRIPT_EXPR:
                ch = list(n.get_children())
                if ch:
                    stack.append(ch[0])  # the array contents flow; the index selects
                continue
            stack.extend(n.get_children())
        return out

    def lvalue_targets(self, e: ci.Cursor) -> list[tuple[str, ci.Cursor, str | None]]:
        """Symbols written by an assignment to `e`: (key, cursor, base-via)."""
        if self.is_errno(e):
            return [(self.errno_key(), e, None)]
        u = self.unwrap(e)
        if self.is_errno(u):
            return [(self.errno_key(), u, None)]
        k = u.kind
        if k == CK.DECL_REF_EXPR and u.referenced is not None:
            kk = self.key(u.referenced)
            return [(kk, u, None)] if kk else []
        if k == CK.MEMBER_REF_EXPR and u.referenced is not None:
            fk = self.key(u.referenced)
            out = [(fk, u, None)] if fk else []
            ch = list(u.get_children())
            if ch:
                for bk, bc, _ in self.lvalue_targets(ch[0]):
                    out.append((bk, bc, fk))       # base object partially written (via field)
            return out
        if k == CK.ARRAY_SUBSCRIPT_EXPR:
            ch = list(u.get_children())
            return self.lvalue_targets(ch[0]) if ch else []
        if k == CK.UNARY_OPERATOR and self.unop(u) == "*":
            ch = list(u.get_children())
            return [(t, c, "deref") for t, c, _ in self.lvalue_targets(ch[0])] if ch else []
        return []

    def _record_fields(self, t: ci.Type) -> list[str]:
        """Field keys of a struct type (or of the struct a pointer points to); [] for anything else."""
        try:
            t = t.get_canonical()
            if t.kind == ci.TypeKind.POINTER:
                t = t.get_pointee().get_canonical()
            if t.kind not in (ci.TypeKind.RECORD, ci.TypeKind.ELABORATED):
                return []
            decl = t.get_declaration()
            return [k for k in (self.key(f) for f in decl.get_children() if f.kind == CK.FIELD_DECL) if k][:64]
        except Exception:
            return []

    def _overlay(self, rhs: ci.Cursor | None):
        """(fields, operand) when rhs is an explicit cast of a buffer pointer to a struct pointer."""
        n = rhs
        while n is not None and n.kind in (CK.PAREN_EXPR, CK.UNEXPOSED_EXPR):
            ch = [x for x in n.get_children() if x.kind != CK.TYPE_REF]
            if len(ch) != 1:
                return None
            n = ch[0]
        if n is None or n.kind != CK.CSTYLE_CAST_EXPR:
            return None
        to = n.type.get_canonical()
        if to.kind != ci.TypeKind.POINTER:
            return None
        ch = [x for x in n.get_children() if x.kind not in (CK.TYPE_REF, CK.PARM_DECL)]
        if not ch:
            return None
        operand = ch[-1]
        frm = operand.type.get_canonical()
        if frm.kind != ci.TypeKind.POINTER:
            return None
        fp = frm.get_pointee().get_canonical()
        tp = to.get_pointee().get_canonical()
        if fp.spelling == tp.spelling or fp.kind in (ci.TypeKind.RECORD,):
            return None                            # same type, or struct-to-struct (a different idiom)
        # only a DATA buffer is an overlay: bytes (char / uint8_t), or a void* that holds data (p->payload, rx_buf);
        # a void* handle cast back to its object (`(struct dhcp *)netif->client_data[i]`, callback `arg`) is not
        byte = fp.kind in (ci.TypeKind.CHAR_S, ci.TypeKind.CHAR_U, ci.TypeKind.SCHAR, ci.TypeKind.UCHAR)
        if not byte:
            if fp.kind != ci.TypeKind.VOID:
                return None
            if not re.search(r"(payload|buf|buffer|data|pkt|packet|msg|frame|rx|raw|bytes)\w*\s*(\[[^\]]*\])?\s*\)*\s*$",
                             self.text(operand), re.I):
                return None
        fields = self._record_fields(to)
        return (fields, operand) if fields else None

    def _outer_field(self, e: ci.Cursor | None) -> str | None:
        """`a->addr.addr` -> the key of field `addr` of a's type (the field that holds the nested struct)."""
        if e is None:
            return None
        u = self.unwrap(e)
        if u.kind != CK.MEMBER_REF_EXPR:
            return None
        ch = list(u.get_children())
        if not ch:
            return None
        b = self.unwrap(ch[0])
        if b.kind == CK.MEMBER_REF_EXPR and b.referenced is not None and b.referenced.kind == CK.FIELD_DECL:
            return self.key(b.referenced)
        return None

    def assign_flows(self, targets: list[str], rhs: ci.Cursor, kind: str, site: ci.Cursor, guards: list[dict]) -> None:
        srcs = self.values(rhs)
        expr = clip(self.text(site), 200)
        # whole-struct assignment (*out = in; a = b): the value reaches every field of the target struct
        agg = []
        if rhs is not None and rhs.type.get_canonical().kind == ci.TypeKind.RECORD:
            agg = self._record_fields(rhs.type)
        targets = list(targets) + [f for f in agg if f not in targets]
        # a struct laid over a buffer (`msg = (struct dhcp_msg *)p->payload`): the struct's fields are read from that
        # buffer, so the buffer's value reaches each of them
        ov = self._overlay(rhs)
        if ov is not None:
            fields, operand = ov
            for s in self.values(operand):
                if s.startswith("fnaddr:"):
                    continue
                for f in fields:
                    self.emit({"t": "flow", "src": s, "dst": f, "kind": "overlay", "file": self.ctx_file,
                               "line": site.location.line, "fn": self.ctx_fn, "expr": expr, "cfg": self.cfg, "site": None})
        for t in targets:
            for s in srcs:
                if s.startswith("fnaddr:"):
                    self.emit({"t": "fnaddr", "fn": s[7:], "dst": t, "file": self.ctx_file,
                               "line": site.location.line, "kind": "assign"})
                elif s != t or kind == "compound":
                    self.emit({"t": "flow", "src": s, "dst": t, "kind": kind, "file": self.ctx_file,
                               "line": site.location.line, "fn": self.ctx_fn, "expr": expr, "cfg": self.cfg,
                               "site": self._sites.get(s) if s.endswith("#ret") else None})

    def visit_expr(self, c: ci.Cursor, guards: list[dict], skip: set | None = None) -> None:
        k = c.kind
        if k == CK.BINARY_OPERATOR:
            op = self.binop(c)
            ch = list(c.get_children())
            if op == "=" and len(ch) == 2:
                self.handle_assign(ch[0], ch[1], c, "assign", guards)
                return
            for x in ch:
                self.visit_expr(x, guards)
            return
        if k == CK.COMPOUND_ASSIGNMENT_OPERATOR:
            ch = list(c.get_children())
            if len(ch) == 2:
                self.handle_assign(ch[0], ch[1], c, "compound", guards)
            return
        if k in (CK.UNARY_OPERATOR, CK.PAREN_EXPR) and self.is_errno(c):
            self.occ(self.errno_key(), c, "read", guards)
            return
        if k == CK.UNARY_OPERATOR:
            op = self.unop(c)
            ch = list(c.get_children())
            if op in ("++", "--", "++post", "--post") and ch:
                for t, tc, via in self.lvalue_targets(ch[0]):
                    self.occ(t, tc, "rw", guards, via=via)
                self.visit_subexprs_of_lvalue(ch[0], guards)
                return
            if op == "&" and ch:
                u = self.unwrap(ch[0])
                if u.kind == CK.DECL_REF_EXPR and u.referenced is not None and u.referenced.kind == CK.FUNCTION_DECL:
                    self.occ(self.key(u.referenced), u, "addr", guards)
                    return
                for t, tc, via in self.lvalue_targets(ch[0]):
                    self.occ(t, tc, "addr", guards, via=via)
                self.visit_subexprs_of_lvalue(ch[0], guards)
                return
            for x in ch:
                self.visit_expr(x, guards)
            return
        if k == CK.CALL_EXPR:
            self.handle_call(c, guards)
            return
        if k == CK.DECL_REF_EXPR:
            ref = c.referenced
            if ref is not None:
                if ref.kind == CK.FUNCTION_DECL:
                    self.occ(self.key(ref), c, "addr", guards)
                elif ref.kind in (CK.VAR_DECL, CK.PARM_DECL, CK.ENUM_CONSTANT_DECL):
                    self.occ(self.key(ref), c, "read", guards)
            return
        if k == CK.MEMBER_REF_EXPR:
            ref = c.referenced
            base_key = None
            ch = list(c.get_children())
            if ch:
                bt = self.lvalue_targets(ch[0])
                base_key = bt[0][0] if bt else None
            if ref is not None:
                self.occ(self.key(ref), c, "read", guards, via=base_key)
            for x in ch:
                self.visit_expr(x, guards)
            return
        if k == CK.CONDITIONAL_OPERATOR:
            ch = list(c.get_children())
            if len(ch) == 3:
                self.branch(c, "ternary", ch[0], ch[1], ch[2])
                self.visit_expr(ch[0], guards)
                self.visit_expr(ch[1], guards + [self.guard_for(ch[0], True, "ternary")])
                self.visit_expr(ch[2], guards + [self.guard_for(ch[0], False, "ternary")])
                return
        if k == CK.StmtExpr:
            for x in c.get_children():
                self.visit_stmt(x, guards)
            return
        for x in c.get_children():
            if x.kind in (CK.COMPOUND_STMT, CK.IF_STMT) or x.kind in LOOP_KINDS:
                self.visit_stmt(x, guards)
            else:
                self.visit_expr(x, guards)

    def visit_subexprs_of_lvalue(self, e: ci.Cursor, guards: list[dict]) -> None:
        """Reads that happen while computing an lvalue: array indices and pointer bases."""
        if self.is_errno(e):
            return
        u = self.unwrap(e)
        if self.is_errno(u):
            return
        if u.kind == CK.ARRAY_SUBSCRIPT_EXPR:
            ch = list(u.get_children())
            if ch:
                self.visit_subexprs_of_lvalue(ch[0], guards)
            for x in ch[1:]:
                self.visit_expr(x, guards)
        elif u.kind == CK.MEMBER_REF_EXPR:
            for x in u.get_children():
                self.visit_subexprs_of_lvalue(x, guards)
        elif u.kind == CK.UNARY_OPERATOR:
            for x in u.get_children():
                bu = self.unwrap(x)
                if bu.kind == CK.DECL_REF_EXPR and bu.referenced is not None:
                    self.occ(self.key(bu.referenced), bu, "read", guards)
                else:
                    self.visit_subexprs_of_lvalue(x, guards)
        elif u.kind == CK.CALL_EXPR:
            self.visit_expr(u, guards)

    def handle_assign(self, lhs: ci.Cursor, rhs: ci.Cursor, site: ci.Cursor, kind: str, guards: list[dict]) -> None:
        targets = self.lvalue_targets(lhs)
        role = "write" if kind == "assign" else "rw"
        for t, tc, via in targets:
            self.occ(t, tc, role, guards, via=via)
        prim = [t for t, _, via in targets if via is None or via == "deref"]
        if not prim and targets:
            prim = [targets[0][0]]
        self.assign_flows(prim, rhs, kind if kind == "compound" else "assign", site, guards)
        # nested fields copied leaf by leaf (`d->ip.addr = s->yiaddr.addr`): also link the outer fields, which keep
        # the value apart from every other struct that happens to use the same small type
        lo, ro = self._outer_field(lhs), self._outer_field(rhs)
        if lo and ro and lo != ro:
            self.emit({"t": "flow", "src": ro, "dst": lo, "kind": "member", "file": self.ctx_file,
                       "line": site.location.line, "fn": self.ctx_fn, "expr": clip(self.text(site), 200), "cfg": self.cfg,
                       "site": None})
        self.visit_subexprs_of_lvalue(lhs, guards)
        self.visit_expr(rhs, guards)

    def fnptr_param_types(self, callee_expr: ci.Cursor | None) -> list[str]:
        if callee_expr is None:
            return []
        t = callee_expr.type.get_canonical()
        if t.kind == ci.TypeKind.POINTER:
            t = t.get_pointee().get_canonical()
        try:
            return [a.get_canonical().spelling for a in t.argument_types()] if t.kind == ci.TypeKind.FUNCTIONPROTO else []
        except Exception:
            return []

    def handle_call(self, c: ci.Cursor, guards: list[dict]) -> None:
        ch = list(c.get_children())
        ref = c.referenced
        callee_expr = ch[0] if ch else None
        args = ch[1:] if ch else []
        callee = via = None
        kind, res = "direct", "exact"
        params: list = []
        if ref is not None and ref.kind == CK.FUNCTION_DECL:
            callee = self.key(ref)
            params = self.fn_params(ref)
            if self.is_stub(ref):
                res = "stub"
            elif not self.in_repo(ref) and callee not in self._ext_emitted:
                # a library / system function: record it so I/O classification and answers can name it
                self._ext_emitted.add(callee)
                proto = " ".join(self.text(ref).split())[:400] or ref.displayname
                self.emit({"t": "symbol", "key": callee, "kind": "function", "name": ref.spelling,
                           "qual": ref.spelling, "sig": proto, "type": ref.type.spelling, "file": None,
                           "line": 0, "end_line": 0, "is_def": False, "extern": True, "lang": "c", "cfg": self.cfg})
            self.occ(callee, c, "call", guards)
        elif ref is not None and ref.kind in (CK.VAR_DECL, CK.FIELD_DECL, CK.PARM_DECL):
            via = self.key(ref)
            kind, res = "fnptr", "unresolved"
            self.occ(via, c, "read", guards)
        else:
            kind, res = "fnptr", "unresolved"
            if callee_expr is not None:
                for n in callee_expr.walk_preorder():
                    if n.kind in (CK.DECL_REF_EXPR, CK.MEMBER_REF_EXPR) and n.referenced is not None:
                        if n.referenced.kind == CK.FUNCTION_DECL:
                            callee, kind, res = self.key(n.referenced), "direct", "exact"
                        else:
                            via = self.key(n.referenced)
                        break
        # the receiver expression of an fnptr call (e.g. k_handlers[i].handler): its reads
        if kind == "fnptr" and callee_expr is not None:
            u = self.unwrap(callee_expr)
            if u.kind == CK.MEMBER_REF_EXPR:
                for x in u.get_children():
                    self.visit_expr(x, guards)
        arg_texts = [clip(self.text(a), 160) for a in args]
        # variadic functions (scanf-style: minmea_scan(sentence, fmt, &a, &b…)): what they write through the variadic
        # pointers is derived from their fixed inputs — a summary edge per fixed input -> variadic output
        if ref is not None and ref.kind == CK.FUNCTION_DECL and callee:
            try:
                variadic = ref.type.get_canonical().is_function_variadic()
            except Exception:
                variadic = False
            if variadic:
                nfixed = len(params)
                for vi in range(nfixed, len(args)):
                    for fi in range(nfixed):
                        self.emit({"t": "flow", "src": f"{callee}#param:{fi}", "dst": f"{callee}#param:{vi}",
                                   "kind": "variadic", "file": self.ctx_file, "line": c.location.line, "fn": callee,
                                   "expr": f"{ref.spelling}(): variadic output {vi + 1} derived from input {fi + 1}",
                                   "cfg": self.cfg})
        # canonical spellings: typedefs like LPVOID / PBYTE hide the pointer that makes an argument an out-parameter
        ptypes = [p.type.get_canonical().spelling for p in params] if params else self.fnptr_param_types(callee_expr)
        written_args = set(_API_WRITES.get(ref.spelling if ref is not None else "", []))
        for i in written_args:
            if i < len(args):
                u = self.unwrap(args[i])
                inner = list(u.get_children())[0] if u.kind == CK.UNARY_OPERATOR and self.unop(u) == "&" and \
                    list(u.get_children()) else u
                for t, tc, via in self.lvalue_targets(inner):
                    if via is None or via == "deref" or inner is not u:
                        self.occ(t, tc, "write", guards, via=f"api:{ref.spelling}")
        target = callee or via
        self.emit({"t": "call", "from": self.ctx_fn, "to": callee, "file": self.ctx_file, "line": c.location.line,
                   "col": c.location.column, "kind": kind, "res": res, "via": via, "args": arg_texts,
                   "guards": guards or None, "cfg": self.cfg})
        name = ref.spelling if ref is not None else ""
        for i, a in enumerate(args):
            pkey = f"{target}#param:{i}" if target else None
            vals = self.values(a)
            if pkey:
                for s in vals:
                    if s.startswith("fnaddr:"):
                        self.emit({"t": "fnaddr", "fn": s[7:], "dst": pkey, "file": self.ctx_file,
                                   "line": a.location.line, "kind": "arg"})
                    else:
                        self.emit({"t": "flow", "src": s, "dst": pkey, "kind": "arg", "file": self.ctx_file,
                                   "line": a.location.line, "fn": self.ctx_fn, "expr": arg_texts[i], "cfg": self.cfg,
                                   "site": c.location.line})
                # out-parameters: &x, arrays, or non-const pointers let the callee write back into x
                u = self.unwrap(a)
                pt = ptypes[i] if i < len(ptypes) else ""
                const_ptr = "*" in pt and "const" in pt.split("*")[0]
                if u.kind == CK.UNARY_OPERATOR and self.unop(u) == "&":
                    writes_back = not const_ptr
                else:
                    writes_back = "*" in pt and not const_ptr and "(" not in pt
                if writes_back:
                    for t, tc, _ in (self.lvalue_targets(list(u.get_children())[0]) if u.kind == CK.UNARY_OPERATOR
                                     and list(u.get_children()) else self.lvalue_targets(a)):
                        self.emit({"t": "flow", "src": pkey, "dst": t, "kind": "outparam", "file": self.ctx_file,
                                   "line": a.location.line, "fn": self.ctx_fn, "expr": arg_texts[i], "cfg": self.cfg,
                                   "site": c.location.line})
                        # a whole struct filled through a pointer (memcpy(out, …), ReadFile(&frame…)): every field
                        for fk in self._record_fields(tc.type):
                            self.emit({"t": "flow", "src": pkey, "dst": fk, "kind": "outparam", "file": self.ctx_file,
                                       "line": a.location.line, "fn": self.ctx_fn, "expr": arg_texts[i],
                                       "cfg": self.cfg, "site": c.location.line})
            self.visit_expr(a, guards)


def _load_api_writes() -> dict[str, list[int]]:
    try:
        from .recon import load_api_pack
        w = load_api_pack().get("writes", {})
        return {k: v for k, v in w.items() if not k.startswith("_")}
    except Exception:
        return {}


_API_WRITES = _load_api_writes()


# ---------------------------------------------------------------------------------------- worker entry points
_W: dict[str, Any] = {}


def worker_init(root: str, profile: CProfile, known: set[str]) -> None:
    _W.update(root=root, profile=profile, known=known, ex={})


def worker_task(payload: tuple[str, str, dict]) -> tuple[dict, list[dict]]:
    """payload = (relative path, cfg label, extra defines). Returns (stats, records)."""
    rel, cfg, defines = payload
    if os.environ.get("VERITY_TEST_CRASH_FILE") == rel:      # test hook: simulate a native crash in the parser
        os.abort()
    recs: list[dict] = []
    ex = _W["ex"].get(cfg)
    if ex is None:
        ex = CExtractor(Path(_W["root"]), _W["profile"], recs.append, _W["known"], cfg=cfg, extra_defines=defines)
        _W["ex"][cfg] = ex
    ex.emit = recs.append
    ex._fn_ranges = {}
    st = ex.extract(rel)
    return st, recs
