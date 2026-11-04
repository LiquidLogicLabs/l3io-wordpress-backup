"""NFR-1/AD-18 — a restore SIGKILLed at every ordinal leaves the Site recoverable.

E003-S02-005's kill harness. The restore is ``execute_restore`` with the real
``load_staged``/``check_staged`` slots over the real adapters, in a
``multiprocessing`` **spawn** child (``live_restore.child_main``) — not the
library entry point and not ``drive_restore``. The child's journal command,
``SiteFilesCommand``, ``ConfigCommand`` and restore database command signal an
ordinal to the parent after each durable write or effect lands and before it
returns, and block; the parent SIGKILLs the child at ordinal *k*. So *k* also
falls between an effect and the journal entry recording it.

**Scope, derived.** *k* runs over every ordinal of a first, uninterrupted
recording run of each scenario — an ``all-clean`` restore, one with a
four-key relocation (same server, new user, new name: CI has one listener),
and one into a target with no ``wp-config.php`` (a lay-down) — asserted
non-empty and to include the cutover writes, the loser drop and, for the
lay-down, ``"config_started"``. A phase or effect added later is covered
without editing this file.

**Per k:** ``wp-config.php`` parses and names the original database, or —
only after the cutover write — the load database, with all four keys at
their original values before it; the original database's ``CHECKSUM TABLE``
is unchanged before it; the next Run's query (this process, after the child
died) raises ``IncompleteRunError`` naming the Run and the phase; the undo
text equals the undo table's row for the phase and step the parent saw (and
starts with no other row), names the load database to drop only before the
cutover began and the ``"cutover_started"`` row inside a write; written/pending
keys match the journal; ``complete_cutover`` and ``resolve`` then leave every
key at its new value on a WordPress database, and a further restore succeeds.
``credential_sentinel`` (the live and the relocated password) must find no
leak in this process's logs or the refusal after resolution and after the
further restore.

Needs the CI ``db`` service (``DB_HOST``/``DB_PASSWORD``); skips without it.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.journal import refuse_incomplete
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.staging import complete_cutover
from l3io.wp.backup.core.restore import (
    CUTOVER_KEYS,
    NO_RELOCATION,
    STEP_CONFIG,
    STEP_CONFIG_STARTED,
    STEP_CUTOVER,
    STEP_CUTOVER_STARTED,
    STEP_LOSER_DROP_STARTED,
    STEP_LOSER_DROPPED,
    IncompleteRunError,
    RestoreRelocation,
    load_names,
    planned_cutover_keys,
    undo_for,
)
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase
from l3io.wp.backup.ports.database import DatabaseCommand
from tests.conftest import CredentialSentinel
from tests.integration.live_restore import (
    NOT_SIGNALLED,
    SIGNALLED,
    WRAPPED,
    LiveSite,
    RestoreSpec,
    adapters_for,
    build_archive,
    config_text,
    drive,
    host,
    journal_for,
    make_site,
    ports_for,
    protocol_members,
    read_keys,
    restore,
    sha,
    signalled,
    spec_for,
    survey_named,
)
from tests.staging_fakes import FILE_PHASES, assert_one_row

needs_database = pytest.mark.skipif(
    "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
)

JOURNAL_STEP = "journal.step_completed:"
JOURNAL_PHASE = "journal.phase_started:"
JOURNAL_PHASE_DONE = "journal.phase_completed:"
CUTOVER_WRITE = "config.set_constants:"


@dataclass(frozen=True)
class Scenario:
    name: str
    relocated: bool = False
    lay_down: bool = False


SCENARIOS = [
    Scenario("all-clean"),
    Scenario("relocated", relocated=True),
    Scenario("lay-down", lay_down=True),
]


def _setup(
    base: Path, scenario: Scenario, user: str, s: CredentialSentinel
) -> tuple[LiveSite, RestoreSpec]:
    password = s.password
    site = make_site(base, user=user, password=password)
    (site.site_root / "wp-includes").mkdir()
    archive_config = None
    relocation: RestoreRelocation = NO_RELOCATION
    new_user = new_password = None
    if scenario.lay_down:
        archive_config = config_text(
            name=site.database, user=user, password=password, db_host=host()
        )
        site.config.unlink()
        relocation = dataclasses.replace(NO_RELOCATION, lay_down_config=True)
    if scenario.relocated:
        new_user = f"wpn_{uuid.uuid4().hex[:10]}"
        new_password = s.relocated_password
        site.extra_users.append(new_user)
        relocation = dataclasses.replace(
            NO_RELOCATION,
            load_database=f"{site.database}_new",
            cutover_host=host(),
            cutover_user=new_user,
            cutover_password=True,
        )
    archive = build_archive(base, config=archive_config)
    spec = spec_for(
        site,
        archive,
        RestoreMode.ALL_CLEAN,
        relocation=relocation,
        relocated_user=new_user,
        relocated_password=new_password,
    )
    return site, spec


def _ordinal(labels: list[str], prefix: str) -> int:
    """The 1-based ordinal of the first label starting with ``prefix``."""
    return next(
        i for i, label in enumerate(labels, start=1) if label.startswith(prefix)
    )


def _targets(site: LiveSite, spec: RestoreSpec) -> dict[str, str]:
    load, _ = load_names(site.database, spec.run_id, spec.relocation)
    targets = {"DB_NAME": load}
    if spec.relocated_user is not None and spec.relocated_password is not None:
        targets |= {
            "DB_HOST": host(),
            "DB_USER": spec.relocated_user,
            "DB_PASSWORD": spec.relocated_password,
        }
    return targets


def _hashed(values: dict[str, object]) -> dict[str, str]:
    return {k: sha(v) for k, v in values.items()}


def _reached(done: list[str]) -> tuple[RestorePhase | None, str | None, bool]:
    """From the ordinals the parent saw: the interrupted phase, its last step,
    and whether a file phase started — independent of the journal file."""
    open_phase: RestorePhase | None = None
    closed: RestorePhase | None = None
    steps: list[tuple[RestorePhase | None, str]] = []
    files = False
    for label in done:
        if label.startswith(JOURNAL_PHASE):
            open_phase = RestorePhase(label.removeprefix(JOURNAL_PHASE))
            files = files or open_phase in FILE_PHASES
        elif label.startswith(JOURNAL_PHASE_DONE):
            closed, open_phase = (
                RestorePhase(label.removeprefix(JOURNAL_PHASE_DONE)),
                None,
            )
        elif label.startswith(JOURNAL_STEP):
            steps.append((open_phase, label.removeprefix(JOURNAL_STEP)))
    phase = open_phase if open_phase is not None else closed
    of_phase = [step for owner, step in steps if phase is not None and owner is phase]
    return phase, (of_phase[-1] if of_phase else None), files


def _check_kill(
    base: Path,
    scenario: Scenario,
    k: int,
    recorded: list[str],
    s: CredentialSentinel,
    user: str,
) -> None:
    site, spec = _setup(base, scenario, user, s)
    try:
        prior = _hashed(read_keys(site.config)) if site.config.exists() else None
        tables = site.checksums()
        driven = drive(spec, kill_at=k)
        done = recorded[:k]
        assert driven.labels == done
        cut_write = _ordinal(recorded, CUTOVER_WRITE)
        before_cutover = k < cut_write
        targets = _targets(site, spec)
        load = targets["DB_NAME"]

        # wp-config.php: parses, and names the original or (later) the load db.
        if site.config.exists():
            keys = read_keys(site.config)
            assert keys["DB_NAME"] in {site.database, load}
            if before_cutover:
                assert keys["DB_NAME"] == site.database
                if prior is not None:
                    assert _hashed(keys) == prior
        else:
            assert scenario.lay_down
            assert JOURNAL_STEP + STEP_CONFIG not in done
        if before_cutover:
            assert site.checksums() == tables

        # The next Run refuses, naming the Run and the phase.
        journal = journal_for(site.site_root)
        with pytest.raises(IncompleteRunError) as caught:
            refuse_incomplete(journal, PurePosixPath(site.site_root))
        error = caught.value
        assert error.run_id == spec.run_id
        phases = [
            label.removeprefix(JOURNAL_PHASE)
            for label in done
            if label.startswith(JOURNAL_PHASE)
        ]
        assert error.interrupted_phase is (RestorePhase(phases[-1]) if phases else None)
        s.add_exception(f"k{k} IncompleteRunError", error)

        # The undo text: exactly the table's row for what ``done`` reached.
        record = journal.incomplete(PurePosixPath(site.site_root))
        assert record is not None
        phase, step, files = _reached(done)
        assert error.interrupted_phase is phase
        assert error.undo == undo_for(phase, step, record.header, files_changed=files)
        assert_one_row(error.undo, phase, step, record.header, files_changed=files)
        steps = [
            label.removeprefix(JOURNAL_STEP)
            for label in done
            if label.startswith(JOURNAL_STEP)
        ]
        journal_labels = [label for label in done if label.startswith("journal.")]
        begun = any(step.startswith(STEP_CUTOVER_STARTED) for step in steps)
        in_write = journal_labels[-1].startswith(JOURNAL_STEP + STEP_CUTOVER_STARTED)
        loading = error.interrupted_phase is RestorePhase.LOAD_DUMP
        if loading and not begun:
            assert load in error.undo
        if begun:
            assert "drop staging database" not in error.undo
            assert f"drop {load}" not in error.undo.replace("not drop", "")
        if loading and in_write:
            assert "the cutover was writing" in error.undo
        keys_planned = planned_cutover_keys(spec.relocation)
        last = steps[-1] if steps and loading else ""
        inside = last.startswith((STEP_CUTOVER, STEP_CUTOVER_STARTED))
        if last.startswith(STEP_CUTOVER) and last != STEP_CUTOVER + keys_planned[-1]:
            assert "to complete the set" in error.undo
        if inside:
            written = tuple(
                key for key in keys_planned if JOURNAL_STEP + STEP_CUTOVER + key in done
            )
            assert error.written_keys == written
            assert error.pending_keys == tuple(
                key for key in keys_planned if key not in written
            )

        # Resolution completes an interrupted set, then acknowledges the Run.
        adapters, _ = adapters_for(spec, SecretRegistry())
        complete_cutover(record, ports=ports_for(spec, adapters))
        journal.resolve(spec.run_id, "kill-harness")
        s.assert_clean()
        if inside:
            landed = _hashed(read_keys(site.config))
            assert {key: landed[key] for key in targets} == _hashed(dict(targets))
            surveyed = survey_named(site.config, user=user, password=s.password)
            assert surveyed.is_wordpress is True  # type: ignore[attr-defined]
        refuse_incomplete(journal_for(site.site_root), PurePosixPath(site.site_root))

        # And a further restore succeeds.
        relocation = NO_RELOCATION
        if not site.config.exists():
            relocation = dataclasses.replace(NO_RELOCATION, lay_down_config=True)
        further = spec_for(
            site, Path(spec.archive), RestoreMode.ALL, relocation=relocation
        )
        report = restore(further)
        assert report.database is not None
        assert report.database.restored is True
        s.assert_clean()
        refuse_incomplete(journal_for(site.site_root), PurePosixPath(site.site_root))
    finally:
        site.teardown()


@pytest.mark.integration
@needs_database
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_a_restore_killed_at_every_ordinal_is_recoverable(
    scenario: Scenario,
    tmp_path: Path,
    credential_sentinel: CredentialSentinel,
    live_account: str,
    subtests: pytest.Subtests,
) -> None:
    site, spec = _setup(
        tmp_path / "record", scenario, live_account, credential_sentinel
    )
    try:
        recorded = drive(spec, kill_at=None)
    finally:
        site.teardown()
    assert recorded.outcome == "done:", recorded
    labels = recorded.labels
    assert labels
    steps = {
        label.removeprefix(JOURNAL_STEP)
        for label in labels
        if label.startswith(JOURNAL_STEP)
    }
    keys = planned_cutover_keys(spec.relocation)
    assert {STEP_CUTOVER_STARTED + key for key in keys} <= steps
    assert {STEP_CUTOVER + key for key in keys} <= steps
    assert {STEP_LOSER_DROP_STARTED, STEP_LOSER_DROPPED} <= steps
    assert sum(label.startswith(CUTOVER_WRITE) for label in labels) == len(keys)
    assert "database.drop_database:" in labels
    if scenario.relocated:
        assert keys == CUTOVER_KEYS
    if scenario.lay_down:
        assert STEP_CONFIG_STARTED in steps

    for k in range(1, len(labels) + 1):
        with subtests.test(k=k, label=labels[k - 1]):
            _check_kill(
                tmp_path / f"k{k}",
                scenario,
                k,
                labels,
                credential_sentinel,
                live_account,
            )


# --------------------------------------------------------------------------
# The kill points' scope: derived from the command Protocols (no database)
# --------------------------------------------------------------------------


def test_every_wrapped_protocol_member_is_signalled_or_excluded() -> None:
    for label, protocol in WRAPPED.items():
        members = protocol_members(protocol)
        assert members, label
        assert SIGNALLED[label] | NOT_SIGNALLED[label] == members, label
        assert not SIGNALLED[label] & NOT_SIGNALLED[label], label


def test_a_member_added_to_a_protocol_becomes_a_kill_point() -> None:
    """Attacks the scope: a new command method, here a grant, is signalled."""

    class GrowingDatabase(DatabaseCommand, Protocol):
        def grant(self, location: object) -> bool: ...

    derived = signalled({**WRAPPED, "database": GrowingDatabase}, NOT_SIGNALLED)
    assert "grant" in derived["database"]
    assert derived["database"] - {"grant"} == SIGNALLED["database"]


def test_a_stale_exclusion_is_refused() -> None:
    stale = {**NOT_SIGNALLED, "config": frozenset({"rename_file"})}
    with pytest.raises(AssertionError, match="rename_file"):
        signalled(WRAPPED, stale)
