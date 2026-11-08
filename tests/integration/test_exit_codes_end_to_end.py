"""FR-21 — every exit code, produced by a real failure driven through ``main``.

The case table is keyed by ``ExitCode`` and the test is parametrised from the
enum: every member but ``OK`` must have a case **or** an entry in
:data:`PENDING_CASES` naming the story that delivers it. A member in both
fails, so a pending entry cannot outlive the story that adds its case.

Each case is real code with a double only at the outermost dependency:
``l3io.wp.database.dump`` raises what database raises, and the package's own
adapter maps it; ``l3io.wp.database.check_dump`` (preflight, AD-13) reports a
ready dump unless a case says otherwise. No MySQL is needed.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

wpdb = pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.cli import main  # noqa: E402
from l3io.wp.backup.domain.exit_codes import DESCRIPTIONS, ExitCode  # noqa: E402
from tests.root_mode import allow_root_argv  # noqa: E402
from tests.site_lock_holder import HOLDER_RUN_ID, held_elsewhere  # noqa: E402

#: A committed encrypted v1 Archive (its passphrase is a sidecar, unused here).
ENCRYPTED_FIXTURE: Final = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "archives"
    / "v1"
    / "encrypted-passphrase.tar.gz"
)

#: Codes only a later story can produce, and the story that adds the case.
PENDING_CASES: Final[dict[ExitCode, str]] = {}


class _Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.site = tmp_path / "www"
        self.site.mkdir()
        (self.site / "wp-config.php").write_text(
            "<?php\n"
            "define('DB_NAME', 'wptest');\n"
            "define('DB_USER', 'wp');\n"
            "define('DB_PASSWORD', 'not-a-real-secret');\n"
            "define('DB_HOST', 'localhost');\n"
            "$table_prefix = 'wp_';\n",
            encoding="utf-8",
        )
        (self.site / "index.php").write_text("<?php\n", encoding="utf-8")
        self.check_dump_finds()

    def check_dump_finds(self, *codes: object) -> None:
        """Preflight's ``check_dump`` reports ``codes`` (none: ready)."""
        findings = tuple(wpdb.Finding(code, "double") for code in codes)

        def check_dump(*_a: object, **_k: object) -> object:
            return wpdb.Verdict("dump", findings)

        self.monkeypatch.setattr(wpdb, "check_dump", check_dump)

    def backup(self, *extra: str) -> int:
        archive = self.tmp / "out.tar.gz"
        return run(
            ["backup", "--wp-dir", str(self.site), "--archive", str(archive), *extra]
        )

    def backup_rehearsed(self) -> int:
        archive = self.tmp / "out.tar.gz"
        return run(
            [
                "--whatIf",
                "backup",
                "--wp-dir",
                str(self.site),
                "--archive",
                str(archive),
            ]
        )

    def dump_raises(self, exc: BaseException) -> None:
        def dump(*_a: object, **_k: object) -> object:
            raise exc

        self.monkeypatch.setattr(wpdb, "dump", dump)


def run(argv: list[str]) -> int:
    """``main``'s return, or argparse's own ``SystemExit`` code."""
    try:
        return main(argv)
    except SystemExit as exited:
        # A non-int code (a message, or None) is not a table code.
        return exited.code if isinstance(exited.code, int) else -1


def _failed(env: _Env) -> list[int]:
    env.dump_raises(wpdb.TransferError("dump failed", wpdb.Verdict("dump", ()), 2, ""))
    return [env.backup()]


def _usage(env: _Env) -> list[int]:
    # BL-E002-074 (owner decision 2026-11-07): a nonexistent --wp-dir or
    # --archive is the caller's mistake, USAGE, on every subcommand.
    missing_site = env.tmp / "no-such-site"
    missing_archive = env.tmp / "no-such-archive.tar.gz"
    return [
        run([]),
        env.backup("--mode", "everything"),
        run(["backup", "--wp-dir", str(missing_site), "--archive", str(env.tmp / "o")]),
        run(["verify", "--archive", str(missing_archive)]),
        run(
            [
                "restore",
                "--wp-dir",
                str(missing_site),
                "--archive",
                str(missing_archive),
                *allow_root_argv(),
            ]
        ),
        run(
            [
                "restore",
                "--wp-dir",
                str(env.site),
                "--archive",
                str(missing_archive),
                *allow_root_argv(),
            ]
        ),
    ]


def _refused(env: _Env) -> list[int]:
    (env.tmp / "out.tar.gz").write_bytes(b"already here")
    return [env.backup("--mode", "files")]


