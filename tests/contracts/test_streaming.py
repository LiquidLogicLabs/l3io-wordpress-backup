"""AD-21 — Archives stream; nothing buffers a whole Archive.

The rule: every port that moves Archive bytes exposes a chunked or file-object
interface, never ``bytes``, and peak RSS is bounded by a constant independent
of Archive size — asserted against a fixture an order of magnitude larger than
that bound.

No such port exists yet. What this file establishes now is the **instrument**,
because an RSS assertion that cannot tell a streaming copy from a whole-file
read is worse than no assertion: it reports success over everything. So the
measurement is calibrated in both directions — a chunked copy stays under the
bound, and a deliberate whole-file read breaches it.

``test_the_real_streaming_bound_is_still_owed`` fails the moment any module
under ``adapters/`` (walked recursively, whatever its name) gains the
*capability* to move Archive bytes, so this placeholder cannot outlive the
thing it is standing in for. Detected from the AST, keyed on what the code does
rather than what it is called:

- it imports ``tarfile`` or ``gzip`` (``import``, ``import ... as``,
  ``from ... import``, or a submodule of either); or
- it defines a function or method named like a member of the
  ``ArchiveStoreCommand`` port (derived from the Protocol, so ``stage`` today),
  i.e. it structurally provides the Archive store command.

Not covered: either library reached through ``importlib``/``__import__``.
(It used to fire on the first ``ports/`` module; E002-S01-001 added the ports,
which declare file-object interfaces but move no bytes. E002-S01-010 replaces
it with the RSS bound.) The ``bytes``-free port signatures are asserted
separately, by ``test_port_types.py``.
"""

from __future__ import annotations

import ast
import inspect
import resource
import shutil
from pathlib import Path

import pytest

from l3io.wp.backup.ports.archive import ArchiveStoreCommand

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PORTS = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup" / "ports"
ADAPTERS = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup" / "adapters"

#: Libraries whose import gives an adapter the means to write an Archive.
ARCHIVE_LIBRARIES = frozenset({"tarfile", "gzip"})
#: The Archive store command's members, derived from the Protocol itself.
STORE_COMMAND_MEMBERS = frozenset(
    name
    for name, member in vars(ArchiveStoreCommand).items()
    if inspect.isfunction(member) and not name.startswith("_")
)

MIB = 1024 * 1024
FIXTURE_BYTES = 64 * MIB
CHUNK_BYTES = 256 * 1024

#: The bound a chunked copy must stay under, independent of fixture size.
RSS_BOUND_BYTES = 24 * MIB
#: What a whole-file read must exceed, or the instrument is not measuring.
RSS_TEETH_BYTES = 48 * MIB


def test_the_ports_directory_exists_and_has_modules() -> None:
    """Scope assertion for everything below that reads PORTS.

    PORTS is built from separate quoted path segments --
    ``SRC / "l3io" / "wp" / "backup" / "ports"`` -- which a search-and-replace over
    ``l3io/wp`` cannot reach. A namespace move would leave this pointing at a
    directory that does not exist, and a check over an absent directory finds
    nothing to complain about and passes.

    py-wpconfigr hit exactly this during the distribution rename: their equivalent
    constant silently pointed nowhere, ten parametrised tests collapsed to an empty
    set, and pytest SKIPPED them. The suite stayed green and the collected-test
    count was the only signal.
    """
    assert PORTS.is_dir(), f"AD-21 scope missing: {PORTS} does not exist"
    assert list(PORTS.glob("*.py")), f"AD-21 scope empty: no modules under {PORTS}"


