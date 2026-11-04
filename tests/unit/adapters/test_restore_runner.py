"""FR-6, AD-4, AD-17, ADR-0021/0022/0024, BL-E002-116 — the restore executor.

Driven over :mod:`tests.restore_harness`: every command half records into one
log tagged with the journal phase active at the time; the ``load_database``
slot and ``take_snapshot`` are fakes (``adapters/staging.py`` is
E003-S02-005's).
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.restore_runner import ArchiveChangedError, _same_dir
from l3io.wp.backup.core.restore import (
    CUTOVER_KEYS,
    NO_RELOCATION,
    ExpansionRatioError,
    InsufficientSpaceError,
    RestoreRelocation,
)
from l3io.wp.backup.domain.archive_format import Integrity, MemberRole
from l3io.wp.backup.domain.errors import (
    DatabaseRefusedError,
    ExitCode,
    ExtractionRefusedError,
    PassphraseRejectedError,
    PayloadDamagedError,
    VerificationFailedError,
)
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    RestoreMode,
    RestorePhase,
    SnapshotPolicy,
    clears_site_root,
    removes_database,
    restores_database,
    restores_files,
)
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
    assert ("restore_database.drop_database" in methods) is removes_database(mode)
    assert h.slot_calls == int(restores_database(mode))
    assert not {m for m in methods if m.startswith("database.")}
    # The phases that issued commands are exactly the plan's destructive ones.
    issued = {phase for phase, *_ in h.log.calls}
    assert issued == set(report.phases) - {P.PREFLIGHT, P.VERIFY}
    # One phase_completed per planned phase; the omitted ones emit nothing.
    emitted = [e["phase"] for e in logs if e["event"] == "phase_completed"]
    assert emitted == [p.value for p in report.phases]
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


def test_overwrite_reads_the_archives_config_into_the_workspace(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(
        tmp_path, archive, mode=RestoreMode.FILES, config_policy=ConfigPolicy.OVERWRITE
    )
    with capture_logs() as logs:
        h.run()
    copied = h.workspace / "archive-wp-config.php"
    assert ARCHIVE_DB["DB_NAME"] in copied.read_text(encoding="utf-8")
    assert copied.stat().st_mode & 0o777 == 0o600
    warning = next(e for e in logs if e["event"] == "wp_config_overwritten")
    assert warning["log_level"] == "warning"


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
    """The Archive's config reaches the workspace by ``extract``, not a raw write."""
    archive = write_v1(tmp_path, config_role=True)
    h = _harness(
        tmp_path, archive, mode=RestoreMode.FILES, config_policy=ConfigPolicy.OVERWRITE
    )
    h.run()
    laid = [c for c in h.log.calls if c[2] == "extract" and c[3][1] == ["config"]]
    assert [c[3][0] for c in laid] == [str(h.workspace / "archive-config")]


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
    cipher = IdentityCipher(failure=failure())
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
    assert [e["phase"] for e in failed] == ["load-dump"]


def test_an_orderly_load_failure_after_extract_is_not_orderly(
    tmp_path: Path, archive: Path
) -> None:
    h = _harness(
        tmp_path, archive, mode=RestoreMode.ALL, slot_failure=ORDERLY_CHECK_FAILURE
    )
    with pytest.raises(RuntimeError):
        h.run()
    assert "finish_run" not in h.journal.names()
