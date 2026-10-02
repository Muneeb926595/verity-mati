"""Repository discovery and C build reconstruction (design §00.3, §12): capture a build if one exists, otherwise
detect dialect packs, resolve includes, find free macros, and prepare stub headers so Clang can still bind the code."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config, osutil

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".idea", ".vs", ".vscode",
             "bin", "obj", "out", "build", "cmake-build-debug", "cmake-build-release", "dist", "target", ".verity"}
C_EXT = {".c", ".h"}
CS_EXT = {".cs"}
MAX_FILE_BYTES = 4 * 1024 * 1024        # larger files are indexed syntactically only (reported as such)

# Compiler / platform predefines never count as "free" configuration macros.
KNOWN_PREDEFINES = re.compile(
    r"^(__\w+__|_WIN32|_WIN64|_MSC_VER|_M_\w+|__GNUC\w*|__clang\w*|__STDC\w*|__cplusplus|NDEBUG|_DEBUG|DEBUG|__linux\w*|"
    r"__APPLE__|__MACH__|__unix\w*|_POSIX\w*|__ARM\w*|__arm\w*|__x86_64\w*|__i386\w*|__aarch64\w*|__ICCARM__|__CC_ARM|"
    r"__TI_\w+|__MSP430\w*|__GNU\w*|_GNU_SOURCE|__has_include|__has_attribute|defined|__FILE__|__LINE__|TRUE|FALSE|"
    r"linux|unix|WIN32|WIN64|_WINDOWS|_CONSOLE|UNICODE|_UNICODE|__MINGW\w*|__CYGWIN\w*|_MSC_FULL_VER|__BORLANDC__|"
    r"__FreeBSD__|__NetBSD__|__OpenBSD__|__sun|__QNX\w*|__VXWORKS\w*|_AIX|__hpux|__ANDROID__|__EMSCRIPTEN__)$")


@dataclass
class RepoScan:
    root: Path
    c_files: list[str] = field(default_factory=list)
    h_files: list[str] = field(default_factory=list)
    cs_files: list[str] = field(default_factory=list)
    xaml_files: list[str] = field(default_factory=list)
    other_files: list[str] = field(default_factory=list)
    too_large: list[str] = field(default_factory=list)
    build: dict[str, Any] = field(default_factory=dict)

    @property
    def languages(self) -> list[str]:
        langs = []
        if self.c_files or self.h_files:
            langs.append("c")
        if self.cs_files:
            langs.append("csharp")
        return langs


def scan_repo(root: str | os.PathLike) -> RepoScan:
    root = Path(root).resolve()
    scan = RepoScan(root=root)
    build: dict[str, list[str]] = {"compile_commands": [], "cmake": [], "makefile": [], "vcxproj": [], "csproj": [],
                                   "sln": [], "ccs": [], "keil": [], "iar": []}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = full.relative_to(root).as_posix()
            ext = full.suffix.lower()
            low = fn.lower()
            if low == "compile_commands.json":
                build["compile_commands"].append(rel)
            elif low == "cmakelists.txt":
                build["cmake"].append(rel)
            elif low in ("makefile", "gnumakefile") or ext == ".mk":
                build["makefile"].append(rel)
            elif ext == ".vcxproj":
                build["vcxproj"].append(rel)
            elif ext == ".csproj":
                build["csproj"].append(rel)
            elif ext == ".sln":
                build["sln"].append(rel)
            elif low in (".cproject", ".ccsproject"):
                build["ccs"].append(rel)
            elif ext in (".uvprojx", ".uvproj"):
                build["keil"].append(rel)
            elif ext == ".ewp":
                build["iar"].append(rel)
            if ext in C_EXT or ext in CS_EXT:
                try:
                    if full.stat().st_size > MAX_FILE_BYTES:
                        scan.too_large.append(rel)
                        continue
                except OSError:
                    continue
            if ext == ".c":
                scan.c_files.append(rel)
            elif ext == ".h":
                scan.h_files.append(rel)
            elif ext == ".cs":
                scan.cs_files.append(rel)
            elif ext == ".xaml":
                scan.xaml_files.append(rel)
    for k in build:
        build[k].sort()
    scan.build = build
    scan.c_files.sort(); scan.h_files.sort(); scan.cs_files.sort()
    return scan


def read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_packs() -> dict[str, dict]:
    data = json.loads((config.PACKS / "dialects.json").read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def entry_patterns() -> list[tuple[re.Pattern, str]]:
    d = json.loads((config.PACKS / "dialects.json").read_text()).get("_entry_patterns", {})
    return [(re.compile(p, re.M), why) for p, why in d.get("patterns", [])]


def load_api_pack() -> dict[str, Any]:
    return json.loads((config.PACKS / "apis.json").read_text())


INCLUDE_RE = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]', re.M)
COND_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif)\b(.*)$", re.M)
DEFINE_RE = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)", re.M)
IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")


@dataclass
class CProfile:
    """Everything needed to parse the C code of one repository (one compiler profile)."""
    packs: list[str]
    pack_scores: dict[str, float]
    args: list[str]
    include_dirs: list[str]
    defines: dict[str, str]
    prelude_path: str
    stub_dir: str
    rewrites: list[tuple[str, str]]
    sysroot_args: list[str]
    compile_db: dict[str, list[str]] | None      # per-file args when a build was captured (F1)
    capture: str                                 # "compile_commands" | "cmake" | "vcxproj" | "reconstructed"
    unresolved_includes: dict[str, list[str]]    # include name -> including files
    free_macros: dict[str, dict]                 # name -> {"lines": n, "files": [...]}
    data_model: str
    label: str
    shim_dirs: list[str] = field(default_factory=list)    # API knowledge-pack headers standing in for a missing SDK
    header_preamble: list[str] = field(default_factory=list)  # headers the project includes first (for parsing headers)
    knobs: dict[str, dict] = field(default_factory=dict)   # overridable feature options -> value for the all-features build


def detect_dialects(root: Path, files: list[str]) -> dict[str, float]:
    packs = load_packs()
    scores = {k: 0.0 for k in packs}
    sample = files[:400]
    for rel in sample:
        text = read_text(root / rel)
        for name, pack in packs.items():
            for pat, w in pack.get("signals", []):
                n = len(re.findall(pat, text))
                if n:
                    scores[name] += w * min(n, 5)
    total = max(1.0, len(sample) ** 0.5)
    return {k: round(v / total, 2) for k, v in scores.items()}


def strip_comments_keep_lines(text: str) -> str:
    """Remove C comments but keep every newline, so line numbers stay valid."""
    return re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: "\n" * m.group(0).count("\n") + " ", text, flags=re.S)


def find_free_macros(root: Path, files: list[str]) -> dict[str, dict]:
    defined: set[str] = set()
    uses: dict[str, dict] = {}
    for rel in files:
        text = strip_comments_keep_lines(read_text(root / rel))     # "#if X /* don't build… */" is not about "don"
        defined.update(DEFINE_RE.findall(text))
        lines = text.split("\n")
        for i, line in enumerate(lines):
            m = re.match(r"^\s*#\s*(if|ifdef|ifndef|elif)\b(.*)$", line)
            if not m:
                continue
            names = [n for n in IDENT_RE.findall(m.group(2)) if not KNOWN_PREDEFINES.match(n) and not n.isdigit()
                     and n != "defined"]
            # count guarded lines until the matching #else/#elif/#endif at the same depth
            depth, j = 0, i + 1
            while j < len(lines):
                s = lines[j].strip()
                if re.match(r"#\s*if", s):
                    depth += 1
                elif re.match(r"#\s*endif", s):
                    if depth == 0:
                        break
                    depth -= 1
                elif re.match(r"#\s*(else|elif)", s) and depth == 0:
                    break
                j += 1
            for n in names:
                u = uses.setdefault(n, {"lines": 0, "files": set(), "first": f"{rel}:{i + 1}"})
                u["lines"] += max(0, j - i - 1)
                u["files"].add(rel)
    out = {}
    for n, u in uses.items():
        if n in defined:
            continue
        # skip include guards (tested with #ifndef and defined right after) — they're in `defined`
        out[n] = {"lines": u["lines"], "files": sorted(u["files"]), "first": u["first"]}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["lines"]))


KNOB_NEGATIVE_RE = re.compile(r"(^|_)(NO|NOT|DISABLE[DS]?|NOASSERT|SKIP|WITHOUT|OFF|STRICT|LEGACY|TEST(ING|MODE)?|DEBUG\w*|"
                              r"TRACE\w*|DEPRECATED|COMPAT\w*|FUZZ\w*|FORCE\w*|MPU|WRAPPERS?|NOSIGNAL)(_|$)|MPU_|_MPU", re.I)


def find_config_knobs(root: Path, files: list[str]) -> dict[str, dict]:
    """Feature options a project lets its users override: `#ifndef X` / `#if !defined X` followed by `#define X default`,
    where X is also tested by `#if`/`#elif` elsewhere. Returns name -> {"value": for the all-features build, "default",
    "where"}. Boolean options get 1, bitmask options get every bit (-1); sizes, selectors and options whose name says
    they turn something OFF (NO_SYS, *_DISABLE) or add diagnostics are left at their defaults."""
    cand: dict[str, dict] = {}
    tested: set[str] = set()
    masked: set[str] = set()
    flags: dict[str, set[str]] = {}          # bitmask option -> the flag macros it is tested against
    for rel in files:
        text = strip_comments_keep_lines(read_text(root / rel))
        lines = text.split("\n")
        for i, line in enumerate(lines):
            m = re.match(r"^\s*#\s*(if|ifdef|ifndef|elif)\b(.*)$", line)
            if not m:
                continue
            expr = m.group(2)
            names = set(IDENT_RE.findall(expr))
            tested |= names
            for n in names:
                if re.search(rf"\b{re.escape(n)}\b\s*\)?\s*&(?!&)|(?<!&)&\s*\(?\s*\b{re.escape(n)}\b", expr):
                    masked.add(n)
                    for f in re.findall(rf"\b{re.escape(n)}\b\s*\)?\s*&(?!&)\s*\(?\s*([A-Za-z_]\w*)", expr) + \
                            re.findall(rf"([A-Za-z_]\w*)\s*\)?\s*(?<!&)&(?!&)\s*\(?\s*\b{re.escape(n)}\b", expr):
                        flags.setdefault(n, set()).add(f)
            g = re.match(r"^\s*(?:!\s*defined\s*\(?\s*([A-Za-z_]\w*)\s*\)?(?:\s*\|\|\s*defined\s*\(?\s*__DOXYGEN__\s*\)?)?"
                         r"\s*$|([A-Za-z_]\w*)\s*$)", expr) if m.group(1) in ("ifndef", "if") else None
            if not g:
                continue
            name = g.group(1) or (g.group(2) if m.group(1) == "ifndef" else None)
            if not name:
                continue
            for j in range(i + 1, min(i + 5, len(lines))):
                d = re.match(rf"^\s*#\s*define\s+{re.escape(name)}\b(?!\()\s*(.*)$", lines[j])
                if d:
                    val = d.group(1).rstrip()
                    k = j
                    while val.endswith("\\") and k + 1 < len(lines):   # continued on the next lines
                        k += 1
                        val = val[:-1] + " " + lines[k].strip()
                    val = " ".join(val.split())
                    if val and name not in cand:
                        cand[name] = {"default": val[:80], "where": f"{rel}:{j + 1}"}
                    break
                if lines[j].strip() and not lines[j].strip().startswith("#"):
                    break
    from ..pathkinds import is_secondary_path
    out = {}
    for name, info in cand.items():
        if name not in tested or KNOB_NEGATIVE_RE.search(name) or KNOWN_PREDEFINES.match(name) or \
                is_secondary_path(info["where"].rsplit(":", 1)[0]):
            continue
        v = re.sub(r"[()\s]|[uUlL]+$", "", info["default"])
        if name in masked and (re.search(r"\||<<|0x[0-9a-fA-F]+|[A-Za-z_]", info["default"]) or re.fullmatch(r"\d+", v)):
            # every feature flag it is tested against, except flags that REMOVE code (…_EXTERNAL: "implemented
            # elsewhere", …_DISABLE); falls back to all bits only when no flag names are known
            pos = sorted(f for f in flags.get(name, ()) if not re.search(r"EXTERN|CUSTOM|OVERRIDE|USER_?DEFINED", f) and
                         not KNOB_NEGATIVE_RE.search(f) and not KNOWN_PREDEFINES.match(f))
            out[name] = dict(info, value="(" + " | ".join(pos) + ")" if pos else "-1")
        elif v == "0":
            out[name] = dict(info, value="1")           # an option that is off by default (1 = already on: no change)
    return out


SHIM_PACKS = ("win32", "posix")  # header shims that stand in for a platform SDK (libc shims are separate)


def find_shim(name: str) -> str | None:
    """The include folder of a knowledge-pack header for a missing system header (windows.h, sys/time.h), or None.
    A header with a folder part (sys/time.h) is found under that path in the pack, which is then the include folder;
    otherwise by its file name."""
    name = name.replace("\\", "/").lower()
    for pack in SHIM_PACKS:
        if "/" in name and (config.SHIMS / pack / name).is_file():
            return str(config.SHIMS / pack)
        cand = config.SHIMS / pack / os.path.basename(name)
        if cand.is_file():
            return str(cand.parent)
    return None


def windows_sdk_include_args() -> list[str]:
    """On Windows: the real Windows SDK + MSVC headers (INCLUDE from a Developer prompt, else the standard
    install locations), so MSVC code is parsed against its true headers (fidelity F2 instead of shims)."""
    if os.name != "nt":
        return []
    dirs: list[str] = []
    for d in (os.environ.get("INCLUDE") or "").split(";"):
        if d and os.path.isdir(d):
            dirs.append(d)
    if not dirs:
        kits = sorted(Path("C:/Program Files (x86)/Windows Kits/10/Include").glob("10.*"))
        if kits:
            for sub in ("ucrt", "um", "shared", "winrt"):
                if (kits[-1] / sub).is_dir():
                    dirs.append(str(kits[-1] / sub))
        for base in ("C:/Program Files/Microsoft Visual Studio", "C:/Program Files (x86)/Microsoft Visual Studio"):
            msvc = sorted(Path(base).glob("*/*/VC/Tools/MSVC/*/include"))
            if msvc:
                dirs.append(str(msvc[-1]))
                break
    out: list[str] = []
    for d in dirs:
        out += ["-isystem", d]
    return out


def host_sysroot_args() -> list[str]:
    """System headers for parsing: the platform SDK when present, else bundled libc shims."""
    win = windows_sdk_include_args()
    if win:
        return win
    args: list[str] = []
    resource = None
    for cand in sorted(Path("/Library/Developer/CommandLineTools/usr/lib/clang").glob("*/include")) if Path(
            "/Library/Developer/CommandLineTools/usr/lib/clang").exists() else []:
        resource = cand
    if shutil.which("xcrun") and osutil.mac_dev_tools():
        try:
            sdk = subprocess.run(["xcrun", "--show-sdk-path"], capture_output=True, text=True, timeout=10).stdout.strip()
            if sdk and os.path.isdir(sdk):
                args += ["-isysroot", sdk]
                if resource:
                    args += ["-isystem", str(resource)]
                return args
        except Exception:
            pass
    for inc in ("/usr/include", "/usr/local/include"):
        if os.path.isfile(os.path.join(inc, "stdio.h")):
            gcc_inc = sorted(Path("/usr/lib/gcc").glob("*/*/include")) if Path("/usr/lib/gcc").exists() else []
            args += ["-isystem", inc] + (["-isystem", str(gcc_inc[-1])] if gcc_inc else [])
            return args
    return ["-nostdinc", "-isystem", str(config.SHIMS / "libc")]


def _resolve_includes(root: Path, files: list[str], headers: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    by_suffix: dict[str, list[str]] = {}
    for h in headers:
        parts = h.split("/")
        for k in range(len(parts)):
            by_suffix.setdefault("/".join(parts[k:]), []).append(h)
    include_dirs: dict[str, int] = {}
    unresolved: dict[str, list[str]] = {}
    for rel in files:
        text = read_text(root / rel)
        base = os.path.dirname(rel)
        for kind, inc in INCLUDE_RE.findall(text):
            inc = inc.strip().replace("\\", "/")
            local = os.path.normpath(os.path.join(base, inc)).replace("\\", "/")
            if (root / local).is_file():
                continue
            cands = by_suffix.get(inc)
            if cands:
                cand = sorted(cands, key=lambda c: (len(c), c))[0]
                d = cand[: len(cand) - len(inc)].rstrip("/")
                include_dirs[d] = include_dirs.get(d, 0) + 1
            elif kind == '"':
                unresolved.setdefault(inc, []).append(rel)
            else:
                unresolved.setdefault("<" + inc + ">", []).append(rel)
    ordered = [d for d, _ in sorted(include_dirs.items(), key=lambda kv: -kv[1])]
    return ordered, unresolved


def _system_header_exists(name: str, sysroot_args: list[str]) -> bool:
    name = name.strip("<>")
    roots = []
    for i, a in enumerate(sysroot_args):
        if a in ("-isysroot",):
            roots.append(os.path.join(sysroot_args[i + 1], "usr", "include"))
        if a == "-isystem":
            roots.append(sysroot_args[i + 1])
    roots.append(str(config.SHIMS / "libc"))
    return any(os.path.isfile(os.path.join(r, name)) for r in roots)


CMAKE_COPY_LIMIT = 1 << 30          # bytes: larger trees are reconstructed instead of copied for a CMake configure
_VCS_DIRS = (".git", ".svn", ".hg")


def _private_source_copy(root: Path, dest: Path) -> bool:
    """Copy the project for a CMake configure: configure scripts may write into their source tree (lwIP writes
    doc/doxygen/lwip.Doxyfile), and the user's folder is never modified. False when it is too large or cannot be
    copied."""
    dest_abs = os.path.normcase(os.path.abspath(dest))

    def skipped(dirpath: str, names: list[str]) -> set[str]:
        # version-control folders, and the folder holding the copy itself (Verity's data folder inside the project)
        return {n for n in names if n in _VCS_DIRS or
                (dest_abs + os.sep).startswith(os.path.normcase(os.path.abspath(os.path.join(dirpath, n))) + os.sep)}

    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skipped(dirpath, dirnames)]
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
        if total > CMAKE_COPY_LIMIT:
            return False
    shutil.rmtree(dest, ignore_errors=True)
    try:
        shutil.copytree(root, dest, ignore=skipped, ignore_dangling_symlinks=True)
    except (OSError, shutil.Error):
        shutil.rmtree(dest, ignore_errors=True)
        return False
    return True


def _compile_db(root: Path, scan: RepoScan, work: Path) -> tuple[dict[str, list[str]] | None, str]:
    """F1 capture: an existing compile_commands.json, or a CMake configure (no build) that emits one."""
    dbs = [root / p for p in scan.build.get("compile_commands", [])]
    copy: Path | None = None                      # the private source copy a CMake configure ran in
    if not dbs and scan.build.get("cmake") and shutil.which("cmake") and (root / "CMakeLists.txt").is_file():
        out = work / "cmake-capture"
        src = work / "cmake-src" / root.name      # one level down: "../x" writes from the scripts stay in work/
        try:
            if _private_source_copy(root, src):
                cache = out / "CMakeCache.txt"    # a cache made for another source folder makes cmake refuse
                if cache.is_file() and f"CMAKE_HOME_DIRECTORY:INTERNAL={src.as_posix()}" not in \
                        cache.read_text(errors="replace"):
                    shutil.rmtree(out, ignore_errors=True)
                subprocess.run(["cmake", "-S", str(src), "-B", str(out), "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"],
                               capture_output=True, timeout=180)
                if (out / "compile_commands.json").is_file():
                    dbs = [out / "compile_commands.json"]
                    copy = src.resolve()
                else:                             # e.g. a Visual Studio generator, which exports nothing
                    shutil.rmtree(src, ignore_errors=True)
        except Exception:
            pass
    if not dbs:
        return None, "reconstructed"
    real_root = root.resolve()
    generated: set[str] = set()                   # folders of the copy where the configure wrote new files
    if copy is not None:
        for dirpath, _dirs, filenames in os.walk(copy):
            rel_dir = Path(dirpath).relative_to(copy)
            if any(not (real_root / rel_dir / n).exists() for n in filenames):
                generated.add(rel_dir.as_posix())

    def real(p: Path) -> Path:
        """A path in the private copy, as the same path in the project; files only the configure generated stay in
        the copy."""
        if copy is None:
            return p
        try:
            q = real_root / p.resolve().relative_to(copy)
        except (ValueError, OSError):
            return p
        return q if q.exists() else p

    def generated_twin(d: Path) -> list[str]:
        """-I for the copy of a project folder the configure generated headers into (after the real folder, so
        the project's own files win)."""
        try:
            rel_dir = d.resolve().relative_to(real_root).as_posix()
        except (ValueError, OSError):
            return []
        return ["-I" + str(copy / rel_dir)] if copy is not None and rel_dir in generated else []

    per_file: dict[str, list[str]] = {}
    for db in dbs:
        try:
            entries = json.loads(db.read_text())
        except Exception:
            continue
        for e in entries:
            f = real(Path(e.get("directory", ".")) / e["file"])
            try:
                rel = f.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            argv = e.get("arguments") or e.get("command", "").split()
            keep: list[str] = []
            it = iter(range(len(argv)))
            skip_next = False
            for k in it:
                a = argv[k]
                if skip_next:
                    skip_next = False
                    continue
                if a in ("-I", "-D", "-U", "-include", "-isystem", "-std"):
                    if k + 1 < len(argv):
                        val = argv[k + 1]
                        if a in ("-I", "-isystem", "-include"):
                            if not os.path.isabs(val):
                                val = str(Path(e.get("directory", ".")) / val)
                            if copy is not None:
                                val = str(real(Path(val)))
                        keep += [a, val]
                    skip_next = True
                elif a.startswith(("-I", "-D", "-U", "-std=", "-include", "-isystem", "-f", "-m", "--target", "-target")):
                    if a.startswith("-I") and len(a) > 2:
                        d = a[2:] if os.path.isabs(a[2:]) else str(Path(e.get("directory", ".")) / a[2:])
                        a = "-I" + (str(real(Path(d))) if copy is not None else d)
                    keep.append(a)
            if generated:
                twins: list[str] = []
                for d in [f.parent] + [Path(keep[i + 1]) for i, a in enumerate(keep[:-1]) if a == "-I"] + \
                        [Path(a[2:]) for a in keep if a.startswith("-I") and len(a) > 2]:
                    for t in generated_twin(d):
                        if t not in twins:
                            twins.append(t)
                keep += twins
            per_file[rel] = keep
    return (per_file or None), "compile_commands"


def _project_file_flags(root: Path, scan: RepoScan) -> tuple[list[str], dict[str, str]]:
    """Include dirs / defines read from IDE project files (vcxproj, CCS .cproject) without building."""
    incs: list[str] = []
    defs: dict[str, str] = {}
    for rel in scan.build.get("vcxproj", [])[:20]:
        text = read_text(root / rel)
        base = (root / rel).parent
        for m in re.findall(r"<AdditionalIncludeDirectories>(.*?)</AdditionalIncludeDirectories>", text, re.S):
            for d in m.split(";"):
                d = d.strip()
                if d and "$(" not in d and "%(" not in d:
                    p = (base / d.replace("\\", "/")).resolve()
                    if p.is_dir():
                        incs.append(str(p))
        for m in re.findall(r"<PreprocessorDefinitions>(.*?)</PreprocessorDefinitions>", text, re.S):
            for d in m.split(";"):
                d = d.strip()
                if d and "%(" not in d and "$(" not in d:
                    k, _, v = d.partition("=")
                    defs.setdefault(k, v or "1")
    for rel in scan.build.get("ccs", [])[:20]:
        text = read_text(root / rel)
        base = (root / rel).parent
        for d in re.findall(r'value="\$\{PROJECT_ROOT\}/?([^"]*)"', text):
            p = (base / d).resolve()
            if p.is_dir():
                incs.append(str(p))
    return incs, defs


def plan_c_profile(root: str | os.PathLike, scan: RepoScan, work: Path, hints: dict | None = None) -> CProfile:
    root = Path(root).resolve()
    hints = hints or {}
    work.mkdir(parents=True, exist_ok=True)
    c_all = scan.c_files + scan.h_files
    scores = detect_dialects(root, c_all)
    packs_data = load_packs()
    chosen = ["neutral"]
    vendor = [k for k, v in sorted(scores.items(), key=lambda kv: -kv[1]) if k != "neutral" and v >= 1.0]
    for v in vendor[:3]:
        chosen.append(v)
    args = ["-x", "c", "-ferror-limit=0", "-Wno-everything", "-Wno-error=implicit-function-declaration",
            "-fno-spell-checking"]
    defines: dict[str, str] = {}
    prelude = ["/* Verity reconstruction prelude: dialect shims + stubs for missing declarations. */"]
    rewrites: list[tuple[str, str]] = []
    data_model = "LP64"
    for name in chosen:
        p = packs_data.get(name, {})
        args += p.get("args", [])
        defines.update(p.get("defines", {}))
        if p.get("prelude"):
            prelude.append(f"/* pack: {name} */\n" + p["prelude"])
        rewrites += [tuple(r) for r in p.get("rewrite", [])]
        if p.get("data_model") and name != "neutral":
            data_model = p["data_model"]
    if "msvc" in chosen:
        data_model = "LLP64"
    for d in (hints.get("c", {}) or {}).get("defines", []) or []:
        k, _, v = str(d).partition("=")
        defines[k] = v or "1"
    include_dirs, unresolved = _resolve_includes(root, c_all, scan.h_files)
    for d in (hints.get("c", {}) or {}).get("include", []) or []:
        include_dirs.append(str((root / d).resolve()))
    # every directory holding a header is a fallback include dir (reconstruction)
    hdr_dirs = sorted({str(Path(h).parent) for h in scan.h_files}, key=lambda d: (d.count("/"), d))
    inc_abs = []
    for d in include_dirs + hdr_dirs:
        p = str((root / d).resolve()) if not os.path.isabs(d) else d
        if p not in inc_abs:
            inc_abs.append(p)
    proj_incs, proj_defs = _project_file_flags(root, scan)
    for p in proj_incs:
        if p not in inc_abs:
            inc_abs.insert(0, p)
    for k, v in proj_defs.items():
        defines.setdefault(k, v)
    sysroot = host_sysroot_args()
    stub_dir = work / "stubs"
    if stub_dir.exists():
        shutil.rmtree(stub_dir, ignore_errors=True)
    stub_dir.mkdir(parents=True, exist_ok=True)
    really_missing: dict[str, list[str]] = {}
    shim_dirs: list[str] = []
    for inc, users in unresolved.items():
        name = inc.strip("<>")
        # like a compiler, a "quoted" include missing from the project falls back to the system headers; an empty
        # stand-in would hide the real one (libmodbus: #include "stdint.h" under _MSC_VER)
        if _system_header_exists(inc, sysroot):
            continue
        shim = find_shim(name)
        if shim:                                  # a knowledge-pack header stands in for the missing SDK header
            if shim not in shim_dirs:
                shim_dirs.append(shim)
            continue
        target = stub_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"/* Verity stub: '{name}' was not found in the repository or on this machine. */\n")
        really_missing[name] = sorted(set(users))
    if os.name != "nt" and "-fms-compatibility" in args:
        # MS compatibility mode changes va_list and other ABI details, which clashes with a non-Windows C library;
        # -fms-extensions keeps the MSVC language extensions (__int64, __declspec, __stdcall…)
        args = [a for a in args if a != "-fms-compatibility"]
    if "msvc" in chosen and not windows_sdk_include_args():
        # MSVC CRT extensions (sprintf_s, strcpy_s, _stricmp…) are missing from other C libraries
        prelude.append(f'/* pack: msvc CRT */\n#include "{config.SHIMS / "win32" / "msvc_crt.h"}"')
    for d in shim_dirs:
        sysroot = sysroot + ["-isystem", d]
    prelude_path = work / "prelude.h"
    prelude_path.write_text("\n".join(prelude) + "\n")
    compile_db, capture = _compile_db(root, scan, work)
    if capture == "reconstructed" and (proj_incs or proj_defs):
        capture = "project-file"
    free = find_free_macros(root, c_all)
    knobs = find_config_knobs(root, c_all)
    label = " + ".join(packs_data.get(n, {}).get("label", n) for n in chosen)
    preamble = _header_preamble(root, scan)
    return CProfile(packs=chosen, pack_scores=scores, args=args, include_dirs=inc_abs, defines=defines,
                    prelude_path=str(prelude_path), stub_dir=str(stub_dir), rewrites=rewrites, sysroot_args=sysroot,
                    compile_db=compile_db, capture=capture, unresolved_includes=really_missing, free_macros=free,
                    data_model=(hints.get("c", {}) or {}).get("data_model", data_model) if hints else data_model,
                    label=label, shim_dirs=shim_dirs, header_preamble=preamble, knobs=knobs)


