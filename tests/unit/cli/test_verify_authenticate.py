"""BL-E002-060 (RT-H2, Shawn 2026-11-03) — what ``verify`` proves; ``--authenticate``.

Keyless verification proves the Archive is **consistent** with its own,
unprotected Manifest, not that it is **authentic**: the output and the
``VERIFY`` record say so (``authenticated: false``). ``--authenticate``
resolves the passphrase through AD-26 (environment, then a terminal prompt;
never ``argv``) and decrypts the whole payload, which ``age`` authenticates.

The encrypted cases run over database's real passphrase backend, which the
``database`` extra's ``encryption`` brings (E004-S01-003, BL-E004-042). One
sealed Archive is shared by the module: scrypt makes each encryption slow.

**The inner Manifest (BL-E002-115, ADR-0024 amendment).** An outer Manifest
edited outside the payload, and an inner Manifest removed, moved or
duplicated by a forger holding the passphrase, each fail ``--authenticate``
with ``PayloadDamagedError``; a matching pair passes; a whole older genuine
Archive passes too, and the output states that permanent limit.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tarfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.crypto import PassphraseCipher
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli import main
from l3io.wp.backup.cli import verify as verify_cli
from l3io.wp.backup.cli.backup import storage_target_from_cli
from l3io.wp.backup.domain.archive_format import INNER_MANIFEST_MEMBER, Integrity
from l3io.wp.backup.domain.errors import (
    ArchiveNotEncryptedError,
    PassphraseRejectedError,
    PassphraseRequiredError,
    PayloadDamagedError,
    WorkFactorTooHighError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.conftest import (
    PASSPHRASE_ENV,
    InnerMembers,
    backdoor_plain_payload,
    forge_archive,
    reseal,
    seal_archive,
)

pytest.importorskip("l3io.wp.database")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "archives" / "v1" / "minimal.tar.gz"


@pytest.fixture(scope="module")
def passphrase() -> str:
    return f"verify-pp-{uuid.uuid4().hex}"


@pytest.fixture(scope="module")
def sealed(tmp_path_factory: pytest.TempPathFactory, passphrase: str) -> Path:
    """One encrypted Archive by the real writer and the real cipher."""
    return seal_archive(tmp_path_factory.mktemp("sealed"), passphrase)


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


@pytest.mark.usefixtures("no_terminal")
def test_authenticate_succeeds_with_the_right_passphrase(
    sealed: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    passphrase: str,
) -> None:
    monkeypatch.setenv(PASSPHRASE_ENV, passphrase)
    code, out, err, records = _verify(capsys, sealed, "--authenticate")
    assert code == ExitCode.OK
    assert "authenticated with the passphrase" in out
    assert verify_cli.ROLLBACK_LIMIT in out  # F-21: the permanent limit
    assert "not checked" not in out
    assert _verify_record(records)["authenticated"] is True
    assert passphrase not in out + err


@pytest.mark.usefixtures("no_terminal")
def test_authenticate_accepts_a_passphrase_below_the_redaction_floor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner decision 69: decrypting takes any length; one under 4 is warned of."""
    short = "q7Z"
    archive = seal_archive(tmp_path, short)
    monkeypatch.setenv(PASSPHRASE_ENV, short)
    code, out, _err, records = _verify(capsys, archive, "--authenticate")
    assert code == ExitCode.OK
    assert "authenticated with the passphrase" in out
    (warning,) = [
        r for r in records if r.get("event") == "credential_below_redaction_floor"
    ]
    assert (warning["level"], warning["source"]) == ("warning", PASSPHRASE_ENV)


@pytest.mark.usefixtures("no_terminal")
def test_authenticate_fails_with_the_wrong_passphrase(
    sealed: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    passphrase: str,
) -> None:
    wrong = f"wrong-pp-{uuid.uuid4().hex}"
    monkeypatch.setenv(PASSPHRASE_ENV, wrong)
    code, out, err, records = _verify(capsys, sealed, "--authenticate")
    assert code == ExitCode.VERIFICATION_FAILED
    assert out == ""
    assert "the passphrase cannot decrypt this archive" in err
    failed = [r for r in records if r.get("event") == "command_failed"]
    assert [r["error"] for r in failed] == [PassphraseRejectedError.__name__]
    decrypt = [r for r in records if r.get("event") == "member_decryption_failed"]
    assert [(r["member"], r["reason"]) for r in decrypt] == [
        ("payload.tar.gz.age", "wrong_passphrase")
    ]
    record = _verify_record(records)
    assert record["outcome"] == "failed"
    assert record["authenticated"] is False
    assert wrong not in out + err


@pytest.mark.usefixtures("no_terminal")
def test_authenticate_refuses_a_plain_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PASSPHRASE_ENV, f"unused-pp-{uuid.uuid4().hex}")
    code, out, err, _ = _verify(capsys, seal_archive(tmp_path, None), "--authenticate")
    assert code == ExitCode.USAGE
    assert ArchiveNotEncryptedError.exit_code is ExitCode.USAGE
    assert out == ""
    assert "nothing to authenticate" in err


