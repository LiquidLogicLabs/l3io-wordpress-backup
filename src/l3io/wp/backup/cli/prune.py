"""Owner decisions 83, 84 — ``wpbackup2 prune``: retention of this Site's Archives.

**What it removes.** Only Archives under the Archive directory (``--archive-dir``,
else the directory a backup writes to by default), flat or in subfolders
(owner decision 86; ``adapters/archive_dir.list_archives``' bounded walk),
whose Manifest is this
Site's, by the one definition ``--archive latest`` uses
(``core/archive_choice.site_archives``): any name, ``wpbak-*`` and the legacy
``wpbackup2-*`` alike. What the retention rules keep stays and the rest go:
``--keep N`` (the newest N) and the tiers ``--keep-daily``, ``--keep-weekly``,
``--keep-monthly``, ``--keep-yearly`` (the newest of each of the last N UTC
days, ISO weeks, months, years that have one; ``core/retention.py``). At
least one rule is given, each at least 1; an Archive kept by any rule stays.

**Never removed:** the newest Archive; a Safety Snapshot (its Manifest has an
``origin``); another site's Archive; a file whose Manifest cannot be read or
whose ``created_at`` does not parse; anything that is not a regular file (a
symlink is never followed); a staged ``.partial`` file (not a ``*.tar.gz``;
the stale-residue sweep owns those). An entry skipped for what it is logs
INFO ``archive_prune_skipped`` with the reason.

**How.** A Run, holding the Site Lock, so it never races a backup of the Site.
Each removal is one ``unlinkat`` after the file is checked to be the one read
(``adapters/archive_dir.delete_archive``) and logs INFO ``archive_pruned``
(``path``, ``bytes``, ``created_at``, ``performed``, and ``reasons``: why
each rule lets it go); each Archive kept logs DEBUG ``archive_kept``
(``path``, ``created_at``, ``reasons``: the rules that keep it).
``prune_completed`` carries the totals, the ``rules`` and both lists. A Dry
Run (``--whatIf``) lists the same and removes nothing (``performed``
``false``). A file that changed or could not be removed
logs WARNING ``archive_prune_failed`` and the rest go on; the command then
exits ``FAILED``.

**Emptied folders (owner decision 86).** After the removals, each folder on
the path from a removed Archive up to the Archive directory that is now empty
is removed, deepest first, logging INFO ``archive_dir_removed``
(``adapters/archive_dir.remove_emptied_directories``). Never the Archive
directory itself, a folder still holding anything, a symlink, or a folder no
removed Archive was in. A Dry Run removes none.

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

from l3io.wp.backup.adapters.archive_dir import (
    delete_archive,
    list_archives,
    remove_emptied_directories,
)
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
from l3io.wp.backup.core.retention import RetentionPolicy
from l3io.wp.backup.domain.errors import (
    PruneFailedError,
    SiteRootNotFoundError,
    UsageError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.ports.lock import SiteLock
from l3io.wp.backup.ports.tracing import Tracer

__all__ = [
    "KeptArchive",
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
    """What a prune needs: the Site, what to keep, where to look."""

    site_root: Path
    policy: RetentionPolicy
    archive_dir: Path | None = None
    wp_config: Path | None = None
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class PrunedArchive:
    """One Archive a prune removed, or in a Dry Run would remove.

    ``reasons``: for each rule given, why it does not keep this Archive
    (``"daily: not the newest of its day"``).
    """

    path: PurePosixPath
    bytes: int
    created_at: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KeptArchive:
    """One of this Site's Archives a prune left, and the rules that keep it.

    ``reasons`` name the rules: ``"newest"`` (``--keep``), ``"daily"``,
    ``"weekly"``, ``"monthly"``, ``"yearly"``.
    """

    path: PurePosixPath
    created_at: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PruneResult:
    """What a prune did (owner decisions 83, 84).

    ``policy`` is the rules applied. ``kept`` are this Site's Archives left,
    newest first, each with the rules that keep it; ``pruned`` those removed
    (in a Dry Run, those that would be: ``performed`` is ``False``), newest
    first, each with why; ``skipped`` the ``*.tar.gz`` entries that were no candidate
    for what they are (path, reason). ``considered`` is how many Archives
    were read, ``matched`` how many are this Site's. ``removed_directories``
    are the folders under the Archive directory the removals emptied and the
    prune removed, deepest first (owner decision 86; none in a Dry Run).
    """

    directory: PurePosixPath
    policy: RetentionPolicy
    kept: tuple[KeptArchive, ...]
    pruned: tuple[PrunedArchive, ...]
    skipped: tuple[tuple[PurePosixPath, str], ...]
    considered: int
    matched: int
    performed: bool
    removed_directories: tuple[PurePosixPath, ...] = ()


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
    The request's :class:`RetentionPolicy` refused a bad rule when it was made.
    """
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
            policy=request.policy,
        )
        for kept in choice.kept:
            _log.debug(
                "archive_kept",
                path=str(kept.path),
                created_at=kept.created_at,
                reasons=list(kept.reasons),
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
                reasons=list(chosen.reasons),
            )
            pruned.append(
                PrunedArchive(
                    path=chosen.path,
                    bytes=archive.size,
                    created_at=chosen.created_at,
                    reasons=chosen.reasons,
                )
            )
        emptied = (
            ()
            if run.dry_run
            else remove_emptied_directories(directory, [p.path for p in pruned])
        )
        result = PruneResult(
            directory=PurePosixPath(directory),
            policy=request.policy,
            kept=tuple(
                KeptArchive(path=k.path, created_at=k.created_at, reasons=k.reasons)
                for k in choice.kept
            ),
            pruned=tuple(pruned),
            skipped=tuple((s.path, s.reason) for s in skipped),
            considered=choice.considered,
            matched=choice.matched,
            performed=not run.dry_run,
            removed_directories=tuple(PurePosixPath(d) for d in emptied),
        )
        _log.info(
            "prune_completed",
            archive_dir=str(directory),
            rules=request.policy.settings(),
            kept=len(result.kept),
            pruned=len(result.pruned),
            bytes=sum(p.bytes for p in result.pruned),
            skipped=len(result.skipped),
            considered=result.considered,
            matched=result.matched,
            performed=result.performed,
            removed_directories=[str(d) for d in result.removed_directories],
            kept_archives=[
                {
                    "path": str(k.path),
                    "created_at": k.created_at,
                    "reasons": [*k.reasons],
                }
                for k in result.kept
            ],
            pruned_archives=[
                {
                    "path": str(p.path),
                    "created_at": p.created_at,
                    "reasons": [*p.reasons],
                }
                for p in result.pruned
            ],
        )
        if failures:
            raise PruneFailedError(failures=tuple(failures))
        return result


