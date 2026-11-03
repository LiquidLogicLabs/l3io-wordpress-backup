# ruff: noqa: ARG002 - each placeholder method raises before reading its arguments
"""ADR-0016/AD-4 — the constructors of an ``AdapterSet``, in the composition-root layer.

:func:`real_adapters` binds the real adapters. :func:`dry_run_adapters` is the
only other constructor: a Dry Run is adapter selection, never a conditional
(AD-4), so it reuses the **same** query instances and tracer as the real set
and substitutes only the command halves with their no-ops from
``adapters/noop.py``.

**Placeholders (E002-S02-012).** A port whose real implementation has not
landed is bound to a placeholder below whose every method raises
``UnboundAdapterError`` naming the port and its supplier — never a silent
no-op in a real Run: ``site_files_*`` and ``config_*`` until E002-S02-009,
``search_replace`` until Epic 7 supplies one, the journal pair until Epic 3
passes ``adapters/journal.py``'s. The restore-half database methods are
``WpDatabase``'s own stubs until E002-S02-009.

:func:`real_site_lock` binds the Site Lock (AD-19). It is Run machinery, not a
field of the ``AdapterSet``: the Run Context acquires it before any set is
bound, and a Dry Run takes it for real, so there is no Dry Run variant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, NoReturn

from l3io.wp.backup.adapters.database import WpDatabase
from l3io.wp.backup.adapters.lock import FcntlSiteLock
from l3io.wp.backup.adapters.noop import (
    ActionLog,
    NoopArchiveStore,
    NoopArchiveWriter,
    NoopConfigWriter,
    NoopDatabase,
    NoopRunJournal,
    NoopSearchReplace,
    NoopSiteFiles,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.store import LocalArchiveStore
from l3io.wp.backup.adapters.tar import (
    TarArchiveProbe,
    TarArchiveWriter,
    verify_or_raise,
)
from l3io.wp.backup.domain.archive_format import ArchiveView, ExtractResult, MemberRole
from l3io.wp.backup.domain.config import ConfigWriteResult, ConstantValue
from l3io.wp.backup.domain.errors import UnboundAdapterError
from l3io.wp.backup.domain.journal import JournalHeader, JournalRecord
from l3io.wp.backup.domain.restore import RestorePhase
from l3io.wp.backup.domain.search_replace import SearchReplaceReport, UrlRelocation
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.adapter_set import AdapterSet
from l3io.wp.backup.ports.database import DatabaseCommand
from l3io.wp.backup.ports.journal import RunJournalCommand, RunJournalQuery
from l3io.wp.backup.ports.lock import SiteLock
from l3io.wp.backup.ports.search_replace import SearchReplaceCommand
from l3io.wp.backup.ports.tracing import Tracer

if TYPE_CHECKING:
    from l3io.wp.database import CredentialSource

__all__ = [
    "UnboundConfig",
    "UnboundRunJournal",
    "UnboundSearchReplace",
    "UnboundSiteFiles",
    "dry_run_adapters",
    "real_adapters",
    "real_site_lock",
]

#: Who supplies each placeholder-bound port's real implementation.
SITE_FILES_SUPPLIER: Final = "E002-S02-009"
CONFIG_SUPPLIER: Final = "E002-S02-009"
SEARCH_REPLACE_SUPPLIER: Final = "Epic 7 (E007-S01-003)"
JOURNAL_SUPPLIER: Final = "Epic 3 (E003-S02-005)"


def _unbound(port: str, supplier: str) -> NoReturn:
    raise UnboundAdapterError(port, supplier=supplier)


class UnboundSiteFiles:
    """``SiteFilesQuery`` and ``SiteFilesCommand`` until E002-S02-009."""

    __slots__ = ()

    def is_empty(self, site_root: Path) -> bool:
        _unbound("SiteFilesQuery.is_empty", SITE_FILES_SUPPLIER)

    def entry_count(self, site_root: Path) -> int:
        _unbound("SiteFilesQuery.entry_count", SITE_FILES_SUPPLIER)

    def file_exists(self, path: Path) -> bool:
        _unbound("SiteFilesQuery.file_exists", SITE_FILES_SUPPLIER)

    def clean(self, site_root: Path, *, keep: frozenset[PurePosixPath]) -> int:
        _unbound("SiteFilesCommand.clean", SITE_FILES_SUPPLIER)

    def extract(
        self,
        source: StorageTarget,
        destination: Path,
        view: ArchiveView,
        *,
        roles: frozenset[MemberRole],
        exclude: frozenset[PurePosixPath] = frozenset(),
        payload: Path | None = None,
    ) -> ExtractResult:
        _unbound("SiteFilesCommand.extract", SITE_FILES_SUPPLIER)


class UnboundConfig:
    """``ConfigQuery`` and ``ConfigCommand`` until E002-S02-009."""

    __slots__ = ()

    def read_constants(
        self, config_file: Path, keys: Sequence[str]
    ) -> Mapping[str, ConstantValue]:
        _unbound("ConfigQuery.read_constants", CONFIG_SUPPLIER)

    def set_constants(
        self, config_file: Path, values: Mapping[str, str | int | bool]
    ) -> ConfigWriteResult:
        _unbound("ConfigCommand.set_constants", CONFIG_SUPPLIER)

    def save_copy(self, config_file: Path, destination: Path) -> ConfigWriteResult:
        _unbound("ConfigCommand.save_copy", CONFIG_SUPPLIER)

    def replace_file(
        self,
        config_file: Path,
        source: Path,
        *,
        keep: Mapping[str, ConstantValue],
    ) -> ConfigWriteResult:
        _unbound("ConfigCommand.replace_file", CONFIG_SUPPLIER)


class UnboundSearchReplace:
    """``SearchReplaceCommand`` until Epic 7 supplies one."""

    __slots__ = ()

    def relocate_content(
        self, site_root: Path, urls: UrlRelocation
    ) -> SearchReplaceReport:
        _unbound("SearchReplaceCommand.relocate_content", SEARCH_REPLACE_SUPPLIER)


class UnboundRunJournal:
    """``RunJournalQuery`` and ``RunJournalCommand`` until Epic 3 passes its pair."""

    __slots__ = ()

    def incomplete(self, site_root: PurePosixPath) -> JournalRecord | None:
        _unbound("RunJournalQuery.incomplete", JOURNAL_SUPPLIER)

    def begin_run(self, header: JournalHeader) -> None:
        _unbound("RunJournalCommand.begin_run", JOURNAL_SUPPLIER)

    def phase_started(self, phase: RestorePhase, target: str) -> None:
        _unbound("RunJournalCommand.phase_started", JOURNAL_SUPPLIER)

    def step_completed(self, phase: RestorePhase, step: str) -> None:
        _unbound("RunJournalCommand.step_completed", JOURNAL_SUPPLIER)

    def phase_completed(self, phase: RestorePhase) -> None:
        _unbound("RunJournalCommand.phase_completed", JOURNAL_SUPPLIER)

    def finish_run(self) -> None:
        _unbound("RunJournalCommand.finish_run", JOURNAL_SUPPLIER)

    def resolve(self, run_id: str, resolved_by: str) -> None:
        _unbound("RunJournalCommand.resolve", JOURNAL_SUPPLIER)


def real_adapters(
    *,
    wordpress_source: CredentialSource,
    secrets: SecretRegistry,
    tracer: Tracer,
    admin_source: CredentialSource | None = None,
    restore_wordpress_source: CredentialSource | None = None,
    search_replace: SearchReplaceCommand | None = None,
    journal: tuple[RunJournalQuery, RunJournalCommand] | None = None,
) -> AdapterSet:
    """The real set; a port not yet implemented is bound to its placeholder.

    One store for both store halves and one ``WpDatabase`` for both database
    halves. ``restore_database_*`` are that same instance unless
    ``restore_wordpress_source`` (a relocated WordPress account) is given, in
    which case they are a second ``WpDatabase`` bound to it and the same
    administrator; the Safety Snapshot keeps using ``database_*``. The
    credential sources are built by ``cli/credentials.py``, never here.
    """
    store = LocalArchiveStore(verify=verify_or_raise)
    database = WpDatabase(wordpress_source, secrets=secrets, admin=admin_source)
    restore_database = (
        database
        if restore_wordpress_source is None
        else WpDatabase(restore_wordpress_source, secrets=secrets, admin=admin_source)
    )
    site_files = UnboundSiteFiles()
    config = UnboundConfig()
    journal_query, journal_command = (
        (UnboundRunJournal(), UnboundRunJournal()) if journal is None else journal
    )
    return AdapterSet(
        archive_store_query=store,
        archive_store_command=store,
        archive_probe=TarArchiveProbe(),
        database_query=database,
        database_command=database,
        archive_writer=TarArchiveWriter(),
        tracer=tracer,
        restore_database_query=restore_database,
        restore_database_command=restore_database,
        site_files_query=site_files,
        site_files_command=site_files,
        config_query=config,
        config_command=config,
        search_replace=(
            UnboundSearchReplace() if search_replace is None else search_replace
        ),
        journal_query=journal_query,
        journal_command=journal_command,
    )


def _administrator_bound(command: DatabaseCommand) -> bool:
    """Whether the real database command has an administrator bound (AD-29).

    Read by attribute, not by ``isinstance``: a test may bind any
    ``DatabaseCommand``, and one that does not say is treated as having none,
    so its no-op refuses where an administrator is needed.
    """
    return getattr(command, "administrator_bound", False) is True


def dry_run_adapters(real: AdapterSet, *, log: ActionLog | None = None) -> AdapterSet:
    """``real`` with every command half replaced by its no-op (AD-4).

    Every other field — each query half, the probe and the tracer — is the
    very instance ``real`` holds. ``log`` receives the skipped calls; a fresh
    one when ``None``. Every field is named, not copied with
    ``dataclasses.replace``, so a field added to ``AdapterSet`` without a
    decision here fails mypy. Each ``NoopDatabase`` carries its real
    counterpart's administrator binding, so a Dry Run refuses where the real
    Run would.
    """
    skipped = ActionLog() if log is None else log
    return AdapterSet(
        archive_store_query=real.archive_store_query,
        archive_store_command=NoopArchiveStore(skipped),
        archive_probe=real.archive_probe,
        database_query=real.database_query,
        database_command=NoopDatabase(
            skipped, administrator=_administrator_bound(real.database_command)
        ),
        archive_writer=NoopArchiveWriter(skipped),
        tracer=real.tracer,
        restore_database_query=real.restore_database_query,
        restore_database_command=NoopDatabase(
            skipped,
            administrator=_administrator_bound(real.restore_database_command),
        ),
        site_files_query=real.site_files_query,
        site_files_command=NoopSiteFiles(skipped),
        config_query=real.config_query,
        config_command=NoopConfigWriter(skipped),
        search_replace=NoopSearchReplace(skipped),
        journal_query=real.journal_query,
        journal_command=NoopRunJournal(skipped),
    )


def real_site_lock() -> SiteLock:
    """The Site Lock over ``fcntl.flock`` in the per-user state directory."""
    return FcntlSiteLock()