def _credentials(env: _Env) -> list[int]:
    env.dump_raises(wpdb.AuthenticationFailedError("rejected"))
    # BL-E002-069 (owner decision 2026-11-07): an encrypted Archive and no
    # passphrase is a missing key, CREDENTIALS, not REFUSED.
    env.monkeypatch.delenv("L3IO_WP_BACKUP_PASSPHRASE", raising=False)
    encrypted = ENCRYPTED_FIXTURE
    return [
        env.backup(),
        run(["verify", "--authenticate", "--archive", str(encrypted)]),
    ]


def _unreachable(env: _Env) -> list[int]:
    env.dump_raises(wpdb.DatabaseUnreachableError("no route"))
    return [env.backup()]


def _not_ready(env: _Env) -> list[int]:
    # Preflight (AD-13): the dump's client tool is missing, in a real Run and
    # in a Dry Run alike; neither reaches the dump.
    env.check_dump_finds(wpdb.Code.CLIENT_TOOL_MISSING)
    env.dump_raises(AssertionError("preflight let the dump run"))
    preflight = [env.backup(), env.backup_rehearsed()]
    # The database extra absent: importing it fails.
    env.monkeypatch.setitem(sys.modules, "l3io.wp.database", None)
    return [*preflight, env.backup()]


def _verification_failed(env: _Env) -> list[int]:
    corrupt = env.tmp / "corrupt.tar.gz"
    corrupt.write_bytes(b"not an archive at all" * 64)
    return [run(["verify", "--archive", str(corrupt)])]


def _locked(env: _Env) -> list[int]:
    # AD-19: another process holds the Site; a real Run and a Dry Run both
    # refuse before reading it, and the dump is never reached.
    env.dump_raises(AssertionError("a locked Run reached the dump"))
    with held_elsewhere(env.site):
        return [env.backup(), env.backup_rehearsed()]


CASES: Final[dict[ExitCode, Callable[[_Env], list[int]]]] = {
    ExitCode.FAILED: _failed,
    ExitCode.USAGE: _usage,
    ExitCode.REFUSED: _refused,
    ExitCode.CREDENTIALS: _credentials,
    ExitCode.UNREACHABLE: _unreachable,
    ExitCode.NOT_READY: _not_ready,
    ExitCode.VERIFICATION_FAILED: _verification_failed,
    ExitCode.LOCKED: _locked,
}

FAILURES = [code for code in ExitCode if code is not ExitCode.OK]


def test_no_member_is_both_pending_and_covered() -> None:
    assert not set(CASES) & set(PENDING_CASES)
    assert ExitCode.OK not in CASES


@pytest.mark.parametrize("code", FAILURES, ids=lambda c: c.name)
def test_every_failure_code_is_produced(
    code: ExitCode,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if code in PENDING_CASES:
        assert code not in CASES, f"{code.name} has a case: drop its pending entry"
        pytest.skip(f"{code.name}: case delivered by {PENDING_CASES[code]}")
    assert code in CASES, f"{code.name} has neither a case nor a pending entry"
    monkeypatch.chdir(tmp_path)
    observed = CASES[code](_Env(tmp_path, monkeypatch))
    assert observed
    assert all(value == int(code) for value in observed), observed
    # USAGE included since BL-E002-058: argparse's own errors carry the suffix.
    err = capsys.readouterr().err
    assert DESCRIPTIONS[code] in err
    if code is ExitCode.LOCKED:
        # The stderr message names the holder (FR-28).
        assert HOLDER_RUN_ID in err


def test_success_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _Env(tmp_path, monkeypatch).backup("--mode", "files") == ExitCode.OK
    capsys.readouterr()


def test_the_three_owner_decided_codes_are_on_their_classes() -> None:
    """BL-E002-069/074/103 (owner decision 2026-11-07, item 9).

    The USAGE and CREDENTIALS cases above drive them through ``main``; the
    refusal after CLEAN needs a restore that clears a Site, which
    ``tests/unit/adapters/test_restore_runner.py`` drives. Here the table's
    class attributes are pinned, so a later edit cannot move one silently.
    """
    from l3io.wp.backup.adapters.restore_runner import (
        ExtractionRefusedAfterCleanError,
    )
    from l3io.wp.backup.domain.errors import (
        ArchiveNotFoundError,
        PassphraseRequiredError,
        SiteRootNotFoundError,
    )

    assert ExtractionRefusedAfterCleanError.exit_code is ExitCode.VERIFICATION_FAILED
    assert PassphraseRequiredError.exit_code is ExitCode.CREDENTIALS
    assert SiteRootNotFoundError.exit_code is ExitCode.USAGE
    assert ArchiveNotFoundError.exit_code is ExitCode.USAGE