def summary_lines(result: PruneResult) -> list[str]:
    """What ``prune`` prints on stdout: each Archive kept and removed, then totals.

    ``kept <path> (created <t>): daily, weekly`` for each kept, newest first;
    ``pruned <path> (<n> bytes, created <t>): <why>; <why>`` for each removed
    (``would prune`` in a Dry Run); ``removed empty folder <path>`` for each
    folder the removals emptied; then one line of totals and the rules.
    """
    verb = "pruned" if result.performed else "would prune"
    lines = [
        f"kept {k.path} (created {k.created_at}): {', '.join(k.reasons)}"
        for k in result.kept
    ]
    lines += [
        f"{verb} {p.path} ({p.bytes} bytes, created {p.created_at}): "
        f"{'; '.join(p.reasons)}"
        for p in result.pruned
    ]
    lines += [f"removed empty folder {d}" for d in result.removed_directories]
    total = sum(p.bytes for p in result.pruned)
    lines.append(
        f"{verb} {len(result.pruned)} of this site's {result.matched} archives "
        f"in {result.directory} ({total} bytes); kept {len(result.kept)} "
        f"({result.policy.describe()})"
    )
    return lines


def run_prune(args: argparse.Namespace, *, secrets: SecretRegistry) -> ExitCode:
    """``wpbackup2 prune``: ``ExitCode.OK``, or raises.

    Started as root, a root-owned Site Root is refused first; then the
    process switches to the Site Root's owner, who must be able to write the
    Archive directory, before the Run opens (owner decision 67's rule).
    """
    # A count below 1 is refused whatever its spelling: a typed one by the
    # parser's ``type``, a variable's or a key's (an ``int``) by the policy.
    policy = RetentionPolicy(
        keep=args.keep,
        keep_daily=args.keep_daily,
        keep_weekly=args.keep_weekly,
        keep_monthly=args.keep_monthly,
        keep_yearly=args.keep_yearly,
    )
    owner = privileges.owner_to_run_as(Path(args.wp_dir), operation="prune")
    request = PruneRequest(
        site_root=Path(args.wp_dir),
        policy=policy,
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
