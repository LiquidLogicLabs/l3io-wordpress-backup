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

#: Codes only a later story can produce, and the story that adds the case.
PENDING_CASES: Final[dict[ExitCode, str]] = {ExitCode.LOCKED: "E002-S02-008"}


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
            "define('DB_HOST', 'db.invalid');\n"
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
    return [run([]), env.backup("--mode", "everything")]


def _refused(env: _Env) -> list[int]:
    (env.tmp / "out.tar.gz").write_bytes(b"already here")
    return [env.backup("--mode", "files")]


def _credentials(env: _Env) -> list[int]:
    env.dump_raises(wpdb.AuthenticationFailedError("rejected"))
    return [env.backup()]


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


CASES: Final[dict[ExitCode, Callable[[_Env], list[int]]]] = {
    ExitCode.FAILED: _failed,
    ExitCode.USAGE: _usage,
    ExitCode.REFUSED: _refused,
    ExitCode.CREDENTIALS: _credentials,
    ExitCode.UNREACHABLE: _unreachable,
    ExitCode.NOT_READY: _not_ready,
    ExitCode.VERIFICATION_FAILED: _verification_failed,
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
    if code is not ExitCode.USAGE:
        assert DESCRIPTIONS[code] in capsys.readouterr().err


def test_success_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _Env(tmp_path, monkeypatch).backup("--mode", "files") == ExitCode.OK
    capsys.readouterr()
