"""AD-13/FR-27 — the binaries this package runs, checked before the first effect.

Layer: ``adapters`` (AD-1). It imports ``domain/``, ``ports/``, ``core/``
(relocation's requirement constant), the stdlib, ``structlog`` and
``packaging`` only; no optional extra is imported at module scope (AD-8).

:class:`BinaryPreflight` satisfies ``ports.preflight.Preflight`` for the
binaries on :data:`KNOWN_TOOLS` — ``age``, ``age-plugin-batchpass`` and ``wp``.
It locates each with ``which``, probes every present tool's version (a
minimum or not) and returns one ``ToolStatus`` per requirement, in order,
never short-circuiting. It decides nothing and raises nothing for an unmet
requirement: the pass/fail decision and its ``found`` wording are
``domain.preflight.preflight_failure``'s, and the raise is
``cli.preflight.require_tools``'s.

**What it never checks.** The MySQL/MariaDB client tools are database's
(ADR-0015, ``check_dump``/``check_load``). The plan's ``database-dump``
requirement (``core.backup.DUMP_REQUIREMENT``) is routed by the root to
``cli.preflight.DumpReadinessPreflight`` and never reaches this adapter, so it
is refused here like any other name outside :data:`KNOWN_TOOLS`.

**The probe.** ``[path, *version_argv, *root_argv-if-root]`` runs in ``cwd``
— the filesystem root from the roots (BL-E008-002), never a Site Root — with no
shell, a 10-second timeout,
in its own process group (killed whole on a timeout) and an ``env`` of
exactly ``PATH``, ``HOME``, ``LC_ALL=C`` plus the tool's own fixed
``probe_env``. Before ``wp`` is probed, ``cwd`` and its resolved path are
searched upwards for a ``wp-cli.yml``/``wp-cli.local.yml``
(:func:`find_wp_cli_config`, which WP-CLI would read and which can
``require:`` PHP); one found raises ``WpCliConfigFoundError`` and nothing is
run. ``wp`` also gets ``--skip-plugins --skip-themes --skip-packages
--no-color``, ``WP_CLI_CONFIG_PATH`` pointed at the empty ``os.devnull`` (no
global config) and ``WP_CLI_PACKAGES_DIR`` at a directory that does not exist.
No credential, passphrase descriptor or ``MYSQL_*`` variable reaches the child.
Each output stream is read up to :data:`PROBE_OUTPUT_LIMIT` bytes; past it the
child is killed and the probe fails. The version is read from the first line
of stdout only. A non-zero exit, a timeout, too much output or an ``OSError``
sets ``ToolStatus.error``, with control characters removed and truncated to
200 characters; a first line with no version token gives ``version=None``.

:func:`run_bounded` is that bounded, process-group runner, which
``adapters/wpcli.py`` runs ``wp`` with too (BL-E007-009).

:func:`unsupported_platform` is the root's first check; :func:`diagnose`
assembles FR-27's diagnostic data; :data:`DOCUMENTED_REQUIREMENT_SOURCES` is
where a later story registers the requirements its plans emit, so that
``tools/gen_tool_docs.py`` documents them.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import os
import platform as _platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import IO, Final

import structlog
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

from l3io.wp.backup.core.relocation import WP_CLI_REQUIREMENT
from l3io.wp.backup.domain.database import Readiness
from l3io.wp.backup.domain.errors import BackupError, WpCliConfigFoundError
from l3io.wp.backup.domain.preflight import (
    ToolRequirement,
    ToolStatus,
    preflight_failure,
)
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.preflight import Preflight

__all__ = [
    "DATABASE_CLIENTS_CHECKED_BY",
    "DISTRIBUTION",
    "DOCUMENTED_REQUIREMENT_SOURCES",
    "KNOWN_TOOLS",
    "PASSPHRASE_BACKEND_VAR",
    "PROBE_OUTPUT_LIMIT",
    "WP_CLI_CONFIG_FILES",
    "BinaryPreflight",
    "ChildOutputLimitError",
    "DatabaseClientsStatus",
    "Diagnosis",
    "ExtraStatus",
    "TargetStatus",
    "ToolInfo",
    "diagnose",
    "find_wp_cli_config",
    "locate_tool",
    "run_bounded",
    "unsupported_platform",
]

_log = structlog.get_logger(__name__)

#: The installed distribution whose ``Provides-Extra`` :func:`diagnose` reports.
DISTRIBUTION: Final = "l3io-wordpress-backup"

#: Database's passphrase-backend selector; reported by name, never echoed raw.
PASSPHRASE_BACKEND_VAR: Final = "L3IO_AGE_PASSPHRASE_BACKEND"  # noqa: S105 - a name
_BACKENDS: Final = frozenset({"auto", "binder", "cli"})

#: What the diagnostic says about the database client tools it never runs.
DATABASE_CLIENTS_CHECKED_BY: Final = (
    "checked per Site by l3io-wordpress-database (check_dump/check_load)"
)

_PROBE_TIMEOUT_S: Final = 10
_ERROR_LIMIT: Final = 200
_VERSION_TOKEN: Final = re.compile(r"\d+(?:\.\d+)+")
#: The most a version probe may print on each stream, in bytes.
PROBE_OUTPUT_LIMIT: Final = 64 * 1024
_READ_CHUNK: Final = 64 * 1024
#: C0 and C1 control characters, removed before a probe's text is reported.
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: The project config files WP-CLI reads from its working directory upwards.
WP_CLI_CONFIG_FILES: Final = ("wp-cli.yml", "wp-cli.local.yml")


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """What this package knows about one binary it may run.

    ``version_argv`` is appended to the resolved path to probe the version;
    ``()`` means the tool is located but not probed. ``root_argv`` is appended
    as well when the effective user is root. ``probe_env`` is added to the
    probe's minimal environment. All three are fixed data, never logic.
    """

    purpose: str
    install_hint: str
    version_argv: tuple[str, ...]
    root_argv: tuple[str, ...] = ()
    probe_env: tuple[tuple[str, str], ...] = ()


#: The binaries this package runs: the allow-list. No minimum lives here — the
#: requirement a plan emits is the only floor.
KNOWN_TOOLS: Final[Mapping[str, ToolInfo]] = MappingProxyType(
    {
        "age": ToolInfo(
            purpose="encrypts the Archive under a passphrase (CLI passphrase backend)",
            install_hint="https://github.com/FiloSottile/age#installation",
            version_argv=("--version",),
        ),
        "age-plugin-batchpass": ToolInfo(
            purpose="supplies the passphrase to age non-interactively (CLI backend)",
            install_hint="https://github.com/FiloSottile/age/releases",
            version_argv=(),
        ),
        "wp": ToolInfo(
            purpose="WP-CLI, which relocation runs to rewrite the Site's URLs",
            install_hint="https://wp-cli.org/#installing",
            version_argv=(
                "cli",
                "version",
                "--skip-plugins",
                "--skip-themes",
                "--skip-packages",
                "--no-color",
            ),
            root_argv=("--allow-root",),
            probe_env=(
                ("WP_CLI_CONFIG_PATH", os.devnull),
                # Never created: WP-CLI then finds no installed package.
                ("WP_CLI_PACKAGES_DIR", os.path.join(os.devnull, "wp-cli-packages")),  # noqa: PTH118
            ),
        ),
    }
)


def _passphrase_backend_requirements(
    *, environ: Mapping[str, str], needed_for: str
) -> tuple[ToolRequirement, ...]:
    """``adapters/crypto.passphrase_backend_requirements``, imported when called.

    ``adapters/crypto.py`` imports this module, so the import is deferred.
    """
    from l3io.wp.backup.adapters.crypto import passphrase_backend_requirements

    return passphrase_backend_requirements(environ=environ, needed_for=needed_for)


#: Each entry returns requirements some plan can emit that a backup plan
#: built over ``BackupScope`` does not show. ``tools/gen_tool_docs.py`` and
#: ``tests/contracts/test_tool_requirements_documented.py`` call every entry.
#: E007-S01-004 registered relocation's (``wp``, what
#: ``core/relocation.py::required_tools`` returns for a URL relocation);
#: E008-S01-001 the passphrase backend's, as the ``cli`` backend needs them
#: (``binder`` needs none, and ``auto`` without the binder is ``cli``; ``auto``
#: itself depends on the host, so it is not registered). A restore plan's own
#: requirement is only ``DUMP_REQUIREMENT``, documented by the fixed MySQL row;
#: the tripwire enumerates ``RestoreMode`` to keep that true.
DOCUMENTED_REQUIREMENT_SOURCES: Final[
    tuple[Callable[[], tuple[ToolRequirement, ...]], ...]
] = (
    (lambda: (WP_CLI_REQUIREMENT,)),
    (
        lambda: _passphrase_backend_requirements(
            environ={PASSPHRASE_BACKEND_VAR: "cli"},
            needed_for="the encrypted payload (ENCRYPT, VERIFY)",
        )
    ),
)


class _ProbeFailedError(BackupError):
    """A probe could not read the tool: non-zero exit, timeout or ``OSError``.

    Private: ``BinaryPreflight`` turns it into ``ToolStatus.error``; it never
    crosses the adapter's boundary.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _truncate(text: str) -> str:
    """Control characters removed, then at most :data:`_ERROR_LIMIT` characters."""
    clean = _CONTROL.sub(" ", text)
    return clean if len(clean) <= _ERROR_LIMIT else clean[: _ERROR_LIMIT - 1] + "…"


