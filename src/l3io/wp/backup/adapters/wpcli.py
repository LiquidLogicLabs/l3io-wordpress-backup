"""FR-11, AD-17, ADR-0015 — Epic 7's restore extension.

:func:`restore_extension` is the factory ``cli/restore.py`` imports as
``RESTORE_EXTENSION`` (the one-line ownership grant). It returns a
:class:`WpCliRestoreExtension`, which satisfies ``cli/restore.py``'s
``RestoreExtension`` Protocol structurally:

* :meth:`~WpCliRestoreExtension.resolve` validates the relocation flags
  (``core/relocation.py``) and fills ``core/restore.py``'s
  ``RestoreRelocation``: the URL keys ``REWRITE_CONFIG`` writes, the
  ``DB_*`` values ``LOAD_DUMP``'s cutover writes with the relocated load
  location, and where ``wp-config.php`` is laid down when the target has
  none (FR-29, ``core/config_location.py``; E007-S01-002);
* :meth:`~WpCliRestoreExtension.wordpress_spec` hands the relocated account to
  the composition root as an ``ExplicitCredentialSpec``; the root registers
  and resolves it. No ``l3io.wp.database`` type is named here;
* :meth:`~WpCliRestoreExtension.search_replace` returns
  :class:`WpCliSearchReplace` (E007-S01-003) when a URL is relocated and
  ``wp`` is on ``PATH``; :meth:`~WpCliRestoreExtension.required_tools`
  returns ``core/relocation.py::required_tools`` (E007-S01-004): WP-CLI at
  ``WP_CLI_MINIMUM`` iff a URL is relocated, which the root appends to the
  plan so ``PREFLIGHT`` refuses (exit 7) before the first effect.

:class:`WpCliSearchReplace` is the one place this package runs ``wp``. Every
call:

* runs from a fresh ``0700`` directory under the Site's per-user state
  directory (``adapters/journal.py::journal_directory``, ADR-0021), never
  ``$TMPDIR`` or the archive-supplied Site Root, after refusing
  (:class:`~l3io.wp.backup.domain.errors.WpCliConfigFoundError`) if that
  directory or any ancestor holds a ``wp-cli.yml``/``wp-cli.local.yml``,
  which WP-CLI reads from its working directory upwards and which can
  ``require:`` PHP;
* gets an explicit minimal environment (:func:`wp_cli_environment`):
  :data:`WP_CLI_ENV_KEYS` plus ``WP_CLI_CONFIG_PATH`` naming an empty file in
  that directory, so no global ``config.yml`` is read either. Nothing else is
  inherited: not the passphrase, not any credential variable (BL-E002-117);
* passes ``--skip-plugins --skip-themes --skip-packages``. What still runs
  is ``wp-config.php``, the restored mu-plugins and drop-ins such as
  ``wp-content/db.php``: WP-CLI bootstraps WordPress, and these load
  regardless (``docs/restore.md``).

As root it adds ``--allow-root`` and logs WARNING ``wp_cli_running_as_root``;
it never refuses (owner decision O4, 2026-11-06).

Every call's output is bounded (:data:`CHILD_OUTPUT_LIMIT` per stream, the
process group killed past it, BL-E007-009). Before any effect, when a URL is
relocated, :meth:`~WpCliRestoreExtension.resolve` prepares the state
directory and refuses a ``wp-cli.yml`` above it (BL-E007-008); the check in
every call stays as the backstop.

**Residual (stated, not closed).** ``restore_extension`` resolves ``wp`` on
``PATH`` once, with ``adapters/preflight.py::locate_tool`` (absolute entries
only); the roots' ``RootPreflight`` routes ``wp`` to ``BinaryPreflight``,
which looks it up again by name with that same function, on the same
``PATH`` in the same process, so the binary checked is the binary run
(BL-E007-016). A ``PATH`` entry changing between the
two lookups is not detected. ``wp`` disappearing after the preflight surfaces
as ``WpCliFailedError`` at ``SEARCH_REPLACE``, journaled incomplete.

The factory is annotated with ``core/relocation.py::RelocationRequest``,
because ``adapters/`` may not import ``cli/`` where ``RelocationFlags`` lives.
"""

from __future__ import annotations

import contextlib
import csv
import ctypes
import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Final, NoReturn, Protocol

import structlog

