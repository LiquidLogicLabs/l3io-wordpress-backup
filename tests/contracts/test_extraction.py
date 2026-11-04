"""AD-11 — an Archive is untrusted input.

The rule: one extraction function in ``adapters/``, used by every restore
path, filtering every member through ``tarfile.data_filter`` and refusing
outright where the interpreter has none. Containment is re-checked after
member-path rewriting.

**One extraction function.** :func:`_extractors` walks every module under
``adapters/`` (recursively, whatever its name) and fails on any call to an
``.extract`` or ``.extractall`` attribute outside :data:`EXTRACTOR` —
``extract_archive`` in ``adapters/tar.py``, asserted to exist. Reading a tar
as a stream (the probe, ``read_member``, ``decrypt_payload``) writes no member
to the filesystem and is not extraction. Not covered: an extraction reached
through ``importlib``/``__import__`` or ``getattr``.

**The containment tests** drive the real ``extract_archive``, on every Python
in the CI matrix: a traversing member refused; a symlink with an absolute or
escaping target skipped and reported (ADR-0017 amendment); a hard link
refused wherever it points, including at an in-destination file that
``data_filter`` alone allows (2026-11-03 amendment); containment re-checked
after the role prefix is stripped; the refusal path when ``data_filter`` is
absent, and when ``os.path.ALLOW_MISSING`` is (an interpreter without the
June 2025 tarfile CVE fixes, BL-E002-059; both states simulated); and
``exclude``. The stdlib mechanism tests below them keep proving
that ``filter='data'`` itself refuses, and that the fixture really escapes
without it.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import io
import os
import tarfile
import typing
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest
import structlog

from l3io.wp.backup.adapters.tar import extract_archive
from l3io.wp.backup.domain.archive_format import (
    MANIFEST_MEMBER,
    PAYLOAD_MEMBER,
    ArchiveView,
    Integrity,
    ManifestPayload,
    MemberKind,
    MemberRole,
    ViewMember,
)
from l3io.wp.backup.domain.errors import ExtractionRefusedError
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.site_files import SiteFilesCommand

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS = PROJECT_ROOT / "src" / "l3io" / "wp" / "backup" / "adapters"

#: Calls that write members to the filesystem.
FILESYSTEM_EXTRACT_CALLS = frozenset({"extract", "extractall"})
#: The one function allowed to make them: module path under ``adapters/``,
#: function name. Asserted to exist.
EXTRACTOR = ("tar.py", "extract_archive")

ESCAPING_MEMBER = "../escape.txt"
PAYLOAD = b"this must never be written outside the destination\n"
CONFIG = b"<?php // the kept config\n"
SITE = MemberRole.SITE_ROOT


# --------------------------------------------------------------------------
# One extraction function
# --------------------------------------------------------------------------


def _function_lines(tree: ast.Module, name: str) -> set[int]:
    return {
        line
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }


def _site_files_fields() -> frozenset[str]:
    """The ``AdapterSet`` fields typed ``SiteFilesCommand``, from its type hints.

    A call made directly on one (``adapters.site_files_command.extract(...)``)
    is the port, whose real implementation delegates to :data:`EXTRACTOR`
    unchanged (``ports/site_files.py``); it is not a second extraction. Only
    that spelling is exempt: an alias (``c = adapters.site_files_command;
    c.extract(...)``) is still reported, so the exemption cannot hide a tar.
    """
    hints = typing.get_type_hints(AdapterSet)
    fields = frozenset(
        f.name
        for f in dataclasses.fields(AdapterSet)
        if hints[f.name] is SiteFilesCommand
    )
    assert fields, "AdapterSet holds no SiteFilesCommand field"
    return fields


def _extracts(module: Path, allowed: str | None) -> list[str]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    allowed_lines = set() if allowed is None else _function_lines(tree, allowed)
    ports = _site_files_fields()
    return [
        f"{module}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in FILESYSTEM_EXTRACT_CALLS
        and node.lineno not in allowed_lines
        and not (
            node.func.attr == "extract"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr in ports
        )
    ]


def _extractors(adapters: Path) -> list[str]:
    module, function = EXTRACTOR
    return [
        hit
        for path in sorted(adapters.rglob("*.py"))
        for hit in _extracts(
            path, function if path.relative_to(adapters).as_posix() == module else None
        )
    ]


def test_the_extractor_exists() -> None:
    module, function = EXTRACTOR
    tree = ast.parse((ADAPTERS / module).read_text(encoding="utf-8"))
    assert _function_lines(tree, function), f"stale: {module}::{function}"
    assert _extracts(ADAPTERS / module, None), f"{function} extracts nothing"


def test_only_the_extractor_extracts() -> None:
    assert ADAPTERS.is_dir(), f"AD-11 scope missing: {ADAPTERS}"
    assert list(ADAPTERS.rglob("*.py")), f"AD-11 scope empty: {ADAPTERS}"
    hits = _extractors(ADAPTERS)
    assert not hits, f"AD-11 — extraction outside {EXTRACTOR}: {hits}"


@pytest.mark.parametrize(
    ("relative", "body"),
    [
        ("nested/archive.py", "def f(a):\n    a.extractall('d', filter='data')\n"),
        ("other.py", "async def f(a):\n    await a.extract('m')\n"),
        ("nested/tar.py", "def extract_archive(a):\n    a.extractall('d')\n"),
        ("tar.py", "def other(a):\n    a.extract('m')\n"),
        ("tar.py", "class R:\n    def f(self, a):\n        a.extractall('d')\n"),
        # The port exemption is the direct ``.extract`` spelling only.
        ("runner.py", "def f(a):\n    c = a.site_files_command\n    c.extract('m')\n"),
        ("runner.py", "def f(a):\n    a.site_files_command.extractall('d')\n"),
        ("runner.py", "def f(a):\n    a.site_files.extract('m')\n"),
    ],
)
def test_the_guard_sees_extraction_anywhere_else(
    tmp_path: Path, relative: str, body: str
) -> None:
    """Scope attack: nested, no ``__init__``, the sanctioned name elsewhere."""
    adapters = tmp_path / "adapters"
    (adapters / relative).parent.mkdir(parents=True, exist_ok=True)
    (adapters / "tar.py").write_text(
        "def extract_archive(t):\n    t.extractall('d')\n", encoding="utf-8"
    )
    if relative == "tar.py":
        with (adapters / relative).open("a", encoding="utf-8") as handle:
            handle.write(body)
    else:
        (adapters / relative).write_text(body, encoding="utf-8")
    hits = _extractors(adapters)
    assert hits, body
    assert all(f"{relative}:" in hit for hit in hits), hits


def test_the_guard_allows_the_site_files_port(tmp_path: Path) -> None:
    (tmp_path / "runner.py").write_text(
        "def f(a):\n    a.site_files_command.extract('m')\n", encoding="utf-8"
    )
    assert _extractors(tmp_path) == []


def test_the_guard_ignores_stream_reads(tmp_path: Path) -> None:
    (tmp_path / "probe.py").write_text(
        "import tarfile\n"
        "def f(p):\n    with tarfile.open(p, 'r|') as t:\n"
        "        t.extractfile(t.next())\n",
        encoding="utf-8",
    )
    assert _extractors(tmp_path) == []


# --------------------------------------------------------------------------
# extract_archive
# --------------------------------------------------------------------------


@dataclass
class Entry:
    """One inner member: its header and, for a file, its bytes."""

    info: tarfile.TarInfo
    data: bytes = b""


def _file(name: str, data: bytes) -> Entry:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    return Entry(info, data)


def _link(name: str, target: str, kind: bytes) -> Entry:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return Entry(info)


def _special(name: str, kind: bytes) -> Entry:
    info = tarfile.TarInfo(name)
    info.type = kind
    return Entry(info)


def _view_kind(info: tarfile.TarInfo) -> MemberKind:
    if info.issym():
        return MemberKind.SYMLINK
    if info.isdir():
        return MemberKind.DIR
    return MemberKind.FILE


def _role(name: str) -> MemberRole:
    return next(r for r in MemberRole if name.split("/", 1)[0] == r.value)


def _v1(tmp_path: Path, entries: list[Entry]) -> tuple[StorageTarget, ArchiveView]:
    """A v1 Archive holding ``entries``, and the view its index would give."""
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w:gz") as inner:
        for entry in entries:
            inner.addfile(entry.info, io.BytesIO(entry.data) if entry.data else None)
    payload = plain.getvalue()
    path = tmp_path / "archive.tar.gz"
    with tarfile.open(path, mode="w") as outer:
        for name, data in ((MANIFEST_MEMBER, b"{}"), (PAYLOAD_MEMBER, payload)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            outer.addfile(info, io.BytesIO(data))
    view = ArchiveView(
        format_version=1,
        members=tuple(
            ViewMember(
                path=e.info.name,
                role=_role(e.info.name),
                kind=_view_kind(e.info),
                link_target=e.info.linkname if e.info.issym() else None,
                size=e.info.size if e.info.isreg() else 0,
            )
            for e in entries
        ),
        dump_present=False,
        config_path=None,
        integrity=Integrity.VERIFIED,
        encrypted=False,
        payload=ManifestPayload(
            name=PAYLOAD_MEMBER,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        ),
        backup_scope="files",
    )
    return StorageTarget(scheme="file", path=PurePosixPath(path)), view


@pytest.fixture
def destination(tmp_path: Path) -> Path:
    path = tmp_path / "out" / "destination"
    path.mkdir(parents=True)
    return path


def _tree(root: Path) -> dict[str, bytes | str | None]:
    found: dict[str, bytes | str | None] = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[key] = f"-> {path.readlink()}"
        elif path.is_file():
            found[key] = path.read_bytes()
        else:
            found[key] = None
    return found


def test_a_good_archive_extracts_below_its_role_prefix(
    tmp_path: Path, destination: Path
) -> None:
    source, view = _v1(
        tmp_path,
        [
            _file("dump/database.sql", b"SQL"),
            _special("site-root", tarfile.DIRTYPE),
            _file("site-root/index.php", b"<?php"),
            _link("site-root/link", "index.php", tarfile.SYMTYPE),
        ],
    )
    result = extract_archive(source, destination, view, roles=frozenset({SITE}))
    assert _tree(destination) == {"index.php": b"<?php", "link": "-> index.php"}
    assert (result.members, result.bytes, result.skipped_links) == (2, 5, ())


def _directory_fixup_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``extractall`` re-apply the filter to each directory afterwards.

    Patched interpreters (3.11.13+, 3.12.11+, 3.13.4+) do this themselves, with
    the same unfiltered ``TarInfo``; this simulates it on any interpreter, so
    the filter is proven idempotent for it everywhere.
    """
    original = tarfile.TarFile.extractall

    def extractall(
        self: tarfile.TarFile,
        path: str | os.PathLike[str] = ".",
        members: object = None,
        *,
        numeric_owner: bool = False,
        filter: Callable[[tarfile.TarInfo, str], tarfile.TarInfo | None],  # noqa: A002
    ) -> None:
        accepted: list[tarfile.TarInfo] = []

        def recording(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
            result = filter(member, dest)
            if result is not None and result.isdir():
                accepted.append(member)
            return result

        original(self, path, numeric_owner=numeric_owner, filter=recording)
        for member in accepted:
            filter(member, str(path))

    monkeypatch.setattr(tarfile.TarFile, "extractall", extractall)


def test_the_stdlib_directory_fixup_pass_is_not_a_duplicate(
    tmp_path: Path, destination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _directory_fixup_pass(monkeypatch)
    source, view = _v1(
        tmp_path,
        [
            _special("site-root", tarfile.DIRTYPE),
            _special("site-root/wp-content", tarfile.DIRTYPE),
            _special("site-root/wp-content/uploads", tarfile.DIRTYPE),
            _file("site-root/wp-content/uploads/a.bin", b"a"),
        ],
    )
    result = extract_archive(source, destination, view, roles=frozenset({SITE}))
    assert _tree(destination) == {
        "wp-content": None,
        "wp-content/uploads": None,
        "wp-content/uploads/a.bin": b"a",
    }
    assert result.members == 3


def test_a_directory_named_twice_is_still_refused(
    tmp_path: Path, destination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second ``TarInfo`` of the same name is a duplicate, fix-up pass or not."""
    _directory_fixup_pass(monkeypatch)
    source, view = _v1(
        tmp_path,
        [
            _special("site-root/d", tarfile.DIRTYPE),
            _special("site-root/d", tarfile.DIRTYPE),
        ],
    )
    view = dataclasses.replace(view, members=view.members[:1])
    with pytest.raises(ExtractionRefusedError, match="twice"):
        extract_archive(source, destination, view, roles=frozenset({SITE}))


def test_a_traversing_member_is_refused(tmp_path: Path, destination: Path) -> None:
    source, view = _v1(tmp_path, [_file(f"site-root/{ESCAPING_MEMBER}", PAYLOAD)])
    with pytest.raises(ExtractionRefusedError):
        extract_archive(source, destination, view, roles=frozenset({SITE}))
    assert not list((tmp_path / "out").rglob("escape.txt"))
    assert not (tmp_path / "escape.txt").exists()


def test_a_member_outside_the_index_is_refused(
    tmp_path: Path, destination: Path
) -> None:
    source, view = _v1(
        tmp_path, [_file("site-root/a", b"a"), _file("site-root/b", b"b")]
    )
    view = dataclasses.replace(view, members=view.members[:1])
    with pytest.raises(ExtractionRefusedError, match="member index"):
        extract_archive(source, destination, view, roles=frozenset({SITE}))


@pytest.mark.parametrize(
    ("target", "reason"),
    [("/etc/passwd", "absolute"), ("../../outside", "outside_destination")],
)
def test_an_unsupported_symlink_is_skipped_and_reported(
    tmp_path: Path, destination: Path, target: str, reason: str
) -> None:
    source, view = _v1(
        tmp_path,
        [
            _file("site-root/before.php", b"1"),
            _link("site-root/evil", target, tarfile.SYMTYPE),
            _file("site-root/after.php", b"2"),
        ],
    )
    with structlog.testing.capture_logs() as logs:
        result = extract_archive(source, destination, view, roles=frozenset({SITE}))
    assert _tree(destination) == {"after.php": b"2", "before.php": b"1"}
    assert [(s.path, s.link_target, s.reason) for s in result.skipped_links] == [
        ("evil", target, reason)
    ]
    warnings = [e for e in logs if e["event"] == "symlink_skipped"]
    assert warnings == [
        {
            "event": "symlink_skipped",
            "log_level": "warning",
            "path": repr("evil"),
            "link_target": repr(target),
            "reason": reason,
        }
    ]
    assert result.members == 2


@pytest.mark.parametrize(
    "target", ["/etc/passwd", "../../outside", "site-root/wp-config.php"]
)
def test_a_hard_link_is_refused_wherever_it_points(
    tmp_path: Path, destination: Path, target: str
) -> None:
    source, view = _v1(
        tmp_path,
        [
            _link("site-root/x", target, tarfile.LNKTYPE),
            _file("site-root/x", b"overwritten through the link"),
        ],
    )
    with pytest.raises(ExtractionRefusedError, match="hard link"):
        extract_archive(source, destination, view, roles=frozenset({SITE}))
    assert _tree(destination) == {}


def test_a_hard_link_to_the_kept_config_leaves_it_byte_identical(
    tmp_path: Path, destination: Path
) -> None:
    """``x -> wp-config.php``: ``data_filter`` alone allows this one."""
    kept = destination / "wp-config.php"
    kept.write_bytes(CONFIG)
    link = _link("site-root/x", "wp-config.php", tarfile.LNKTYPE)
    assert (
        tarfile.data_filter(link.info.replace(name="x"), str(destination)) is not None
    )
    source, view = _v1(tmp_path, [link, _file("site-root/x", b"<?php evil();")])
    with pytest.raises(ExtractionRefusedError):
        extract_archive(
            source,
            destination,
            view,
            roles=frozenset({SITE}),
            exclude=frozenset({PurePosixPath("wp-config.php")}),
        )
    assert kept.read_bytes() == CONFIG
    assert not (destination / "x").exists()


def test_containment_is_rechecked_after_the_prefix_is_stripped(
    tmp_path: Path, destination: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (destination / "uploads").symlink_to(outside)
    source, view = _v1(tmp_path, [_file("site-root/uploads/x.php", PAYLOAD)])
    with pytest.raises(ExtractionRefusedError):
        extract_archive(source, destination, view, roles=frozenset({SITE}))
    assert list(outside.iterdir()) == []


def test_a_role_not_asked_for_is_not_written(tmp_path: Path, destination: Path) -> None:
    source, view = _v1(
        tmp_path,
        [_file("dump/database.sql", b"SQL"), _file("config/wp-config.php", CONFIG)],
    )
    result = extract_archive(
        source, destination, view, roles=frozenset({MemberRole.CONFIG})
    )
    assert _tree(destination) == {"wp-config.php": CONFIG}
    assert result.members == 1


def test_no_data_filter_takes_the_refusal_path(
    tmp_path: Path, destination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, view = _v1(tmp_path, [_file("site-root/index.php", b"<?php")])
    monkeypatch.delattr(tarfile, "data_filter")
    with pytest.raises(ExtractionRefusedError, match="data filter"):
        extract_archive(source, destination, view, roles=frozenset({SITE}))
    assert _tree(destination) == {}


def test_no_allow_missing_takes_the_refusal_path(
    tmp_path: Path, destination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-E002-059: no ``os.path.ALLOW_MISSING`` is an unpatched tarfile; refused.

    Simulated, so it runs on any interpreter: the attribute is removed (and
    ``raising=False`` lets it run on one that never had it).
    """
    source, view = _v1(tmp_path, [_file("site-root/index.php", b"<?php")])
    monkeypatch.delattr(os.path, "ALLOW_MISSING", raising=False)
    with pytest.raises(ExtractionRefusedError, match="CVE-2025-4517") as caught:
        extract_archive(source, destination, view, roles=frozenset({SITE}))
    assert "upgrade Python" in str(caught.value)
    assert _tree(destination) == {}


def test_with_allow_missing_present_extraction_proceeds(
    tmp_path: Path, destination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other state, simulated on an unpatched interpreter that lacks it."""
    if not hasattr(os.path, "ALLOW_MISSING"):
        monkeypatch.setattr(os.path, "ALLOW_MISSING", object(), raising=False)
    source, view = _v1(tmp_path, [_file("site-root/index.php", b"<?php")])
    result = extract_archive(source, destination, view, roles=frozenset({SITE}))
    assert _tree(destination) == {"index.php": b"<?php"}
    assert result.members == 1


def test_an_excluded_member_is_never_written(tmp_path: Path, destination: Path) -> None:
    kept = destination / "wp-config.php"
    kept.write_bytes(CONFIG)
    kept.chmod(0o640)
    os.utime(kept, (1_000_000_000, 1_000_000_000))
    before = kept.stat()
    source, view = _v1(
        tmp_path,
        [
            _file("site-root/wp-config.php", b"<?php // from the archive"),
            _file("site-root/index.php", b"<?php"),
        ],
    )
    result = extract_archive(
        source,
        destination,
        view,
        roles=frozenset({SITE}),
        exclude=frozenset({PurePosixPath("wp-config.php")}),
    )
    after = kept.stat()
    assert kept.read_bytes() == CONFIG
    assert (after.st_mode, after.st_mtime_ns, after.st_ino) == (
        before.st_mode,
        before.st_mtime_ns,
        before.st_ino,
    )
    assert (destination / "index.php").read_bytes() == b"<?php"
    assert result.members == 1


def _kept_config(destination: Path) -> Path:
    kept = destination / "wp-config.php"
    kept.write_bytes(CONFIG)
    return kept


def _extract_excluding_config(
    source: StorageTarget, destination: Path, view: ArchiveView
) -> None:
    extract_archive(
        source,
        destination,
        view,
        roles=frozenset({SITE}),
        exclude=frozenset({PurePosixPath("wp-config.php")}),
    )


def test_a_file_below_an_archive_symlink_cannot_reach_the_kept_config(
    tmp_path: Path, destination: Path
) -> None:
    """``l -> .`` (inside, so ``data_filter`` allows it), then ``l/wp-config.php``."""
    kept = _kept_config(destination)
    source, view = _v1(
        tmp_path,
        [
            _link("site-root/l", ".", tarfile.SYMTYPE),
            _file("site-root/l/wp-config.php", b"<?php evil();"),
        ],
    )
    with pytest.raises(ExtractionRefusedError, match="below a symlink"):
        _extract_excluding_config(source, destination, view)
    assert kept.read_bytes() == CONFIG


def test_a_name_twice_cannot_write_through_its_own_symlink(
    tmp_path: Path, destination: Path
) -> None:
    """``foo -> wp-config.php``, then a regular file ``foo``; indexed once."""
    kept = _kept_config(destination)
    source, view = _v1(
        tmp_path,
        [
            _link("site-root/foo", "wp-config.php", tarfile.SYMTYPE),
            _file("site-root/foo", b"<?php evil();"),
        ],
    )
    view = dataclasses.replace(view, members=view.members[:1])
    with pytest.raises(ExtractionRefusedError, match="twice"):
        _extract_excluding_config(source, destination, view)
    assert kept.read_bytes() == CONFIG


def test_a_member_resolving_onto_an_excluded_path_is_refused(
    tmp_path: Path, destination: Path
) -> None:
    """A link already in the destination leads a member onto the kept config."""
    kept = _kept_config(destination)
    (destination / "alias.php").symlink_to("wp-config.php")
    source, view = _v1(tmp_path, [_file("site-root/alias.php", b"<?php evil();")])
    with pytest.raises(ExtractionRefusedError, match="excluded"):
        _extract_excluding_config(source, destination, view)
    assert kept.read_bytes() == CONFIG


def test_a_header_kind_other_than_the_index_is_refused(
    tmp_path: Path, destination: Path
) -> None:
    source, view = _v1(tmp_path, [_link("site-root/x", "y", tarfile.SYMTYPE)])
    as_file = dataclasses.replace(
        view.members[0], kind=MemberKind.FILE, link_target=None
    )
    view = dataclasses.replace(view, members=(as_file,))
    with pytest.raises(ExtractionRefusedError, match="indexed file"):
        extract_archive(source, destination, view, roles=frozenset({SITE}))
    assert _tree(destination) == {}


@pytest.mark.parametrize("entry", ["../wp-config.php", "/wp-config.php", "."])
def test_an_unsafe_exclude_entry_is_a_programming_error(
    tmp_path: Path, destination: Path, entry: str
) -> None:
    source, view = _v1(tmp_path, [_file("site-root/index.php", b"<?php")])
    with pytest.raises(ValueError, match="exclude"):
        extract_archive(
            source,
            destination,
            view,
            roles=frozenset({SITE}),
            exclude=frozenset({PurePosixPath(entry)}),
        )


def test_an_excluded_member_that_is_unsafe_still_refuses(
    tmp_path: Path, destination: Path
) -> None:
    source, view = _v1(
        tmp_path, [_special("site-root/wp-config.php", tarfile.FIFOTYPE)]
    )
    with pytest.raises(ExtractionRefusedError, match="SpecialFileError"):
        extract_archive(
            source,
            destination,
            view,
            roles=frozenset({SITE}),
            exclude=frozenset({PurePosixPath("wp-config.php")}),
        )


# --------------------------------------------------------------------------
# The stdlib mechanism AD-11 relies on
# --------------------------------------------------------------------------


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


def test_the_interpreter_has_the_tarfile_cve_fixes() -> None:
    """BL-E002-059, asserted per interpreter: the dev pin and CI legs are patched.

    A leg failing this extracts nothing (``extract_archive`` refuses); the fix
    is a patched interpreter, never a fallback.
    """
    assert hasattr(os.path, "ALLOW_MISSING")


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
