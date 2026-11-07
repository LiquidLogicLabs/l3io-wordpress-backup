"""E008-S01-001 (owner decision 2026-11-07, a) — the relocated password's sources.

``--new-db-password`` stays (AD-22); the password may also come from
``--new-db-password-file`` or ``L3IO_WP_BACKUP_NEW_DB_PASSWORD``, in that
order after the flag. Each value is registered with the Run's registry as it
is read and held to the ADR-0018 floor; the flag warns, naming itself only.
``restore --db-credentials-aws-*`` reaches ``RestoreRequest.credentials`` and
from there ``cli/credentials.wordpress_source`` (B1).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli import restore as restore_mod
from l3io.wp.backup.cli.credentials import (
    NEW_DB_PASSWORD_FILE_FLAG,
    NEW_DB_PASSWORD_FLAG,
    new_db_password,
)
from l3io.wp.backup.cli.parser import NEW_DB_PASSWORD_VAR, build_parser
from l3io.wp.backup.domain.credentials import AwsSecretCredentialSpec
from l3io.wp.backup.domain.errors import CredentialTooShortError, UsageError
from tests.restore_root_rig import Rig


def _plain(value: object) -> object:
    """The text of ``value``, whether a ``str`` or an opaque secret."""
    reveal = getattr(value, "reveal", None)
    return reveal() if callable(reveal) else value


def _secret(tag: str) -> str:
    return f"sentinel-{tag}-{uuid.uuid4().hex}"


class Spy(SecretRegistry):
    __slots__ = ("seen",)

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, str]] = []

    def register(self, value: str, *, source: str = "unspecified") -> None:
        self.seen.append((value, source))
        super().register(value, source=source)


def _resolve(
    *,
    explicit: str | None = None,
    file: Path | None = None,
    environ: dict[str, str] | None = None,
    user_given: bool = True,
    secrets: SecretRegistry | None = None,
) -> object:
    return _plain(
        new_db_password(
            explicit=explicit,
            file=file,
            environ=environ or {},
            secrets=SecretRegistry() if secrets is None else secrets,
            user_given=user_given,
        )
    )


def _file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "pw"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_each_source_wins_in_precedence_order(tmp_path: Path) -> None:
    flag, in_file, env = _secret("flag"), _secret("file"), _secret("env")
    path = _file(tmp_path, in_file + "\n")
    environ = {NEW_DB_PASSWORD_VAR: env}
    with capture_logs():
        assert _resolve(explicit=flag, file=path, environ=environ) == flag
    assert _resolve(file=path, environ=environ) == in_file
    assert _resolve(environ=environ) == env
    assert _resolve() is None


def test_the_environment_is_read_only_with_a_new_db_user() -> None:
    environ = {NEW_DB_PASSWORD_VAR: _secret("env")}
    assert _resolve(environ=environ, user_given=False) is None


def test_the_flag_warns_naming_itself_never_the_value() -> None:
    value = _secret("flag")
    with capture_logs() as logs:
        _resolve(explicit=value)
    warned = [r for r in logs if r["event"] == "password_on_command_line"]
    assert len(warned) == 1
    assert warned[0]["log_level"] == "warning"
    assert warned[0]["flag"] == NEW_DB_PASSWORD_FLAG
    assert value not in repr(logs)


def test_the_file_and_environment_do_not_warn(tmp_path: Path) -> None:
    with capture_logs() as logs:
        _resolve(file=_file(tmp_path, _secret("file")))
        _resolve(environ={NEW_DB_PASSWORD_VAR: _secret("env")})
    assert [r for r in logs if r["event"] == "password_on_command_line"] == []


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_the_file_gives_its_first_line_without_its_ending(
    tmp_path: Path, ending: str
) -> None:
    value = _secret("file")
    assert _resolve(file=_file(tmp_path, f"{value}{ending}second line\n")) == value


def test_a_file_with_no_line_ending_is_read_whole(tmp_path: Path) -> None:
    value = _secret("file")
    assert _resolve(file=_file(tmp_path, value)) == value


def test_an_unreadable_file_is_usage_naming_the_flag(tmp_path: Path) -> None:
    with pytest.raises(UsageError) as caught:
        _resolve(file=tmp_path / "missing")
    assert NEW_DB_PASSWORD_FILE_FLAG in str(caught.value)
    assert caught.value.arguments == (NEW_DB_PASSWORD_FILE_FLAG,)


def test_every_source_registers_its_value(tmp_path: Path) -> None:
    for kwargs in (
        {"explicit": _secret("flag")},
        {"file": _file(tmp_path, _secret("file"))},
        {"environ": {NEW_DB_PASSWORD_VAR: _secret("env")}},
    ):
        registry = Spy()
        with capture_logs():
            value = _resolve(secrets=registry, **kwargs)  # type: ignore[arg-type]
        assert [_plain(v) for v, _ in registry.seen] == [value]


@pytest.mark.parametrize("source", ["explicit", "file", "environ"])
def test_a_short_password_is_refused_from_every_source(
    tmp_path: Path, source: str
) -> None:
    short = "x9Z"
    kwargs: dict[str, Any] = {
        "explicit": {"explicit": short},
        "file": {"file": _file(tmp_path, short)},
        "environ": {"environ": {NEW_DB_PASSWORD_VAR: short}},
    }[source]
    with capture_logs(), pytest.raises(CredentialTooShortError) as caught:
        _resolve(**kwargs)
    assert short not in str(caught.value)


# -- run_restore --------------------------------------------------------------


class _Stop(Exception):
    pass


def _run_restore(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, *extra: str
) -> restore_mod.RestoreRequest:
    seen: list[restore_mod.RestoreRequest] = []

    def drive(request: restore_mod.RestoreRequest, **kwargs: object) -> object:
        seen.append(request)
        raise _Stop

    monkeypatch.setattr(restore_mod, "drive_restore", drive)
    monkeypatch.setattr(restore_mod, "terminal_prompt", lambda: None)
    args = build_parser().parse_args(
        [
            "restore",
            "--wp-dir",
            str(rig.site_root),
            "--archive",
            str(rig.archive_path),
            *extra,
        ]
    )
    with capture_logs(), pytest.raises(_Stop):
        restore_mod.run_restore(args, secrets=SecretRegistry())
    return seen[0]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def test_run_restore_passes_the_file_password_into_the_relocation(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    value = _secret("file")
    monkeypatch.setenv(NEW_DB_PASSWORD_VAR, _secret("env"))
    request = _run_restore(
        rig,
        monkeypatch,
        "--new-db-user",
        "e008_user",
        "--new-db-password-file",
        str(_file(tmp_path, value + "\n")),
    )
    assert _plain(request.relocation.new_db_password) == value


def test_run_restore_reads_the_environment_with_a_new_db_user(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = _secret("env")
    monkeypatch.setenv(NEW_DB_PASSWORD_VAR, value)
    with_user = _run_restore(rig, monkeypatch, "--new-db-user", "e008_user")
    assert _plain(with_user.relocation.new_db_password) == value
    without = _run_restore(rig, monkeypatch)
    assert without.relocation.new_db_password is None
    assert without.relocation.is_empty


def test_run_restore_builds_the_wordpress_aws_spec(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_id = f"e008/{uuid.uuid4().hex}"
    request = _run_restore(
        rig,
        monkeypatch,
        "--db-credentials-aws-secret-id",
        secret_id,
        "--db-credentials-aws-region",
        "eu-west-1",
    )
    assert request.credentials == AwsSecretCredentialSpec(secret_id, "eu-west-1")
    assert _run_restore(rig, monkeypatch).credentials is None


def test_drive_restore_hands_the_spec_to_wordpress_source(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = AwsSecretCredentialSpec(f"e008/{uuid.uuid4().hex}", "eu-west-1")
    specs: list[object] = []
    real = restore_mod.wordpress_source

    def spy(given: object, **kwargs: Any) -> object:  # noqa: ANN401
        specs.append(given)
        return real(None, **kwargs)  # the stub rig has no AWS; record only

    monkeypatch.setattr(restore_mod, "wordpress_source", spy)
    with capture_logs():
        rig.drive(credentials=spec)
    assert specs[0] == spec