def _peak_rss_bytes() -> int:
    """Peak RSS of this process. ``ru_maxrss`` is KiB on Linux, bytes on macOS."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if peak > (1 << 32) else peak * 1024


def _fixture(path: Path) -> Path:
    block = b"\xa5" * MIB
    with path.open("wb") as handle:
        for _ in range(FIXTURE_BYTES // MIB):
            handle.write(block)
    return path


def test_a_chunked_copy_stays_under_the_bound(tmp_path: Path) -> None:
    source = _fixture(tmp_path / "fixture.bin")
    before = _peak_rss_bytes()

    with source.open("rb") as src, (tmp_path / "copy.bin").open("wb") as dst:
        shutil.copyfileobj(src, dst, CHUNK_BYTES)

    growth = _peak_rss_bytes() - before
    assert growth < RSS_BOUND_BYTES, (
        f"AD-21 — a {FIXTURE_BYTES // MIB} MiB chunked copy grew peak RSS by "
        f"{growth // MIB} MiB, over the {RSS_BOUND_BYTES // MIB} MiB bound."
    )


def test_the_instrument_detects_a_whole_file_read(tmp_path: Path) -> None:
    """The teeth. Without this, the bound above could pass on a broken meter."""
    source = _fixture(tmp_path / "fixture.bin")
    before = _peak_rss_bytes()

    buffered = source.read_bytes()
    assert len(buffered) == FIXTURE_BYTES

    growth = _peak_rss_bytes() - before
    del buffered
    assert growth > RSS_TEETH_BYTES, (
        f"reading {FIXTURE_BYTES // MIB} MiB into memory grew peak RSS by only "
        f"{growth // MIB} MiB — the RSS measurement is not measuring, so the "
        f"bound in this file proves nothing."
    )


def _moves_archive_bytes(module: Path) -> list[str]:
    """Where ``module`` can move Archive bytes, as ``path:line`` strings."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported = [node.module]
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name in STORE_COMMAND_MEMBERS:
                hits.append(f"{module}:{node.lineno}")
            continue
        else:
            continue
        if any(name.split(".")[0] in ARCHIVE_LIBRARIES for name in imported):
            hits.append(f"{module}:{node.lineno}")
    return hits


def _byte_movers(adapters: Path) -> list[str]:
    return [
        hit
        for path in sorted(adapters.rglob("*.py"))
        for hit in _moves_archive_bytes(path)
    ]


def test_the_store_command_members_are_derived() -> None:
    assert "stage" in STORE_COMMAND_MEMBERS


@pytest.mark.parametrize(
    "body",
    [
        "import tarfile\n",
        "import gzip as gz\n",
        "import os, tarfile\n",
        "from tarfile import TarFile\n",
        "from gzip import GzipFile as G\n",
        "def f():\n    import tarfile\n",
        "class Store:\n    def stage(self, target): ...\n",
        "class Store:\n    async def stage(self, target): ...\n",
    ],
)
def test_the_tripwire_sees_a_byte_mover_in_any_adapter_module(
    tmp_path: Path, body: str
) -> None:
    """Scope attack: a module not named ``tar.py``, nested with no ``__init__``."""
    adapters = tmp_path / "adapters"
    (adapters / "nested").mkdir(parents=True)
    (adapters / "database.py").write_text("import json\n", encoding="utf-8")
    (adapters / "nested" / "archive.py").write_text(body, encoding="utf-8")
    hits = _byte_movers(adapters)
    assert hits, body
    assert all("nested/archive.py:" in hit for hit in hits)


@pytest.mark.parametrize(
    "body",
    [
        "import json\n",
        "from . import tarfile\n",
        "def stage_files(): ...\n",
        "def write_archives(): ...\n",
    ],
)
def test_the_tripwire_ignores_unrelated_code(tmp_path: Path, body: str) -> None:
    (tmp_path / "archive.py").write_text(body, encoding="utf-8")
    assert _byte_movers(tmp_path) == []


def test_the_real_streaming_bound_is_still_owed() -> None:
    """Tripwire. AD-21 binds the byte-moving code, not ``shutil``.

    Delete this test when an adapter can move Archive bytes (E002-S01-010),
    and replace this file with one that drives the Archive writer against a
    fixture Archive an order of magnitude larger than the bound.
    """
    assert ADAPTERS.is_dir(), f"AD-21 scope missing: {ADAPTERS}"
    assert list(ADAPTERS.rglob("*.py")), f"AD-21 scope empty: {ADAPTERS}"
    hits = _byte_movers(ADAPTERS)
    assert not hits, (
        f"AD-21 — an adapter can now move Archive bytes ({hits}) but this file "
        "still only measures shutil. Write the RSS bound against the Archive writer."
    )
