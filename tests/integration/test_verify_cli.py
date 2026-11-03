"""FR-3 — ``wpbackup2 verify`` against the committed fixture, a corrupted copy
and an in-test v0 Archive. Unmarked: it needs no database.
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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "archives" / "v1" / "minimal.tar.gz"


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


def test_a_v0_archive_passes_structural_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = tmp_path / "old.tar.gz"
    with tarfile.open(old, mode="w:gz") as out:
        info = tarfile.TarInfo(f"{V0_SITE_ROOT}/index.php")
        info.size = 5
        out.addfile(info, io.BytesIO(b"<?php"))
    assert main(["verify", "--archive", str(old)]) == 0
    out = capsys.readouterr().out
    assert "integrity STRUCTURAL_ONLY (format v0)" in out
    assert "integrity was not checked" in out


def test_a_missing_archive_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["verify", "--archive", str(tmp_path / "absent.tar.gz")])
        == ExitCode.FAILED
    )
    assert "does not exist" in capsys.readouterr().err


def test_a_remote_target_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["verify", "--archive", "s3:///bucket/a.tar.gz"]) == ExitCode.REFUSED
    assert "unsupported storage scheme" in capsys.readouterr().err


def test_archive_is_required() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["verify"])
    assert caught.value.code == ExitCode.USAGE