@pytest.mark.usefixtures("no_terminal")
def test_authenticate_with_no_passphrase_refuses(
    sealed: Path, capsys: pytest.CaptureFixture[str], passphrase: str
) -> None:
    code, out, err, _ = _verify(capsys, sealed, "--authenticate")
    assert code == PassphraseRequiredError.exit_code
    assert out == ""
    assert "verify needs a passphrase" in err


def test_authenticate_prompts_on_a_terminal(
    sealed: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    passphrase: str,
) -> None:
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


def test_the_library_path_authenticates_and_registers_the_passphrase(
    sealed: Path, passphrase: str
) -> None:
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


@pytest.mark.usefixtures("no_terminal")
def test_a_forged_encrypted_payload_fails_authenticate(
    sealed: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    passphrase: str,
) -> None:

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
    decrypt = [r for r in records if r.get("event") == "member_decryption_failed"]
    assert [r["reason"] for r in decrypt] == ["corrupt"]
    assert out == ""


@pytest.mark.usefixtures("no_terminal")
def test_a_work_factor_above_the_limit_is_refused_not_damaged(
    sealed: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    passphrase: str,
) -> None:
    """An intact payload whose scrypt log_n (23) exceeds the decrypt limit (22).

    The binder refuses it before running scrypt, so this is quick.
    """

    def raise_work_factor(payload: bytes) -> bytes:
        raised, count = re.subn(
            rb"^(-> scrypt \S+) \d+$", rb"\g<1> 23", payload, count=1, flags=re.M
        )
        assert count == 1
        return raised

    raised = forge_archive(sealed, tmp_path / "wf23.tar.gz", raise_work_factor)
    monkeypatch.setenv(PASSPHRASE_ENV, passphrase)
    code, out, err, records = _verify(capsys, raised, "--authenticate")
    assert code == ExitCode.REFUSED
    assert out == ""
    assert "work factor" in err
    assert "damaged or tampered with" not in err
    failed = [r for r in records if r.get("event") == "command_failed"]
    assert [r["error"] for r in failed] == [WorkFactorTooHighError.__name__]
    decrypt = [r for r in records if r.get("event") == "member_decryption_failed"]
    assert [r["reason"] for r in decrypt] == ["work_factor_too_high"]


def test_the_two_authentication_failures_are_distinct() -> None:
    assert PassphraseRejectedError is not PayloadDamagedError
    assert not issubclass(PassphraseRejectedError, PayloadDamagedError)
    assert not issubclass(PayloadDamagedError, PassphraseRejectedError)
    assert PassphraseRejectedError.exit_code is ExitCode.VERIFICATION_FAILED
    assert PayloadDamagedError.exit_code is ExitCode.VERIFICATION_FAILED


# --------------------------------------------------------------------------
# The real backend runs every case above (E004-S01-003)
# --------------------------------------------------------------------------


def test_the_real_backend_authenticates(sealed: Path, passphrase: str) -> None:
    target = storage_target_from_cli(str(sealed))
    request = verify_cli.VerifyRequest(archive=target, authenticate=True)
    assert verify_cli.drive_verify(request, ctx=None, passphrase=passphrase).passed
    with pytest.raises(PassphraseRejectedError):
        verify_cli.drive_verify(request, ctx=None, passphrase=f"x-{passphrase}")


# --------------------------------------------------------------------------
# The inner Manifest (BL-E002-115, ADR-0024 amendment 2026-11-06)
# --------------------------------------------------------------------------


def _authenticate_records(
    archive: Path, passphrase: str
) -> tuple[BaseException | None, list[dict[str, Any]]]:
    request = verify_cli.VerifyRequest(
        archive=storage_target_from_cli(str(archive)), authenticate=True
    )
    raised: BaseException | None = None
    with capture_logs() as logs:
        try:
            verify_cli.drive_verify(request, ctx=None, passphrase=passphrase)
        except PayloadDamagedError as exc:
            raised = exc
    return raised, [dict(e) for e in logs]


def _keep(members: InnerMembers) -> InnerMembers:
    return members


@pytest.mark.parametrize(
    ("edit", "field"),
    [
        (lambda m: m.update(backup_scope="files"), "backup_scope"),
        (lambda m: m["source"].update(db_name="elsewhere"), "source.db_name"),
        (
            lambda m: m["members"][0].update(size=m["members"][0]["size"] + 1),
            "members[0].size",
        ),
        (lambda m: m["members"].pop(), "members"),
    ],
    ids=["scope", "source", "member-size", "member-removed"],
)
def test_an_outer_manifest_edited_outside_the_payload_is_refused(
    sealed: Path,
    tmp_path: Path,
    passphrase: str,
    edit: Callable[[dict[str, Any]], None],
    field: str,
) -> None:
    forged = forge_archive(
        sealed, tmp_path / "edited.tar.gz", lambda p: p, manifest_edit=edit
    )
    keyless = verify_cli.drive_verify(
        verify_cli.VerifyRequest(archive=storage_target_from_cli(str(forged))),
        ctx=None,
    )
    assert keyless.passed, "keyless verify cannot see an edit outside the payload"
    raised, logs = _authenticate_records(forged, passphrase)
    assert isinstance(raised, PayloadDamagedError)
    assert raised.field == field
    assert field in str(raised)
    record = next(e for e in logs if e.get("phase") == "VERIFY")
    assert record["reason"] == "inner_manifest_mismatch"
    assert record["field"] == field
    assert record["exit_code"] == ExitCode.VERIFICATION_FAILED
    assert "elsewhere" not in str(raised) + repr(record), "a value, not a path"