def find_wp_cli_config(directory: Path) -> Path | None:
    """The first ``wp-cli.yml``/``wp-cli.local.yml`` in ``directory`` or above.

    ``lexists``, so a dangling symlink or a directory by that name counts.
    """
    for candidate in (directory, *directory.parents):
        for name in WP_CLI_CONFIG_FILES:
            path = candidate / name
            if os.path.lexists(path):
                return path
    return None


def _probe_environment(extra: Mapping[str, str]) -> dict[str, str]:
    """``PATH``, ``HOME``, ``LC_ALL=C`` and ``extra`` — nothing inherited besides."""
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/"),
        "LC_ALL": "C",
        **extra,
    }


class ChildOutputLimitError(BackupError):
    """A child printed more than ``limit`` bytes on one stream and was killed.

    Raised by :func:`run_bounded`; each caller maps it to its own failure
    (a probe's ``ToolStatus.error``, ``WpCliFailedError``).
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"printed more than {limit} bytes and was killed")


def _kill_group(leader: int) -> None:
    """``SIGKILL`` the process group ``leader`` leads; it may already be gone."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(leader, signal.SIGKILL)


#: How long the readers may take to see end-of-file once the group is killed;
#: a grandchild that left the group can hold a pipe open past it.
_READER_GRACE_S: Final = 2.0