from l3io.wp.backup.adapters.database import database_location_from_db_host
from l3io.wp.backup.adapters.journal import journal_directory
from l3io.wp.backup.adapters.preflight import (
    WP_CLI_CONFIG_FILES,
    ChildOutputLimitError,
    find_wp_cli_config,
    locate_tool,
    run_bounded,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.restore_runner import config_member
from l3io.wp.backup.adapters.workspace import private_directories
from l3io.wp.backup.core.config_location import (
    ConfigFacts,
    ConfigPlacement,
    config_file,
    existing_location,
    manifest_location,
    place_config,
)
from l3io.wp.backup.core.relocation import (
    RelocationRequest,
    nested,
    pairs_merged,
    relocation_actions,
    relocation_undo,
    replacement_pairs,
    required_tools,
    rewrite_values,
    safe_order,
    stored_url_rule_broken,
    url_relocation,
    validate_relocation,
    validate_relocation_values,
)
from l3io.wp.backup.core.restore import (
    RestoreRelocation,
    planned_cutover_keys,
)
from l3io.wp.backup.domain.archive_format import (
    ArchiveView,
    ConfigLocation,
    MemberRole,
)
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.errors import (
    ConfigLocationConflictError,
    SearchReplaceIncompleteError,
    StoredUrlInvalidError,
    StoredUrlMissingError,
    UsageError,
    WpCliConfigFoundError,
    WpCliFailedError,
    WpCliOutputUnrecognisedError,
)
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase, restores_files
from l3io.wp.backup.domain.search_replace import (
    Replacement,
    SearchReplaceReport,
    StoredUrls,
    UrlRelocation,
)
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.ports.search_replace import SearchReplaceCommand
from l3io.wp.backup.ports.site_files import SiteFilesQuery

__all__ = [
    "WP_CLI_CONFIG_FILES",
    "WP_CLI_ENV_KEYS",
    "WpCliRestoreExtension",
    "WpCliSearchReplace",
    "find_wp_cli_config",
    "restore_extension",
    "search_replace_argv",
    "wp_cli_environment",
]

_log = structlog.get_logger(__name__)

#: How the relocated account's source is labelled in records (never a value).
RELOCATED_CREDENTIAL_SOURCE = "--new-db-user/--new-db-password"

#: The process-environment keys a ``wp`` child gets, besides
#: ``WP_CLI_CONFIG_PATH``: ``PATH`` and ``HOME`` copied, ``LC_ALL`` set to
#: ``C``. The same keys as ``adapters/preflight.py``'s probe environment.
WP_CLI_ENV_KEYS: Final = ("PATH", "HOME", "LC_ALL")
#: The flags every ``wp`` call carries after ``--path``.
_GLOBAL_FLAGS: Final = (
    "--skip-plugins",
    "--skip-themes",
    "--skip-packages",
    "--no-color",
)
#: The flags every ``search-replace`` call carries after its two URLs.
_PAIR_FLAGS: Final = (
    "--precise",
    "--all-tables-with-prefix",
    "--skip-columns=guid",
    "--report-changed-only",
)
#: The header of ``search-replace --dry-run``'s table, tab-separated off a TTY.
_PREVIEW_HEADER: Final = ("Table", "Column", "Replacements", "Type")
#: How many trailing stderr lines a ``WpCliFailedError`` carries.
_STDERR_TAIL_LINES: Final = 20
#: The steps that run after a replacement was (or may have been) made.
_AFTER_A_WRITE: Final = frozenset({"replace", "recount"})
#: The prefix ``wp cli version`` prints before the version number.
_VERSION_PREFIX: Final = "WP-CLI "
#: The empty global config file ``WP_CLI_CONFIG_PATH`` names.
_EMPTY_CONFIG: Final = "config.yml"
#: The file ``wp-load.php`` looks for, and the one that marks an install.
_CONFIG: Final = "wp-config.php"
_SETTINGS: Final = "wp-settings.php"


def wp_cli_environment(config_path: Path) -> dict[str, str]:
    """A ``wp`` child's whole environment: :data:`WP_CLI_ENV_KEYS` and the config.

    Nothing else from ``os.environ`` reaches it: not the passphrase, not
    ``MYSQL_*``, not any ``L3IO_WP_BACKUP_*`` or ``WP_CLI_*`` variable. WP-CLI
    reads ``DB_PASSWORD`` from ``wp-config.php`` itself (AD-7).
    """
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/"),
        "LC_ALL": "C",
        "WP_CLI_CONFIG_PATH": str(config_path),
    }


def _global_argv(wp: str, site_root: Path, *, as_root: bool) -> list[str]:
    root = ["--allow-root"] if as_root else []
    return [wp, f"--path={site_root}", *_GLOBAL_FLAGS, *root]


