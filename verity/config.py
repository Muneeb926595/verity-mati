"""Paths, hints and per-repository locations. Everything lives under the project directory by default."""
from __future__ import annotations

import hashlib
import os
import pathlib
import tomllib
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOME = pathlib.Path(os.environ.get("VERITY_HOME", ROOT / "data"))
MODELS = pathlib.Path(os.environ.get("VERITY_MODELS", ROOT / "models"))
TOOLS = pathlib.Path(os.environ.get("VERITY_TOOLS", ROOT / "tools"))
PACKS = pathlib.Path(__file__).resolve().parent / "extract" / "packs"
SHIMS = pathlib.Path(__file__).resolve().parent / "extract" / "shims"

# Offline guarantees for any library we might import or spawn (set before anything else runs).
for _k, _v in {
    "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1", "DO_NOT_TRACK": "1",
}.items():
    os.environ.setdefault(_k, _v)


def repo_id(path: str | os.PathLike) -> str:
    real = os.path.realpath(os.fspath(path))
    name = os.path.basename(real.rstrip(os.sep)) or "repo"
    return f"{name}-{hashlib.sha1(real.encode()).hexdigest()[:10]}"


def repo_dir(path: str | os.PathLike) -> pathlib.Path:
    d = HOME / "repos" / repo_id(path)
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_hints(repo_root: str | os.PathLike) -> dict[str, Any]:
    """Optional hints file (verity.toml in the repository root). Never required."""
    p = pathlib.Path(repo_root) / "verity.toml"
    if not p.is_file():
        return {}
    try:
        return tomllib.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {"_error": "verity.toml could not be parsed; hints ignored"}


def tool_path(*parts: str) -> pathlib.Path:
    return TOOLS.joinpath(*parts)