INT_TYPE_RE = re.compile(r"^(u?int\d*|U?INT\d*|Uint\d+|int\d+|u\d+|s\d+|i\d+|BYTE|WORD|DWORD\w*|BOOL\w*|UCHAR|USHORT|ULONG\w*|"
                         r"LONG\w*|SHORT|CHAR|Bool|bool_t|boolean|uchar|ushort|ulong|byte|word|dword|size_type)$")
FLOAT_TYPE_RE = re.compile(r"^(float\d*(_t)?|Float\d*|FLOAT\d*|real\d*(_T)?|REAL\d*|f32|f64|float32_t|float64_t|"
                           r"double\d*(_t)?|Double|FP32|FP64|single)$")
CONST_MACRO_RE = re.compile(r"^([A-Z][A-Z0-9]*_[A-Z0-9_]+|[A-Z]{3,}[0-9]*|[a-z]{2,6}[A-Z][A-Z0-9_]{2,})$")   # FOO_BAR, configMAX_X
SAL_RE = re.compile(r"^(_[A-Z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*_|__(?:in|out|inout|deref|checkReturn|reserved|nullterminated)\w*)$")


_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_CODE_CACHE: dict[int, str] = {}


def code_only(text: str) -> str:
    """Source text without comments (documentation must not be mistaken for code)."""
    k = hash(text)
    if k not in _CODE_CACHE:
        if len(_CODE_CACHE) > 8:
            _CODE_CACHE.clear()
        _CODE_CACHE[k] = _COMMENT_RE.sub(" ", text)
    return _CODE_CACHE[k]


