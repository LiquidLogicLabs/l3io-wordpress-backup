"""FR-27 — ``wpbackup2 doctor``: is this host ready to back up and restore?

:func:`drive_doctor` is the driver ``wpbackup2 doctor`` and the library's
``api.doctor`` share: it opens a Run with no Site Lock (it changes nothing),
probes the known binaries with ``BinaryPreflight`` in the Run workspace
(``cli/preflight.binary_preflight``, not ``RootPreflight``, which would raise
on an unsupported platform that ``Diagnosis.platform_supported`` must
report), and returns ``adapters/preflight.diagnose``'s ``Diagnosis``.

With ``site_root`` it also reads that Site's ``wp-config.php``, builds the
WordPress credential source through ``cli/credentials.wordpress_source`` and
asks ``DatabaseQuery.check_dump``. A ``BackupError`` on that path is recorded
as ``Readiness(ready=False, blockers=(<the error's exit-code name>,))``: the
diagnosis still prints and the exit is ``NOT_READY``, never the error's own
code (doctor reports; it does not fail on what it reports).

:func:`run_doctor` renders the diagnosis — text on a terminal, JSON otherwise
or with ``--json`` — and returns ``OK`` when it is ready, else ``NOT_READY``.
Nothing it prints holds a credential or an environment variable's value, and
it passes through the Run's registry before it is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from l3io.wp.backup.adapters.database import (
    locate_wp_config,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.preflight import Diagnosis, diagnose
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli.adapter_sets import real_adapters
from l3io.wp.backup.cli.backup import storage_target_from_cli
from l3io.wp.backup.cli.context import RunContext, open_run
from l3io.wp.backup.cli.credentials import wordpress_source
from l3io.wp.backup.cli.exit import exit_code_for
from l3io.wp.backup.cli.output import emit_result
from l3io.wp.backup.cli.preflight import binary_preflight
from l3io.wp.backup.domain.database import Readiness
from l3io.wp.backup.domain.errors import BackupError, InvalidSiteDescriptorError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.storage_target import StorageTarget
from l3io.wp.backup.ports.tracing import Tracer

__all__ = ["drive_doctor", "render_text", "run_doctor"]


def _site_readiness(run: RunContext, site_root: Path) -> Readiness:
    """The Site's dump readiness; a ``BackupError`` becomes a blocker."""
    try:
        try:
            root = site_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            msg = "site root does not exist"
            raise InvalidSiteDescriptorError(
                msg, paths=(str(Path(site_root).absolute()),)
            ) from exc
        config = locate_wp_config(root, stated=None)
        site, wp_config = site_descriptor_from_wp_config(root, config_file=config.path)
        source = wordpress_source(
            None, wp_config=wp_config, environ=os.environ, secrets=run.secrets
        )
        adapters = real_adapters(
            wordpress_source=source, secrets=run.secrets, tracer=run.tracer
        )
        run.bind_adapters(adapters)
        return run.adapters.database_query.check_dump(site.database_location())
    except BackupError as exc:
        return Readiness(ready=False, blockers=(exit_code_for(exc).name,))


def drive_doctor(
    *,
    site_root: Path | None,
    target: StorageTarget | None,
    secrets: SecretRegistry | None = None,
    tracer: Tracer | None = None,
) -> Diagnosis:
    """FR-27's diagnosis in a Run of its own, with no Site Lock; never prints."""
    with open_run(operation="doctor", secrets=secrets, tracer=tracer) as run:
        database = None if site_root is None else _site_readiness(run, site_root)
        diagnosis = diagnose(
            preflight=binary_preflight(run, optional=True),
            environ=os.environ,
            target=target,
            database=database,
        )
        if not diagnosis.ready:
            run.report_failure(exit_code=ExitCode.NOT_READY, error_type="NotReady")
        return diagnosis


def _yes(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def render_text(diagnosis: Diagnosis) -> str:
    """The diagnosis for a terminal: one fact per line."""
    lines = [
        f"ready: {_yes(diagnosis.ready)}",
        f"python: {diagnosis.python}",
        f"platform: {diagnosis.platform} "
        f"(supported: {_yes(diagnosis.platform_supported)})",
        f"passphrase backend: {diagnosis.passphrase_backend}",
    ]
    for tool in diagnosis.tools:
        version = ".".join(map(str, tool.version)) if tool.version else "-"
        state = "present" if tool.present else "absent"
        error = f", error: {tool.error}" if tool.error else ""
        lines.append(f"tool {tool.name}: {state}, version {version}{error}")
    clients = diagnosis.database_clients
    blockers = f", blockers: {', '.join(clients.blockers)}" if clients.blockers else ""
    lines.append(f"database: ready {_yes(clients.ready)}{blockers}")
    lines.append(f"database clients: {clients.checked_by}")
    for extra in diagnosis.extras:
        missing = f" (missing: {', '.join(extra.missing)})" if extra.missing else ""
        lines.append(f"extra {extra.name}: {_yes(extra.installed)}{missing}")
    if diagnosis.storage_target is not None:
        target = diagnosis.storage_target
        lines.append(
            f"archive target {target.path}: exists {_yes(target.exists)}, "
            f"writable {_yes(target.writable)}"
        )
    return "\n".join(lines)


def run_doctor(args: argparse.Namespace, *, secrets: SecretRegistry) -> ExitCode:
    """``wpbackup2 doctor``: prints the diagnosis; ``OK`` or ``NOT_READY``."""
    target = None if args.archive is None else storage_target_from_cli(args.archive)
    diagnosis = drive_doctor(
        site_root=None if args.wp_dir is None else Path(args.wp_dir),
        target=target,
        secrets=secrets,
    )
    as_json = bool(args.json) or not sys.stdout.isatty()
    text = (
        json.dumps(diagnosis.to_dict(), indent=2, sort_keys=True)
        if as_json
        else render_text(diagnosis)
    )
    emit_result(secrets.redact(text))
    return ExitCode.OK if diagnosis.ready else ExitCode.NOT_READY
