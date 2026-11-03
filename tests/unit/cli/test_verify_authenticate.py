"""BL-E002-060 (RT-H2, Shawn 2026-11-03) — what ``verify`` proves; ``--authenticate``.

Keyless verification proves the Archive is **consistent** with its own,
unprotected Manifest, not that it is **authentic**: the output and the
``VERIFY`` record say so (``authenticated: false``). ``--authenticate``
resolves the passphrase through AD-26 (environment, then a terminal prompt;
never ``argv``) and decrypts the whole payload, which ``age`` authenticates.

The encrypted cases run over :class:`~tests.conftest.FakeAge`, bound below
database's ``encryption.decrypt`` (no ``age`` backend is installed until
E004-S01-003); ``test_the_real_backend_authenticates`` runs the same path over
database's real backend when one is installed, and skips otherwise.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli import main
from l3io.wp.backup.cli import verify as verify_cli
from l3io.wp.backup.cli.backup import storage_target_from_cli
from l3io.wp.backup.domain.archive_format import Integrity
from l3io.wp.backup.domain.errors import (
    ArchiveNotEncryptedError,
    PassphraseRejectedError,
    PassphraseRequiredError,
    PayloadDamagedError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.conftest import (
    PASSPHRASE_ENV,
    backdoor_plain_payload,
    forge_archive,
    seal_archive,
)

pytest.importorskip("l3io.wp.database")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "archives" / "v1" / "minimal.tar.gz"


@pytest.fixture
def passphrase() -> str:
    return f"verify-pp-{uuid.uuid4().hex}"


@pytest.fixture
def no_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """No prompt step: the chain ends at the environment."""
    monkeypatch.setattr(verify_cli, "terminal_prompt", lambda: None)
    monkeypatch.delenv(PASSPHRASE_ENV, raising=False)


def _verify(
    capsys: pytest.CaptureFixture[str], archive: Path, *flags: str
) -> tuple[int, str, str, list[dict[str, Any]]]:
    argv = ["--log-format", "json", "verify", "--archive", str(archive), *flags]
    capsys.readouterr()  # what building the fixture Archive printed
    code = main(argv)
    out = capsys.readouterr()
    records = [
        json.loads(line) for line in out.err.splitlines() if line.startswith("{")
    ]
    return code, out.out, out.err, records


def _verify_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    found = [
        r
        for r in records
        if r.get("event") == "phase_completed" and r.get("phase") == "VERIFY"
    ]
    assert len(found) == 1, "one VERIFY record per Run"
    return found[0]


# --------------------------------------------------------------------------
# Keyless: consistent, not authenticated
# --------------------------------------------------------------------------


def test_keyless_verify_says_not_authenticated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _, records = _verify(capsys, FIXTURE)
    assert code == ExitCode.OK
    assert "integrity VERIFIED (format v1)" in out
    assert "checksums consistent" in out
    assert "authenticity not checked (no key)" in out
    record = _verify_record(records)
    assert record["authenticated"] is False
    assert record["integrity"] == Integrity.VERIFIED.value


def test_a_library_pass_is_keyless_by_default() -> None:
    request = verify_cli.VerifyRequest(archive=storage_target_from_cli(str(FIXTURE)))
    assert request.authenticate is False
    result = verify_cli.drive_verify(request, ctx=None)
    assert result.passed
    assert result.integrity is Integrity.VERIFIED


# --------------------------------------------------------------------------
# --authenticate
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("fake_age", "no_terminal")
def test_authenticate_succeeds_with_the_right_passphrase(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    passphrase: str,
) -> None:
    sealed = seal_archive(tmp_path, passphrase)
    monkeypatch.setenv(PASSPHRASE_ENV, passphrase)
    code, out, err, records = _verify(capsys, sealed, "--authenticate")
    assert code == ExitCode.OK
    assert "authenticated with the passphrase" in out
    assert "not checked" not in out
    assert _verify_record(records)["authenticated"] is True
    assert passphrase not in out + err


@pytest.mark.usefixtures("fake_age", "no_terminal")
def test_authenticate_fails_with_the_wrong_passphrase(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    passphrase: str,
) -> None:
    sealed = seal_archive(tmp_path, passphrase)
    wrong = f"wrong-pp-{uuid.uuid4().hex}"
    monkeypatch.setenv(PASSPHRASE_ENV, wrong)
    code, out, err, records = _verify(capsys, sealed, "--authenticate")
    assert code == ExitCode.VERIFICATION_FAILED
    assert out == ""
    assert "the passphrase cannot decrypt this archive" in err
    failed = [r for r in records if r.get("event") == "command_failed"]
    assert [r["error"] for r in failed] == [PassphraseRejectedError.__name__]
    record = _verify_record(records)
    assert record["outcome"] == "failed"
    assert record["authenticated"] is False
    assert wrong not in out + err


@pytest.mark.usefixtures("fake_age", "no_terminal")
def test_authenticate_refuses_a_plain_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PASSPHRASE_ENV, f"unused-pp-{uuid.uuid4().hex}")
    code, out, err, _ = _verify(capsys, seal_archive(tmp_path, None), "--authenticate")
    assert code == ExitCode.USAGE
    assert ArchiveNotEncryptedError.exit_code is ExitCode.USAGE
    assert out == ""
    assert "nothing to authenticate" in err


@pytest.mark.usefixtures("fake_age", "no_terminal")
def test_authenticate_with_no_passphrase_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], passphrase: str
) -> None:
    sealed = seal_archive(tmp_path, passphrase)
    code, out, err, _ = _verify(capsys, sealed, "--authenticate")
    assert code == PassphraseRequiredError.exit_code
    assert out == ""
    assert "verify needs a passphrase" in err


@pytest.mark.usefixtures("fake_age")
def test_authenticate_prompts_on_a_terminal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    passphrase: str,
) -> None:
    sealed = seal_archive(tmp_path, passphrase)
    monkeypatch.delenv(PASSPHRASE_ENV, raising=False)
    asked: list[str] = []

    def prompt(text: str) -> str:
        asked.append(text)
        return passphrase

    monkeypatch.setattr(verify_cli, "terminal_prompt", lambda: prompt)
    code, out, _, _ = _verify(capsys, sealed, "--authenticate")
    assert code == ExitCode.OK
    assert len(asked) == 1
    assert "authenticated" in out


def test_authenticate_takes_no_value() -> None:
    """FR-4/NFR-3: a boolean; a value after it is a usage error, never a passphrase."""
    with pytest.raises(SystemExit) as caught:
        main(["verify", "--archive", str(FIXTURE), "--authenticate=secret"])
    assert caught.value.code == ExitCode.USAGE


@pytest.mark.usefixtures("fake_age")
def test_the_library_path_authenticates_and_registers_the_passphrase(
    tmp_path: Path, passphrase: str
) -> None:
    sealed = seal_archive(tmp_path, passphrase)
    registry = SecretRegistry()
    request = verify_cli.VerifyRequest(
        archive=storage_target_from_cli(str(sealed)), authenticate=True
    )
    result = verify_cli.drive_verify(
        request, ctx=None, secrets=registry, passphrase=passphrase
    )
    assert result.passed
    assert passphrase not in registry.redact(passphrase)
    with pytest.raises(PassphraseRequiredError):
        verify_cli.drive_verify(request, ctx=None, environ={PASSPHRASE_ENV: ""})


# --------------------------------------------------------------------------
# The red-team forgery (RT-H2): edit the payload, recompute the Manifest
# --------------------------------------------------------------------------


def test_a_forged_plain_archive_verifies_but_says_not_authenticated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    forged = forge_archive(FIXTURE, tmp_path / "forged.tar.gz", backdoor_plain_payload)
    code, out, _, records = _verify(capsys, forged)
    assert code == ExitCode.OK, "keyless verify cannot see a recomputed checksum"
    assert "authenticity not checked (no key)" in out
    assert _verify_record(records)["authenticated"] is False


@pytest.mark.usefixtures("fake_age", "no_terminal")
def test_a_forged_encrypted_payload_fails_authenticate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    passphrase: str,
) -> None:
    sealed = seal_archive(tmp_path, passphrase)

    def tamper(payload: bytes) -> bytes:
        flipped = bytearray(payload)
        flipped[len(flipped) // 2] ^= 0x01
        return bytes(flipped)

    forged = forge_archive(sealed, tmp_path / "forged.tar.gz", tamper)
    code, out, _, _ = _verify(capsys, forged)
    assert code == ExitCode.OK
    assert "authenticity not checked (no key)" in out
    monkeypatch.setenv(PASSPHRASE_ENV, passphrase)
    code, out, err, records = _verify(capsys, forged, "--authenticate")
    assert code == ExitCode.VERIFICATION_FAILED
    assert "damaged or tampered with" in err
    failed = [r for r in records if r.get("event") == "command_failed"]
    assert [r["error"] for r in failed] == [PayloadDamagedError.__name__]
    assert out == ""


def test_the_two_authentication_failures_are_distinct() -> None:
    assert PassphraseRejectedError is not PayloadDamagedError
    assert not issubclass(PassphraseRejectedError, PayloadDamagedError)
    assert not issubclass(PayloadDamagedError, PassphraseRejectedError)
    assert PassphraseRejectedError.exit_code is ExitCode.VERIFICATION_FAILED
    assert PayloadDamagedError.exit_code is ExitCode.VERIFICATION_FAILED


# --------------------------------------------------------------------------
# The real backend, when one is installed (E004-S01-003 installs it)
# --------------------------------------------------------------------------


def _real_backend() -> bool:
    backend = pytest.importorskip("l3io.wp.database._passphrase")
    return bool(backend.binder_available() or backend.cli_available())


def test_the_real_backend_authenticates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passphrase: str
) -> None:
    if not _real_backend():
        pytest.skip("no age passphrase backend installed (E004-S01-003 adds it)")
    sealed = seal_archive(tmp_path, passphrase)
    target = storage_target_from_cli(str(sealed))
    request = verify_cli.VerifyRequest(archive=target, authenticate=True)
    assert verify_cli.drive_verify(request, ctx=None, passphrase=passphrase).passed
    with pytest.raises(PassphraseRejectedError):
        verify_cli.drive_verify(request, ctx=None, passphrase=f"x-{passphrase}")
