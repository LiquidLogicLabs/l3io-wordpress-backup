"""AD-18.2/AD-18.4 — ``WpConfigWriter``, the one writer of ``wp-config.php``.

Ordinary assertions against the pinned ``l3io-wordpress-config`` (0.1.6): mode
and owner kept, file and directory fsynced, read-back failures leave the
original byte-identical, one atomic write per key, and the ADR-0023 file
effects (``save_copy``, ``replace_file``) keep line endings byte for byte and
never write the replaced file's ``DB_*`` values.

Runs as root too (``make ci``): an owner that cannot be kept is forced through
``os.fstat``/``os.fchown`` doubles, and the one permission-bit case skips as
root, where the bit is not enforced.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

pytest.importorskip("l3io.wp.config")

from l3io.wp.config import WpConfigString
from l3io.wp.config.adapters import config_file as library_file

from l3io.wp.backup.adapters import config_writer as writer_module
from l3io.wp.backup.adapters.config_writer import (
    WHOLE_FILE,
    WpConfigWriter,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.domain.config import ConfigWriteResult
from l3io.wp.backup.domain.errors import ConfigWriteFailedError
from l3io.wp.backup.ports.config import ConfigCommand

TARGET_PASSWORD = "target-secret-9f1c2e"
SOURCE_PASSWORD = "source-secret-7a3b4d"


def _config(eol: str, *, name: str, user: str, password: str, host: str) -> str:
    lines = [
        "<?php",
        f"define( 'DB_NAME', '{name}' );",
        f"define( 'DB_USER', '{user}' );",
        f"define( 'DB_PASSWORD', '{password}' );",
        f"define( 'DB_HOST', '{host}' );",
        "define( 'WP_DEBUG', false );",
        "$table_prefix = 'wp_';",
        "require_once ABSPATH . 'wp-settings.php';",
        "",
    ]
    return eol.join(lines)


TARGET_KEEP = {
    "DB_NAME": "target_db",
    "DB_USER": "target_user",
    "DB_PASSWORD": TARGET_PASSWORD,
    "DB_HOST": "target-host",
}
#: What ``replace_file`` is told to keep: the names; the values are the target's.
KEEP = tuple(TARGET_KEEP)
SOURCE_VALUES = {
    "name": "source_db",
    "user": "source_user",
    "password": SOURCE_PASSWORD,
    "host": "source-host",
}


@pytest.fixture
def secrets() -> SecretRegistry:
    return SecretRegistry()


@pytest.fixture
def writer(secrets: SecretRegistry) -> WpConfigWriter:
    return WpConfigWriter(secrets)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    # The trailing comment is the target's own: with it, a replacement whose
    # kept DB_* equal the target's still differs, so replace_file writes
    # (an identical text is not rewritten, ``replace_content`` returns False).
    path = tmp_path / "site" / "wp-config.php"
    path.parent.mkdir()
    path.write_bytes(
        (
            _config(
                "\r\n",
                name="target_db",
                user="target_user",
                password=TARGET_PASSWORD,
                host="target-host",
            )
            + "// the target's own line\r\n"
        ).encode()
    )
    path.chmod(0o640)
    return path


def _temps(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.endswith(".l3io-tmp")]


def _no_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer is neither root nor the owner: ``fchown`` is refused."""
    real_fstat = os.fstat

    def fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        fields = list(st[:10])
        fields[4] = st.st_uid + 1  # the temp file is not the original's owner
        return os.stat_result(fields)

    def fchown(*_: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "fchown", fchown)


def test_it_satisfies_the_command_protocol(writer: WpConfigWriter) -> None:
    assert isinstance(writer, ConfigCommand)


# --------------------------------------------------------------------------
# read_constants
# --------------------------------------------------------------------------


def test_read_constants_reads_and_registers_the_password(
    writer: WpConfigWriter, config: Path, secrets: SecretRegistry
) -> None:
    values = writer.read_constants(config, ["DB_NAME", "DB_PASSWORD", "WP_HOME"])
    assert values == {
        "DB_NAME": "target_db",
        "DB_PASSWORD": TARGET_PASSWORD,
        "WP_HOME": None,
    }
    assert TARGET_PASSWORD not in secrets.redact(TARGET_PASSWORD)


# --------------------------------------------------------------------------
# set_constants
# --------------------------------------------------------------------------