def _member_tree(name: str, text: str) -> tuple[dict, bool, bool]:
    """Member chains used on `name` (N.a.b, N->a[i].b) as a nested dict; (tree, via_pointer, via_dot)."""
    tree: dict = {}
    ptr = dot = False
    text = code_only(text)
    for m in re.finditer(rf"\b{re.escape(name)}((?:(?:\.|\s*->\s*)[A-Za-z_]\w*\s*(?:\[[^\]]*\])?)+)", text):
        chain = m.group(1)
        first = chain.lstrip()[:2]
        if first == "->":
            ptr = True
        else:
            dot = True
        node = tree
        for mm in re.finditer(r"(?:\.|->\s*)([A-Za-z_]\w*)\s*(\[[^\]]*\])?", chain):
            key = mm.group(1) + ("[]" if mm.group(2) else "")
            node = node.setdefault(key, {})
    return tree, ptr, dot


def _struct_body(tree: dict) -> str:
    parts = []
    for key, sub in sorted(tree.items()):
        arr = key.endswith("[]")
        nm = key[:-2] if arr else key
        suffix = "[256]" if arr else ""
        if sub:
            parts.append(f"struct {{ {_struct_body(sub)} }} {nm}{suffix};")
        else:
            parts.append(f"volatile unsigned long long {nm}{suffix};")     # wide enough for addresses (vector tables)
    return " ".join(parts)


