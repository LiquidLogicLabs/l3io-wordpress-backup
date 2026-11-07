"""E008-S01-001 (gate D2 (b)) — ``verify`` refuses an unsupported platform too.

"Unsupported" means one behaviour on every command: ``verify``, which has no
plan and no preflight, raises ``PreflightFailedError(tool="platform")``
(``NOT_READY``) before it opens a Run or reads the Archive.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.cli import main
from l3io.wp.backup.cli import verify as verify_mod
from l3io.wp.backup.cli.exit import exit_code_for
from l3io.wp.backup.cli.verify import VerifyRequest, drive_verify
from l3io.wp.backup.domain.errors import PreflightFailedError
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.storage_target import StorageTarget


@pytest.mark.parametrize("platform", ["win32", "cygwin"])
def test_verify_on_windows_is_refused_before_the_archive_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    opened: list[object] = []
    monkeypatch.setattr(verify_mod, "join_or_open", lambda *a, **k: opened.append(a))
    read: list[object] = []
    monkeypatch.setattr(
        verify_mod, "verify_archive_manifest", lambda *a, **k: read.append(a)
    )
    request = VerifyRequest(
        archive=StorageTarget(scheme="file", path=PurePosixPath(tmp_path / "a.tgz")),
        authenticate=False,
    )
    with pytest.raises(PreflightFailedError) as caught:
        drive_verify(request, ctx=None)
    assert caught.value.tool == "platform"
    assert exit_code_for(caught.value) is ExitCode.NOT_READY
    assert opened == read == []


def test_the_cli_exits_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with capture_logs():
        code = main(["verify", "--archive", str(tmp_path / "missing.tar.gz")])
    assert code == int(ExitCode.NOT_READY)


def test_a_supported_platform_reaches_the_archive(tmp_path: Path) -> None:
    """The control: on Linux a missing Archive fails as missing, not as platform."""
    with capture_logs():
        code = main(["verify", "--archive", str(tmp_path / "missing.tar.gz")])
    assert code != int(ExitCode.NOT_READY)