def test_set_constants_keeps_a_0640_mode_and_reports_the_changed_keys(
    writer: WpConfigWriter, config: Path
) -> None:
    owner = config.stat().st_uid
    result = writer.set_constants(config, {"DB_NAME": "moved", "WP_DEBUG": False})
    assert result == ConfigWriteResult(changed=("DB_NAME",), performed=True)
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert config.stat().st_uid == owner
    text = config.read_bytes().decode()
    assert "'moved'" in text
    assert "\r\n" in text
    assert "\n" not in text.replace("\r\n", "")


def test_set_constants_fsyncs_the_file_and_its_directory(
    writer: WpConfigWriter, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[tuple[int, bool]] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        st = os.fstat(fd)
        synced.append((st.st_ino, stat.S_ISDIR(st.st_mode)))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    writer.set_constants(config, {"DB_NAME": "moved"})
    assert (config.stat().st_ino, False) in synced
    assert (config.parent.stat().st_ino, True) in synced


def test_a_read_back_failure_leaves_the_original_byte_identical(
    writer: WpConfigWriter, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = config.read_bytes()
    monkeypatch.setattr(library_file, "verify_write", lambda **_: ["forced"])
    with pytest.raises(ConfigWriteFailedError) as caught:
        writer.set_constants(config, {"DB_NAME": "moved"})
    assert caught.value.key == "DB_NAME"
    assert "read-back" in caught.value.reason
    assert type(caught.value.__cause__).__name__ == "ReadBackError"
    assert caught.value.written == ()
    assert config.read_bytes() == before
    assert _temps(config.parent) == []


def test_each_key_is_its_own_atomic_write_in_order(
    writer: WpConfigWriter, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[list[str]] = []
    real_verify = library_file.verify_write

    def verify(**kwargs: Any) -> list[str]:  # noqa: ANN401
        writes.append([key for _, key in kwargs["written"]])
        return real_verify(**kwargs)

    monkeypatch.setattr(library_file, "verify_write", verify)
    writer.set_constants(config, {"DB_HOST": "h2", "DB_NAME": "n2", "DB_USER": "u2"})
    assert writes == [["DB_HOST"], ["DB_NAME"], ["DB_USER"]]


def test_a_four_key_call_failing_at_the_third_keeps_exactly_the_first_two(
    writer: WpConfigWriter, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = config.read_bytes().decode()
    real_verify = library_file.verify_write

    def verify(**kwargs: Any) -> list[str]:  # noqa: ANN401
        if ("define", "DB_USER") in kwargs["written"]:
            return ["forced"]
        return real_verify(**kwargs)

    monkeypatch.setattr(library_file, "verify_write", verify)
    values = {
        "DB_NAME": "n2",
        "DB_HOST": "h2",
        "DB_USER": "u2",
        "DB_PASSWORD": "p2-long",
    }
    with pytest.raises(ConfigWriteFailedError) as caught:
        writer.set_constants(config, values)
    assert caught.value.key == "DB_USER"
    assert caught.value.written == ("DB_NAME", "DB_HOST")
    expected = WpConfigString(original)
    expected.set("DB_NAME", "n2")
    expected.set("DB_HOST", "h2")
    assert config.read_bytes().decode() == expected.content


def test_an_unrenderable_value_fails_and_leaves_the_file_unchanged(
    writer: WpConfigWriter, config: Path
) -> None:
    before = config.read_bytes()
    with pytest.raises(ConfigWriteFailedError) as caught:
        writer.set_constants(config, {"DB_NAME": object()})  # type: ignore[dict-item]
    assert caught.value.reason == "unrenderable"
    assert type(caught.value.__cause__).__name__ == "ConfigValueError"
    assert config.read_bytes() == before


def test_a_key_already_at_its_value_is_not_rewritten(
    writer: WpConfigWriter, config: Path
) -> None:
    before = config.stat()
    result = writer.set_constants(config, {"DB_NAME": "target_db"})
    assert result.changed == ()
    assert result.performed
    after = config.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_values_never_reach_a_record_and_the_password_is_registered(
    writer: WpConfigWriter, config: Path, secrets: SecretRegistry
) -> None:
    new_password = "relocated-password-51e0"
    with capture_logs() as logs:
        writer.set_constants(config, {"DB_PASSWORD": new_password, "DB_NAME": "n3"})
    (record,) = [e for e in logs if e["event"] == "config_written"]
    assert record["keys"] == ["DB_PASSWORD", "DB_NAME"]
    assert "duration_ms" in record
    assert all(new_password not in repr(e) and "n3" not in repr(e) for e in logs)
    assert new_password not in secrets.redact(new_password)


def test_an_absent_config_is_refused_and_never_created(
    writer: WpConfigWriter, tmp_path: Path
) -> None:
    missing = tmp_path / "wp-config.php"
    with pytest.raises(ConfigWriteFailedError, match="does not exist"):
        writer.set_constants(missing, {"DB_NAME": "x"})
    assert not missing.exists()


def test_an_unkeepable_owner_is_refused_and_the_file_untouched(
    writer: WpConfigWriter, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = config.read_bytes()
    _no_owner(monkeypatch)
    with pytest.raises(ConfigWriteFailedError) as caught:
        writer.set_constants(config, {"DB_NAME": "moved"})
    assert "owner cannot be kept" in caught.value.reason
    assert type(caught.value.__cause__).__name__ == "ConfigWriteError"
    assert config.read_bytes() == before
    assert _temps(config.parent) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores a directory's mode")
def test_an_unwritable_directory_is_refused_and_the_file_untouched(
    writer: WpConfigWriter, config: Path
) -> None:
    before = config.read_bytes()
    config.parent.chmod(0o500)
    try:
        with pytest.raises(ConfigWriteFailedError):
            writer.set_constants(config, {"DB_NAME": "moved"})
    finally:
        config.parent.chmod(0o700)
    assert config.read_bytes() == before
    assert _temps(config.parent) == []


# --------------------------------------------------------------------------
# save_copy
# --------------------------------------------------------------------------


def test_save_copy_writes_an_owner_only_copy(
    writer: WpConfigWriter, config: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "saved" / "wp-config.php.saved"
    destination.parent.mkdir()
    result = writer.save_copy(config, destination)
    assert result == ConfigWriteResult(changed=(), performed=True)
    assert destination.read_bytes() == config.read_bytes()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert _temps(destination.parent) == []


def test_save_copy_creates_a_missing_directory_owner_only(
    writer: WpConfigWriter, config: Path, tmp_path: Path
) -> None:
    """M-4: the command creates the snapshot directory, so a Dry Run's twin does not."""
    destination = tmp_path / "state" / "snapshots" / "wp-config.php.saved"
    writer.save_copy(config, destination)
    assert destination.read_bytes() == config.read_bytes()
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700


def test_save_copy_into_an_uncreatable_directory_is_a_package_error(
    writer: WpConfigWriter, config: Path, tmp_path: Path
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    with pytest.raises(ConfigWriteFailedError):
        writer.save_copy(config, blocker / "snapshots" / "wp-config.php.saved")


# --------------------------------------------------------------------------
# replace_file
# --------------------------------------------------------------------------


class _WriteSpy:
    """A file handle that records every ``write`` before passing it on."""

    def __init__(self, inner: Any, seen: list[str]) -> None:  # noqa: ANN401
        self._inner, self._seen = inner, seen

    def write(self, data: str) -> int:
        self._seen.append(data)
        return int(self._inner.write(data))

    def __enter__(self) -> _WriteSpy:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self._inner, name)


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real_fdopen = os.fdopen

    def fdopen(*args: Any, **kwargs: Any) -> _WriteSpy:  # noqa: ANN401
        return _WriteSpy(real_fdopen(*args, **kwargs), seen)

    monkeypatch.setattr(writer_module.os, "fdopen", fdopen)
    return seen


def _source(tmp_path: Path, eol: str) -> str:
    """The Archive's config as ``replace_file`` receives it: text, no file."""
    del tmp_path
    return _config(eol, **SOURCE_VALUES)


@pytest.mark.parametrize("eol", ["\r\n", "\n"])
def test_replace_file_keeps_line_endings_and_writes_only_the_kept_values(
    writer: WpConfigWriter,
    config: Path,
    tmp_path: Path,
    eol: str,
    written: list[str],
) -> None:
    seen = written
    source = _source(tmp_path, eol)
    result = writer.replace_file(config, source, keep=KEEP)
    assert result.performed
    assert set(result.changed) == set(TARGET_KEEP)
    expected = source.encode()
    for old, new in zip(SOURCE_VALUES.values(), TARGET_KEEP.values(), strict=True):
        expected = expected.replace(f"'{old}'".encode(), f"'{new}'".encode())
    assert config.read_bytes() == expected
    assert WpConfigString(config.read_bytes().decode()).get("DB_PASSWORD") == (
        TARGET_PASSWORD
    )
    assert seen, "the write spy saw nothing"
    for value in SOURCE_VALUES.values():
        assert all(value not in data for data in seen)


def test_replace_file_keeps_a_0640_mode_and_the_owner(
    writer: WpConfigWriter, config: Path, tmp_path: Path
) -> None:
    owner = (config.stat().st_uid, config.stat().st_gid)
    writer.replace_file(config, _source(tmp_path, "\r\n"), keep=KEEP)
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert (config.stat().st_uid, config.stat().st_gid) == owner
    assert _temps(config.parent) == []


def test_a_failed_replace_leaves_the_original_byte_identical(
    writer: WpConfigWriter,
    config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = config.read_bytes()

    def refuse(self: Path, target: object) -> Path:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(ConfigWriteFailedError) as caught:
        writer.replace_file(config, _source(tmp_path, "\r\n"), keep=KEEP)
    assert caught.value.key == WHOLE_FILE
    assert config.read_bytes() == before
    assert _temps(config.parent) == []


def test_replace_file_refuses_when_the_owner_cannot_be_kept(
    writer: WpConfigWriter,
    config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = config.read_bytes()
    source = _source(tmp_path, "\r\n")
    _no_owner(monkeypatch)
    with pytest.raises(ConfigWriteFailedError) as caught:
        writer.replace_file(config, source, keep=KEEP)
    assert "owner cannot be kept" in caught.value.reason
    assert config.read_bytes() == before
    assert _temps(config.parent) == []


def test_replace_file_refuses_a_keep_the_target_does_not_define(
    writer: WpConfigWriter, config: Path, tmp_path: Path
) -> None:
    text = config.read_bytes().decode()
    config.write_bytes(
        text.replace("define( 'DB_HOST', 'target-host' );\r\n", "").encode()
    )
    before = config.read_bytes()
    with pytest.raises(ConfigWriteFailedError) as caught:
        writer.replace_file(config, _source(tmp_path, "\n"), keep=KEEP)
    assert caught.value.key == "DB_HOST"
    assert "does not define it" in caught.value.reason
    assert config.read_bytes() == before


def test_replace_file_refuses_an_absent_config(
    writer: WpConfigWriter, tmp_path: Path
) -> None:
    missing = tmp_path / "site" / "wp-config.php"
    missing.parent.mkdir()
    with pytest.raises(ConfigWriteFailedError, match="does not exist"):
        writer.replace_file(missing, _source(tmp_path, "\n"), keep=KEEP)
    assert not missing.exists()


def test_replace_file_registers_both_passwords(
    writer: WpConfigWriter, config: Path, tmp_path: Path, secrets: SecretRegistry
) -> None:
    writer.replace_file(config, _source(tmp_path, "\n"), keep=KEEP)
    redacted = secrets.redact(f"{TARGET_PASSWORD} {SOURCE_PASSWORD}")
    assert TARGET_PASSWORD not in redacted
    assert SOURCE_PASSWORD not in redacted


# --------------------------------------------------------------------------
# replace_file delegates its write (BL-E002-091, owner decision 34)
# --------------------------------------------------------------------------


def test_replace_file_writes_through_the_library_replace_content(
    writer: WpConfigWriter,
    config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one whole-file write is ``WpConfigFile.replace_content``; none of ours."""
    calls: list[tuple[Path, str]] = []
    real = library_file.WpConfigFile.replace_content

    def spy(self: Any, content: str) -> bool:  # noqa: ANN401
        calls.append((Path(self._path), content))
        return bool(real(self, content))

    monkeypatch.setattr(library_file.WpConfigFile, "replace_content", spy)
    writer.replace_file(config, _source(tmp_path, "\n"), keep=KEEP)
    assert [path for path, _ in calls] == [config]
    assert calls[0][1].encode() == config.read_bytes()
    assert not hasattr(writer_module.WpConfigWriter, "_write_over")


def test_replace_file_refuses_a_replacement_defining_a_key_twice(
    writer: WpConfigWriter, config: Path, tmp_path: Path
) -> None:
    before = config.read_bytes()
    twice = _source(tmp_path, "\n") + "define( 'WP_DEBUG', true );\n"
    with pytest.raises(ConfigWriteFailedError) as caught:
        writer.replace_file(config, twice, keep=KEEP)
    assert caught.value.key == WHOLE_FILE
    assert "read-back" in caught.value.reason
    assert config.read_bytes() == before
    assert _temps(config.parent) == []


def test_replace_file_with_the_same_text_changes_nothing(
    writer: WpConfigWriter, config: Path
) -> None:
    before = config.read_bytes()
    mtime = config.stat().st_mtime_ns
    result = writer.replace_file(config, before.decode(), keep=KEEP)
    assert result.performed
    assert result.changed == ()
    assert config.read_bytes() == before
    assert config.stat().st_mtime_ns == mtime
