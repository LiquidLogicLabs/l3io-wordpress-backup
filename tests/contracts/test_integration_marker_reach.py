"""Every test that needs a database is marked ``integration``, and none skips in CI.

``ci.yml`` splits the suite by the marker: the database legs (every Python x
every engine) run ``-m integration``, the no-database legs (every Python) run
``-m "not integration"``. The two expressions are complements, so the union is
the whole suite -- PROVIDED the marker is right. A test that needs the database
but is unmarked skips in the no-database legs and is deselected in the database
legs: it runs nowhere, and every leg is green.

"Needs a database" is derived, never listed (``tests/db_reach.py``):

* the skip differential -- the suite is collected twice, without and with
  ``DB_HOST``; a test that is skipped (or not collected at all) only without it
  is gated on the database, however the gate is spelled;
* the environment reads -- the test function, every fixture in its closure and
  every test-tree function they call, read ``DB_HOST``/``DB_PASSWORD`` as a
  requirement (``live_account``, ``live_restore.host``, any new fixture);
* the client imports -- its module imports, at module level, a database client
  that some database-reading function imports (``pymysql``).

The second half is ``tests/conftest.py``'s ``pytest_runtest_makereport``: with
``CI`` set an ``integration`` test that skips fails, so a database leg that lost
its database cannot pass by skipping everything.

Each rule is planted against in a throwaway tree so the guard is shown to
reach a fixture name nothing lists and a gate spelled in each shape.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tests.db_reach import DB_ENV, OUT_ENV

ROOT = Path(__file__).resolve().parents[2]

#: One collection: nodeid -> the row ``tests/db_reach.py`` wrote for it.
Rows = dict[str, dict[str, Any]]
#: The derived client modules, then the suite without and with the database.
Suite = tuple[list[str], Rows, Rows]
PYPROJECT = ROOT / "pyproject.toml"


def _env(*, with_db: bool, ci: bool | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in DB_ENV and not k.startswith(("COV_CORE", "PYTEST_XDIST"))
    }
    env.pop("PYTEST_CURRENT_TEST", None)
    if with_db:
        env.update(DB_HOST="127.0.0.1", DB_PASSWORD="reach-probe")
    if ci is not None:
        env.pop("CI", None)
        if ci:
            env["CI"] = "true"
    return env


def _tree_args(tree: Path) -> list[str]:
    """Run pytest over a throwaway tree with this project's configuration."""
    return ["-c", str(PYPROJECT), "--rootdir", str(tree), "--confcutdir", str(tree)]


