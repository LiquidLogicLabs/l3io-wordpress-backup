"""E008-S01-001 — the encryption, exclusion and credential flags through ``run_backup``.

Each flag the story bound is driven through the real parser and
``run_backup``; ``drive_backup`` is replaced by a spy that records the
``BackupRequest`` and stops, so what is asserted is the translation the root
makes, and that a refusal comes before any Run opens.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.exclusions import LoggedExclusionPolicy
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli import backup as backup_cli
from l3io.wp.backup.cli.exit import exit_code_for
from l3io.wp.backup.cli.parser import build_parser
from l3io.wp.backup.cli.passphrase import (
    MIN_ENCRYPTION_PASSPHRASE_LENGTH,
    PASSPHRASE_ENV,
)
from l3io.wp.backup.core.exclusions import (
    DEFAULT_EXCLUSIONS,
    ExclusionSet,
    InvalidExclusionPatternError,
    enumerate_defaults,
)
from l3io.wp.backup.domain.credentials import AwsSecretCredentialSpec
from l3io.wp.backup.domain.errors import (
    PassphraseTooShortError,
    PassphraseUnavailableError,
    UsageError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.preflight import ToolRequirement

TOOL = ToolRequirement("age", (1, 3), "the encrypted payload")


class _Stop(Exception):
    """Raised by the ``drive_backup`` spy once it has the request."""


class _Cipher:
    def required_tools(self) -> tuple[ToolRequirement, ...]:
        return (TOOL,)


class Rig:
    def __init__(self, site: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.site = site
        self.requests: list[backup_cli.BackupRequest] = []
        self.joined: list[object] = []
        self.built: list[dict[str, Any]] = []
        self.cipher = _Cipher()
        monkeypatch.delenv(PASSPHRASE_ENV, raising=False)
        monkeypatch.setattr(backup_cli, "terminal_prompt", lambda: None)

        def drive(request: backup_cli.BackupRequest, **kwargs: object) -> object:
            self.requests.append(request)
            raise _Stop

        def join(*args: object, **kwargs: object) -> object:
            self.joined.append(args)
            raise _Stop

        def build(**kwargs: Any) -> object:  # noqa: ANN401
            self.built.append(kwargs)
            return self.cipher

        monkeypatch.setattr(backup_cli, "drive_backup", drive)
        monkeypatch.setattr(backup_cli, "join_or_open", join)
        monkeypatch.setattr(backup_cli, "build_payload_cipher", build)

    def run(self, *extra: str) -> ExitCode:
        args = build_parser().parse_args(["backup", "--wp-dir", str(self.site), *extra])
        return backup_cli.run_backup(args, secrets=SecretRegistry())

    def request(self, *extra: str) -> backup_cli.BackupRequest:
        with pytest.raises(_Stop):
            self.run(*extra)
        return self.requests[-1]


@pytest.fixture
def rig(site: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(site, monkeypatch)


def _plain(value: object) -> object:
    """The text of ``value``, whether a ``str`` or an opaque ``Secret``."""
    reveal = getattr(value, "reveal", None)
    return reveal() if callable(reveal) else value


def _passphrase(length: int) -> str:
    text = f"sentinel-pp-{uuid.uuid4().hex}"
    return text[:length]


# -- encryption ---------------------------------------------------------------


def test_encrypt_builds_the_cipher_and_passes_its_tools(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = _passphrase(MIN_ENCRYPTION_PASSPHRASE_LENGTH + 4)
    monkeypatch.setenv(PASSPHRASE_ENV, value)
    request = rig.request("--encrypt-with-passphrase")
    assert [_plain(b["passphrase"]) for b in rig.built] == [value]
    assert request.cipher is rig.cipher
    assert request.extra_tools == (TOOL,)


def test_no_encrypt_flag_builds_no_cipher(rig: Rig) -> None:
    request = rig.request()
    assert request.cipher is None
    assert request.extra_tools == ()
    assert rig.built == []


def test_encrypt_with_no_passphrase_and_no_terminal_is_usage_before_a_run(
    rig: Rig,
) -> None:
    with pytest.raises(PassphraseUnavailableError) as caught:
        rig.run("--encrypt-with-passphrase")
    assert exit_code_for(caught.value) is ExitCode.USAGE
    assert rig.requests == rig.joined == rig.built == []


def test_an_eleven_character_passphrase_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = _passphrase(MIN_ENCRYPTION_PASSPHRASE_LENGTH - 1)
    monkeypatch.setenv(PASSPHRASE_ENV, value)
    with pytest.raises(PassphraseTooShortError) as caught:
        rig.run("--encrypt-with-passphrase")
    assert exit_code_for(caught.value) is ExitCode.USAGE
    assert int(ExitCode.USAGE) == 2
    assert value not in str(caught.value)
    assert rig.requests == rig.joined == rig.built == []


def test_a_twelve_character_passphrase_is_accepted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert MIN_ENCRYPTION_PASSPHRASE_LENGTH == 12
    monkeypatch.setenv(PASSPHRASE_ENV, _passphrase(12))
    assert rig.request("--encrypt-with-passphrase").cipher is rig.cipher


# -- exclusions ---------------------------------------------------------------


def test_no_exclusion_flag_takes_the_logged_default(rig: Rig) -> None:
    policy = rig.request().exclusion
    assert isinstance(policy, LoggedExclusionPolicy)
    assert policy.policy == ExclusionSet.default()
    assert policy.source == "default"
    assert backup_cli.default_exclusion_policy().policy == ExclusionSet.default()


def test_exclude_adds_to_the_defaults(rig: Rig) -> None:
    policy = rig.request("--exclude", "*.log", "--exclude", "cache/").exclusion
    assert isinstance(policy, LoggedExclusionPolicy)
    expected = ExclusionSet.from_patterns(("*.log", "cache/"), include_defaults=True)
    assert policy.policy == expected
    assert policy.patterns[: len(DEFAULT_EXCLUSIONS)] == DEFAULT_EXCLUSIONS


def test_no_default_excludes_drops_the_defaults(rig: Rig) -> None:
    policy = rig.request("--no-default-excludes", "--exclude", "*.log").exclusion
    assert isinstance(policy, LoggedExclusionPolicy)
    assert policy.policy == ExclusionSet.from_patterns(
        ("*.log",), include_defaults=False
    )
    alone = rig.request("--no-default-excludes").exclusion
    assert isinstance(alone, LoggedExclusionPolicy)
    assert alone.policy == ExclusionSet.none()


def test_an_invalid_pattern_is_usage_before_a_run(rig: Rig) -> None:
    with pytest.raises(InvalidExclusionPatternError) as caught:
        rig.run("--exclude", "")
    assert exit_code_for(caught.value) is ExitCode.USAGE
    assert rig.requests == rig.joined == []


def test_list_default_excludes_prints_them_and_opens_no_run(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rig.run("--list-default-excludes") is ExitCode.OK
    out = capsys.readouterr().out
    assert out.splitlines() == list(enumerate_defaults())
    assert rig.requests == rig.joined == rig.built == []


# -- credentials --------------------------------------------------------------


def test_the_db_aws_pair_reaches_the_request(rig: Rig) -> None:
    secret_id = f"e008/{uuid.uuid4().hex}"
    request = rig.request(
        "--db-credentials-aws-secret-id",
        secret_id,
        "--db-credentials-aws-region",
        "eu-west-1",
    )
    assert request.credentials == AwsSecretCredentialSpec(secret_id, "eu-west-1")
    assert rig.request().credentials is None


@pytest.mark.parametrize(
    "flags",
    [
        ("--exclude", "wp-content/cache"),
        ("--no-default-excludes",),
        ("--encrypt-with-passphrase",),
        ("--archive", "x.tar.gz"),
        ("--mode", "db"),
        ("--db-credentials-aws-secret-id", "e008/ignored"),
        ("--what-if",),
    ],
)
def test_list_default_excludes_refuses_every_other_backup_flag(
    rig: Rig, flags: tuple[str, ...]
) -> None:
    """E008 closure A-02: a flag the listing would ignore is a UsageError."""
    with pytest.raises(UsageError) as caught:
        rig.run("--list-default-excludes", *flags)
    assert exit_code_for(caught.value) is ExitCode.USAGE
    assert flags[0] in caught.value.arguments
    assert rig.requests == rig.joined == rig.built == []
