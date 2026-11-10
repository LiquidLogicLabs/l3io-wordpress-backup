"""FR-6, AD-4, AD-17, ADR-0021/0022/0024, BL-E002-116 — the restore executor.

Driven over :mod:`tests.restore_harness`: every command half records into one
log tagged with the journal phase active at the time; the ``load_database``
slot and ``take_snapshot`` are fakes (``adapters/database_load.py`` is
E003-S02-005's).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters import restore_runner
from l3io.wp.backup.adapters.restore_runner import (
    _CHECK_PHASES,
    _DESTROYING,
    ArchiveChangedError,
    _device,
    _same_dir,
    snapshot_result,
)
from l3io.wp.backup.adapters.site_files import LocalSiteFiles
from l3io.wp.backup.cli.adapter_sets import dry_run_adapters
from l3io.wp.backup.core.restore import (
    CUTOVER_KEYS,
    NO_RELOCATION,
    NonEmptyTargetError,
    RestoreRelocation,
    SnapshotFailedError,
    SnapshotResult,
    TargetChangedError,
    TargetState,
)
from l3io.wp.backup.core.restore_space import (
    FREE_SPACE_MARGIN,
    SNAPSHOT_FILESYSTEM,
    ExpansionRatioError,
    InsufficientSpaceError,
)
from l3io.wp.backup.domain.archive_format import (
    INNER_MANIFEST_MEMBER,
    Integrity,
    MemberRole,
)
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.errors import (
    ArchiveNotFoundError,
    ConfigWriteFailedError,
    DatabaseRefusedError,
    ExitCode,
    ExtractionRefusedError,
    PassphraseRejectedError,
    PayloadDamagedError,
    RunTerminated,
    VerificationFailedError,
)
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    OverrideSet,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
    clears_site_root,
    restores_database,
    restores_files,
)
from l3io.wp.backup.domain.search_replace import SearchReplaceReport
from tests.conftest import InnerMembers, forge_archive, reseal
from tests.load_fakes import LoadHarness
from tests.restore_builders import RELOCATIONS
from tests.restore_harness import (
    ARCHIVE_DB,
    NON_ORDERLY_FAILURE,
    ORDERLY_CHECK_FAILURE,
    TARGET_DB,
    Harness,
    IdentityCipher,
    write_v0,
    write_v1,
)

P = RestorePhase
#: Which command calls belong to which part of the Site.
FILE_CALLS = {"site_files.clean", "site_files.extract"}
DATABASE_CALLS = {"slot.load", "restore_database.drop_database"}


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return write_v1(tmp_path)


def _harness(tmp_path: Path, archive: Path, **knobs: object) -> Harness:
    return Harness(work=tmp_path, archive_path=archive, **knobs)  # type: ignore[arg-type]


def _fits(relocation: RestoreRelocation, mode: RestoreMode) -> RestoreRelocation:
    """``relocation`` as Epic 7's ``validate_relocation`` lets it reach ``mode``."""
    if restores_database(mode):
        return relocation
    return dataclasses.replace(
        relocation,
        search_replace=None,
        undo=tuple(u for u in relocation.undo if u[0] is not P.SEARCH_REPLACE),
    )


# --------------------------------------------------------------------------
# Per Mode: what it touches and what it does not (FR-6)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_each_mode_touches_exactly_its_parts(
    tmp_path: Path, archive: Path, mode: RestoreMode
) -> None:
    h = _harness(tmp_path, archive, mode=mode)
    with capture_logs() as logs:
        report = h.run()
    methods = set(h.log.methods())
    assert bool(methods & FILE_CALLS) is restores_files(mode)
    assert ("site_files.clean" in methods) is clears_site_root(mode)
    assert bool(methods & DATABASE_CALLS) is restores_database(mode)
    # What the executor controls is the Mode it hands the slot (BL-E003-031);
    # whether the real slot then drops the old database is test_staging.py's
    # (E003-S02-005), not this fake slot's own decision checked against itself.
    handed = [
        (phase, detail)
        for phase, port, method, detail in h.log.calls
        if (port, method) == ("slot", "load")
    ]
    expected = [(P.LOAD_DUMP, h.plan().mode)] if restores_database(mode) else []
    assert handed == expected
    assert h.slot_calls == int(restores_database(mode))
    assert not {m for m in methods if m.startswith("database.")}
    # The phases that issued commands are exactly the plan's destructive ones.
    issued = {phase for phase, *_ in h.log.calls}
    assert issued == set(report.phases) - {P.PREFLIGHT, P.VERIFY}
    # One phase_completed per planned phase; the omitted ones emit nothing.
    emitted = [e["phase"] for e in logs if e["event"] == "phase_completed"]
    assert emitted == [p.name for p in report.phases]
    assert report.integrity is Integrity.VERIFIED
    assert (report.database is not None) is restores_database(mode)


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_the_journal_begins_before_the_snapshot_and_finishes_last(
    tmp_path: Path, archive: Path, mode: RestoreMode
) -> None:
    h = _harness(tmp_path, archive, mode=mode)
    h.run()
    names = h.journal.names()
    assert names[0] == "begin_run"
    assert h.journal.events[1] == ("phase_started", (P.SNAPSHOT, h.snapshots[0].value))
    assert names[-1] == "finish_run"
    assert names.count("finish_run") == 1