def _collect(out: Path, *args: str, with_db: bool) -> tuple[list[str], Rows]:
    env = _env(with_db=with_db)
    env[OUT_ENV] = str(out)
    run = subprocess.run(  # noqa: S603 - this interpreter, fixed arguments
        [
            sys.executable,
            *("-m", "pytest", "--collect-only", "-q", "-p", "tests.db_reach"),
            *args,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    data = json.loads(out.read_text(encoding="utf-8"))
    return data["clients"], {row["nodeid"]: row for row in data["items"]}


def _collect_both(tmp: Path, *args: str) -> Suite:
    """Without and with the database environment, side by side."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        bare = pool.submit(_collect, tmp / "without.json", *args, with_db=False)
        full = pool.submit(_collect, tmp / "with.json", *args, with_db=True)
        clients, with_db = full.result()
        return clients, bare.result()[1], with_db


def _needs(without: Rows, with_db: Rows) -> dict[str, list[str]]:
    """Each test that needs the database, with why (a signal per entry)."""
    needs: dict[str, list[str]] = {}
    for nodeid, row in with_db.items():
        why: list[str] = []
        bare = without.get(nodeid)
        if bare is None:
            why.append("skip: not collected without DB_HOST")
        elif bare["skip"] is not None and row["skip"] is None:
            why.append(f"skip: {bare['skip']}")
        reads = sorted(set(row["reads"]) | set(bare["reads"] if bare else ()))
        if reads:
            why.append(f"reads: {', '.join(reads)}")
        if row["clients"]:
            why.append(f"clients: {', '.join(row['clients'])}")
        if why:
            needs[nodeid] = why
    return needs


def _unmarked(without: Rows, with_db: Rows) -> dict[str, list[str]]:
    return {
        nodeid: why
        for nodeid, why in _needs(without, with_db).items()
        if not with_db[nodeid]["integration"]
    }


# ---- the real suite ----------------------------------------------------------


@pytest.fixture(scope="module")
def suite(tmp_path_factory: pytest.TempPathFactory) -> Suite:
    return _collect_both(tmp_path_factory.mktemp("db-reach"), "tests")


def test_the_suite_is_collected(suite: Suite) -> None:
    """Scope: the whole suite, both ways, or every check below passes over nothing."""
    _, without, with_db = suite
    assert len(with_db) > 1000, f"only {len(with_db)} tests collected"
    assert set(without) <= set(with_db)


def test_every_signal_fires_on_the_real_suite(suite: Suite) -> None:
    """Each derivation finds something in the real suite, so none is dead code.

    A signal that matched nothing would let its whole class of test through.
    """
    clients, without, with_db = suite
    kinds = Counter(
        why.split(":")[0]
        for reasons in _needs(without, with_db).values()
        for why in reasons
    )
    assert clients, "no database client derived from the database-reading functions"
    assert kinds["skip"], kinds
    assert kinds["reads"], kinds
    assert kinds["clients"], kinds


def test_every_test_that_needs_a_database_is_marked_integration(suite: Suite) -> None:
    _, without, with_db = suite
    unmarked = _unmarked(without, with_db)
    assert not unmarked, (
        "these tests need the CI database but are not marked `integration`, so "
        "they run in NO leg of ci.yml (skipped without a database, deselected "
        "with one):\n"
        + "\n".join(f"  {n}\n      {'; '.join(w)}" for n, w in sorted(unmarked.items()))
    )


# ---- planted violations ------------------------------------------------------

_PLANT_CONFTEST = """
import os

import pytest

from tests.conftest import (  # noqa: F401 - fixtures, and the CI skip hook
    credential_sentinel,
    live_account,
    pytest_runtest_makereport,
)


@pytest.fixture
def brand_new_database():
    '''A database fixture whose name appears nowhere in the guard.'''
    return os.environ["DB_HOST"]
"""

_PLANT_BODY = """
import os

import pytest


def _host():
    return os.environ["DB_HOST"]


def _optional_host():
    return os.environ.get("DB_HOST", "localhost")


def test_PREFIX_live_account(live_account):
    pass


def test_PREFIX_new_fixture(brand_new_database):
    pass


def test_PREFIX_helper():
    _host()


@pytest.mark.skipif("DB_HOST" not in os.environ, reason="needs the database")
def test_PREFIX_skipif_string():
    pass


@pytest.mark.skipif(not os.environ.get("DB_HOST"), reason="needs the database")
def test_PREFIX_skipif_bool():
    pass


def test_PREFIX_in_test_skip():
    if "DB_HOST" not in os.environ:
        pytest.skip("needs the database")


def test_optional_default_PREFIX():
    '''Runs against the default: not a database need, never reported.'''
    _optional_host()
"""

_PLANT_MODULE_SKIP = """
import os

import pytest

if not os.environ.get("DB_HOST"):
    pytest.skip("needs the database", allow_module_level=True)
MARK


def test_PREFIX_module_skip():
    pass
"""

_PLANT_CLIENT = """
import pytest

pymysql = pytest.importorskip("pymysql")
MARK


def test_PREFIX_client_import():
    pass
"""


@pytest.fixture(scope="module")
def planted(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tree = tmp_path_factory.mktemp("planted")
    (tree / "conftest.py").write_text(textwrap.dedent(_PLANT_CONFTEST))
    mark = "pytestmark = pytest.mark.integration"
    for prefix, header in (("unmarked", ""), ("marked", mark)):
        body = _PLANT_BODY.replace("PREFIX", prefix)
        if header:
            body = body.replace("import pytest\n", f"import pytest\n\n{header}\n", 1)
        (tree / f"test_body_{prefix}.py").write_text(body)
        for name, text in (
            ("module_skip", _PLANT_MODULE_SKIP),
            ("client", _PLANT_CLIENT),
        ):
            (tree / f"test_{name}_{prefix}.py").write_text(
                text.replace("PREFIX", prefix).replace("MARK", header)
            )
    return tree


def test_every_planted_violation_is_caught_and_nothing_else(planted: Path) -> None:
    """Each unmarked plant attacks one signal; its marked twin must pass.

    ``brand_new_database`` attacks the scope (a fixture nothing names),
    ``skipif_bool`` a gate the environment-read scan cannot see, ``helper`` a
    read reached only through a call, ``module_skip`` a module that is not
    collected at all without the database, ``client_import`` a module-level
    ``importorskip`` of a database client.
    """
    args = [*_tree_args(planted), str(planted)]
    _, without, with_db = _collect_both(planted, *args)
    assert len(with_db) == 18, sorted(with_db)
    caught = {n.split("::")[-1] for n in _unmarked(without, with_db)}
    assert caught == {
        "test_unmarked_live_account",
        "test_unmarked_new_fixture",
        "test_unmarked_helper",
        "test_unmarked_skipif_string",
        "test_unmarked_skipif_bool",
        "test_unmarked_in_test_skip",
        "test_unmarked_module_skip",
        "test_unmarked_client_import",
    }


# ---- an integration test never skips in CI -----------------------------------

_SKIP_PLANTS = """
import os

import pytest

from tests.conftest import skip_unsupported_engine
from tests.root_mode import ROOT_MARK, running_as_root


@pytest.mark.integration
def test_integration_skips():
    pytest.skip("the database is missing")


@pytest.mark.integration
@pytest.mark.skipif("DB_HOST" not in os.environ, reason="needs the database")
def test_integration_skipif():
    pass


@pytest.mark.integration
@pytest.mark.mysql_only
def test_integration_engine_gap():
    skip_unsupported_engine("this engine lacks the feature")


@pytest.mark.integration
def test_integration_undeclared_engine_gap():
    skip_unsupported_engine("an engine gap the test does not declare")


@pytest.mark.integration
@getattr(pytest.mark, ROOT_MARK)
@pytest.mark.skipif(not running_as_root(), reason="needs real root")
def test_integration_root_only():
    pass


def test_unit_skips():
    pytest.skip("a unit test may skip")
"""


def _outcomes(tree: Path, *, ci: bool) -> dict[str, str]:
    junit = tree / f"junit-{ci}.xml"
    subprocess.run(  # noqa: S603 - this interpreter, fixed arguments
        [
            *(sys.executable, "-m", "pytest", "-q", f"--junitxml={junit}"),
            *_tree_args(tree),
            str(tree),
        ],
        cwd=ROOT,
        env=_env(with_db=False, ci=ci),
        capture_output=True,
        check=False,
    )
    outcomes: dict[str, str] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):  # noqa: S314 - own file
        kinds = [child.tag for child in case] or ["passed"]
        outcomes[case.attrib["name"]] = next(
            (k for k in kinds if k in ("failure", "error", "skipped")), "passed"
        )
    return outcomes


def test_an_integration_skip_fails_in_ci_and_only_there(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(textwrap.dedent(_PLANT_CONFTEST))
    (tmp_path / "test_skips.py").write_text(_SKIP_PLANTS)
    root_only = "passed" if os.geteuid() == 0 else "skipped"

    assert _outcomes(tmp_path, ci=False) == {
        "test_integration_skips": "skipped",
        "test_integration_skipif": "skipped",
        "test_integration_engine_gap": "skipped",
        "test_integration_undeclared_engine_gap": "skipped",
        "test_integration_root_only": root_only,
        "test_unit_skips": "skipped",
    }
    in_ci = _outcomes(tmp_path, ci=True)
    assert in_ci == {
        "test_integration_skips": "failure",
        # A skipif skips in setup, so the failure is reported as a setup error.
        "test_integration_skipif": "error",
        "test_integration_engine_gap": "skipped",
        # EngineUnsupported without the marker: not declared, so it fails.
        "test_integration_undeclared_engine_gap": "failure",
        "test_integration_root_only": root_only,
        "test_unit_skips": "skipped",
    }