def search_replace_argv(
    wp: str,
    site_root: Path,
    pair: Replacement,
    *,
    as_root: bool,
    extra: Sequence[str] = (),
) -> list[str]:
    """The argv for a ``search-replace`` of ``pair``: a list, never a shell string."""
    return [
        *_global_argv(wp, site_root, as_root=as_root),
        "search-replace",
        pair.old,
        pair.new,
        *_PAIR_FLAGS,
        *extra,
    ]


def _running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


class ChildRunner(Protocol):
    """What runs one ``wp`` child; injectable so tests can record the call."""

    def __call__(
        self, argv: list[str], *, cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]: ...


#: The most one ``wp`` child may print on each of stdout and stderr, in bytes
#: (BL-E007-009); past it the child's process group is killed and the call
#: fails. A ``search-replace`` table of every column of a large Site fits.
CHILD_OUTPUT_LIMIT: Final = 8 * 1024 * 1024
#: The longest one ``wp`` child may run, in seconds. Generous: a ``--precise``
#: replace unserializes every row. It exists so restored code that never returns
#: cannot hold the Site Lock for ever.
CHILD_TIMEOUT_S: Final = 4 * 60 * 60
#: ``prctl(2)``'s ``PR_SET_DUMPABLE`` and ``PR_GET_DUMPABLE`` option numbers.
_PR_SET_DUMPABLE: Final = 4
_PR_GET_DUMPABLE: Final = 3


class _DumpableGuard:
    """Reference-counts the process-wide ``PR_SET_DUMPABLE`` toggle.

    The flag belongs to the process, not to a thread, so two overlapping
    ``wp`` children (a library host running two restores) must share one
    toggle: it drops on the first holder and is put back only when the last
    one is done. Without the count the first thread to finish re-enabled
    ``/proc`` access while the other's child was still running.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holders = 0
        self._previous = 0

    def enter(self) -> None:
        with self._lock:
            if self._holders == 0:
                self._previous = _set_dumpable(0)
            self._holders += 1

    def leave(self) -> None:
        with self._lock:
            self._holders -= 1
            if self._holders == 0 and self._previous:
                _set_dumpable(1)  # PR_SET_DUMPABLE takes 0 or 1 only


_DUMPABLE = _DumpableGuard()


@contextlib.contextmanager
def _unreadable_by_the_child() -> Iterator[None]:
    """Make this process's ``/proc/<pid>`` entries unreadable to its own children.

    The child runs the restored Site's PHP as the same user, and a same-user
    process can read ``/proc/<ppid>/environ``: the passphrase and the admin
    credentials this process was started with. A non-dumpable process's entries
    belong to root. The flag is reset by ``execve``, so the child itself is not
    affected, and it is put back once the last overlapping child is done
    (:class:`_DumpableGuard`). Linux only; elsewhere, or if the call fails, it
    does nothing (``docs/restore.md`` says so). While a child runs the whole
    host process is non-dumpable: no core dump, no same-user ``ptrace``.
    """
    _DUMPABLE.enter()
    try:
        yield
    finally:
        _DUMPABLE.leave()


def _set_dumpable(value: int) -> int:
    """Set ``PR_SET_DUMPABLE`` and return the previous value; ``0`` if unsupported."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        previous = int(libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0))
        if previous < 0 or libc.prctl(_PR_SET_DUMPABLE, value, 0, 0, 0) != 0:
            return 0
    except (OSError, AttributeError):
        return 0
    return previous


