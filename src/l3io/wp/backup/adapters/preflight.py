"""AD-13/FR-27 — the binaries this package runs, checked before the first effect.

Layer: ``adapters`` (AD-1). It imports ``domain/``, ``ports/``, the stdlib,
``structlog`` and ``packaging`` only; no optional extra is imported at module
scope (AD-8).

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
— the Run workspace, never a Site Root, so no ``wp-cli.yml`` above it is read
— with no shell, a 10-second timeout and an ``env`` of exactly ``PATH``,
``HOME``, ``LC_ALL=C`` plus the tool's own fixed ``probe_env`` (``wp`` gets
``WP_CLI_CONFIG_PATH`` pointed at the empty ``os.devnull``, so no global
WP-CLI config is read either). No credential, passphrase descriptor or
``MYSQL_*`` variable reaches the child. A non-zero exit, a timeout or an
``OSError`` sets ``ToolStatus.error`` (truncated to 200 characters); output
with no version token gives ``version=None`` and no error.

:func:`unsupported_platform` is the root's first check; :func:`diagnose`
assembles FR-27's diagnostic data; :data:`DOCUMENTED_REQUIREMENT_SOURCES` is
where a later story registers the requirements its plans emit, so that
``tools/gen_tool_docs.py`` documents them.
"""

from __future__ import annotations

import importlib.metadata
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

import structlog
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

from l3io.wp.backup.domain.database import Readiness
from l3io.wp.backup.domain.errors import BackupError
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
    "BinaryPreflight",
    "DatabaseClientsStatus",
    "Diagnosis",
    "ExtraStatus",
    "TargetStatus",
    "ToolInfo",
    "diagnose",
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
            version_argv=("cli", "version"),
            root_argv=("--allow-root",),
            probe_env=(("WP_CLI_CONFIG_PATH", os.devnull),),
        ),
    }
)

#: Each entry returns requirements some plan can emit that a backup plan
#: built over ``BackupScope`` does not show. ``tools/gen_tool_docs.py`` and
#: ``tests/contracts/test_tool_requirements_documented.py`` call every entry.
#: E008-S01-001 (crypto backends, restore modes) and E007-S01-004 (``wp``)
#: register theirs here.
DOCUMENTED_REQUIREMENT_SOURCES: Final[
    tuple[Callable[[], tuple[ToolRequirement, ...]], ...]
] = ()


class _ProbeFailedError(BackupError):
    """A probe could not read the tool: non-zero exit, timeout or ``OSError``.

    Private: ``BinaryPreflight`` turns it into ``ToolStatus.error``; it never
    crosses the adapter's boundary.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _truncate(text: str) -> str:
    return text if len(text) <= _ERROR_LIMIT else text[: _ERROR_LIMIT - 1] + "…"


def _probe_environment(extra: Mapping[str, str]) -> dict[str, str]:
    """``PATH``, ``HOME``, ``LC_ALL=C`` and ``extra`` — nothing inherited besides."""
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/"),
        "LC_ALL": "C",
        **extra,
    }


def _version_output(
    argv: Sequence[str], cwd: Path, extra_env: Mapping[str, str]
) -> str:
    """Run ``argv`` in ``cwd`` and return its combined output; no shell, ever."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv from KNOWN_TOOLS, no shell
            list(argv),
            cwd=cwd,
            env=_probe_environment(extra_env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"timed out after {_PROBE_TIMEOUT_S}s"
        raise _ProbeFailedError(msg) from exc
    except OSError as exc:
        msg = f"{type(exc).__name__}: {exc.strerror or exc}"
        raise _ProbeFailedError(msg) from exc
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        first = output.strip().splitlines()[0] if output.strip() else ""
        msg = f"exit {result.returncode}" + (f": {first}" if first else "")
        raise _ProbeFailedError(msg)
    return output


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


class BinaryPreflight:
    """``Preflight`` for the binaries on :data:`KNOWN_TOOLS`: statuses only.

    ``cwd`` is the Run workspace the probes run in; there is deliberately no
    default, since the current directory could be a Site Root.
    """

    __slots__ = ("_cwd", "_probe", "_which")

    def __init__(
        self,
        *,
        cwd: Path,
        which: Callable[[str], str | None] = shutil.which,
        probe: Callable[
            [Sequence[str], Path, Mapping[str, str]], str
        ] = _version_output,
    ) -> None:
        self._cwd = cwd
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
            _log_checked(requirement, status)
            statuses.append(status)
        return tuple(statuses)

    def _status(self, name: str) -> ToolStatus:
        path = self._which(name)
        if path is None:
            return ToolStatus(name=name, present=False, version=None, path=None)
        info = KNOWN_TOOLS[name]
        if not info.version_argv:
            return ToolStatus(name=name, present=True, version=None, path=path)
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


def _log_checked(requirement: ToolRequirement, status: ToolStatus) -> None:
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
    else:
        _log.error("preflight_checked", **fields)


def unsupported_platform(platform: str = sys.platform) -> str | None:
    """Why ``platform`` is refused, or ``None`` when it is supported."""
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
    passphrase_backend: str
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
