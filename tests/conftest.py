"""Test fixtures: every test run uses a private VERITY_HOME and private copies of the sample projects, so the real
data/ folder and samples/ are never touched."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_HOME = tempfile.mkdtemp(prefix="verity-test-home-")
os.environ["VERITY_HOME"] = _HOME
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from verity.runtime_exec.harness import sandbox_available  # noqa: E402

# Running the user's code needs an OS sandbox (macOS sandbox-exec, Linux bubblewrap). Windows has no adapter yet, so
# there scenarios are answered by reading the code and these tests are skipped.
needs_sandbox = pytest.mark.skipif(not sandbox_available()[0],
                                   reason="needs a sandbox to run code (no Windows adapter yet)")


@pytest.fixture(scope="session")
def c_repo(tmp_path_factory):
    d = tmp_path_factory.mktemp("repos") / "navgps-c"
    shutil.copytree(ROOT / "samples" / "navgps-c", d)
    return d


@pytest.fixture(scope="session")
def cs_repo(tmp_path_factory):
    d = tmp_path_factory.mktemp("repos") / "navgps-cs"
    shutil.copytree(ROOT / "samples" / "navgps-cs", d)
    return d


@pytest.fixture(scope="session")
def core():
    from verity.engine.core import EngineCore
    c = EngineCore(llm_mode="direct", embeddings=False)
    yield c
    c.shutdown()


@pytest.fixture(scope="session")
def c_indexed(core, c_repo):
    j = core.start_index(str(c_repo), full=True)
    r = core.wait(j["id"], 600)
    assert r["state"] == "done", r
    return c_repo


@pytest.fixture(scope="session")
def cs_indexed(core, cs_repo):
    j = core.start_index(str(cs_repo), full=True)
    r = core.wait(j["id"], 600)
    assert r["state"] == "done", r
    return cs_repo


def ask(core, repo, q):
    return core.ask(str(repo), q, use_llm=False)["result"]