def test_under_skip_the_journal_begins_before_the_first_destructive_phase(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(
        tmp_path, archive, mode=RestoreMode.FILES_CLEAN, snapshot=SnapshotPolicy.SKIP
    )
    h.run()
    assert h.snapshots == []
    assert h.journal.events[0][0] == "begin_run"
    assert h.journal.events[1] == ("phase_started", (P.CLEAN, str(h.site_root)))


def test_keep_excludes_the_target_config_and_clean_keeps_it(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(tmp_path, archive, mode=RestoreMode.FILES_CLEAN)
    with capture_logs() as logs:
        report = h.run()
    clean = next(c for c in h.log.calls if c[2] == "clean")
    extract = next(c for c in h.log.calls if c[2] == "extract")
    assert clean[3] == ["wp-config.php"]
    assert extract[3] == (str(h.site_root), ["site-root"], ["wp-config.php"])
    assert report.wp_config_kept
    assert any(e["event"] == "wp_config_kept" for e in logs)


def test_a_target_without_config_takes_the_archives_with_the_tree(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(tmp_path, archive, mode=RestoreMode.FILES)
    h.config_path.unlink()
    report = h.run()
    extract = next(c for c in h.log.calls if c[2] == "extract")
    assert extract[3][2] == []
    assert not report.wp_config_kept
    assert not report.wp_config_absent, "the tree laid the Archive's config down"


def test_a_database_restore_onto_a_folder_without_config_writes_none(
    tmp_path: Path, archive: Path
) -> None:
    """Owner decision 74: the database loads; no config is written, and the
    report and the log say so."""
    h = _harness(tmp_path, archive, mode=RestoreMode.DATABASE)
    h.config_path.unlink()
    with capture_logs() as logs:
        report = h.run()
    assert P.LOAD_DUMP in report.phases
    assert not h.config_path.exists()
    assert report.wp_config_absent
    assert not report.wp_config_kept
    (record,) = [e for e in logs if e["event"] == "wp_config_not_written"]
    assert record["path"] == str(h.config_path)


def test_a_database_restore_beside_a_config_does_not_report_it_absent(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(tmp_path, archive, mode=RestoreMode.DATABASE)
    with capture_logs() as logs:
        report = h.run()
    assert not report.wp_config_absent
    assert "wp_config_not_written" not in [e["event"] for e in logs]


# --------------------------------------------------------------------------
# The phase-sequence test: no DB_* write before LOAD_DUMP's cutover
# --------------------------------------------------------------------------


#: Every Mode x relocation x config policy, less ``--overwrite-wp-config``
#: outside the file Modes (a usage error E003-S02-006 raises before planning).
SEQUENCES = [
    pytest.param(mode, name, policy, id=f"{mode.value}-{name}-{policy.value}")
    for mode in RestoreMode
    for name in sorted(RELOCATIONS)
    for policy in ConfigPolicy
    if policy is ConfigPolicy.KEEP or restores_files(mode)
]


@pytest.mark.parametrize(("mode", "name", "policy"), SEQUENCES)
def test_db_keys_are_written_only_in_the_cutover(
    tmp_path: Path, archive: Path, mode: RestoreMode, name: str, policy: ConfigPolicy
) -> None:
    relocation = _fits(RELOCATIONS[name], mode)
    h = _harness(
        tmp_path, archive, mode=mode, relocation=relocation, config_policy=policy
    )
    h.run()
    writes = [(c[0], c[3]) for c in h.log.calls if c[2] == "set_constants"]
    for phase, values in writes:
        if set(values) & set(CUTOVER_KEYS):
            assert phase is P.LOAD_DUMP, (phase, sorted(values))
    rewrites = [values for phase, values in writes if phase is P.REWRITE_CONFIG]
    assert rewrites == (
        [dict(relocation.rewrite_values)] if relocation.rewrite_values else []
    )
    replaced = [c[3] for c in h.log.calls if c[2] == "replace_file"]
    if policy is ConfigPolicy.OVERWRITE:
        assert replaced == [TARGET_DB]
        if not restores_database(mode):
            # The file that lands carries the target's own DB_* values.
            assert {k: h.config.values[k] for k in CUTOVER_KEYS} == TARGET_DB
        saved = [c for c in h.log.calls if c[2] == "save_copy"]
        assert saved[0][0] is P.REWRITE_CONFIG
        steps = [e[1][1] for e in h.journal.events if e[0] == "step_completed"]
        assert steps[:2] == ["config_saved", "config_replaced"]
    else:
        assert replaced == []
    assert all(ARCHIVE_DB["DB_PASSWORD"] != v for v in h.config.values.values())


def _workspace_files(h: Harness) -> list[str]:
    return sorted(
        str(p.relative_to(h.workspace)) for p in h.workspace.rglob("*") if p.is_file()
    )


def test_overwrite_hands_the_archives_config_over_in_memory(
    tmp_path: Path, archive: Path
) -> None:
    """BL-E003-057: the Archive's config reaches ``replace_file`` in memory."""
    h = _harness(
        tmp_path, archive, mode=RestoreMode.FILES, config_policy=ConfigPolicy.OVERWRITE
    )
    seen: list[tuple[bytes, list[str]]] = []
    real_replace = h.config.replace_file

    def replace_file(config_file: Path, content: str, *, keep: object) -> object:
        seen.append((content, _workspace_files(h)))
        return real_replace(config_file, content, keep=keep)  # type: ignore[arg-type]

    h.config.replace_file = replace_file  # type: ignore[method-assign]
    with capture_logs() as logs:
        h.run()
    ((content, workspace),) = seen
    assert isinstance(content, str)
    assert ARCHIVE_DB["DB_NAME"] in content
    # No credential-bearing copy exists, not even while the replace runs.
    assert workspace == []
    warning = next(e for e in logs if e["event"] == "wp_config_overwritten")
    assert warning["log_level"] == "warning"


def test_an_archive_config_that_is_not_utf8_fails_before_the_replace(
    tmp_path: Path, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _harness(
        tmp_path, archive, mode=RestoreMode.FILES, config_policy=ConfigPolicy.OVERWRITE
    )
    monkeypatch.setattr(
        type(h.probe), "read_member", lambda *_a, **_k: b"<?php\n\xff\xfe\n"
    )
    with pytest.raises(ConfigWriteFailedError, match="could not be read"):
        h.run()
    assert [c for c in h.log.calls if c[2] == "replace_file"] == []
    assert _workspace_files(h) == []


def test_a_dry_run_overwrite_writes_no_config_file(
    tmp_path: Path, archive: Path
) -> None:
    """BL-E003-057: under the no-op adapters nothing lands in the workspace."""
    h = _harness(
        tmp_path, archive, mode=RestoreMode.FILES, config_policy=ConfigPolicy.OVERWRITE
    )
    before = h.config_path.read_bytes()
    dry = dry_run_adapters(h.adapters())
    during: list[list[str]] = []
    noop = dry.config_command

    class _Watching:
        """The no-op command half, noting the workspace at ``replace_file``."""

        def __getattr__(self, name: str) -> object:
            return getattr(noop, name)

        def replace_file(
            self, config_file: Path, content: str, *, keep: object
        ) -> object:
            during.append(_workspace_files(h))
            return noop.replace_file(config_file, content, keep=keep)  # type: ignore[arg-type]

    h.run(adapters=dataclasses.replace(dry, config_command=_Watching()))  # type: ignore[arg-type]
    assert during == [[]], "the Archive's config was written during a Dry Run"
    assert _workspace_files(h) == []
    assert h.config_path.read_bytes() == before


#: Callable names that open or write a file, however reached (``os.open``,
#: ``Path(...).open``, ``shutil.copyfile``, ``tempfile.mkstemp``, ...).
_FILE_WRITERS = frozenset(
    {
        "open",
        "fdopen",
        "write_bytes",
        "write_text",
        "copy",
        "copy2",
        "copyfile",
        "copyfileobj",
        "copytree",
        "mkstemp",
        "NamedTemporaryFile",
        "TemporaryFile",
        "SpooledTemporaryFile",
    }
)
#: Modules whose import of one of those names is itself a finding.
_FILE_MODULES = frozenset({"os", "io", "builtins", "shutil", "tempfile", "pathlib"})


def _file_calls(source: str) -> list[str]:
    """Every call or import in ``source`` that can open or write a file.

    An AST check, not a grep: a call whose callee is named (``open(...)``) or
    reached as an attribute (``os.open``, ``Path(p).write_bytes``,
    ``shutil.copyfile``), and a ``from os import open``-style import of such a
    name. Not covered: a writer reached dynamically (``getattr``), or through
    a helper function defined in another module and called here by another
    name — the behavioural Dry Run tests above are the guard for those.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in _FILE_WRITERS:
                found.append(ast.unparse(node))
        elif isinstance(node, ast.ImportFrom) and node.module in _FILE_MODULES:
            found.extend(
                f"from {node.module} import {alias.name}"
                for alias in node.names
                if alias.name in _FILE_WRITERS
            )
    return found


def test_the_executor_opens_no_file_itself() -> None:
    """BL-E003-057: ``adapters/restore_runner.py`` opens and writes no file itself.

    Every file effect goes through a port. The scope is the executor module,
    read from its own ``__file__``. Not covered: ``cli/restore.py``'s
    ``_archive_config``, which writes the Archive's config into the workspace
    when the target has none, to build the Site Descriptor — the composition
    root's write, which ADR-0016's 2026-10-27 amendment prescribes; and what
    :func:`_file_calls` states it cannot see.
    """
    source = Path(inspect.getfile(restore_runner)).read_text(encoding="utf-8")
    assert _file_calls(source) == []


@pytest.mark.parametrize(
    "planted",
    [
        "os.open(p, os.O_WRONLY)",
        "open(p, 'wb')",
        "Path(p).write_bytes(b)",
        "Path(p).write_text(t)",
        "Path(p).open('wb')",
        "os.fdopen(fd, 'wb')",
        "shutil.copyfile(a, b)",
        "tempfile.mkstemp()",
        "tempfile.NamedTemporaryFile()",
    ],
)
def test_each_planted_file_write_is_caught(planted: str) -> None:
    source = f"def f(p, a, b, t, fd):\n    return {planted}\n"
    assert _file_calls(source) == [planted]


def test_an_imported_writer_is_caught() -> None:
    assert _file_calls("from os import open\n") == ["from os import open"]
    assert _file_calls("from shutil import copyfile as cp\n") == [
        "from shutil import copyfile"
    ]


def test_a_symlinked_site_root_still_keeps_the_targets_config(
    tmp_path: Path, archive: Path
) -> None:
    """CLEAN compares directories, not strings: a link to the root is the root."""
    h = _harness(tmp_path, archive, mode=RestoreMode.FILES_CLEAN)
    link = tmp_path / "root-link"
    link.symlink_to(h.site_root)
    assert _same_dir(link, h.site_root)
    assert not _same_dir(tmp_path, h.site_root)


def test_an_overwrite_lays_a_config_role_member_down_through_the_port(
    tmp_path: Path,
) -> None:
    """BL-E003-057: a ``config``-role member is read into memory, never laid down."""
    archive = write_v1(tmp_path, config_role=True)
    h = _harness(
        tmp_path, archive, mode=RestoreMode.FILES, config_policy=ConfigPolicy.OVERWRITE
    )
    seen: list[str] = []
    real_replace = h.config.replace_file

    def replace_file(config_file: Path, content: str, *, keep: object) -> object:
        seen.append(content)
        return real_replace(config_file, content, keep=keep)  # type: ignore[arg-type]

    h.config.replace_file = replace_file  # type: ignore[method-assign]
    h.run()
    laid = [c for c in h.log.calls if c[2] == "extract" and c[3][1] == ["config"]]
    assert laid == []
    assert len(seen) == 1
    assert ARCHIVE_DB["DB_NAME"] in seen[0]
    assert _workspace_files(h) == []


def test_a_lay_down_extracts_the_config_role_first(tmp_path: Path) -> None:
    archive = write_v1(tmp_path, config_role=True)
    relocation = dataclasses.replace(RELOCATIONS["url-only"], lay_down_config=True)
    h = _harness(tmp_path, archive, mode=RestoreMode.ALL, relocation=relocation)
    h.config_path.unlink()
    h.run()
    extracts = [c for c in h.log.calls if c[2] == "extract"]
    assert extracts[0][3] == (str(h.site_root), ["config"], [])
    assert extracts[1][3] == (str(h.site_root), ["site-root"], ["wp-config.php"])
    first_write = next(i for i, c in enumerate(h.log.calls) if c[2] == "set_constants")
    assert h.log.calls.index(extracts[0]) < first_write
    steps = [
        e[1] for e in h.journal.events if e[0] in {"phase_started", "step_completed"}
    ]
    at = steps.index((P.EXTRACT, f"config:{h.config_path}"))
    assert steps[at + 1 : at + 3] == [
        (P.EXTRACT, "config_started"),
        (P.EXTRACT, "config"),
    ]


@pytest.mark.parametrize(
    ("archive_mode", "laid_mode"),
    [(0o640, 0o640), (0o644, 0o640), (0o666, 0o660), (0o604, 0o600)],
    ids=["private", "world-readable", "world-writable", "other-only"],
)
def test_a_lay_down_takes_other_permissions_off_and_logs_the_mode(
    tmp_path: Path, archive_mode: int, laid_mode: int
) -> None:
    """E007 closure RT-4: the Archive's "other" bits never survive the lay-down."""
    archive = write_v1(tmp_path, config_role=True)
    relocation = dataclasses.replace(RELOCATIONS["url-only"], lay_down_config=True)
    h = _harness(
        tmp_path, archive, mode=RestoreMode.ALL, relocation=relocation, real_files=True
    )
    h.config_path.unlink()
    real_extract = LocalSiteFiles.extract

    def extract(self: LocalSiteFiles, *args: object, **kwargs: object) -> object:
        result = real_extract(self, *args, **kwargs)  # type: ignore[arg-type]
        if h.config_path.exists():
            h.config_path.chmod(archive_mode)
        return result

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(LocalSiteFiles, "extract", extract)
        with capture_logs() as logs:
            h.run()
    (laid,) = [e for e in logs if e["event"] == "config_laid_down"]
    assert laid["config_path"] == str(h.config_path)
    assert laid["mode"] == f"{laid_mode:04o}"
    assert laid["duration_ms"] >= 0
    narrowed = [e for e in logs if e["event"] == "config_mode_restricted"]
    if archive_mode == laid_mode:
        assert narrowed == []
    else:
        assert narrowed == [
            {
                "event": "config_mode_restricted",
                "log_level": "warning",
                "config_path": str(h.config_path),
                "archive_mode": f"{archive_mode:04o}",
                "mode": f"{laid_mode:04o}",
            }
        ]
    body = h.config_path.read_text(encoding="utf-8")
    assert all(body not in repr(e) for e in logs)


@pytest.mark.parametrize(
    ("archive_mode", "laid_mode"), [(0o644, 0o640), (0o604, 0o600), (0o640, 0o640)]
)
def test_a_config_laid_down_by_the_tree_loses_other_permissions_too(
    tmp_path: Path, archive_mode: int, laid_mode: int
) -> None:
    """BL-E008-023: no config role, so the tree extraction lays it down; its
    "other" bits go before the cutover writes a password into it."""
    archive = write_v1(tmp_path)
    relocation = dataclasses.replace(RELOCATIONS["url-only"], lay_down_config=True)
    h = _harness(
        tmp_path, archive, mode=RestoreMode.ALL, relocation=relocation, real_files=True
    )
    h.config_path.unlink()
    real_extract = LocalSiteFiles.extract

    def extract(self: LocalSiteFiles, *args: object, **kwargs: object) -> object:
        result = real_extract(self, *args, **kwargs)  # type: ignore[arg-type]
        if h.config_path.exists():
            h.config_path.chmod(archive_mode)
        return result

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(LocalSiteFiles, "extract", extract)
        with capture_logs() as logs:
            h.run()
    laid = [c for c in h.log.calls if c[2] == "extract" and c[3][1] == ["config"]]
    assert laid == []  # the tree laid it down, not the config role
    assert stat.S_IMODE(h.config_path.stat().st_mode) == laid_mode
    narrowed = [e for e in logs if e["event"] == "config_mode_restricted"]
    assert len(narrowed) == (0 if archive_mode == laid_mode else 1)


def test_a_config_laid_down_above_the_root_excludes_the_trees_own(
    tmp_path: Path,
) -> None:
    """E007 closure RT-3: an Archive's root ``wp-config.php`` cannot shadow it."""
    from pathlib import PurePosixPath

    archive = write_v1(tmp_path, config_role=True)
    parent = PurePosixPath(tmp_path / "wp-config.php")
    relocation = dataclasses.replace(
        RELOCATIONS["url-only"], lay_down_config=True, config_file=parent
    )
    h = _harness(tmp_path, archive, mode=RestoreMode.ALL, relocation=relocation)
    h.config_path.unlink()
    Path(parent).unlink()  # the Archive's source file sat there: the target has none
    h.run()
    extracts = [c for c in h.log.calls if c[2] == "extract"]
    assert extracts[0][3] == (str(tmp_path), ["config"], [])
    assert extracts[1][3] == (str(h.site_root), ["site-root"], ["wp-config.php"])


def test_a_lay_down_of_an_in_root_config_arrives_with_the_tree(
    tmp_path: Path, archive: Path
) -> None:
    """F-2: the Archive's config is in its tree; the tree must not exclude it."""
    relocation = dataclasses.replace(RELOCATIONS["url-only"], lay_down_config=True)
    h = _harness(
        tmp_path, archive, mode=RestoreMode.ALL, relocation=relocation, real_files=True
    )
    h.config_path.unlink()
    h.run()
    assert ARCHIVE_DB["DB_NAME"] in h.config_path.read_text(encoding="utf-8")
    extracts = [c for c in h.log.calls if c[2] == "extract"]
    assert [e[3][1] for e in extracts] == [["site-root"]]
    steps = [e[1][1] for e in h.journal.events if e[0] == "step_completed"]
    assert steps[:2] == ["config_started", "config"]


@pytest.mark.parametrize("mode", [RestoreMode.DATABASE, RestoreMode.ALL])
def test_a_lay_down_the_archive_cannot_supply_is_refused_first(
    tmp_path: Path, archive: Path, mode: RestoreMode
) -> None:
    """An in-tree config cannot be laid down one level up, or without the tree."""
    from pathlib import PurePosixPath

    from l3io.wp.backup.core.restore import ArchiveHasNoConfigError

    if mode is RestoreMode.ALL:  # one level up: the tree cannot place it there
        parent = PurePosixPath(tmp_path / "wp-config.php")
        relocation = dataclasses.replace(
            RELOCATIONS["url-only"], lay_down_config=True, config_file=parent
        )
    else:  # a Mode that extracts no tree
        relocation = dataclasses.replace(NO_RELOCATION, lay_down_config=True)
    h = _harness(tmp_path, archive, mode=mode, relocation=relocation)
    h.config_path.unlink()
    with pytest.raises(ArchiveHasNoConfigError):
        h.run()
    assert h.log.calls == []
    assert h.journal.events == []


def test_a_config_gone_since_planning_is_refused_not_skipped(
    tmp_path: Path, archive: Path
) -> None:
    """F-3: the planned overwrite is never skipped silently."""
    from l3io.wp.backup.adapters.restore_runner import TargetConfigMissingError

    h = _harness(
        tmp_path, archive, mode=RestoreMode.FILES, config_policy=ConfigPolicy.OVERWRITE
    )
    plan = h.plan()
    assert P.REWRITE_CONFIG in plan.phases
    h.config_path.unlink()
    with pytest.raises(TargetConfigMissingError) as caught:
        h.run(plan=plan)
    assert caught.value.exit_code is ExitCode.REFUSED
    assert h.log.calls == []
    assert h.journal.events == []


def test_a_header_with_another_mode_is_a_programming_error(
    tmp_path: Path, archive: Path
) -> None:
    """F-4: the Mode is carried once; plan and journal header must agree."""
    h = _harness(tmp_path, archive, mode=RestoreMode.ALL_CLEAN)
    plan = h.plan()
    h.mode = RestoreMode.ALL
    with pytest.raises(ValueError, match="mode"):
        h.run(plan=plan)
    assert h.log.calls == []
    assert h.preflights == []


def test_a_relocation_with_a_db_key_is_unrepresentable() -> None:
    with pytest.raises(ValueError, match="DB_HOST"):
        dataclasses.replace(NO_RELOCATION, rewrite_values=(("DB_HOST", "x"),))


def test_search_replace_runs_last_and_is_reported(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(
        tmp_path, archive, mode=RestoreMode.ALL, relocation=RELOCATIONS["both"]
    )
    report = h.run()
    assert h.log.methods()[-1] == "search_replace.relocate_content"
    assert report.search_replace is not None


def test_search_replace_is_told_whether_the_archive_was_encrypted(
    tmp_path: Path, archive: Path
) -> None:
    """BL-E007-020: the fact is an argument of the port call, not shared state."""
    h = _harness(
        tmp_path, archive, mode=RestoreMode.ALL, relocation=RELOCATIONS["both"]
    )
    told: list[bool | None] = []

    class Telling:
        def relocate_content(
            self, site_root: Path, urls: object, *, archive_encrypted: bool | None
        ) -> SearchReplaceReport:
            told.append(archive_encrypted)
            return SearchReplaceReport(
                pairs=(), replacements=0, tables=(), remaining=0, merged=True
            )

    h.run(adapters=dataclasses.replace(h.adapters(), search_replace=Telling()))
    assert told == [h.view.encrypted]


# --------------------------------------------------------------------------
# PREFLIGHT — queries only, in order, before any command
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_preflight_checks_the_plans_tools_once_and_the_database_only_when_loading(
    tmp_path: Path, archive: Path, mode: RestoreMode
) -> None:
    h = _harness(tmp_path, archive, mode=mode)
    plan = h.plan()
    h.run(plan=plan)
    assert h.preflights == [plan.required_tools]
    assert h.checks == int(restores_database(mode))


def test_a_refused_database_check_changes_nothing(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(tmp_path, archive, check_failure=DatabaseRefusedError(("not empty",)))
    with pytest.raises(DatabaseRefusedError):
        h.run()
    assert h.log.calls == []
    assert h.journal.events == []


def test_an_unpatched_tarfile_is_refused_before_clean(
    tmp_path: Path, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _harness(tmp_path, archive, mode=RestoreMode.FILES_CLEAN)
    monkeypatch.delattr(os.path, "ALLOW_MISSING", raising=False)
    with pytest.raises(ExtractionRefusedError, match="CVE-2025-4517") as caught:
        h.run()
    assert caught.value.exit_code is ExitCode.REFUSED
    assert h.log.calls == []
    assert h.journal.events == []
    assert h.snapshots == []


def test_an_archive_too_large_for_the_target_is_refused_before_clean(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(tmp_path, archive, mode=RestoreMode.ALL_CLEAN, free=100)
    with pytest.raises(InsufficientSpaceError) as caught:
        h.run()
    assert "restore target" in str(caught.value)
    assert h.log.calls == []
    assert h.journal.events == []


def test_the_workspace_need_counts_the_dump_not_the_target(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(tmp_path, archive, mode=RestoreMode.DATABASE)
    sizes = {m.role: m.size for m in h.view.members if m.size}
    h.free = int((sizes[MemberRole.DUMP] - 1) / 0.95) + 1
    with pytest.raises(InsufficientSpaceError) as caught:
        h.run()
    assert "workspace" in str(caught.value)


def test_an_inflated_index_is_refused_as_a_bomb(tmp_path: Path, archive: Path) -> None:
    h = _harness(tmp_path, archive, mode=RestoreMode.FILES)
    assert h.view.payload is not None
    bomb = h.view.payload.size * 201
    members = tuple(
        dataclasses.replace(m, size=bomb) if m.path == "site-root/index.php" else m
        for m in h.view.members
    )
    h.view = dataclasses.replace(h.view, members=members)
    with pytest.raises(ExpansionRatioError):
        h.run()
    assert h.log.calls == []


def test_a_v0_view_takes_its_sizes_from_the_probe(tmp_path: Path) -> None:
    v0 = write_v0(tmp_path, index=b"x" * 50_000)
    h = _harness(tmp_path, v0, mode=RestoreMode.FILES, free=40_000)
    assert all(m.size is None for m in h.view.members)
    with pytest.raises(InsufficientSpaceError):
        h.run()
    assert h.probe.digest_reads == 1
    assert h.log.calls == []


def test_a_v0_archive_proceeds_structural_only(tmp_path: Path) -> None:
    h = _harness(tmp_path, write_v0(tmp_path), mode=RestoreMode.ALL)
    report = h.run()
    assert report.integrity is Integrity.STRUCTURAL_ONLY
    assert h.slot_calls == 1


# --------------------------------------------------------------------------
# VERIFY — before the first destructive step
# --------------------------------------------------------------------------


def test_a_failed_verification_changes_nothing(tmp_path: Path, archive: Path) -> None:
    h = _harness(tmp_path, archive)
    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2])
    with pytest.raises(VerificationFailedError) as caught:
        h.run()
    assert caught.value.exit_code is ExitCode.VERIFICATION_FAILED
    assert h.log.calls == []
    assert h.journal.events == []
    assert h.snapshots == []


def test_restore_errors_name_the_operators_archive_not_the_workspace_copy(
    tmp_path: Path, archive: Path
) -> None:
    """BL-E002-055 / BL-E003-068: the workspace copy is deleted with the Run."""
    h = _harness(tmp_path, archive)
    h.archive_name = "/backups/operator-named.tar.gz"
    h.view = dataclasses.replace(h.view, config_path="../wp-config.php")
    with pytest.raises(ArchiveChangedError) as caught:
        h.run()
    assert caught.value.archive == h.archive_name
    (tmp_path / "again").mkdir()
    h = _harness(tmp_path / "again", archive)
    h.archive_name = "/backups/operator-named.tar.gz"
    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2])
    with pytest.raises(VerificationFailedError) as failed:
        h.run()
    assert failed.value.archive == h.archive_name
    assert repr(h.archive_name) in str(failed.value)


def test_a_changed_archive_is_refused(tmp_path: Path, archive: Path) -> None:
    h = _harness(tmp_path, archive)
    h.view = dataclasses.replace(h.view, config_path="../wp-config.php")
    with pytest.raises(ArchiveChangedError) as caught:
        h.run()
    assert caught.value.exit_code is ExitCode.VERIFICATION_FAILED
    assert h.log.calls == []
    assert h.journal.events == []


@pytest.mark.parametrize("failure", [PassphraseRejectedError, PayloadDamagedError])
def test_an_undecryptable_payload_changes_nothing(
    tmp_path: Path, failure: type[Exception]
) -> None:
    sealed = write_v1(tmp_path, cipher=IdentityCipher())
    raised = (
        PassphraseRejectedError(scheme="age")
        if failure is PassphraseRejectedError
        else failure()
    )
    cipher = IdentityCipher(failure=raised)
    h = _harness(tmp_path, sealed, cipher=cipher)
    assert h.view.encrypted
    with pytest.raises(failure) as caught:
        h.run()
    assert caught.value.exit_code is ExitCode.VERIFICATION_FAILED  # type: ignore[attr-defined]
    assert cipher.checked == 1
    assert h.log.calls == []
    assert h.journal.events == []
    assert h.snapshots == []


def test_an_encrypted_archive_is_read_through_its_decrypted_payload(
    tmp_path: Path,
) -> None:
    sealed = write_v1(tmp_path, cipher=IdentityCipher())
    h = _harness(
        tmp_path,
        sealed,
        mode=RestoreMode.FILES,
        cipher=IdentityCipher(),
        real_files=True,
    )
    h.run()
    assert (h.workspace / "payload.tar.gz").is_file()
    assert (h.site_root / "index.php").read_bytes().startswith(b"<?php")


# --------------------------------------------------------------------------
# VERIFY: the inner Manifest (BL-E002-115, ADR-0024 amendment 2026-11-06)
# --------------------------------------------------------------------------


def _verify_record(logs: list[dict[str, object]]) -> dict[str, object]:
    return next(
        e
        for e in logs
        if e.get("event") == "phase_completed" and e.get("phase") == P.VERIFY.name
    )


def _refused_before_snapshot(
    h: Harness,
) -> tuple[PayloadDamagedError, dict[str, object]]:
    with capture_logs() as logs, pytest.raises(PayloadDamagedError) as caught:
        h.run()
    assert h.log.calls == []
    assert h.journal.events == []
    assert h.snapshots == []
    return caught.value, _verify_record([dict(e) for e in logs])


@pytest.mark.parametrize(
    ("edit", "field"),
    [
        (lambda m: m.update(backup_scope="files"), "backup_scope"),
        (lambda m: m["source"].update(db_name="elsewhere"), "source.db_name"),
        (lambda m: m.update(config_path=None), "config_path"),
    ],
    ids=["scope", "source", "config-path"],
)
def test_verify_refuses_an_outer_manifest_the_payload_does_not_carry(
    tmp_path: Path, edit: Callable[[dict[str, object]], None], field: str
) -> None:
    sealed = write_v1(tmp_path, cipher=IdentityCipher())
    forged = forge_archive(
        sealed, tmp_path / "edited.tar.gz", lambda p: p, manifest_edit=edit
    )
    h = _harness(tmp_path, forged, cipher=IdentityCipher())
    error, record = _refused_before_snapshot(h)
    assert error.field == field
    assert record["reason"] == "inner_manifest_mismatch"
    assert record["field"] == field
    assert record["exit_code"] == ExitCode.VERIFICATION_FAILED


def _no_inner(members: InnerMembers) -> InnerMembers:
    return [m for m in members if m[0].name != INNER_MANIFEST_MEMBER]


def _inner(members: InnerMembers) -> InnerMembers:
    return [m for m in members if m[0].name == INNER_MANIFEST_MEMBER]


@pytest.mark.parametrize(
    ("layout", "reason"),
    [
        (_no_inner, "inner_manifest_missing"),
        (lambda m: _inner(m) + _no_inner(m), "inner_manifest_misplaced"),
        (lambda m: _no_inner(m) + _inner(m) * 2, "inner_manifest_duplicated"),
    ],
    ids=["removed", "moved", "duplicated"],
)
def test_verify_refuses_an_inner_manifest_out_of_place(
    tmp_path: Path, layout: Callable[[InnerMembers], InnerMembers], reason: str
) -> None:
    sealed = write_v1(tmp_path, cipher=IdentityCipher())
    forged = forge_archive(
        sealed, tmp_path / "relaid.tar.gz", reseal(IdentityCipher(), layout)
    )
    h = _harness(tmp_path, forged, cipher=IdentityCipher())
    error, record = _refused_before_snapshot(h)
    assert error.field is None
    assert record["reason"] == reason


def test_an_encrypted_archive_without_an_inner_manifest_is_refused(
    tmp_path: Path,
) -> None:
    """An encrypted v1 Archive written before the amendment carries none."""
    sealed = write_v1(tmp_path, cipher=IdentityCipher())
    forged = forge_archive(
        sealed, tmp_path / "old.tar.gz", reseal(IdentityCipher(), _no_inner)
    )
    with pytest.raises(PayloadDamagedError):
        _harness(tmp_path, forged, cipher=IdentityCipher()).run()


# --------------------------------------------------------------------------
# Failure after the journal began (ADR-0021)
# --------------------------------------------------------------------------


def test_an_orderly_slot_failure_finishes_the_journal(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(
        tmp_path, archive, mode=RestoreMode.DATABASE, slot_failure=ORDERLY_CHECK_FAILURE
    )
    with pytest.raises(RuntimeError):
        h.run()
    assert h.journal.names()[-1] == "finish_run"


def test_a_journal_that_cannot_finish_does_not_mask_the_runs_error(
    tmp_path: Path, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADV-13 (BL-E003-040): the Run's own error, and so its exit code, stands."""
    h = _harness(
        tmp_path, archive, mode=RestoreMode.DATABASE, slot_failure=ORDERLY_CHECK_FAILURE
    )

    def full_disk() -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(h.journal, "finish_run", full_disk)
    with capture_logs() as logs, pytest.raises(RuntimeError) as caught:
        h.run()
    assert not isinstance(caught.value, OSError)
    (failed,) = [e for e in logs if e["event"] == "journal_finish_failed"]
    assert failed["log_level"] == "error"
    assert failed["error"] == "OSError"


def test_a_failure_after_the_cutover_leaves_the_journal_blocking(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(
        tmp_path, archive, mode=RestoreMode.DATABASE, slot_failure=NON_ORDERLY_FAILURE
    )
    with capture_logs() as logs, pytest.raises(RuntimeError):
        h.run()
    assert "finish_run" not in h.journal.names()
    failed = [
        e for e in logs if e["event"] == "phase_completed" and e["outcome"] == "failed"
    ]
    assert [e["phase"] for e in failed] == ["LOAD_DUMP"]


def test_an_orderly_load_failure_after_extract_is_not_orderly(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(
        tmp_path, archive, mode=RestoreMode.ALL, slot_failure=ORDERLY_CHECK_FAILURE
    )
    with pytest.raises(RuntimeError):
        h.run()
    assert "finish_run" not in h.journal.names()


# --------------------------------------------------------------------------
# FR-8 — the Safety Snapshot phase (E003-S02-003)
# --------------------------------------------------------------------------


def _snapshot_of(
    h: Harness, **fields: object
) -> Callable[[BackupScope], SnapshotResult]:
    """A ``take_snapshot`` returning the harness's result with ``fields`` changed."""

    def take(scope: BackupScope) -> SnapshotResult:
        h.snapshots.append(scope)
        good = SnapshotResult(
            path=PurePosixPath(h.snapshot_file),
            scope=scope,
            encrypted=False,
            bytes=1,
            integrity=Integrity.VERIFIED,
            performed=True,
        )
        return dataclasses.replace(good, **fields)  # type: ignore[arg-type]

    return take


def _destroyed(h: Harness) -> list[str]:
    return [m for m in h.log.methods() if m in FILE_CALLS | DATABASE_CALLS]


@pytest.mark.parametrize("mode", list(RestoreMode))
def test_a_failing_snapshot_refuses_with_nothing_destroyed(
    tmp_path: Path, archive: Path, mode: RestoreMode
) -> None:
    h = _harness(tmp_path, archive, mode=mode)
    disk_full = OSError(28, "No space left on device")

    def take(scope: BackupScope) -> SnapshotResult:
        raise disk_full

    h.take_snapshot = take  # type: ignore[method-assign]
    with pytest.raises(SnapshotFailedError) as caught:
        h.run()
    assert caught.value.exit_code is ExitCode.REFUSED
    assert caught.value.cause is disk_full
    assert caught.value.path == str(h.snapshot_file)
    assert _destroyed(h) == []


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"integrity": Integrity.STRUCTURAL_ONLY}, "STRUCTURAL_ONLY"),
        ({"integrity": None}, "integrity None"),
        ({"bytes": 0}, "0 bytes"),
        ({"path": PurePosixPath("/elsewhere/s.tar.gz")}, "not the planned path"),
        ({"scope": BackupScope.DATABASE}, "covers 'database'"),
    ],
)
def test_a_result_that_proves_no_verified_snapshot_is_refused_before_clean(
    tmp_path: Path, archive: Path, fields: dict[str, object], match: str
) -> None:
    """ADV-5: performed, VERIFIED, non-empty, at the planned path, of the scope."""
    h = _harness(tmp_path, archive, mode=RestoreMode.FILES_CLEAN)
    h.take_snapshot = _snapshot_of(h, **fields)  # type: ignore[method-assign]
    with pytest.raises(SnapshotFailedError, match=match):
        h.run()
    assert _destroyed(h) == []


def test_a_not_performed_snapshot_passes_only_when_nothing_can_be_destroyed(
    tmp_path: Path, archive: Path
) -> None:
    """The Dry Run's no-op halves: SNAPSHOT completes and reports "would verify"."""
    h = LoadHarness(work=tmp_path, archive_path=archive, mode=RestoreMode.ALL_CLEAN)
    h.take_snapshot = _snapshot_of(  # type: ignore[method-assign]
        h, performed=False, integrity=None, bytes=0
    )
    with capture_logs() as logs:
        report = h.run(adapters=dry_run_adapters(h.adapters()))
    assert report.snapshot is not None
    assert report.snapshot.performed is False
    planned = [e for e in logs if e["event"] == "safety_snapshot_planned"]
    assert [e["integrity"] for e in planned] == ["would verify"]
    done = [e for e in logs if e["event"] == "phase_completed"]
    assert "SNAPSHOT" in [e["phase"] for e in done]


def test_a_relocating_dry_run_changes_nothing_and_plans_keys_only(
    tmp_path: Path, archive: Path
) -> None:
    """E007-S01-001: under the no-op adapters, a relocation writes and loads nothing."""
    pytest.importorskip("l3io.wp.database")
    from l3io.wp.backup.adapters.redaction import SecretRegistry
    from l3io.wp.backup.adapters.restore_extension import restore_extension
    from l3io.wp.backup.cli.restore_request import RelocationFlags

    secret = "e007-dry-run-password-0"
    flags = dataclasses.replace(
        RelocationFlags.none(),
        new_site_url="https://staging.e007.example",
        new_home_url="https://www.staging.e007.example",
        new_db_host="db.e007.example:3310",
        new_db_name="e007_staging",
        new_db_user="e007_user",
        new_db_password=secret,
    )
    probe = LoadHarness(work=tmp_path, archive_path=archive, mode=RestoreMode.ALL)
    relocation = restore_extension(flags, secrets=SecretRegistry()).resolve(
        site_root=probe.site_root,
        site=probe.site,
        view=probe.view,
        files=LocalSiteFiles(),
        mode=RestoreMode.ALL,
    )
    h = LoadHarness(
        work=tmp_path,
        archive_path=archive,
        mode=RestoreMode.ALL,
        relocation=relocation,
        password=secret,
    )
    h.take_snapshot = _snapshot_of(  # type: ignore[method-assign]
        h, performed=False, integrity=None, bytes=0
    )
    before = h.config_path.read_bytes()
    plan = h.plan()
    h.run_load(adapters=dry_run_adapters(h.adapters()))
    assert h.config_path.read_bytes() == before
    effects = {"set_constants", "save_copy", "replace_file", "ensure", "load"}
    assert [c for c in h.log.calls if c[2] in effects] == []
    texts = [a.text for a in plan.actions]
    assert any("set WP_SITEURL, WP_HOME in" in t for t in texts), texts
    assert any(
        "the cutover sets DB_HOST, DB_USER, DB_PASSWORD, DB_NAME" in t for t in texts
    ), texts
    for value in (secret, "https://staging.e007.example", "e007_user"):
        assert all(value not in t for t in texts)


def test_every_phase_is_a_check_the_snapshot_or_destroying() -> None:
    """The re-survey's trigger set covers every phase AD-17 orders after SNAPSHOT."""
    classes = (_CHECK_PHASES, frozenset({P.SNAPSHOT}), _DESTROYING)
    for step in RestorePhase:
        assert sum(step in c for c in classes) == 1, step
    assert P.CLEAN in _DESTROYING
    assert P.LOAD_DUMP in _DESTROYING


def test_the_written_snapshot_is_logged_with_its_facts(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(tmp_path, archive, mode=RestoreMode.ALL_CLEAN)
    with capture_logs() as logs:
        h.run()
    written = [e for e in logs if e["event"] == "safety_snapshot_written"]
    assert len(written) == 1
    assert written[0]["path"] == str(h.snapshot_file)
    assert written[0]["scope"] == "all"
    assert written[0]["integrity"] == "VERIFIED"
    assert written[0]["restore_mode"] == "all-clean"
    assert {"bytes", "encrypted"} <= written[0].keys()


def test_skip_logs_the_opt_out_and_takes_no_snapshot(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(
        tmp_path, archive, mode=RestoreMode.ALL_CLEAN, snapshot=SnapshotPolicy.SKIP
    )
    with capture_logs() as logs:
        report = h.run()
    assert h.snapshots == []
    assert P.SNAPSHOT not in report.phases
    assert all(a.phase is not P.SNAPSHOT for a in h.plan().actions)
    skipped = [e for e in logs if e["event"] == "safety_snapshot_skipped"]
    assert [(e["log_level"], e["opted_out_by"]) for e in skipped] == [
        ("warning", "--no-safety-snapshot")
    ]


def test_an_empty_target_logs_that_no_snapshot_is_needed(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(tmp_path, archive, mode=RestoreMode.FILES)
    for entry in h.site_root.iterdir():
        entry.unlink()
    with capture_logs() as logs:
        report = h.run()
    assert P.SNAPSHOT not in report.phases
    needed = [e for e in logs if e["event"] == "safety_snapshot_not_needed"]
    assert [(e["log_level"], e["empty"]) for e in needed] == [("info", ["files"])]


def test_a_planned_snapshot_with_no_header_path_is_a_programming_error(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(tmp_path, archive, mode=RestoreMode.FILES_CLEAN)
    header = dataclasses.replace(h.header(), snapshot_path=None)
    h.header = lambda: header  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="snapshot_path"):
        h.run()
    assert h.log.calls == []


# --------------------------------------------------------------------------
# RT-003 / ADV-4 — the target is surveyed again before the first destroying phase
# --------------------------------------------------------------------------


def _emptied(h: Harness) -> None:
    for entry in h.site_root.iterdir():
        entry.unlink()


def test_content_that_appears_after_planning_is_refused_before_clean(
    tmp_path: Path, archive: Path
) -> None:
    """Empty at plan time (no SNAPSHOT planned), populated before CLEAN."""
    h = _harness(tmp_path, archive, mode=RestoreMode.FILES_CLEAN)
    _emptied(h)
    plan = h.plan()
    assert P.SNAPSHOT not in plan.phases
    (h.site_root / "deployed.php").write_bytes(b"<?php // new\n")
    with pytest.raises(TargetChangedError) as caught:
        h.run(plan=plan)
    assert caught.value.exit_code is ExitCode.REFUSED
    assert caught.value.found is BackupScope.FILES
    assert _destroyed(h) == []
    assert (h.site_root / "deployed.php").exists()


def test_a_newly_non_empty_target_is_refused_by_fr7_without_force(
    tmp_path: Path, archive: Path
) -> None:
    """The plan's own waivers are re-applied: no ``--force``, so FR-7 refuses."""
    h = _harness(
        tmp_path,
        archive,
        mode=RestoreMode.FILES_CLEAN,
        snapshot=SnapshotPolicy.SKIP,
        overrides=OverrideSet(waived=frozenset()),
    )
    _emptied(h)
    plan = h.plan()
    (h.site_root / "deployed.php").write_bytes(b"<?php // new\n")
    with pytest.raises(NonEmptyTargetError):
        h.run(plan=plan)
    assert _destroyed(h) == []


def test_the_resurvey_runs_once_after_the_snapshot_and_before_clean(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(tmp_path, archive, mode=RestoreMode.ALL_CLEAN)
    order: list[str] = []
    survey = h.target

    def counted() -> TargetState:
        order.append(f"survey after {h.log.methods()}")
        return survey()

    h.target = counted  # type: ignore[method-assign]
    plan = h.plan()
    order.clear()
    h.run(plan=plan)
    assert order == ["survey after ['snapshot.take']"]


# --------------------------------------------------------------------------
# ADV-6 — the snapshot directory's free space, summed per filesystem
# --------------------------------------------------------------------------


def _shared_free(h: Harness) -> tuple[int, int, int]:
    """Free bytes each need fits alone but their sum on one filesystem does not."""
    target = sum(m.size or 0 for m in h.view.members if m.role is MemberRole.SITE_ROOT)
    snapshot = LocalSiteFiles().tree_bytes(h.site_root)
    free = int((target + snapshot - 1) / (1 - FREE_SPACE_MARGIN))
    assert max(target, snapshot) <= free * (1 - FREE_SPACE_MARGIN)
    return target, snapshot, free


def test_the_snapshot_need_is_summed_with_the_target_on_one_filesystem(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(tmp_path, archive, mode=RestoreMode.FILES_CLEAN)
    (h.site_root / "uploads.bin").write_bytes(b"\0" * 50_000)
    target, snapshot, h.free = _shared_free(h)
    with pytest.raises(InsufficientSpaceError) as caught:
        h.run()
    assert caught.value.needed == target + snapshot
    assert SNAPSHOT_FILESYSTEM in caught.value.filesystem
    assert h.snapshots == []
    assert h.log.calls == []


def test_a_snapshot_directory_on_another_filesystem_is_not_summed(
    tmp_path: Path, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The planted scope attack: devices differ, so the same free space fits."""
    h = _harness(tmp_path, archive, mode=RestoreMode.FILES_CLEAN)
    (h.site_root / "uploads.bin").write_bytes(b"\0" * 50_000)
    _, _, h.free = _shared_free(h)
    snapshots = h.snapshot_file.parent
    snapshots.mkdir()
    monkeypatch.setattr(
        restore_runner,
        "_device",
        lambda path: -1 if path == snapshots else _device(path),
    )
    h.run()
    assert h.snapshots == [BackupScope.FILES]


def test_skip_claims_no_snapshot_space(tmp_path: Path, archive: Path) -> None:
    h = _harness(
        tmp_path, archive, mode=RestoreMode.FILES_CLEAN, snapshot=SnapshotPolicy.SKIP
    )
    (h.site_root / "uploads.bin").write_bytes(b"\0" * 50_000)
    _, _, h.free = _shared_free(h)
    h.run()


# --------------------------------------------------------------------------
# snapshot_result — the post-publish check (AD-12, ADR-0022)
# --------------------------------------------------------------------------


def test_a_not_performed_result_reads_nothing_and_would_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(path: Path) -> object:
        raise AssertionError(path)

    monkeypatch.setattr(restore_runner, "verify_archive", refuse)
    result = snapshot_result(
        PurePosixPath(tmp_path / "absent.tar.gz"),
        scope=BackupScope.ALL,
        encrypted=False,
        size=0,
        performed=False,
    )
    assert result.performed is False
    assert result.integrity is None


def test_a_performed_result_is_verified_again(tmp_path: Path) -> None:
    path = write_v1(tmp_path)
    path.chmod(0o600)
    result = snapshot_result(
        PurePosixPath(path),
        scope=BackupScope.ALL,
        encrypted=False,
        size=path.stat().st_size,
        performed=True,
    )
    assert result.integrity is Integrity.VERIFIED
    assert result.performed is True


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_a_snapshot_readable_by_others_fails_and_is_left_in_place(
    tmp_path: Path, mode: int
) -> None:
    """BL-E003-076: the published file holds the Site's secrets; owner-only."""
    path = write_v1(tmp_path)
    path.chmod(mode)
    with pytest.raises(SnapshotFailedError, match=f"{mode:04o}") as caught:
        snapshot_result(
            PurePosixPath(path),
            scope=BackupScope.ALL,
            encrypted=False,
            size=path.stat().st_size,
            performed=True,
        )
    assert caught.value.path == str(path)
    assert path.exists()


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_an_owner_only_snapshot_passes(tmp_path: Path, mode: int) -> None:
    path = write_v1(tmp_path)
    path.chmod(mode)
    result = snapshot_result(
        PurePosixPath(path),
        scope=BackupScope.ALL,
        encrypted=False,
        size=path.stat().st_size,
        performed=True,
    )
    assert result.integrity is Integrity.VERIFIED


def test_a_performed_result_for_a_missing_file_fails(tmp_path: Path) -> None:
    missing = tmp_path / "gone.tar.gz"
    with pytest.raises(SnapshotFailedError) as caught:
        snapshot_result(
            PurePosixPath(missing),
            scope=BackupScope.FILES,
            encrypted=False,
            size=1,
            performed=True,
        )
    assert isinstance(caught.value.cause, ArchiveNotFoundError)


def test_a_structural_only_snapshot_fails_and_is_left_in_place(tmp_path: Path) -> None:
    path = write_v0(tmp_path)
    with pytest.raises(SnapshotFailedError, match="STRUCTURAL_ONLY"):
        snapshot_result(
            PurePosixPath(path),
            scope=BackupScope.ALL,
            encrypted=False,
            size=1,
            performed=True,
        )
    assert path.exists()


def test_a_damaged_snapshot_fails_and_is_left_in_place(tmp_path: Path) -> None:
    path = write_v1(tmp_path)
    body = path.read_bytes()
    path.write_bytes(body[: len(body) // 2])
    with pytest.raises(SnapshotFailedError, match="snapshot") as caught:
        snapshot_result(
            PurePosixPath(path),
            scope=BackupScope.ALL,
            encrypted=False,
            size=1,
            performed=True,
        )
    assert caught.value.path == str(path)
    assert path.exists()


# --------------------------------------------------------------------------
# BL-E003-038: config handling (ADV-7, ADV-8)
# --------------------------------------------------------------------------


def test_an_oversized_archive_config_is_refused_before_clean(
    tmp_path: Path, archive: Path
) -> None:
    """ADV-7: the 1 MiB limit is checked in PREFLIGHT, not inside REWRITE_CONFIG."""
    from l3io.wp.backup.adapters.restore_runner import CONFIG_MAX_BYTES
    from l3io.wp.backup.adapters.tar import verify_archive_view
    from l3io.wp.backup.domain.archive_format import config_member
    from l3io.wp.backup.domain.errors import MemberReadRefusedError

    view, _ = verify_archive_view(archive)
    assert view is not None
    member = config_member(view)
    assert member is not None
    big = dataclasses.replace(
        view,
        members=tuple(
            dataclasses.replace(m, size=CONFIG_MAX_BYTES + 1) if m == member else m
            for m in view.members
        ),
    )
    h = _harness(
        tmp_path,
        archive,
        mode=RestoreMode.FILES_CLEAN,
        config_policy=ConfigPolicy.OVERWRITE,
        view_override=big,
    )
    with pytest.raises(MemberReadRefusedError, match=str(CONFIG_MAX_BYTES)):
        h.run()
    assert not FILE_CALLS & set(h.log.methods())


def test_the_existence_probe_is_the_file_the_rewrite_acts_on(
    tmp_path: Path, archive: Path
) -> None:
    """ADV-8: a relocated config that does not exist is refused in PREFLIGHT."""
    from l3io.wp.backup.adapters.restore_runner import TargetConfigMissingError

    moved = tmp_path / "moved" / "wp-config.php"
    relocation = dataclasses.replace(
        NO_RELOCATION,
        config_file=PurePosixPath(moved),
        rewrite_values=(("WP_HOME", "https://example.invalid"),),
        undo=((P.REWRITE_CONFIG, "restore WP_HOME by hand"),),
    )
    h = _harness(tmp_path, archive, mode=RestoreMode.FILES, relocation=relocation)
    assert h.config_path.exists()
    with pytest.raises(TargetConfigMissingError) as caught:
        h.run()
    assert str(moved) in str(caught.value)
    assert not FILE_CALLS & set(h.log.methods())


# --------------------------------------------------------------------------
# BL-E002-069 (owner decision 2026-11-07): a refusal after CLEAN exits 8
# --------------------------------------------------------------------------


def _refuse_extract(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.restore_harness import RecordingSiteFiles

    def refuse(*_a: object, **_k: object) -> object:
        member, reason = "wp-content/evil", "a hard link"
        raise ExtractionRefusedError(member, reason)

    monkeypatch.setattr(RecordingSiteFiles, "extract", refuse)


def test_an_extraction_refusal_after_clean_is_verification_failed(
    tmp_path: Path, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from l3io.wp.backup.adapters.restore_runner import (
        ExtractionRefusedAfterCleanError,
    )

    h = _harness(tmp_path, archive, mode=RestoreMode.FILES_CLEAN)
    _refuse_extract(monkeypatch)
    with pytest.raises(ExtractionRefusedAfterCleanError) as caught:
        h.run()
    error = caught.value
    assert error.exit_code is ExitCode.VERIFICATION_FAILED
    assert error.member == "wp-content/evil"
    assert "WAS changed" in str(error)
    assert error.snapshot is not None
    assert error.snapshot in str(error)
    assert isinstance(error.__cause__, ExtractionRefusedError)


def test_an_extraction_refusal_without_clean_stays_refused(
    tmp_path: Path, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _harness(tmp_path, archive, mode=RestoreMode.FILES)
    _refuse_extract(monkeypatch)
    with pytest.raises(ExtractionRefusedError) as caught:
        h.run()
    assert caught.value.exit_code is ExitCode.REFUSED


# --------------------------------------------------------------------------
# Owner decision 72: a failed SEARCH_REPLACE calls the undo slot
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "orderly"),
    [(RestoreMode.DATABASE, True), (RestoreMode.ALL, False)],
    ids=["database", "all"],
)
def test_a_failed_url_rewrite_is_undone_before_its_error_propagates(
    tmp_path: Path, archive: Path, mode: RestoreMode, orderly: bool
) -> None:
    """The config was saved before ``REWRITE_CONFIG`` wrote it; the slot gets
    it to put back. Extracted files are not undone, so ``all`` stays
    interrupted."""
    failure = RuntimeError("wp search-replace exited 1")
    h = _harness(
        tmp_path,
        archive,
        mode=mode,
        relocation=RELOCATIONS["url-only"],
        search_replace_failure=failure,
    )
    with pytest.raises(RuntimeError) as caught:
        h.run()
    assert caught.value is failure
    assert h.undone == [("RuntimeError", False, h.config_path)]
    calls = [c[2] for c in h.log.calls]
    assert calls.index("save_copy") < calls.index("set_constants")
    assert calls.index("relocate_content") < calls.index("undo_relocation")
    assert ("finish_run" in h.journal.names()) is orderly


def test_an_interrupted_url_rewrite_calls_no_undo(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(
        tmp_path,
        archive,
        mode=RestoreMode.DATABASE,
        relocation=RELOCATIONS["url-only"],
        search_replace_failure=RunTerminated("SIGTERM"),
    )
    with pytest.raises(RunTerminated):
        h.run()
    assert h.undone == []


def test_a_relocation_without_urls_saves_no_config_copy(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(
        tmp_path,
        archive,
        mode=RestoreMode.DATABASE,
        relocation=RELOCATIONS["database-only"],
    )
    h.run()
    assert "save_copy" not in [c[2] for c in h.log.calls]