def _header_preamble(root: Path, scan: RepoScan) -> list[str]:
    """The project header most .c files include FIRST (FreeRTOS.h, lwip/opt.h, stdafx.h…): headers are parsed after
    it, the way the project itself uses them, instead of standalone."""
    by_name: dict[str, str] = {}
    for h in scan.h_files:
        by_name.setdefault(os.path.basename(h), h)
    counts: dict[str, int] = {}
    for rel in scan.c_files:
        for kind, inc in INCLUDE_RE.findall(read_text(root / rel)):
            name = os.path.basename(inc.strip())
            if name in by_name:
                counts[by_name[name]] = counts.get(by_name[name], 0) + 1
                break
    if not counts or not scan.c_files:
        return []
    top, n = max(counts.items(), key=lambda kv: kv[1])
    return [str((root / top).resolve())] if n >= max(2, 0.3 * len(scan.c_files)) else []


def add_stub_declarations(prelude_path: str, diagnostics: list[str], source_text: str, known: set[str]) -> list[str]:
    """Stub loop (design §00.3): turn 'unknown type' / 'undeclared identifier' diagnostics into declarations,
    guided by how the name is USED in the code (`source_text` = the file plus the project headers):
      annotation macros (SAL `_In_`, `__out`)      -> removed (#define)
      N.a.b / N->a used on an undeclared name      -> a synthesised (register) struct with exactly those members
      unknown type used with members               -> a struct with those members
      unknown integer-like type (Uint16, DWORD…)   -> an integer typedef
      N( ... )                                     -> a function;  otherwise an extern int
    Returns the names that were stubbed in this round."""
    added: list[str] = []
    lines: list[str] = []
    text = code_only(source_text)
    # resolve unknown TYPES first: while a type is unknown its declarations fail, and names they declare (locals)
    # show up as "undeclared identifiers" that must not be stubbed as globals
    if any("unknown type name" in d for d in diagnostics):
        diagnostics = [d for d in diagnostics if "unknown type name" in d or
                       re.search(r"undeclared identifier '(\w+)'", d) and SAL_RE.match(re.search(r"'(\w+)'", d).group(1))]
    for msg in diagnostics:
        m = re.search(r"unknown type name '(\w+)'", msg)
        if m and m.group(1) not in known:
            n = m.group(1)
            known.add(n)
            added.append(n)
            if SAL_RE.match(n):
                lines.append(f"#define {n} /* stub: annotation macro */")
                continue
            # members accessed through variables declared with this type
            tree: dict = {}
            for vm in re.finditer(rf"\b{re.escape(n)}\s*\*?\s*([A-Za-z_]\w*)\s*[;,=)\[]", text):
                t2, _, _ = _member_tree(vm.group(1), text)
                for k, v in t2.items():
                    tree.setdefault(k, {}).update(v)
            if tree:
                lines.append(f"typedef struct __vr_stub_{n} {{ {_struct_body(tree)} }} {n}; /* stub: struct from member use */")
            elif FLOAT_TYPE_RE.match(n):
                lines.append(f"typedef {'double' if '64' in n or 'ouble' in n else 'float'} {n}; /* stub: float-like type */")
            elif INT_TYPE_RE.match(n) or n.isupper() or re.search(rf"\b{re.escape(n)}\s+\w+\s*=\s*[-\d(]", text):
                lines.append(f"typedef {'unsigned int' if n[:1] in 'uU' or 'BYTE' in n or 'WORD' in n else 'int'} {n}; "
                             f"/* stub: integer-like type */")
            else:
                lines.append(f"typedef struct __vr_stub_{n} {n}; /* stub: unknown type */")
            continue
        m = re.search(r"(?:use of undeclared identifier|implicit declaration of function|call to undeclared function|"
                      r"call to undeclared library function) '(\w+)'", msg)
        if m and m.group(1) not in known:
            n = m.group(1)
            known.add(n)
            added.append(n)
            if SAL_RE.match(n):
                if re.search(rf"\b{re.escape(n)}\s*\(", text):
                    lines.append(f"#define {n}(...) /* stub: annotation macro */")
                else:
                    lines.append(f"#define {n} /* stub: annotation macro */")
                continue
            tree, ptr, dot = _member_tree(n, text)
            if tree:
                body = _struct_body(tree)
                if ptr and not dot:
                    lines.append(f"struct __vr_regs_{n} {{ {body} }}; extern struct __vr_regs_{n} *{n}; /* stub: register block */")
                else:
                    lines.append(f"struct __vr_regs_{n} {{ {body} }}; extern volatile struct __vr_regs_{n} {n}; "
                                 f"/* stub: register block */")
            elif re.search(rf"\b{re.escape(n)}\s*\(\s*\*", text) or \
                    (re.search(rf"\b{re.escape(n)}\s+\**\s*[A-Za-z_]\w*\s*[;=,)\[]", text) and
                     (INT_TYPE_RE.match(n) or n.isupper())):
                lines.append(f"typedef int {n}; /* stub: type used in declarations */")
            elif re.search(rf"\b{re.escape(n)}\s*\(", text):
                lines.append(f"int {n}(); /* stub: undeclared function */")
            elif re.search(rf"(?<![\w.>])\b{re.escape(n)}\s*(=(?!=)|[|&^+\-*/%]=|<<=|>>=|\+\+|--)|(\+\+|--)\s*{re.escape(n)}\b", text):
                # assigned somewhere (`IER = 0x0000;`, `IFR |= M_INT1`): a variable or CPU/hardware register, even when
                # it is spelled like a constant
                lines.append(f"extern volatile unsigned long {n}; /* stub: undeclared variable / register */")
            elif CONST_MACRO_RE.match(n):
                # a configuration/constant macro from a missing header (configMAX_PRIORITIES, PIEACK_GROUP1): it may size
                # arrays or label cases, so it must be a constant expression
                lines.append(f"#define {n} 1 /* stub: unknown constant */")
            else:
                lines.append(f"extern int {n}; /* stub: undeclared identifier */")
    if lines:
        with open(prelude_path, "a") as f:
            f.write("\n".join(lines) + "\n")
    return added
