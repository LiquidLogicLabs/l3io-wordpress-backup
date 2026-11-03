"""AD-20/AG-21 — ``LocalArchiveStore``: staged, verified, then renamed into place."""

from __future__ import annotations

import errno
import inspect
import os
import stat
from pathlib import Path, PurePosixPath

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.store import LocalArchiveStore
from l3io.wp.backup.domain.errors import (
    ArchiveExistsError,
    ArchiveWriteError,
    BackupError,
)
from l3io.wp.backup.domain.storage_target import StorageTarget


class VerifierFailed(BackupError):
    """What the failing verifier double raises."""


class Verifier:
    """Records each call; fails when told to. Checks the staged file exists."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[Path] = []

    def __call__(self, path: Path) -> None:
        assert path.is_file()
        self.calls.append(path)
        if self.fail:
            msg = "verifier double failed"
            raise VerifierFailed(msg)


def _target(path: Path) -> StorageTarget:
    return StorageTarget(scheme="file", path=PurePosixPath(path))


def _others(directory: Path, final: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p != final]


def test_the_verifier_has_no_default() -> None:
    parameter = inspect.signature(LocalArchiveStore).parameters["verify"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_a_staged_archive_is_owner_only_beside_the_final_name(tmp_path: Path) -> None:
    store = LocalArchiveStore(verify=Verifier())
    staged = store.stage(_target(tmp_path / "site.tar.gz"))
    try:
        (temp,) = list(tmp_path.iterdir())
        assert temp.parent == tmp_path
        assert temp.name != "site.tar.gz"
        assert stat.S_IMODE(temp.stat().st_mode) == 0o600
    finally:
        staged.discard()


def test_publish_verifies_once_then_renames_and_keeps_0600(tmp_path: Path) -> None:
    final = tmp_path / "site.tar.gz"
    verifier = Verifier()
    store = LocalArchiveStore(verify=verifier)
    staged = store.stage(_target(final))
    staged.stream.write(b"archive bytes")
    with capture_logs() as logs:
        assert staged.publish() is True
    assert final.read_bytes() == b"archive bytes"
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert len(verifier.calls) == 1
    assert verifier.calls[0] != final
    assert _others(tmp_path, final) == []
    phases = [e["phase"] for e in logs if e["event"] == "phase_completed"]
    assert phases == ["VERIFY", "PUBLISH"]
    assert store.exists(_target(final))


def test_a_failing_verifier_means_no_replace_and_no_staged_file(
    tmp_path: Path,
) -> None:
    final = tmp_path / "site.tar.gz"
    verifier = Verifier(fail=True)
    store = LocalArchiveStore(verify=verifier)
    staged = store.stage(_target(final))
    staged.stream.write(b"archive bytes")
    with capture_logs() as logs, pytest.raises(VerifierFailed):
        staged.publish()
    assert not final.exists()
    assert list(tmp_path.iterdir()) == []
    assert len(verifier.calls) == 1
    records = [e for e in logs if e["event"] == "phase_completed"]
    assert [(e["phase"], e["outcome"]) for e in records] == [("VERIFY", "failed")]


def test_an_existing_final_name_is_refused(tmp_path: Path) -> None:
    final = tmp_path / "site.tar.gz"
    final.write_bytes(b"previous")
    store = LocalArchiveStore(verify=Verifier())
    assert store.exists(_target(final))


def test_a_final_name_appearing_before_publish_is_refused(tmp_path: Path) -> None:
    final = tmp_path / "site.tar.gz"
    store = LocalArchiveStore(verify=Verifier())
    staged = store.stage(_target(final))
    staged.stream.write(b"new")
    final.write_bytes(b"raced in")
    with pytest.raises(ArchiveExistsError):
        staged.publish()
    assert final.read_bytes() == b"raced in"
    assert _others(tmp_path, final) == []


def test_a_dangling_symlink_at_the_final_name_counts_as_existing(
    tmp_path: Path,
) -> None:
    final = tmp_path / "site.tar.gz"
    final.symlink_to(tmp_path / "nowhere")
    assert LocalArchiveStore(verify=Verifier()).exists(_target(final))


def test_discard_on_an_exception_leaves_nothing(tmp_path: Path) -> None:
    final = tmp_path / "site.tar.gz"
    store = LocalArchiveStore(verify=Verifier())
    staged = store.stage(_target(final))
    try:
        staged.stream.write(b"half")
        msg = "the writer failed"
        raise RuntimeError(msg)  # noqa: TRY301 - a failure between stage and publish
    except RuntimeError:
        staged.discard()
    assert list(tmp_path.iterdir()) == []
    staged.discard()  # idempotent


def test_a_missing_destination_directory_is_an_archive_write_error(
    tmp_path: Path,
) -> None:
    final = tmp_path / "missing" / "site.tar.gz"
    with pytest.raises(ArchiveWriteError) as caught:
        LocalArchiveStore(verify=Verifier()).stage(_target(final))
    assert caught.value.path == final


def test_a_full_disk_mid_write_names_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = tmp_path / "site.tar.gz"
    store = LocalArchiveStore(verify=Verifier())
    staged = store.stage(_target(final))

    def full(fd: int) -> None:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    staged.stream.write(b"x" * 10)
    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(ArchiveWriteError) as caught:
        staged.publish()
    assert caught.value.path == final
    assert caught.value.reason == os.strerror(errno.ENOSPC)
    assert list(tmp_path.iterdir()) == []


def test_publish_after_discard_is_refused(tmp_path: Path) -> None:
    store = LocalArchiveStore(verify=Verifier())
    staged = store.stage(_target(tmp_path / "site.tar.gz"))
    staged.discard()
    with pytest.raises(ArchiveWriteError):
        staged.publish()