def run_child(
    argv: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` with exactly ``env``, no shell, no stdin, text output.

    ``adapters/preflight.py::run_bounded``: the child leads its own process
    group, killed whole after :data:`CHILD_TIMEOUT_S`
    (``subprocess.TimeoutExpired``) or once either stream passes
    :data:`CHILD_OUTPUT_LIMIT` (``ChildOutputLimitError``).
    Runs with this process non-dumpable, so the child cannot read its ``/proc``.
    """
    with _unreadable_by_the_child():
        return run_bounded(
            argv,
            cwd=cwd,
            env=env,
            timeout=CHILD_TIMEOUT_S,
            limit=CHILD_OUTPUT_LIMIT,
        )


class RunFacts:
    """What the extension learns at ``resolve`` that the replacer logs later."""

    __slots__ = ("archive_encrypted",)

    def __init__(self) -> None:
        self.archive_encrypted: bool | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _Preview:
    total: int
    tables: tuple[str, ...]


@contextlib.contextmanager
def _wp_cli_directory(site_root: Path) -> Iterator[tuple[Path, Path]]:
    """A fresh ``0700`` directory under the Site's state directory, and a config.

    The config is an empty ``0600`` file for ``WP_CLI_CONFIG_PATH``.

    Removed on exit, whatever happened.
    """
    try:
        base = journal_directory(site_root)
        private_directories(base)
        directory = Path(tempfile.mkdtemp(prefix="wp-cli-", dir=base))
    except OSError as exc:
        step, reason = "prepare", f"{type(exc).__name__}: {exc.strerror or exc}"
        raise WpCliFailedError(step, None, reason) from exc
    try:
        config = directory / _EMPTY_CONFIG
        os.close(os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        yield directory, config
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _check_wp_cli_directory(site_root: Path) -> None:
    """BL-E007-008: what ``_wp_cli_directory`` would meet, checked before any effect.

    The state directory is prepared (``0700``, a ``WpCliFailedError`` at
    ``prepare`` if it cannot be), and a ``wp-cli.yml`` in it or above refuses
    with ``WpCliConfigFoundError``. ``_Session.call`` repeats the check.
    """
    try:
        base = journal_directory(site_root)
        private_directories(base)
    except OSError as exc:
        step, reason = "prepare", f"{type(exc).__name__}: {exc.strerror or exc}"
        raise WpCliFailedError(step, None, reason) from exc
    found = find_wp_cli_config(base) or find_wp_cli_config(base.resolve())
    if found is not None:
        raise WpCliConfigFoundError(found)


class _Session:
    """One ``relocate_content``: its directory, its flags, its ``wp`` calls."""

    def __init__(
        self,
        *,
        wp: str,
        site_root: Path,
        cwd: Path,
        config: Path,
        secrets: SecretRegistry,
        run: ChildRunner,
        as_root: bool,
    ) -> None:
        self.wp = wp
        self.site_root = site_root
        self.cwd = cwd
        self.config = config
        self.secrets = secrets
        self.run = run
        self.as_root = as_root
        self.version = "unknown"

    def call(self, step: str, argv: list[str]) -> str:
        """Run ``argv`` and return its stdout; refuse or raise as the module says."""
        # WP-CLI walks up from PHP's getcwd(), the physical path; the state
        # path may run through a symlink, so both walks are checked.
        found = find_wp_cli_config(self.cwd) or find_wp_cli_config(self.cwd.resolve())
        if found is not None:
            raise WpCliConfigFoundError(found)
        started = time.monotonic()
        status: int | None = None
        try:
            result = self.run(argv, cwd=self.cwd, env=wp_cli_environment(self.config))
            status = result.returncode
        except subprocess.TimeoutExpired as exc:
            raise WpCliFailedError(step, None, "timed out and was killed") from exc
        except ChildOutputLimitError as exc:
            reason = f"its output exceeded {exc.limit} bytes and it was killed"
            raise WpCliFailedError(step, None, reason) from exc
        except OSError as exc:
            reason = self.secrets.redact(f"{type(exc).__name__}: {exc.strerror or exc}")
            raise WpCliFailedError(step, None, reason) from exc
        finally:
            _log.debug(
                "wp_cli_invoked",
                argv=self.secrets.redact_argv(argv),
                cwd=str(self.cwd),
                step=step,
                exit_status=status,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        if result.returncode != 0:
            lines = (result.stderr or "").strip().splitlines()[-_STDERR_TAIL_LINES:]
            tail = self.secrets.redact("\n".join(lines))
            raise WpCliFailedError(step, result.returncode, tail)
        return result.stdout or ""

    def wp_command(self, step: str, *args: str) -> str:
        argv = [*_global_argv(self.wp, self.site_root, as_root=self.as_root), *args]
        return self.call(step, argv)

    def pair_command(self, step: str, pair: Replacement, *extra: str) -> str:
        argv = search_replace_argv(
            self.wp, self.site_root, pair, as_root=self.as_root, extra=extra
        )
        return self.call(step, argv)

    def unrecognised(self, step: str, reason: str) -> NoReturn:
        """Raise :class:`WpCliOutputUnrecognisedError`.

        ``read_urls`` and ``preview`` precede every replacement; ``replace`` and
        ``recount`` follow one, so the error says it may have been made.
        """
        raise WpCliOutputUnrecognisedError(
            step,
            self.secrets.redact(reason),
            wp_cli_version=self.version,
            replaced_maybe=step in _AFTER_A_WRITE,
        )

    def stored(self, option: str) -> str:
        """``option`` as the options table holds it: ``option list``, not ``get``.

        ``wp option get`` is filtered by the ``WP_SITEURL``/``WP_HOME``
        constants ``REWRITE_CONFIG`` just wrote, and would return the new URL.
        """
        out = self.wp_command(
            "read_urls", "option", "list", f"--search={option}", "--format=json"
        )
        try:
            rows = json.loads(out)
        except ValueError:
            self.unrecognised("read_urls", "option list is not JSON")
        if not isinstance(rows, list):
            self.unrecognised("read_urls", "option list is not a JSON list")
        for row in rows:
            if not isinstance(row, dict):
                self.unrecognised("read_urls", "an option list row is not an object")
            if row.get("option_name") == option:
                value = row.get("option_value")
                if isinstance(value, str) and value:
                    broken = stored_url_rule_broken(value)
                    if broken is not None:
                        raise StoredUrlInvalidError(option, broken)
                    return value
                break
        raise StoredUrlMissingError(option)

    def preview(self, pair: Replacement) -> _Preview:
        """Parse ``--dry-run``'s tab-separated table and its ``Success:`` total."""
        out = self.pair_command("preview", pair, "--dry-run")
        lines = [line for line in out.splitlines() if line.strip()]
        if not lines or not lines[-1].startswith("Success: "):
            self.unrecognised("preview", "no closing 'Success:' line")
        words = lines[-1].split()
        if len(words) < 2 or not words[1].isdigit():
            self.unrecognised("preview", "the 'Success:' line has no count")
        total = int(words[1])
        rows = list(csv.reader(lines[:-1], delimiter="\t", quoting=csv.QUOTE_NONE))
        if not rows:
            if total != 0:
                self.unrecognised("preview", "a non-zero total with no table")
            return _Preview(total=0, tables=())
        if tuple(rows[0]) != _PREVIEW_HEADER:
            self.unrecognised("preview", f"unexpected table header {rows[0]!r}")
        counted = 0
        tables: set[str] = set()
        for row in rows[1:]:
            if len(row) != len(_PREVIEW_HEADER) or not row[2].isdigit():
                self.unrecognised("preview", f"unexpected table row {row!r}")
            counted += int(row[2])
            tables.add(row[0])
        if counted != total:
            self.unrecognised(
                "preview", f"the rows sum to {counted}, the total says {total}"
            )
        return _Preview(total=total, tables=tuple(sorted(tables)))

    def count(self, step: str, pair: Replacement, *extra: str) -> int:
        out = self.pair_command(step, pair, *extra, "--format=count").strip()
        if not out.isdigit():
            self.unrecognised(step, "--format=count did not print one integer")
        return int(out)


class WpCliSearchReplace:
    """``SearchReplaceCommand`` over WP-CLI's ``search-replace`` (ADR-0005).

    Serialized PHP values are rewritten by WP-CLI with ``--precise``, which
    unserializes, replaces and re-serializes them with corrected lengths; this
    class builds argument vectors and reads WP-CLI's output, nothing else.
    Steps, each a ``wp`` call: ``version``; ``read_urls`` (``option list``
    for ``siteurl`` and ``home``); ``preview`` per pair (``--dry-run``);
    ``replace`` per pair (``--format=count``); ``recount`` per non-nested pair
    (``--dry-run --format=count``, which must be 0). Every read and preview
    precedes the first replacement.
    """

    __slots__ = ("_facts", "_run", "_secrets", "_wp")

    def __init__(
        self,
        wp: str,
        *,
        secrets: SecretRegistry,
        facts: RunFacts | None = None,
        run: ChildRunner = run_child,
    ) -> None:
        self._wp = wp
        self._secrets = secrets
        self._facts = facts if facts is not None else RunFacts()
        self._run = run

    def __repr__(self) -> str:
        return f"WpCliSearchReplace({self._wp!r})"

    def relocate_content(
        self, site_root: Path, urls: UrlRelocation
    ) -> SearchReplaceReport:
        """Replace the stored ``siteurl``/``home`` with ``urls`` across the content."""
        as_root = _running_as_root()
        if as_root:
            _log.warning(
                "wp_cli_running_as_root",
                site_root=str(site_root),
                archive_encrypted=self._facts.archive_encrypted,
                executes="wp-config.php, mu-plugins and drop-ins as root",
            )
        with _wp_cli_directory(site_root) as (cwd, config):
            session = _Session(
                wp=self._wp,
                site_root=site_root,
                cwd=cwd,
                config=config,
                secrets=self._secrets,
                run=self._run,
                as_root=as_root,
            )
            printed = session.wp_command("version", "cli", "version").strip()
            session.version = printed.removeprefix(_VERSION_PREFIX)
            stored = StoredUrls(
                siteurl=session.stored("siteurl"), home=session.stored("home")
            )
            report = _relocate(session, stored, urls)
        _log.info(
            "search_replace_completed",
            pairs=[{"old": p.old, "new": p.new} for p in report.pairs],
            replacements=report.replacements,
            tables=list(report.tables or ()),
            remaining=report.remaining,
            merged=report.merged,
            wp_cli_version=session.version,
        )
        return report


def _relocate(
    session: _Session, stored: StoredUrls, urls: UrlRelocation
) -> SearchReplaceReport:
    pairs = safe_order(replacement_pairs(stored, urls))
    merged = pairs_merged(stored, urls)
    one_url = (urls.site_url is None) != (urls.home_url is None)
    if one_url and stored.siteurl == stored.home and pairs:
        # One stored value serves both: its one pair moves home-derived links
        # (and the home option row) too, though WP_HOME is not being set.
        _log.warning(
            "search_replace_home_moved",
            old=stored.home,
            new=pairs[0].new,
            reason="the stored siteurl and home are equal",
        )
    previews = [session.preview(pair) for pair in pairs]
    replaced = 0
    for pair, preview in zip(pairs, previews, strict=True):
        made = session.count("replace", pair)
        if made != preview.total:
            _log.warning(
                "search_replace_count_drift",
                old=pair.old,
                new=pair.new,
                previewed=preview.total,
                replaced=made,
            )
        replaced += made
    remaining: int | None = 0
    checked = 0
    for pair in pairs:
        if nested(pair):
            _log.warning(
                "post_check_skipped",
                old=pair.old,
                new=pair.new,
                reason="the new URL contains the old one",
            )
            continue
        left = session.count("recount", pair, "--dry-run")
        checked += 1
        if left:
            raise SearchReplaceIncompleteError(pair.old, pair.new, left)
    if pairs and not checked:
        remaining = None
    tables = sorted({table for preview in previews for table in preview.tables})
    return SearchReplaceReport(
        pairs=pairs,
        replacements=replaced,
        tables=tuple(tables),
        remaining=remaining,
        merged=merged,
    )


def owed_search_replace(site_root: Path, urls: UrlRelocation, *, wp: str | None) -> str:
    """The journal's text for an unfinished ``SEARCH_REPLACE``: the commands owed.

    The stored URLs are not known until the Dump is loaded, so they are named
    as placeholders. Re-running is safe only for a pair whose new URL contains
    neither its own old URL nor another pair's, so the text says to check
    each with ``--dry-run`` first.
    """
    as_root = _running_as_root()
    owed = [
        shlex.join(
            search_replace_argv(
                wp or "wp",
                site_root,
                Replacement(f"<stored {name}>", new),
                as_root=as_root,
            )
        )
        for name, new in (("siteurl", urls.site_url), ("home", urls.home_url))
        if new is not None
    ]
    return (
        "the content URL rewrite may be partly done. From a directory with no "
        "wp-cli.yml in it or above it, check each command first with --dry-run "
        "added, and re-run only those that still find the old URL; never re-run "
        "one whose new URL contains its old URL or another command's old URL, as "
        "it would rewrite its own results (when the stored siteurl and home were "
        "equal, only one is owed): " + "; ".join(owed)
    )


class WpCliRestoreExtension:
    """What Epic 7 supplies to one restore, for one set of relocation flags."""

    __slots__ = ("_facts", "_replacer", "_request", "_secrets", "_wp")

    def __init__(
        self,
        request: RelocationRequest,
        *,
        secrets: SecretRegistry | None = None,
        wp: str | None = None,
    ) -> None:
        self._request = request
        self._secrets = secrets if secrets is not None else SecretRegistry()
        self._wp = wp
        self._facts = RunFacts()
        self._replacer: WpCliSearchReplace | None = None

    def __repr__(self) -> str:
        return "WpCliRestoreExtension()"

    def required_tools(self) -> tuple[ToolRequirement, ...]:
        """WP-CLI's requirement iff a URL is relocated (``core/relocation.py``).

        This is the one route a relocation's tools reach the plan:
        ``RestoreRelocation.required_tools`` stays ``()``.
        """
        return required_tools(self._request)

    def wordpress_spec(self) -> ExplicitCredentialSpec | None:
        """The relocated account, or ``None`` without the user/password pair."""
        user, password = self._request.new_db_user, self._request.new_db_password
        if user is None or password is None:
            return None
        return ExplicitCredentialSpec(user, password)

    def search_replace(self) -> SearchReplaceCommand | None:
        """WP-CLI's ``search-replace`` when a URL is relocated and ``wp`` was found.

        ``None`` otherwise: with no URL there is no ``SEARCH_REPLACE`` phase,
        and with no ``wp`` the root's ``UnboundSearchReplace`` fails closed
        (story 7.4's preflight refuses first). One instance per extension.
        """
        if url_relocation(self._request) is None or self._wp is None:
            return None
        if self._replacer is None:
            self._replacer = WpCliSearchReplace(
                self._wp, secrets=self._secrets, facts=self._facts
            )
        return self._replacer

    def resolve(
        self,
        *,
        site_root: Path,
        site: SiteDescriptor,
        view: ArchiveView,
        files: SiteFilesQuery,
        mode: RestoreMode,
    ) -> RestoreRelocation:
        """The relocation for ``mode``, or a ``UsageError`` before any effect.

        ``validate_relocation`` first. With ``--new-db-host`` or
        ``--new-db-name``, the load location is parsed once by
        ``database_location_from_db_host`` (the one ``host:port`` splitter),
        defaulting each half to the Site's own; a bad host or name is its
        ``UsageError``. Then the config placement (:meth:`_config_file`).
        """
        request = self._request
        validate_relocation(request, mode=mode)
        self._facts.archive_encrypted = view.encrypted
        load_location = None
        if request.new_db_host is not None or request.new_db_name is not None:
            host_text = (
                request.new_db_host
                if request.new_db_host is not None
                else f"{site.db_host}:{site.db_port}"
            )
            name = (
                request.new_db_name
                if request.new_db_name is not None
                else (site.db_name)
            )
            load_location = database_location_from_db_host(host_text, name)
        prior = {"WP_SITEURL": site.site_url, "WP_HOME": site.home_url}
        urls = url_relocation(request)
        undo = relocation_undo(request, prior=prior)
        if urls is not None:
            _check_wp_cli_directory(site_root)
            owed = owed_search_replace(site_root, urls, wp=self._wp)
            undo = (*undo, (RestorePhase.SEARCH_REPLACE, owed))
        laid_down = self._config_file(
            site_root=site_root,
            view=view,
            files=files,
            mode=mode,
            urls_relocated=urls is not None,
        )
        relocation = RestoreRelocation(
            rewrite_values=rewrite_values(request),
            load_database=request.new_db_name,
            load_location=load_location,
            cutover_host=request.new_db_host,
            cutover_user=request.new_db_user,
            cutover_password=request.new_db_password is not None,
            search_replace=urls,
            config_file=laid_down,
            lay_down_config=laid_down is not None,
            required_tools=(),
            actions=(),
            undo=undo,
        )
        relocation = dataclasses.replace(
            relocation, actions=relocation_actions(relocation)
        )
        relocated = dataclasses.replace(
            relocation, config_file=None, lay_down_config=False
        )
        if relocated != RestoreRelocation.none():
            # Logged as the relocation is resolved, before the survey and
            # FR-7's check: a Run refused after this still logged it.
            _log.info(
                "relocation_planned",
                keys=[key for key, _ in relocation.rewrite_values],
                cutover_keys=list(planned_cutover_keys(relocation)),
                db_location=None
                if load_location is None
                else {
                    "host": load_location.host,
                    "port": load_location.port,
                    # The database loaded: None when it is the Run's
                    # staging database on that host (no --new-db-name).
                    "name": request.new_db_name,
                },
                load_target=request.new_db_name or "a staging database",
                credential_source=RELOCATED_CREDENTIAL_SOURCE
                if relocation.cutover_password
                else site.credential_source,
                site_url_changed=request.new_site_url is not None,
                home_url_changed=request.new_home_url is not None,
            )
        return relocation

    def _config_file(
        self,
        *,
        site_root: Path,
        view: ArchiveView,
        files: SiteFilesQuery,
        mode: RestoreMode,
        urls_relocated: bool,
    ) -> PurePosixPath | None:
        """The file to lay ``wp-config.php`` down at, or ``None`` (FR-29).

        ``None`` when the target already has the config WordPress reads (the
        root located it at step 3 and it stands), when ``--wp-config`` named
        one (used as given, overriding the search), or when nothing is laid
        down. A Manifest ``config_path`` outside ``ConfigLocation`` is refused
        whether or not ``--wp-config`` is given. The facts are what
        ``locate_wp_config`` reads: a regular file (symlinks followed) at the
        two places ``wp-load.php`` looks, and anything at
        ``<root>/../wp-settings.php``. A lay-down is refused when anything at
        all (a directory, a dangling symlink) is at its path. ``site_root`` is
        the root's canonical Site Root.

        A URL relocation whose ``--wp-config`` is not the file WordPress would
        load is a ``UsageError``: WP-CLI's ``search-replace`` would bootstrap
        from another config than the one rewritten, a partial relocation
        (FR-13).
        """
        root = PurePosixPath(site_root)
        in_root = site_root / _CONFIG
        in_parent = site_root.parent / _CONFIG
        by_role = any(m.role is MemberRole.CONFIG for m in view.members)
        wanted = (
            manifest_location(view.config_path)
            if by_role and view.config_path is not None
            else None
        )
        facts = ConfigFacts(
            at_site_root=_regular_file(files, in_root),
            at_parent=_regular_file(files, in_parent),
            parent_has_wp_settings=files.file_exists(site_root.parent / _SETTINGS),
        )
        recorded = None if wanted is None else wanted.value
        stated = self._request.wp_config
        if stated is not None:
            existing = existing_location(facts)
            read = (
                None
                if existing is None
                else Path(config_file(root, _existing(existing)))
            )
            loaded = read is not None and _same_file(read, Path(stated))
            if urls_relocated and not loaded:
                reads = "no wp-config.php" if read is None else str(read)
                msg = (
                    f"--wp-config {str(stated)!r} is not the file WordPress "
                    f"loads for this Site ({reads}; it reads {in_root}, else "
                    f"{in_parent}), so WP-CLI could not rewrite its URLs from "
                    "it; nothing was changed"
                )
                raise UsageError(msg, arguments=("--wp-config",))
            _log.info(
                "config_located",
                config_path=os.path.realpath(stated),
                location=existing.value if loaded and existing else None,
                action="use-existing",
                source="stated",
                manifest_config_path=recorded,
            )
            return None
        placement = place_config(
            recorded,
            facts,
            site_root=root,
            restores_files=restores_files(mode),
            archive_has_config=config_member(view) is not None,
        )
        path = config_file(root, placement)
        if placement.action == "lay-down" and os.path.lexists(path):
            raise ConfigLocationConflictError(
                path,
                "is there and is not a regular file, so wp-config.php cannot be "
                "laid down at it",
            )
        _log.info(
            "config_located",
            config_path=str(path),
            location=placement.location.value,
            action=placement.action,
            source=placement.source,
            manifest_config_path=recorded,
        )
        return path if placement.action == "lay-down" else None


def _existing(location: ConfigLocation) -> ConfigPlacement:
    return ConfigPlacement(location=location, action="use-existing", source="target")


def _regular_file(files: SiteFilesQuery, path: Path) -> bool:
    """``locate_wp_config``'s test: a regular file, a symlink to one followed."""
    return files.file_exists(path) and os.path.isfile(path)  # noqa: PTH113


def _same_file(left: Path, right: Path) -> bool:
    """Whether two paths name one file, symlinks and ``..`` resolved."""
    return os.path.realpath(left) == os.path.realpath(right)


def restore_extension(
    flags: RelocationRequest, *, secrets: SecretRegistry
) -> WpCliRestoreExtension:
    """``cli/restore.py``'s ``RESTORE_EXTENSION`` (``RestoreExtensionFactory``).

    ``--new-db-password`` is registered with ``secrets`` again here, so a root
    that skipped it still redacts the value (registration is idempotent). The
    Mode-independent rules (``validate_relocation_values``) run here, before
    the root reads the Site or the Archive; the Mode rule runs in
    :meth:`WpCliRestoreExtension.resolve`. With a URL relocated, ``wp`` is
    looked up on ``PATH`` here, once, by preflight's own rule
    (:func:`~l3io.wp.backup.adapters.preflight.locate_tool`: relative and
    empty entries ignored), so the path is absolute and is the one checked.
    """
    if flags.new_db_password:
        secrets.register(flags.new_db_password, source="--new-db-password")
    validate_relocation_values(flags)
    wp = None
    if url_relocation(flags) is not None:
        wp = locate_tool("wp")
    return WpCliRestoreExtension(flags, secrets=secrets, wp=wp)
