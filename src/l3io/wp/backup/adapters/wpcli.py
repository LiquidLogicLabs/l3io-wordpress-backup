"""FR-12, ADR-0005, ADR-0015 — WP-CLI's ``search-replace``, run safely.

``adapters/restore_extension.py`` (Epic 7's restore extension) returns
:class:`WpCliSearchReplace` when a URL is relocated; ``adapters/wpcli_child.py``
runs each ``wp`` process (BL-E007-019: three modules, one concern each).

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
relocated, ``adapters/restore_extension.py``'s ``resolve`` calls
:func:`check_wp_cli_directory`: the state directory is prepared and a
``wp-cli.yml`` above it refused (BL-E007-008); the check in every call stays
as the backstop.

**Residual (stated, not closed).** ``restore_extension`` resolves ``wp`` on
``PATH`` once, with ``adapters/preflight.py::locate_tool`` (absolute entries
only); the roots' ``RootPreflight`` routes ``wp`` to ``BinaryPreflight``,
which looks it up again by name with that same function, on the same
``PATH`` in the same process, so the binary checked is the binary run
(BL-E007-016). A ``PATH`` entry changing between the
two lookups is not detected. ``wp`` disappearing after the preflight surfaces
as ``WpCliFailedError`` at ``SEARCH_REPLACE``, journaled incomplete.
Accepted for v1 (owner decision 2026-11-07, BL-E005-010): changing a ``PATH``
directory of the user running the restore already lets that user run any code
as themselves, so the window grants nothing new. The binaries the database
library starts (the client tools, ``age``) are resolved by that library, not
here.

The factory is annotated with ``core/relocation.py::RelocationRequest``,
because ``adapters/`` may not import ``cli/`` where ``RelocationFlags`` lives.
"""

from __future__ import annotations

import contextlib
import csv
import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final, NoReturn

import structlog

from l3io.wp.backup.adapters.journal import journal_directory
from l3io.wp.backup.adapters.lock import state_root
from l3io.wp.backup.adapters.preflight import (
    WP_CLI_CONFIG_FILES,
    ChildOutputLimitError,
    absolute_search_path,
    find_wp_cli_config,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.workspace import private_state_directories
from l3io.wp.backup.adapters.wpcli_child import ChildRunner, run_child
from l3io.wp.backup.core.relocation import (
    nested,
    pairs_merged,
    replacement_pairs,
    safe_order,
    stored_url_rule_broken,
)
from l3io.wp.backup.domain.archive_format import (
    WP_CONFIG_NAME,
    WP_SETTINGS_NAME,
)
from l3io.wp.backup.domain.errors import (
    SearchReplaceIncompleteError,
    StoredUrlInvalidError,
    StoredUrlMissingError,
    WpCliConfigFoundError,
    WpCliFailedError,
    WpCliOutputUnrecognisedError,
)
from l3io.wp.backup.domain.search_replace import (
    Replacement,
    SearchReplaceReport,
    StoredUrls,
    UrlRelocation,
)

__all__ = [
    "WP_CLI_CONFIG_FILES",
    "WP_CLI_ENV_KEYS",
    "WpCliSearchReplace",
    "check_wp_cli_directory",
    "find_wp_cli_config",
    "owed_search_replace",
    "search_replace_argv",
    "wp_cli_environment",
]

_log = structlog.get_logger(__name__)


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


def _printed_version(stdout: str) -> str:
    """The version ``wp cli version`` printed, from its ``WP-CLI`` line.

    A PHP notice or warning can be printed before it, so the first line that
    starts with :data:`_VERSION_PREFIX` is used; without one, the first line.
    """
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    for line in lines:
        if line.startswith(_VERSION_PREFIX):
            return line.removeprefix(_VERSION_PREFIX)
    return lines[0] if lines else ""


def _escape_controls(text: str) -> str:
    """``text`` with every non-printable character but a newline escaped.

    ``wp``'s stderr is untrusted: a terminal control sequence in it must not
    reach the operator's terminal through the error message (RT-7).
    """
    return "".join(
        char
        if char == "\n" or char.isprintable()
        else char.encode("unicode_escape").decode("ascii")
        for char in text
    )


#: The empty global config file ``WP_CLI_CONFIG_PATH`` names.
_EMPTY_CONFIG: Final = "config.yml"
#: The file ``wp-load.php`` looks for, and the one that marks an install.
_CONFIG: Final = WP_CONFIG_NAME
_SETTINGS: Final = WP_SETTINGS_NAME


def wp_cli_environment(config_path: Path) -> dict[str, str]:
    """A ``wp`` child's whole environment: :data:`WP_CLI_ENV_KEYS` and the config.

    Nothing else from ``os.environ`` reaches it: not the passphrase, not
    ``MYSQL_*``, not any ``L3IO_WP_BACKUP_*`` or ``WP_CLI_*`` variable. WP-CLI
    reads ``DB_PASSWORD`` from ``wp-config.php`` itself (AD-7). ``PATH`` holds
    only its absolute entries, the ones ``wp`` was located over, so the
    ``php`` WP-CLI runs is found by the same rule (BL-E007-013).
    """
    return {
        "PATH": absolute_search_path(),
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
        private_state_directories(base, root=state_root())  # BL-E008-027
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


def check_wp_cli_directory(site_root: Path) -> None:
    """BL-E007-008: what ``_wp_cli_directory`` would meet, checked before any effect.

    The state directory is prepared (``0700``, a ``WpCliFailedError`` at
    ``prepare`` if it cannot be), and a ``wp-cli.yml`` in it or above refuses
    with ``WpCliConfigFoundError``. ``_Session.call`` repeats the check.
    """
    try:
        base = journal_directory(site_root)
        private_state_directories(base, root=state_root())  # BL-E008-027
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
            tail = _escape_controls(self.secrets.redact("\n".join(lines)))
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

    __slots__ = ("_run", "_secrets", "_wp")

    def __init__(
        self,
        wp: str,
        *,
        secrets: SecretRegistry,
        run: ChildRunner = run_child,
    ) -> None:
        self._wp = wp
        self._secrets = secrets
        self._run = run

    def __repr__(self) -> str:
        return f"WpCliSearchReplace({self._wp!r})"

    def relocate_content(
        self,
        site_root: Path,
        urls: UrlRelocation,
        *,
        archive_encrypted: bool | None = None,
    ) -> SearchReplaceReport:
        """Replace the stored ``siteurl``/``home`` with ``urls`` across the content.

        ``archive_encrypted`` (the restore's own fact, BL-E007-020) goes on the
        root WARNING only.
        """
        as_root = _running_as_root()
        if as_root:
            _log.warning(
                "wp_cli_running_as_root",
                site_root=str(site_root),
                archive_encrypted=archive_encrypted,
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
            printed = session.wp_command("version", "cli", "version")
            session.version = _printed_version(printed)
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
