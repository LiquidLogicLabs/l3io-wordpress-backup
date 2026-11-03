"""AD-16/AD-26 — the Secret Registry is created before the Run, by the root.

The root creates one ``SecretRegistry``, resolves the passphrase into it, and
passes it to the driver, which passes it to ``open_run``; ``open_run`` adopts
it. A joined Run keeps the opener's. One default exclusion set, two roots.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs
from structlog.typing import EventDict, WrappedLogger

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli import main
from l3io.wp.backup.cli.context import join_or_open, open_run
from l3io.wp.backup.cli.parser import build_parser
from l3io.wp.backup.cli.passphrase import resolve_passphrase
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.exit_codes import ExitCode

pytestmark = pytest.mark.usefixtures("fake_database")

PASSPHRASE = "sentinel-registry-order"


def test_a_registry_given_to_open_run_is_the_runs(workdir: Path) -> None:
    registry = SecretRegistry()
    with open_run(operation="backup", secrets=registry) as ctx:
        assert ctx.secrets is registry


def test_open_run_creates_a_registry_only_when_given_none(workdir: Path) -> None:
    with open_run(operation="backup") as ctx:
        assert isinstance(ctx.secrets, SecretRegistry)


def test_a_joined_run_keeps_the_openers_registry(workdir: Path) -> None:
    opener, other = SecretRegistry(), SecretRegistry()
    with (
        open_run(operation="restore", secrets=opener) as ctx,
        join_or_open(ctx, operation="backup", secrets=other) as handle,
    ):
        assert handle.ctx.secrets is opener


def test_run_backup_registers_the_passphrase_before_run_started(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    registries: list[SecretRegistry] = []
    real_register = SecretRegistry.register

    def register(self: SecretRegistry, value: str, *, source: str = "x") -> None:
        order.append(f"register:{source}")
        registries.append(self)
        real_register(self, value, source=source)

    def cipher(*, environ: object, secrets: SecretRegistry) -> None:
        resolve_passphrase(
            explicit=PASSPHRASE, environ={}, prompt=None, secrets=secrets
        )

    def record(_l: WrappedLogger, _m: str, event_dict: EventDict) -> EventDict:
        order.append(str(event_dict["event"]))
        return event_dict

    opened_with: list[object] = []
    real_join = backup_cli.join_or_open

    def join(ctx: object, **kw: object) -> object:
        opened_with.append(kw["secrets"])
        return real_join(ctx, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(SecretRegistry, "register", register)
    monkeypatch.setattr(backup_cli, "_cipher", cipher)
    monkeypatch.setattr(backup_cli, "join_or_open", join)
    final = tmp_path / "out.tar.gz"
    args = build_parser().parse_args(
        ["backup", "--wp-dir", str(site), "--archive", str(final)]
    )
    with capture_logs(processors=[record]):
        code = backup_cli.run_backup(args, secrets=SecretRegistry())
    assert code is ExitCode.OK
    assert order.index("register:passphrase:argument") < order.index("run_started")
    # The registry the passphrase went into is the one the Run adopted.
    assert opened_with == [registries[0]]


def test_a_request_built_without_exclusion_carries_the_default() -> None:
    request = backup_cli.BackupRequest(
        site_root=Path("/srv/www"),
        scope=BackupScope.ALL,
        target=None,
        wp_config=None,
        credentials=None,
        cipher=None,
    )
    assert request.exclusion == backup_cli.default_exclusion_policy()


def test_the_cli_with_no_exclusion_flag_builds_the_same_request(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[backup_cli.BackupRequest] = []
    real = backup_cli.drive_backup

    def spy(request: backup_cli.BackupRequest, **kw: object) -> object:
        seen.append(request)
        return real(request, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(backup_cli, "drive_backup", spy)
    final = tmp_path / "out.tar.gz"
    assert main(["backup", "--wp-dir", str(site), "--archive", str(final)]) == 0
    (request,) = seen
    library = backup_cli.BackupRequest(
        site_root=site,
        scope=BackupScope.ALL,
        target=backup_cli.storage_target_from_cli(str(final)),
        wp_config=None,
        credentials=None,
        cipher=None,
    )
    assert request == library
    assert request.exclusion == backup_cli.default_exclusion_policy()
