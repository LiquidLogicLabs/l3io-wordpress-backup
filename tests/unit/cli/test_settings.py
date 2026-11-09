"""Owner decision 60, ADR-0028 — the layered settings, behaviour by behaviour.

Precedence (flag, variable, config file, parser default); the files read and
when; the refusals (an exposed secret, an unknown key, a bad value), each
naming the file or the variable and never a value; secrets registered before
the Run; a required option satisfied by a variable or a key. Each test plants a
unique sentinel and the scope of what it drives is one setting at a time.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli import main, settings
from l3io.wp.backup.cli.parser import build_parser
from l3io.wp.backup.cli.settings import parse_layered, user_config_file
from l3io.wp.backup.domain.secret import Secret
from tests.conftest import pipeline_events

RESTORE = ["restore", "--wp-dir", "/srv/site", "--archive", "a.tar.gz"]


def _config(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _layered(argv: list[str]) -> settings.LayeredArguments:
    return parse_layered(build_parser(), argv)


# --------------------------------------------------------------------------
# Precedence
# --------------------------------------------------------------------------


def test_a_flag_wins_over_a_variable_over_the_file_over_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path / "c.toml", 'mode = "files"\nsnapshot_dir = "/f"\n')
    monkeypatch.setenv("L3IO_WP_BACKUP_CONFIG", str(config))
    monkeypatch.setenv("L3IO_WP_BACKUP_MODE", "db")
    layered = _layered([*RESTORE, "--mode", "all"])
    assert layered.namespace.mode == "all"
    assert layered.origins["mode"] == "command line"
    layered = _layered(RESTORE)
    assert layered.namespace.mode == "db"
    assert layered.origins["mode"] == "environment"
    monkeypatch.delenv("L3IO_WP_BACKUP_MODE")
    layered = _layered(RESTORE)
    assert layered.namespace.mode == "files"
    assert layered.origins["mode"] == "config file"
    assert layered.namespace.snapshot_dir == "/f"
    monkeypatch.delenv("L3IO_WP_BACKUP_CONFIG")
    layered = _layered(RESTORE)
    assert layered.namespace.mode == "all"
    assert layered.origins["mode"] == "default"


def test_a_required_option_is_satisfied_by_a_variable_or_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("L3IO_WP_BACKUP_WP_DIR", "/srv/from-env")
    config = _config(tmp_path / "c.toml", 'archive = "latest"\n')
    layered = _layered(["--config", str(config), "restore"])
    assert layered.namespace.wp_dir == "/srv/from-env"
    assert layered.namespace.archive == "latest"
    assert layered.config_files == (config,)


def test_a_missing_required_option_is_argparses_own_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        _layered(["restore", "--wp-dir", "/srv/site"])
    assert exited.value.code == 2
    assert "the following arguments are required: --archive" in (
        capsys.readouterr().err
    )


def test_the_help_shows_the_required_options_as_declared(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        _layered(["restore", "--help"])
    usage = capsys.readouterr().out
    assert "--wp-dir WP_DIR --archive" in " ".join(usage.split())


@pytest.mark.parametrize(
    ("variable", "value", "dest", "expected"),
    [
        ("L3IO_WP_BACKUP_FORCE", "true", "force", True),
        ("L3IO_WP_BACKUP_REQUIRE_ENCRYPTED", "1", "require_encrypted", True),
        ("L3IO_WP_BACKUP_ARCHIVE_DIR", "/backups", "archive_dir", "/backups"),
        ("L3IO_WP_BACKUP_DB_TLS", "verify-ca", "db_tls", "verify-ca"),
        ("L3IO_WP_BACKUP_LOG_LEVEL", "debug", "log_level", "DEBUG"),
    ],
)
def test_a_variable_is_parsed_by_its_kind_and_the_parsers_type(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    dest: str,
    expected: object,
) -> None:
    monkeypatch.setenv(variable, value)
    assert getattr(_layered(RESTORE).namespace, dest) == expected


def test_a_list_setting_is_a_json_list_in_a_variable_and_an_array_in_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = ["backup", "--wp-dir", "/srv/site"]
    monkeypatch.setenv("L3IO_WP_BACKUP_EXCLUDE", '["a/", "b.log"]')
    assert _layered(backup).namespace.exclude == ["a/", "b.log"]
    monkeypatch.delenv("L3IO_WP_BACKUP_EXCLUDE")
    config = _config(tmp_path / "c.toml", 'exclude = ["c/"]\n')
    assert _layered(["--config", str(config), *backup]).namespace.exclude == ["c/"]


# --------------------------------------------------------------------------
# Which files
# --------------------------------------------------------------------------


def test_the_user_file_is_read_over_the_system_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = _config(
        tmp_path / "etc" / "config.toml", 'mode = "db"\nsnapshot_dir = "/sys"\n'
    )
    monkeypatch.setattr(settings, "SYSTEM_CONFIG_FILE", system)
    user = _config(user_config_file(), 'mode = "files"\n')
    layered = _layered(RESTORE)
    assert layered.config_files == (system, user)
    assert layered.namespace.mode == "files"
    assert layered.namespace.snapshot_dir == "/sys"


def test_a_named_file_replaces_the_default_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(user_config_file(), 'mode = "files"\n')
    named = _config(tmp_path / "named.toml", 'snapshot_dir = "/n"\n')
    layered = _layered(["--config", str(named), *RESTORE])
    assert layered.config_files == (named,)
    assert layered.namespace.mode == "all"


@pytest.mark.parametrize("spelling", ["flag", "variable"])
def test_a_named_file_that_does_not_exist_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spelling: str,
) -> None:
    missing = tmp_path / "missing.toml"
    argv = list(RESTORE)
    if spelling == "flag":
        argv = ["--config", str(missing), *argv]
    else:
        monkeypatch.setenv("L3IO_WP_BACKUP_CONFIG", str(missing))
    with pytest.raises(SystemExit) as exited:
        _layered(argv)
    assert exited.value.code == 2
    assert f"config file {missing} does not exist" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Refusals: never a value
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
@pytest.mark.parametrize(
    "key", ["passphrase", "db_password", "admin_password", "new_db_password"]
)
def test_a_readable_file_holding_a_secret_is_refused_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: int, key: str
) -> None:
    secret = f"sentinel-{uuid.uuid4().hex}"
    config = _config(tmp_path / "c.toml", f'{key} = "{secret}"\n', mode)
    with pytest.raises(SystemExit) as exited:
        _layered(["--config", str(config), *RESTORE])
    err = capsys.readouterr().err
    assert exited.value.code == 2
    assert f"config file {config} holds a secret ({key})" in err
    assert secret not in err


def test_a_readable_file_without_a_secret_is_read(tmp_path: Path) -> None:
    config = _config(tmp_path / "c.toml", 'mode = "db"\n', 0o644)
    assert _layered(["--config", str(config), *RESTORE]).namespace.mode == "db"


def test_an_unknown_key_is_refused_naming_the_file_and_the_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path / "c.toml", 'wp-dir = "/srv/site"\n')
    with pytest.raises(SystemExit):
        _layered(["--config", str(config), *RESTORE])
    err = capsys.readouterr().err
    assert f"config file {config}: unknown setting 'wp-dir'" in err


def test_a_file_that_is_not_toml_is_refused_without_its_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = f"sentinel-{uuid.uuid4().hex}"
    config = _config(tmp_path / "c.toml", f"passphrase = {secret}\n")
    with pytest.raises(SystemExit):
        _layered(["--config", str(config), *RESTORE])
    err = capsys.readouterr().err
    assert f"config file {config} is not valid TOML" in err
    assert secret not in err


def test_a_bad_value_names_its_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("L3IO_WP_BACKUP_DB_TLS", "sometimes")
    with pytest.raises(SystemExit):
        _layered(RESTORE)
    assert "L3IO_WP_BACKUP_DB_TLS: invalid choice 'sometimes'" in (
        capsys.readouterr().err
    )


def test_a_bad_key_value_names_its_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path / "c.toml", "force = 3\n")
    with pytest.raises(SystemExit):
        _layered(["--config", str(config), *RESTORE])
    err = capsys.readouterr().err
    assert "setting force from key 'force' in" in err


# --------------------------------------------------------------------------
# Secrets, and the root
# --------------------------------------------------------------------------


def test_a_secret_is_held_as_a_secret_from_every_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = f"sentinel-{uuid.uuid4().hex}"
    config = _config(tmp_path / "c.toml", f'passphrase = "{value}"\n')
    for argv in (
        ["--config", str(config), *RESTORE],
        [*RESTORE, "--passphrase", value],
    ):
        layered = _layered(argv)
        assert isinstance(layered.namespace.passphrase, Secret)
        assert layered.namespace.passphrase.reveal() == value
        assert value not in repr(layered.namespace.passphrase)
    monkeypatch.setenv("L3IO_WP_BACKUP_PASSPHRASE", value)
    layered = _layered(RESTORE)
    assert isinstance(layered.namespace.passphrase, Secret)
    assert layered.origins["passphrase"] == "environment"


def test_the_root_registers_a_config_files_secrets_before_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    value = f"sentinel-{uuid.uuid4().hex}"
    config = _config(tmp_path / "c.toml", f'admin_password = "{value}"\n')
    registries: list[SecretRegistry] = []
    real = SecretRegistry.register

    def register(self: SecretRegistry, secret: object, *, source: str = "") -> None:
        registries.append(self)
        real(self, secret, source=source)  # type: ignore[arg-type]

    monkeypatch.setattr(SecretRegistry, "register", register)
    argv = ["--config", str(config), "verify", "--archive", str(tmp_path / "none")]
    main(argv)
    assert registries
    assert value not in registries[0].redact(f"x {value} x")
    assert all(value not in repr(e) for e in pipeline_events(caplog))


def test_an_unknown_variable_is_warned_of_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("L3IO_WP_BACKUP_WPDIR", "/srv/typo")
    main(["verify", "--archive", str(tmp_path / "none")])
    (warning,) = [
        e for e in pipeline_events(caplog) if e["event"] == "unknown_setting_variable"
    ]
    assert warning["variable"] == "L3IO_WP_BACKUP_WPDIR"
    assert "/srv/typo" not in repr(warning)


def test_a_variable_name_is_read_whatever_its_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pydantic-settings matches names case-insensitively (ADR-0028)."""
    monkeypatch.setenv("l3io_wp_backup_mode", "files")
    assert _layered(RESTORE).namespace.mode == "files"
    assert "l3io_wp_backup_mode" in os.environ
