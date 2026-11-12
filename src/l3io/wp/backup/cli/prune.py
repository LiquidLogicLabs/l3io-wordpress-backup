"""Owner decision 83 — ``wpbackup2 prune --keep N``: retention of this Site's Archives.

**What it removes.** Only Archives in the Archive directory (``--archive-dir``,
else the directory a backup writes to by default) whose Manifest is this
Site's, by the one definition ``--archive latest`` uses
(``core/archive_choice.site_archives``): any name, ``wpbak-*`` and the legacy
``wpbackup2-*`` alike. The newest ``keep`` (at least 1) stay; the rest go.

**Never removed:** the newest Archive; a Safety Snapshot (its Manifest has an
``origin``); another site's Archive; a file whose Manifest cannot be read or
whose ``created_at`` does not parse; anything that is not a regular file (a
symlink is never followed); a staged ``.partial`` file (not a ``*.tar.gz``;
the stale-residue sweep owns those). An entry skipped for what it is logs
INFO ``archive_prune_skipped`` with the reason.

**How.** A Run, holding the Site Lock, so it never races a backup of the Site.
Each removal is one ``unlinkat`` after the file is checked to be the one read
(``adapters/archive_dir.delete_archive``) and logs INFO ``archive_pruned``
(``path``, ``bytes``, ``created_at``, ``performed``); ``prune_completed``
carries the totals. A Dry Run (``--whatIf``) lists the same and removes
nothing (``performed`` ``false``). A file that changed or could not be removed
logs WARNING ``archive_prune_failed`` and the rest go on; the command then
exits ``FAILED``.

Started as root, the CLI runs as the Site Root's owner, as a backup does
(owner decision 67); the library refuses to run as root (decision 68).
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import structlog

from l3io.wp.backup.adapters.archive_dir import delete_archive, list_archives
from l3io.wp.backup.adapters.database import (
    locate_wp_config,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli import privileges
from l3io.wp.backup.cli.adapter_sets import real_site_lock
from l3io.wp.backup.cli.backup import NO_ENVIRONMENT, default_archive_directory
from l3io.wp.backup.cli.context import RunContext, join_or_open
from l3io.wp.backup.cli.output import emit_result
from l3io.wp.backup.core.archive_choice import (
    ArchiveSite,
    LatestCandidate,
    choose_prune,
)
from l3io.wp.backup.domain.errors import (
    PruneFailedError,
    SiteRootNotFoundError,
    UsageError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.ports.lock import SiteLock
from l3io.wp.backup.ports.tracing import Tracer

__all__ = [
    "PruneRequest",
    "PruneResult",
    "PrunedArchive",
    "drive_prune",
    "run_prune",
    "summary_lines",
]

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PruneRequest:
    """What a prune needs: the Site, how many to keep, where to look."""

    site_root: Path
    keep: int
    archive_dir: Path | None = None
    wp_config: Path | None = None
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class PrunedArchive:
    """One Archive a prune removed, or in a Dry Run would remove."""

    path: PurePosixPath
    bytes: int
    created_at: str


@dataclass(frozen=True, slots=True)
class PruneResult:
    """What a prune did (owner decision 83).

    ``kept`` are this Site's Archives left, newest first; ``pruned`` those
    removed (in a Dry Run, those that would be: ``performed`` is ``False``),
    newest first; ``skipped`` the ``*.tar.gz`` entries that were no candidate
    for what they are (path, reason). ``considered`` is how many Archives
    were read, ``matched`` how many are this Site's.
    """

    directory: PurePosixPath
    keep: int
    kept: tuple[PurePosixPath, ...]
    pruned: tuple[PrunedArchive, ...]
    skipped: tuple[tuple[PurePosixPath, str], ...]
    considered: int
    matched: int
    performed: bool


def _directory(request: PruneRequest, site_root: Path) -> Path:
    if request.archive_dir is not None:
        directory = Path(request.archive_dir)
    else:
        directory = default_archive_directory(site_root)[0]
    if not directory.is_dir():
        msg = f"the archive directory {str(directory)!r} does not exist"
        raise UsageError(msg, arguments=("archive_dir",))
    return directory.resolve()


def drive_prune(
    request: PruneRequest,
    *,
    ctx: RunContext | None,
    tracer: Tracer | None = None,
    secrets: SecretRegistry | None = None,
    site_lock: SiteLock | None = None,
    environ: Mapping[str, str] = NO_ENVIRONMENT,
) -> PruneResult:
    """The prune driver: returns a :class:`PruneResult` or raises; never prints.

    ``environ`` resolves an environment-driven ``wp-config.php`` (the official
    image's ``getenv_docker()``), as the backup driver's does. Refused as root
    (the library never switches users; the CLI root has switched by now).
    """
    if request.keep < 1:
        msg = (
            f"keep must be at least 1, not {request.keep}: the newest archive is "
            "never pruned"
        )
        raise UsageError(msg, arguments=("keep",))
    privileges.require_not_root(request.site_root, operation="prune")
    try:
        site_root = Path(request.site_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SiteRootNotFoundError(str(Path(request.site_root).absolute())) from exc
    with join_or_open(
        ctx,
        operation="prune",
        tracer=tracer,
        secrets=secrets,
        dry_run=request.dry_run,
        site_root=site_root,
        site_lock=real_site_lock() if site_lock is None else site_lock,
    ) as handle:
        run = handle.ctx
        config = locate_wp_config(site_root, stated=request.wp_config)
        site, _ = site_descriptor_from_wp_config(
            site_root, config_file=config.path, environ=environ, secrets=run.secrets
        )
        directory = _directory(request, site_root)
        files, skipped = list_archives(directory)
        for entry in skipped:
            _log.info(
                "archive_prune_skipped", path=str(entry.path), reason=entry.reason
            )
        by_path = {f.path: f for f in files}
        choice = choose_prune(
            [LatestCandidate(path=f.path, manifest=f.manifest) for f in files],
            ArchiveSite.from_descriptor(site),
            keep=request.keep,
        )
        pruned: list[PrunedArchive] = []
        failures: list[tuple[str, str]] = []
        for chosen in choice.pruned:
            archive = by_path[chosen.path]
            reason = None if run.dry_run else delete_archive(archive)
            if reason is not None:
                _log.warning(
                    "archive_prune_failed", path=str(chosen.path), reason=reason
                )
                failures.append((str(chosen.path), reason))
                continue
            _log.info(
                "archive_pruned",
                path=str(chosen.path),
                bytes=archive.size,
                created_at=chosen.created_at,
                performed=not run.dry_run,
            )
            pruned.append(
                PrunedArchive(
                    path=chosen.path, bytes=archive.size, created_at=chosen.created_at
                )
            )
        result = PruneResult(
            directory=PurePosixPath(directory),
            keep=request.keep,
            kept=choice.kept,
            pruned=tuple(pruned),
            skipped=tuple((s.path, s.reason) for s in skipped),
            considered=choice.considered,
            matched=choice.matched,
            performed=not run.dry_run,
        )
        _log.info(
            "prune_completed",
            archive_dir=str(directory),
            keep=request.keep,
            kept=len(result.kept),
            pruned=len(result.pruned),
            bytes=sum(p.bytes for p in result.pruned),
            skipped=len(result.skipped),
            considered=result.considered,
            matched=result.matched,
            performed=result.performed,
        )
        if failures:
            raise PruneFailedError(failures=tuple(failures))
        return result


def summary_lines(result: PruneResult) -> list[str]:
    """What ``prune`` prints on stdout: each Archive removed, then the totals."""
    verb = "pruned" if result.performed else "would prune"
    lines = [
        f"{verb} {p.path} ({p.bytes} bytes, created {p.created_at})"
        for p in result.pruned
    ]
    total = sum(p.bytes for p in result.pruned)
    lines.append(
        f"{verb} {len(result.pruned)} of this site's {result.matched} archives "
        f"in {result.directory} ({total} bytes); kept the newest {len(result.kept)} "
        f"(--keep {result.keep})"
    )
    return lines


def run_prune(args: argparse.Namespace, *, secrets: SecretRegistry) -> ExitCode:
    """``wpbackup2 prune``: ``ExitCode.OK``, or raises.

    Started as root, a root-owned Site Root is refused first; then the
    process switches to the Site Root's owner, who must be able to write the
    Archive directory, before the Run opens (owner decision 67's rule).
    """
    owner = privileges.owner_to_run_as(Path(args.wp_dir), operation="prune")
    request = PruneRequest(
        site_root=Path(args.wp_dir),
        keep=args.keep,
        archive_dir=None if args.archive_dir is None else Path(args.archive_dir),
        wp_config=None if args.wp_config is None else Path(args.wp_config),
        dry_run=bool(args.what_if),
    )
    if owner is not None:
        privileges.switch_to(owner, environ=os.environ, operation="prune")
        site_root = Path(request.site_root).resolve()
        privileges.require_writable(_directory(request, site_root), owner)
    result = drive_prune(request, ctx=None, secrets=secrets, environ=os.environ)
    emit_result("\n".join(summary_lines(result)))
    return ExitCode.OK
