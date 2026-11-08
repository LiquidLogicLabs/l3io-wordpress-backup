"""FR-3 — ``wpbackup2 verify`` against the committed fixture, a corrupted copy
and the committed v0 fixtures (FR-10: every Archive in ``v0/`` passes as
``STRUCTURAL_ONLY``, whatever its file name, and every one in ``v0/malformed/``
exits ``VERIFICATION_FAILED``). Unmarked: it needs no database.
"""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path

import pytest

from l3io.wp.backup.cli import main
from l3io.wp.backup.domain.archive_format import PAYLOAD_MEMBER, V0_SITE_ROOT
from l3io.wp.backup.domain.exit_codes import ExitCode
from tests.archive_fixtures import archive_files

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "archives" / "v1" / "minimal.tar.gz"
V0 = PROJECT_ROOT / "tests" / "fixtures" / "archives" / "v0"
V0_PASSING = archive_files(V0)
V0_MALFORMED = archive_files(V0 / "malformed")


def test_the_fixture_passes_verified(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["verify", "--archive", str(FIXTURE)]) == 0
    assert "OK, integrity VERIFIED (format v1)" in capsys.readouterr().out


def test_a_corrupted_copy_fails_naming_the_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    copy = tmp_path / "corrupted.tar.gz"
    shutil.copyfile(FIXTURE, copy)
    with tarfile.open(copy) as outer:
        offset = outer.getmember(PAYLOAD_MEMBER).offset_data + 100
    data = bytearray(copy.read_bytes())
    data[offset] ^= 0x01
    copy.write_bytes(data)
    assert (
        main(["verify", "--archive", f"file://{copy}"]) == ExitCode.VERIFICATION_FAILED
    )
    err = capsys.readouterr().err
    assert f"CHECKSUM_MISMATCH ({PAYLOAD_MEMBER!r})" in err
    # BL-E002-055: a job verifying several Archives can tell which one failed.
    assert repr(str(copy)) in err


@pytest.mark.parametrize("archive", V0_PASSING, ids=lambda p: p.name)
def test_a_v0_archive_passes_structural_only(
    archive: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["verify", "--archive", str(archive)]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "integrity STRUCTURAL_ONLY (format v0)" in out
    assert "integrity was not checked" in out
    assert "VERIFIED" not in out


def test_a_v0_archive_without_a_site_root_directory_member_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """BL-E004-048 (review F6): every committed fixture carries the ``wp-root``
    directory member; this one holds only ``wp-root/index.php`` (no directory
    member, no dump, no wp-config.php) and still verifies end to end."""
    archive = tmp_path / "dirless.tar.gz"
    with tarfile.open(archive, mode="w:gz") as out:
        info = tarfile.TarInfo(f"{V0_SITE_ROOT}/index.php")
        info.size = len(b"<?php\n")
        out.addfile(info, io.BytesIO(b"<?php\n"))
    with tarfile.open(archive) as check:
        assert check.getnames() == [f"{V0_SITE_ROOT}/index.php"]
    assert main(["verify", "--archive", str(archive)]) == ExitCode.OK
    assert "integrity STRUCTURAL_ONLY (format v0)" in capsys.readouterr().out


@pytest.mark.parametrize("archive", V0_MALFORMED, ids=lambda p: p.name)
def test_a_malformed_v0_archive_fails_verification(
    archive: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["verify", "--archive", str(archive)])
    assert code == ExitCode.VERIFICATION_FAILED
    assert "STRUCTURAL_ONLY" not in capsys.readouterr().out


def test_the_v0_fixture_sets_are_not_empty() -> None:
    assert V0_PASSING, f"no v0 fixtures in {V0}"
    assert V0_MALFORMED, f"no malformed v0 fixtures in {V0}/malformed"


def test_a_missing_archive_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["verify", "--archive", str(tmp_path / "absent.tar.gz")])
        == ExitCode.USAGE  # BL-E002-074 (owner decision 2026-11-07)
    )
    assert "does not exist" in capsys.readouterr().err


def test_a_remote_target_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["verify", "--archive", "s3:///bucket/a.tar.gz"]) == ExitCode.REFUSED
    assert "unsupported storage scheme" in capsys.readouterr().err


def test_archive_is_required() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["verify"])
    assert caught.value.code == ExitCode.USAGE
