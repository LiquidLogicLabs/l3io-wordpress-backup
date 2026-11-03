"""AD-11 — an Archive is untrusted input.

The rule: one extraction function in ``adapters/``, used by every restore
path, passing ``filter='data'`` explicitly wherever the interpreter supports
it and refusing outright where it does not. Containment is re-checked after
member-path rewriting.

That adapter does not exist yet, so this file cannot test it. What it can and
does test today is the **mechanism AD-11 mandates**, on every Python in the CI
matrix: that ``filter='data'`` actually refuses a traversing member here, and
that the refusal is not an artefact of the fixture being harmless — the same
archive is shown to escape without the filter.

``test_the_real_containment_test_is_still_owed`` fails the moment any module
under ``adapters/`` (walked recursively, whatever its name) gains the
*capability* to extract an Archive, so this placeholder cannot outlive the
thing it is standing in for. The capability is detected from the AST, keyed on
what the code does rather than what it is called:

- a call to a ``.extract``, ``.extractall`` or ``.extractfile`` attribute; or
- a tar opened for reading: in a module that imports ``tarfile`` (any import
  spelling, aliases followed), a call to ``tarfile.open``, ``TarFile(...)``,
  ``TarFile.open`` or ``open`` imported from ``tarfile``, whose mode is not a
  literal write mode (``w``/``x``/``a``). An absent mode is ``"r"``; a
  non-literal mode counts as reading.

**One stated exemption.** :data:`STREAM_READERS` names the functions allowed
to *read* a tar as a stream without extracting it: E002-S01-010's publish-gate
``structural_check`` in ``adapters/tar.py`` (and its helper ``_check``), which
reads the staged Archive's two members through ``extractfile`` and writes
nothing to the filesystem, so containment does not arise. The exemption covers
read-mode opens and ``extractfile`` only — ``extract``/``extractall`` inside an
exempted function still fire — and it is keyed by module *and* function, so the
same code anywhere else fires. Every exempted name must still exist: when
E002-S01-003 deletes ``structural_check``, the stale entry fails CI and this
file is due its real containment test.

Not covered: a tar reached through ``importlib``/``__import__`` or a re-export
from another module. (It used to fire on the first adapter module;
E002-S01-001's ``adapters/database.py`` extracts nothing. E002-S01-003 replaces
it with the real containment test.)
"""

from __future__ import annotations

import ast
import io
import tarfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup" / "adapters"

EXTRACT_CALLS = frozenset({"extract", "extractall", "extractfile"})
#: Calls no exemption covers: these write members to the filesystem.
FILESYSTEM_EXTRACT_CALLS = frozenset({"extract", "extractall"})
#: Functions, by module path under ``adapters/``, that may read a tar as a
#: stream (see the module docstring). Asserted to exist.
STREAM_READERS: dict[str, frozenset[str]] = {
    "tar.py": frozenset({"structural_check", "_check"}),
}
WRITE_MODES = ("w", "x", "a")

ESCAPING_MEMBER = "../escape.txt"
PAYLOAD = b"this must never be written outside the destination\n"


def _archive_with_a_traversing_member(path: Path) -> Path:
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo(ESCAPING_MEMBER)
        info.size = len(PAYLOAD)
        archive.addfile(info, io.BytesIO(PAYLOAD))
    return path


def test_the_interpreter_provides_the_data_filter() -> None:
    """AD-11's preferred path, asserted per interpreter rather than assumed.

    ``filter=`` is only patch-level-dependent on 3.11 via backport, which is
    why AD-11 guards it with ``hasattr`` instead of passing it unconditionally.
    If this ever fails on a matrix leg, that leg must take the refusal path —
    it is not licence to fall back to ``fully_trusted``.
    """
    assert hasattr(tarfile, "data_filter")


def test_the_data_filter_refuses_a_traversing_member(tmp_path: Path) -> None:
    archive_path = _archive_with_a_traversing_member(tmp_path / "evil.tar")
    destination = tmp_path / "destination"
    destination.mkdir()

    with tarfile.open(archive_path) as archive, pytest.raises(tarfile.FilterError):
        archive.extractall(destination, filter="data")

    assert not (tmp_path / "escape.txt").exists()


def test_the_fixture_really_does_escape_without_the_filter(tmp_path: Path) -> None:
    """The teeth of the test above: prove the archive is genuinely hostile."""
    archive_path = _archive_with_a_traversing_member(tmp_path / "evil.tar")
    destination = tmp_path / "destination"
    destination.mkdir()

    with tarfile.open(archive_path) as archive:
        # Deliberately unsafe: this is what AD-11 forbids, called here only
        # to prove the fixture above is genuinely hostile.
        archive.extractall(destination, filter="fully_trusted")  # noqa: S202

    escaped = tmp_path / "escape.txt"
    assert escaped.is_file(), (
        "the fixture did not traverse, so the refusal above proved nothing"
    )
    assert escaped.read_bytes() == PAYLOAD


def _tarfile_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Local names bound to the ``tarfile`` module, and to its openers."""
    modules: set[str] = set()
    openers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "tarfile"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "tarfile":
            openers.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in {"open", "TarFile"}
            )
    return modules, openers


def _reads_a_tar(call: ast.Call, modules: set[str], openers: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        opens = func.id in openers
    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        receiver = func.value.id
        opens = (receiver in modules and func.attr in {"open", "TarFile"}) or (
            receiver in openers and func.attr == "open"
        )
    else:
        opens = False
    if not opens:
        return False
    mode = next((k.value for k in call.keywords if k.arg == "mode"), None)
    if mode is None and len(call.args) > 1:
        mode = call.args[1]
    if mode is None:
        return True  # tarfile's default mode is "r"
    return not (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and mode.value.startswith(WRITE_MODES)
    )


def _exempt_lines(tree: ast.Module, names: frozenset[str]) -> set[int]:
    """The lines of the module-level functions named in ``names``."""
    return {
        line
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }


def _extracts(module: Path, exempt: frozenset[str] = frozenset()) -> list[str]:
    """Where ``module`` can extract an Archive, as ``path:line`` strings."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    modules, openers = _tarfile_names(tree)
    exempt_lines = _exempt_lines(tree, exempt)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        attr = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if attr in FILESYSTEM_EXTRACT_CALLS or (
            (attr in EXTRACT_CALLS or _reads_a_tar(node, modules, openers))
            and node.lineno not in exempt_lines
        ):
            hits.append(f"{module}:{node.lineno}")
    return hits


