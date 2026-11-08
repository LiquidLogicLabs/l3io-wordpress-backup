"""Close-out owner decisions 2026-11-07: items 29 and 32.

29 (BL-E002-077): ``verify`` accepts ``--whatIf`` in either position, writes
one note and an INFO ``what_if_not_applicable`` record, and still verifies.

32 (BL-E006-016, BL-E008-005): a credential source that is set but empty is
skipped with WARNING ``credential_source_empty`` naming the source (never a
value), and the chain continues. One test per source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.cli import main
from l3io.wp.backup.cli.credentials import (
    NEW_DB_PASSWORD_FILE_FLAG,
    NEW_DB_PASSWORD_FLAG,
    new_db_password,
)
from l3io.wp.backup.cli.parser import NEW_DB_PASSWORD_VAR
from l3io.wp.backup.cli.passphrase import PASSPHRASE_ENV, resolve_passphrase
from l3io.wp.backup.cli.verify import WHAT_IF_NOTE

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/archives/v1/minimal.tar.gz"
#: A unique sentinel, never a real credential.
SENTINEL = "sentinel-closeout-32-0f9a7c"

# --------------------------------------------------------------------------
# 29. verify --whatIf
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["verify", "--whatIf", "--archive", str(FIXTURE)],
        ["--whatIf", "verify", "--archive", str(FIXTURE)],
        ["verify", "--what-if", "--archive", str(FIXTURE)],
    ],
    ids=["after", "before", "alias"],
)
def test_verify_what_if_notes_it_and_still_verifies(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--log-format", "console", *argv]) == 0
    captured = capsys.readouterr()
    assert captured.err.count(WHAT_IF_NOTE) >= 1
    assert "what_if_not_applicable" in captured.err
    assert ": OK" in captured.out


def test_verify_what_if_keeps_json_stderr_one_object_per_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["--log-format", "json", "verify", "--whatIf", "--archive", str(FIXTURE)]
    assert main(argv) == 0
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    (note,) = [r for r in records if r["event"] == "what_if_not_applicable"]
    assert note["message"] == WHAT_IF_NOTE
    assert note["level"] == "info"


def test_verify_without_what_if_says_nothing_about_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["verify", "--archive", str(FIXTURE)]) == 0
    assert WHAT_IF_NOTE not in capsys.readouterr().err


# --------------------------------------------------------------------------
# 32. credential_source_empty
# --------------------------------------------------------------------------


def _empty(logs: list[dict[str, object]]) -> list[object]:
    return [e["source"] for e in logs if e["event"] == "credential_source_empty"]


def test_an_empty_passphrase_argument_is_noted_and_the_variable_used() -> None:
    with capture_logs() as logs:
        resolved = resolve_passphrase(
            explicit="",
            environ={PASSPHRASE_ENV: SENTINEL},
            prompt=None,
            secrets=SecretRegistry(),
        )
    assert resolved is not None
    assert resolved.source == "environment"
    assert _empty(logs) == ["passphrase"]
    assert SENTINEL not in repr(logs)


def test_an_empty_passphrase_variable_is_noted_and_the_prompt_used() -> None:
    with capture_logs() as logs:
        resolved = resolve_passphrase(
            explicit=None,
            environ={PASSPHRASE_ENV: ""},
            prompt=lambda _label: SENTINEL,
            secrets=SecretRegistry(),
        )
    assert resolved is not None
    assert resolved.source == "prompt"
    assert _empty(logs) == [PASSPHRASE_ENV]


def test_a_set_passphrase_variable_is_not_noted() -> None:
    with capture_logs() as logs:
        resolve_passphrase(
            explicit=None,
            environ={PASSPHRASE_ENV: SENTINEL},
            prompt=None,
            secrets=SecretRegistry(),
        )
    assert _empty(logs) == []


def test_an_empty_new_db_password_flag_falls_to_the_file(tmp_path: Path) -> None:
    password_file = tmp_path / "pw"
    password_file.write_text(SENTINEL + "\n", encoding="utf-8")
    with capture_logs() as logs:
        value = new_db_password(
            explicit="",
            file=password_file,
            environ={},
            secrets=SecretRegistry(),
            user_given=True,
        )
    assert value is not None
    assert value.reveal() == SENTINEL
    assert _empty(logs) == [NEW_DB_PASSWORD_FLAG]
    assert SENTINEL not in repr(logs)


def test_an_empty_password_file_falls_to_the_variable(tmp_path: Path) -> None:
    password_file = tmp_path / "pw"
    password_file.write_text("", encoding="utf-8")
    with capture_logs() as logs:
        value = new_db_password(
            explicit=None,
            file=password_file,
            environ={NEW_DB_PASSWORD_VAR: SENTINEL},
            secrets=SecretRegistry(),
            user_given=True,
        )
    assert value is not None
    assert value.reveal() == SENTINEL
    assert _empty(logs) == [NEW_DB_PASSWORD_FILE_FLAG]


def test_an_empty_new_db_password_variable_is_noted_and_gives_none() -> None:
    with capture_logs() as logs:
        value = new_db_password(
            explicit=None,
            file=None,
            environ={NEW_DB_PASSWORD_VAR: ""},
            secrets=SecretRegistry(),
            user_given=True,
        )
    assert value is None
    assert _empty(logs) == [NEW_DB_PASSWORD_VAR]


# --------------------------------------------------------------------------
# 18. --list-default-excludes without --wp-dir (BL-E008-013)
# --------------------------------------------------------------------------


def test_listing_the_defaults_needs_no_wp_dir(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from l3io.wp.backup.core.exclusions import enumerate_defaults

    assert main(["backup", "--list-default-excludes"]) == 0
    assert capsys.readouterr().out.splitlines() == list(enumerate_defaults())


@pytest.mark.parametrize(
    "argv", [["backup"], ["backup", "--mode", "files"], ["--whatIf", "backup"]]
)
def test_a_backup_without_wp_dir_fails_as_before(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        main(argv)
    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert "error: the following arguments are required: --wp-dir" in err
    assert err.startswith("usage: wpbackup2 ")