def _join(readers: Sequence[threading.Thread]) -> None:
    """Join the reader threads, waiting at most :data:`_READER_GRACE_S` in all.

    A reader still blocked after that (a pipe held by a process outside the
    group) is a daemon thread and is abandoned, so the call stays bounded.
    """
    deadline = time.monotonic() + _READER_GRACE_S
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))


def run_bounded(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    limit: int,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` (no shell, no stdin) with ``env`` exactly, its output bounded.

    The child leads a new process group. Each of stdout and stderr is kept up
    to ``limit`` bytes; past that the group is killed and
    :class:`ChildOutputLimitError` raised. After ``timeout`` seconds the group
    is killed and ``subprocess.TimeoutExpired`` raised. Once the child exits,
    anything left in its group is killed, so a stray grandchild cannot hold
    the pipes open. Output is decoded as UTF-8 with replacement.
    """
    # Not ``with Popen``: its exit closes the pipes, which blocks while a
    # reader is still reading one a stray grandchild holds open. Each reader
    # closes its own pipe at end-of-file instead.
    process = subprocess.Popen(  # noqa: S603 - a fixed list argv, never a shell
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    kept: dict[str, bytes] = {}
    exceeded = threading.Event()

    def drain(name: str, stream: IO[bytes]) -> None:
        buffer = bytearray()
        while chunk := stream.read(_READ_CHUNK):
            if len(buffer) + len(chunk) > limit:
                exceeded.set()
                _kill_group(process.pid)
                buffer += chunk[: limit - len(buffer)]
                while stream.read(_READ_CHUNK):
                    pass
                break
            buffer += chunk
        kept[name] = bytes(buffer)
        stream.close()

    readers = [
        threading.Thread(target=drain, args=(name, stream), daemon=True)
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
        if stream is not None
    ]
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=timeout)
    except BaseException:
        # A timeout, SIGTERM's RunTerminated or Ctrl-C: the whole group
        # goes, so no child outlives the Run that started it.
        _kill_group(process.pid)
        process.wait()
        _join(readers)
        raise
    _kill_group(process.pid)
    _join(readers)
    if exceeded.is_set():
        raise ChildOutputLimitError(limit)
    return subprocess.CompletedProcess(
        list(argv),
        returncode,
        kept.get("stdout", b"").decode("utf-8", errors="replace"),
        kept.get("stderr", b"").decode("utf-8", errors="replace"),
    )


def _version_output(
    argv: Sequence[str], cwd: Path, extra_env: Mapping[str, str]
) -> str:
    """Run ``argv`` in ``cwd`` and return the first line of its stdout."""
    try:
        result = run_bounded(
            argv,
            cwd=cwd,
            env=_probe_environment(extra_env),
            timeout=_PROBE_TIMEOUT_S,
            limit=PROBE_OUTPUT_LIMIT,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"timed out after {_PROBE_TIMEOUT_S}s"
        raise _ProbeFailedError(msg) from exc
    except ChildOutputLimitError as exc:
        msg = f"its output exceeded {PROBE_OUTPUT_LIMIT} bytes"
        raise _ProbeFailedError(msg) from exc
    except OSError as exc:
        msg = f"{type(exc).__name__}: {exc.strerror or exc}"
        raise _ProbeFailedError(msg) from exc
    if result.returncode != 0:
        output = (result.stdout or "") + (result.stderr or "")
        first = output.strip().splitlines()[0] if output.strip() else ""
        msg = f"exit {result.returncode}" + (f": {first}" if first else "")
        raise _ProbeFailedError(msg)
    lines = (result.stdout or "").strip().splitlines()
    return lines[0] if lines else ""


def _parse_version(output: str) -> tuple[int, ...] | None:
    match = _VERSION_TOKEN.search(output)
    if match is None:
        return None
    try:
        return Version(match.group()).release
    except InvalidVersion:
        return None


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def _dotted(version: tuple[int, ...] | None) -> str | None:
    return None if version is None else ".".join(str(part) for part in version)


def _absolute_entries(path: str) -> list[str]:
    """The entries of ``path`` that are absolute: not empty, not relative."""
    return [e for e in path.split(os.pathsep) if e and Path(e).is_absolute()]


def has_relative_path_entry(path: str | None = None) -> bool:
    """Whether ``path`` (default: ``PATH``) has an empty or relative entry.

    The one rule :func:`_locate` applies, so a caller that cannot control how
    a tool is resolved (database's ``age`` lookup) can refuse in the same case.
    """
    entries = (os.environ.get("PATH", os.defpath) if path is None else path).split(
        os.pathsep
    )
    return len(_absolute_entries(os.pathsep.join(entries))) != len(entries)


def _locate(name: str) -> str | None:
    """``shutil.which`` over the absolute ``PATH`` entries only.

    An empty or ``.`` entry makes ``which`` return a path relative to the
    current directory, and the probe then runs it from the Run workspace -- a
    different file from the one a later lookup finds. Relative entries are
    dropped, so the result is always absolute.
    """
    entries = os.environ.get("PATH", os.defpath)
    return shutil.which(name, path=os.pathsep.join(_absolute_entries(entries)))


#: The one lookup rule for a binary this package runs: what
#: :class:`BinaryPreflight` checks, and what ``adapters/wpcli.py`` resolves
#: ``wp`` with, so the binary checked is the binary run (E007-S01-004).
locate_tool: Final = _locate


class BinaryPreflight:
    """``Preflight`` for the binaries on :data:`KNOWN_TOOLS`: statuses only.

    ``cwd`` is the Run workspace the probes run in; there is deliberately no
    default, since the current directory could be a Site Root. With
    ``optional=True`` (``doctor``, where no plan requires anything) an absent
    tool's ``preflight_checked`` is INFO rather than ERROR (BL-E005-003).
    """

    __slots__ = ("_cwd", "_optional", "_probe", "_which")

    def __init__(
        self,
        *,
        cwd: Path,
        which: Callable[[str], str | None] = _locate,
        probe: Callable[
            [Sequence[str], Path, Mapping[str, str]], str
        ] = _version_output,
        optional: bool = False,
    ) -> None:
        self._cwd = cwd
        self._optional = optional
        self._which = which
        self._probe = probe

    def __repr__(self) -> str:
        return "BinaryPreflight()"

    def check(self, requirements: Sequence[ToolRequirement]) -> Sequence[ToolStatus]:
        """One status per requirement, in order; every requirement is probed.

        A name outside :data:`KNOWN_TOOLS` is a programming error: ``ValueError``
        before anything is located or run.
        """
        unknown = [r.name for r in requirements if r.name not in KNOWN_TOOLS]
        if unknown:
            msg = (
                f"not a binary this package runs: {', '.join(unknown)} "
                f"(known: {', '.join(KNOWN_TOOLS)})"
            )
            raise ValueError(msg)
        statuses = []
        for requirement in requirements:
            status = self._status(requirement.name)
            _log_checked(requirement, status, optional=self._optional)
            statuses.append(status)
        return tuple(statuses)

    def _status(self, name: str) -> ToolStatus:
        path = self._which(name)
        if path is None:
            return ToolStatus(name=name, present=False, version=None, path=None)
        info = KNOWN_TOOLS[name]
        if not info.version_argv:
            return ToolStatus(name=name, present=True, version=None, path=path)
        if name == WP_CLI_REQUIREMENT.name:
            # BL-E007-007: WP-CLI reads these from its cwd upwards, and PHP's
            # getcwd() is the physical path, so both walks are checked.
            found = find_wp_cli_config(self._cwd) or find_wp_cli_config(
                self._cwd.resolve()
            )
            if found is not None:
                if self._optional:
                    # doctor reports it rather than failing on what it reports.
                    return ToolStatus(
                        name=name,
                        present=True,
                        version=None,
                        path=path,
                        error=_truncate(f"not probed: {found} would configure it"),
                    )
                raise WpCliConfigFoundError(found)
        argv = [path, *info.version_argv, *(info.root_argv if _is_root() else ())]
        try:
            output = self._probe(argv, self._cwd, dict(info.probe_env))
        except _ProbeFailedError as failed:
            return ToolStatus(
                name=name,
                present=True,
                version=None,
                path=path,
                error=_truncate(failed.reason),
            )
        except subprocess.TimeoutExpired:
            return ToolStatus(
                name=name,
                present=True,
                version=None,
                path=path,
                error=f"timed out after {_PROBE_TIMEOUT_S}s",
            )
        return ToolStatus(
            name=name, present=True, version=_parse_version(output), path=path
        )


def _outcome(requirement: ToolRequirement, status: ToolStatus) -> str:
    if preflight_failure(requirement, status) is None:
        return "ok"
    if not status.present:
        return "missing"
    if status.error is not None or status.version is None:
        return "version_unknown"
    return "too_old"


def _log_checked(
    requirement: ToolRequirement, status: ToolStatus, *, optional: bool = False
) -> None:
    outcome = _outcome(requirement, status)
    fields: dict[str, object] = {
        "tool": requirement.name,
        "present": status.present,
        "version": _dotted(status.version),
        "path": status.path,
        "minimum": _dotted(requirement.minimum_version),
        "needed_for": requirement.needed_for,
        "outcome": outcome,
    }
    if status.error is not None:
        fields["error"] = status.error
    if outcome == "ok":
        _log.debug("preflight_checked", **fields)
    elif outcome == "missing" and optional:
        _log.info("preflight_checked", **fields)
    else:
        _log.error("preflight_checked", **fields)


def unsupported_platform(platform: str | None = None) -> str | None:
    """Why ``platform`` (default: ``sys.platform`` now) is refused, or ``None``."""
    if platform is None:
        platform = sys.platform
    if platform in {"win32", "cygwin"}:
        return f"Windows ({platform}) is unsupported in v1.x"
    return None


@dataclass(frozen=True, slots=True)
class ExtraStatus:
    """One optional extra: installed when every requirement it names is."""

    name: str
    installed: bool
    missing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TargetStatus:
    """Whether an Archive could be written at a Storage Target's path."""

    path: str
    exists: bool
    writable: bool


@dataclass(frozen=True, slots=True)
class DatabaseClientsStatus:
    """The database client tools, which this package never runs or names.

    ``ready`` is ``None`` when no Site was given; otherwise database's verdict
    with its finding codes.
    """

    checked_by: str
    site_given: bool
    ready: bool | None
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """FR-27's diagnostic: what is present, never an environment value.

    ``ready`` is false for an unsupported platform, a known tool present but
    unusable (``error``), an unwritable or occupied Storage Target, or a Site
    whose database is not ready. An absent tool alone does not unready it: no
    plan was given, so nothing is required.
    """

    python: str
    platform: str
    platform_supported: bool
    tools: tuple[ToolStatus, ...]
    database_clients: DatabaseClientsStatus
    #: A backend name, never a passphrase; kept out of ``repr`` all the same,
    #: as every field whose name says passphrase is (``test_public_surface``).
    passphrase_backend: str = field(repr=False)
    extras: tuple[ExtraStatus, ...]
    storage_target: TargetStatus | None
    ready: bool

    def to_dict(self) -> dict[str, object]:
        """A JSON-serialisable rendering."""
        return {
            "python": self.python,
            "platform": self.platform,
            "platform_supported": self.platform_supported,
            "tools": [
                {
                    "name": tool.name,
                    "present": tool.present,
                    "version": _dotted(tool.version),
                    "path": tool.path,
                    "error": tool.error,
                }
                for tool in self.tools
            ],
            "database_clients": {
                "checked_by": self.database_clients.checked_by,
                "site_given": self.database_clients.site_given,
                "ready": self.database_clients.ready,
                "blockers": list(self.database_clients.blockers),
                "warnings": list(self.database_clients.warnings),
            },
            "passphrase_backend": self.passphrase_backend,
            "extras": [
                {
                    "name": extra.name,
                    "installed": extra.installed,
                    "missing": list(extra.missing),
                }
                for extra in self.extras
            ],
            "storage_target": None
            if self.storage_target is None
            else {
                "path": self.storage_target.path,
                "exists": self.storage_target.exists,
                "writable": self.storage_target.writable,
            },
            "ready": self.ready,
        }


_METADATA_UNAVAILABLE: Final = "<metadata unavailable>"


def _extras() -> tuple[ExtraStatus, ...]:
    """Every ``Provides-Extra`` of the installed distribution, checked."""
    try:
        names = importlib.metadata.metadata(DISTRIBUTION).get_all("Provides-Extra")
    except importlib.metadata.PackageNotFoundError:
        return (ExtraStatus("*", installed=False, missing=(_METADATA_UNAVAILABLE,)),)
    statuses = []
    for name in names or []:
        missing = tuple(_unmet(DISTRIBUTION, name, set()))
        statuses.append(ExtraStatus(name, installed=not missing, missing=missing))
    return tuple(statuses)


def _gated(distribution: str, extra: str) -> list[Requirement]:
    """``distribution``'s requirements that ``extra`` adds — and only those.

    A marker true for ``extra`` but also for no extra at all is a core
    requirement with an environment marker, not the extra's.
    """
    gated = []
    for line in importlib.metadata.requires(distribution) or []:
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            continue
        marker = requirement.marker
        if (
            marker is not None
            and marker.evaluate({"extra": extra})
            and not marker.evaluate({"extra": ""})
        ):
            gated.append(requirement)
    return gated


def _unmet(distribution: str, extra: str, seen: set[tuple[str, str]]) -> list[str]:
    """The requirements of ``distribution[extra]`` not installed, nested extras too."""
    if (distribution, extra) in seen:
        return []
    seen.add((distribution, extra))
    missing = []
    for requirement in _gated(distribution, extra):
        label = f"{requirement.name}{requirement.specifier}"
        try:
            installed = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(label)
            continue
        if not requirement.specifier.contains(installed, prereleases=True):
            missing.append(label)
            continue
        for nested in sorted(requirement.extras):
            missing.extend(_unmet(requirement.name, nested, seen))
    return missing


def _target_status(target: StorageTarget) -> TargetStatus:
    """Reported, never raised: an inaccessible path is neither there nor writable.

    ``os.path`` rather than ``Path.is_dir``/``exists``: on 3.11 and 3.12 those
    re-raise ``EACCES`` where ``os.path`` returns ``False``.
    """
    path = Path(target.path)
    parent = path.parent
    writable = os.path.isdir(parent) and os.access(parent, os.W_OK | os.X_OK)  # noqa: PTH112
    return TargetStatus(path=str(path), exists=os.path.lexists(path), writable=writable)


def _database_clients(database: Readiness | None) -> DatabaseClientsStatus:
    if database is None:
        return DatabaseClientsStatus(
            checked_by=DATABASE_CLIENTS_CHECKED_BY, site_given=False, ready=None
        )
    return DatabaseClientsStatus(
        checked_by=DATABASE_CLIENTS_CHECKED_BY,
        site_given=True,
        ready=database.ready,
        blockers=database.blockers,
        warnings=database.warnings,
    )


def diagnose(
    *,
    preflight: Preflight,
    environ: Mapping[str, str],
    target: StorageTarget | None = None,
    database: Readiness | None = None,
) -> Diagnosis:
    """FR-27's diagnostic data. Runs only ``preflight``'s probes; changes nothing.

    ``preflight`` is built by the caller with its Run workspace as ``cwd``;
    ``database`` is the caller's ``DatabaseQuery.check_dump`` result. Of
    ``environ`` only :data:`PASSPHRASE_BACKEND_VAR` is read, and it is
    reported as one of ``auto``/``binder``/``cli`` or ``"invalid"``.
    """
    supported = unsupported_platform() is None
    tools = tuple(
        preflight.check(
            [
                ToolRequirement(
                    name=name, minimum_version=None, needed_for=info.purpose
                )
                for name, info in KNOWN_TOOLS.items()
            ]
        )
    )
    backend = environ.get(PASSPHRASE_BACKEND_VAR, "auto")
    clients = _database_clients(database)
    storage = None if target is None else _target_status(target)
    ready = (
        supported
        and all(tool.error is None for tool in tools)
        and (storage is None or (storage.writable and not storage.exists))
        and clients.ready is not False
    )
    return Diagnosis(
        python=_platform.python_version(),
        platform=sys.platform,
        platform_supported=supported,
        tools=tools,
        database_clients=clients,
        passphrase_backend=backend if backend in _BACKENDS else "invalid",
        extras=_extras(),
        storage_target=storage,
        ready=ready,
    )