def _extractors(
    adapters: Path, readers: dict[str, frozenset[str]] | None = None
) -> list[str]:
    readers = STREAM_READERS if readers is None else readers
    return [
        hit
        for path in sorted(adapters.rglob("*.py"))
        for hit in _extracts(
            path, readers.get(path.relative_to(adapters).as_posix(), frozenset())
        )
    ]


def test_every_stream_reader_exemption_names_a_function_that_exists() -> None:
    """A stale exemption fails, so it cannot outlive what it exempts."""
    for relative, names in STREAM_READERS.items():
        module = ADAPTERS / relative
        assert module.is_file(), f"exempted module {module} does not exist"
        tree = ast.parse(module.read_text(encoding="utf-8"))
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        assert names <= defined, f"stale exemption: {sorted(names - defined)}"


@pytest.mark.parametrize(
    ("relative", "body"),
    [
        # The exempted name, in a module the exemption does not name.
        ("nested/tar.py", "def structural_check(a):\n    a.extractfile('m')\n"),
        ("other.py", "import tarfile\ndef _check():\n    tarfile.open('x')\n"),
        # Filesystem extraction inside an exempted function.
        ("tar.py", "def structural_check(a):\n    a.extractall('d')\n"),
        ("tar.py", "def _check(a):\n    a.extract('m')\n"),
        # A reader in the exempted module, outside the exempted functions.
        ("tar.py", "import tarfile\ndef other():\n    tarfile.open('x')\n"),
    ],
)
def test_the_exemption_does_not_reach_beyond_its_functions(
    tmp_path: Path, relative: str, body: str
) -> None:
    """Scope attack on the exemption itself."""
    adapters = tmp_path / "adapters"
    (adapters / relative).parent.mkdir(parents=True, exist_ok=True)
    (adapters / relative).write_text(body, encoding="utf-8")
    assert _extractors(adapters), body


def test_the_exemption_covers_a_stream_reader(tmp_path: Path) -> None:
    adapters = tmp_path / "adapters"
    adapters.mkdir()
    (adapters / "tar.py").write_text(
        "import tarfile\n"
        "def structural_check(p):\n    tarfile.open(p, mode='r|')\n"
        "def _check(o):\n    o.extractfile(o.next())\n",
        encoding="utf-8",
    )
    assert _extractors(adapters) == []
    assert _extractors(adapters, readers={}) != []


@pytest.mark.parametrize(
    "body",
    [
        "def f(a):\n    a.extractall('d', filter='data')\n",
        "async def f(a):\n    await a.extract('m')\n",
        "class R:\n    def f(self, a):\n        return a.extractfile('m')\n",
        "import tarfile\ntarfile.open('x.tar')\n",
        "import tarfile as t\nt.open('x.tar', 'r:gz')\n",
        "from tarfile import open as topen\ntopen(name='x', mode='r|*')\n",
        "from tarfile import TarFile\nTarFile('x.tar')\n",
        "from tarfile import TarFile as T\nT.open(fileobj=f, mode=m)\n",
    ],
)
def test_the_tripwire_sees_extraction_in_any_adapter_module(
    tmp_path: Path, body: str
) -> None:
    """Scope attack: a module not named ``tar.py``, nested with no ``__init__``."""
    adapters = tmp_path / "adapters"
    (adapters / "nested").mkdir(parents=True)
    (adapters / "database.py").write_text("import json\n", encoding="utf-8")
    (adapters / "nested" / "archive.py").write_text(body, encoding="utf-8")
    hits = _extractors(adapters)
    assert hits, body
    assert all("nested/archive.py:" in hit for hit in hits)


@pytest.mark.parametrize(
    "body",
    [
        "import tarfile\ntarfile.open('x.tar', 'w:gz')\n",
        "import tarfile\ntarfile.open(name='x', mode='x')\n",
        "from tarfile import TarFile\nTarFile('x.tar', mode='a')\n",
        "from pathlib import Path\nPath('x').open('rb')\n",
        "def extract_archives(): ...\n",
    ],
)
def test_the_tripwire_ignores_a_writer_and_unrelated_opens(
    tmp_path: Path, body: str
) -> None:
    (tmp_path / "archive.py").write_text(body, encoding="utf-8")
    assert _extractors(tmp_path) == []


def test_the_real_containment_test_is_still_owed() -> None:
    """Tripwire. AD-11 binds the adapter, not the stdlib.

    Delete this test when an adapter can extract an Archive (E002-S01-003),
    and replace this file with one that drives that adapter: containment
    re-checked after member-path rewriting, against the enumerated member roles
    of AD-6.
    """
    assert ADAPTERS.is_dir(), f"AD-11 scope missing: {ADAPTERS}"
    assert list(ADAPTERS.rglob("*.py")), f"AD-11 scope empty: {ADAPTERS}"
    hits = _extractors(ADAPTERS)
    assert not hits, (
        f"AD-11 — an adapter can now extract an Archive ({hits}) but this file "
        "still only tests the stdlib. Write the containment test against it."
    )
