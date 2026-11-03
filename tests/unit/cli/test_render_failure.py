"""FR-21 / ADR-0018 — the stderr failure text is redacted and names its code.

Also closes BL-E002-054: an exception that is not a ``BackupError`` reaching
the root is reported through the Run's registry — its text is never printed,
and its traceback (logged) is redacted — and exits ``FAILED``.
"""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

import pytest

from l3io.wp.backup.adapters.redaction import REDACTED, SecretRegistry
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli import main
from l3io.wp.backup.cli.exit import render_failure, report_failure
from l3io.wp.backup.domain.errors import ArchiveExistsError, CredentialsError
from l3io.wp.backup.domain.exit_codes import DESCRIPTIONS, ExitCode

SECRET = "sentinel-render-failure-value"


def _registry() -> SecretRegistry:
    registry = SecretRegistry()
    registry.register(SECRET, source="test")
    return registry


def test_a_registered_secret_in_the_message_is_redacted() -> None:
    exc = CredentialsError("wp-config.php", reason=f"rejected {SECRET}")
    text = render_failure(exc, redact=_registry().redact)
    assert SECRET not in text
    assert REDACTED in text
    assert DESCRIPTIONS[ExitCode.CREDENTIALS] in text
    assert "CREDENTIALS" in text


def test_the_text_names_the_cause_and_the_meaning() -> None:
    exc = ArchiveExistsError(Path("/srv/a.tar.gz"))
    text = render_failure(exc, redact=SecretRegistry().redact)
    assert text.startswith("wpbackup2: error: archive '/srv/a.tar.gz' already exists")
    assert DESCRIPTIONS[ExitCode.REFUSED] in text


def test_a_non_package_error_is_named_by_class_only() -> None:
    text = render_failure(ValueError("dependency says hi"), redact=lambda t: t)
    assert "ValueError" in text
    assert "dependency says hi" not in text
    assert DESCRIPTIONS[ExitCode.FAILED] in text


def test_the_traceback_follows_at_debug_and_is_redacted() -> None:
    def fail() -> None:
        raise CredentialsError(SECRET, reason="rejected")

    with pytest.raises(CredentialsError) as caught:
        fail()
    root = logging.getLogger()
    saved = root.level
    try:
        root.setLevel(logging.INFO)
        assert "Traceback" not in render_failure(
            caught.value, redact=_registry().redact
        )
        root.setLevel(logging.DEBUG)
        text = render_failure(caught.value, redact=_registry().redact)
    finally:
        root.setLevel(saved)
    assert "Traceback" in text
    assert SECRET not in text


class _BrokenStream(io.StringIO):
    def write(self, s: str) -> int:
        msg = "stderr is gone"
        raise OSError(msg)


def test_a_broken_stderr_does_not_change_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from l3io.wp.backup.cli import output

    def broken(*_a: object, **_k: object) -> None:
        msg = "logging is gone"
        raise OSError(msg)

    monkeypatch.setattr(sys, "stderr", _BrokenStream())
    monkeypatch.setattr(output, "emit_failure", broken)
    monkeypatch.setattr("l3io.wp.backup.cli.exit.emit_failure", broken)
    exc = ArchiveExistsError(Path("/srv/a.tar.gz"))
    assert report_failure(exc, secrets=SecretRegistry()) is ExitCode.REFUSED


@pytest.mark.usefixtures("fake_database")
def test_bl_e002_054_a_defect_exits_failed_and_leaks_nothing(
    site: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def defect(request: object, *, ctx: object, secrets: SecretRegistry) -> None:
        secrets.register(SECRET, source="test")
        msg = f"a dependency echoed {SECRET}"
        raise RuntimeError(msg)

    monkeypatch.setattr(backup_cli, "drive_backup", defect)
    argv = ["--log-level", "DEBUG", "--log-format", "json", "backup"]
    assert main([*argv, "--wp-dir", str(site)]) == ExitCode.FAILED
    err = capsys.readouterr().err
    assert SECRET not in err
    assert "RuntimeError" in err
    assert "Traceback" in err, "the defect's traceback was not logged"
    assert REDACTED in err


def test_at_debug_a_dependency_message_in_the_chain_is_withheld() -> None:
    dependency = "the driver said user wp at db.internal"

    def dependency_call() -> None:
        raise ValueError(dependency)

    def fail() -> None:
        source = "wp-config.php"
        try:
            dependency_call()
        except ValueError as exc:
            raise CredentialsError(source) from exc

    with pytest.raises(CredentialsError) as caught:
        fail()
    text = render_failure(caught.value, redact=lambda t: t, with_traceback=True)
    assert "Traceback" in text
    assert "ValueError: <message withheld" in text
    assert dependency not in text
    assert "CredentialsError: credentials from 'wp-config.php' unusable" in text
    plain = render_failure(
        RuntimeError(dependency), redact=lambda t: t, with_traceback=True
    )
    assert dependency not in plain


@pytest.mark.usefixtures("fake_database")
def test_the_record_message_is_one_line_at_debug(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    argv = ["--log-level", "DEBUG", "--log-format", "json", "backup"]
    archive = site / "index.php"
    code = main([*argv, "--wp-dir", str(site), "--archive", str(archive)])
    assert code == ExitCode.REFUSED
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    (failed,) = [r for r in records if r["event"] == "command_failed"]
    assert "\n" not in failed["message"]
    assert failed["exit_reason"] == "REFUSED"