def _without_inner(members: InnerMembers) -> InnerMembers:
    return [m for m in members if m[0].name != INNER_MANIFEST_MEMBER]


def _inner_first(members: InnerMembers) -> InnerMembers:
    inner = [m for m in members if m[0].name == INNER_MANIFEST_MEMBER]
    return inner + _without_inner(members)


def _inner_twice(members: InnerMembers) -> InnerMembers:
    inner = [m for m in members if m[0].name == INNER_MANIFEST_MEMBER]
    return _without_inner(members) + inner + inner


@pytest.mark.parametrize(
    ("layout", "reason"),
    [
        (_without_inner, "inner_manifest_missing"),
        (_inner_first, "inner_manifest_misplaced"),
        (_inner_twice, "inner_manifest_duplicated"),
    ],
    ids=["removed", "moved", "duplicated"],
)
def test_an_inner_manifest_removed_moved_or_duplicated_is_refused(
    sealed: Path,
    tmp_path: Path,
    passphrase: str,
    layout: Callable[[InnerMembers], InnerMembers],
    reason: str,
) -> None:
    cipher = PassphraseCipher(passphrase)
    forged = forge_archive(sealed, tmp_path / "relaid.tar.gz", reseal(cipher, layout))
    raised, logs = _authenticate_records(forged, passphrase)
    assert isinstance(raised, PayloadDamagedError)
    record = next(e for e in logs if e.get("phase") == "VERIFY")
    assert record["reason"] == reason
    assert record["exit_code"] == ExitCode.VERIFICATION_FAILED


def test_a_payload_swapped_in_from_another_archive_is_refused(
    sealed: Path, tmp_path: Path, passphrase: str
) -> None:
    """RT-2: another Archive's payload, same passphrase, checksums recomputed."""
    from tests.restore_harness import write_v1

    (tmp_path / "other").mkdir()
    other = write_v1(tmp_path / "other", cipher=PassphraseCipher(passphrase))  # type: ignore[arg-type]
    with tarfile.open(other) as outer:
        body = outer.extractfile(outer.getnames()[1])
        assert body is not None
        swapped_in = body.read()
    forged = forge_archive(sealed, tmp_path / "swapped.tar.gz", lambda _: swapped_in)
    keyless = verify_cli.drive_verify(
        verify_cli.VerifyRequest(archive=storage_target_from_cli(str(forged))),
        ctx=None,
    )
    assert keyless.passed, "keyless verify cannot see a swap with recomputed checksums"
    raised, logs = _authenticate_records(forged, passphrase)
    assert isinstance(raised, PayloadDamagedError)
    assert raised.field is not None
    record = next(e for e in logs if e.get("phase") == "VERIFY")
    assert record["reason"] == "inner_manifest_mismatch"
    assert record["field"] == raised.field


def test_a_matching_pair_passes_after_a_reseal(
    sealed: Path, tmp_path: Path, passphrase: str
) -> None:
    """The control for the cases above: the reseal itself changes nothing."""
    cipher = PassphraseCipher(passphrase)
    forged = forge_archive(sealed, tmp_path / "same.tar.gz", reseal(cipher, _keep))
    raised, logs = _authenticate_records(forged, passphrase)
    assert raised is None
    record = next(e for e in logs if e.get("phase") == "VERIFY")
    assert record["authenticated"] is True
    assert "field" not in record


def test_an_older_genuine_archive_passes_a_rollback_is_never_detected(
    sealed: Path,
    tmp_path: Path,
    passphrase: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-21: a whole older Archive under the same passphrase is authentic."""
    newer = seal_archive(tmp_path, passphrase)
    assert newer.read_bytes() != sealed.read_bytes()
    newer.unlink()
    shutil.copyfile(sealed, newer)  # the older one, put back in place
    monkeypatch.setattr(verify_cli, "terminal_prompt", lambda: None)
    monkeypatch.setenv(PASSPHRASE_ENV, passphrase)
    code, out, _, _ = _verify(capsys, newer, "--authenticate")
    assert code == ExitCode.OK
    assert verify_cli.ROLLBACK_LIMIT in out


def test_the_help_states_the_rollback_limit() -> None:
    """N-4: read from the built parser, not from README.md."""
    from l3io.wp.backup.cli.parser import build_parser

    parser = build_parser()
    verify = next(
        action
        for action in parser._subparsers._group_actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    ).choices["verify"]
    help_text = " ".join(verify.format_help().split())
    assert "does not show the archive is the latest one" in help_text
    assert "an older archive under the same passphrase also passes" in help_text
