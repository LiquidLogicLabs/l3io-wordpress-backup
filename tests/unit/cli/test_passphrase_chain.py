"""AD-26 — the one passphrase resolution chain."""

from __future__ import annotations

import ast
import importlib
import os
import pkgutil
import pty
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from structlog.testing import capture_logs

import l3io.wp.backup as package
from l3io.wp.backup.adapters.redaction import REDACTED, SecretRegistry
from l3io.wp.backup.cli import passphrase as passphrase_module
from l3io.wp.backup.cli.passphrase import (
    PASSPHRASE_ENV,
    note_unused_passphrase,
    require_passphrase,
    resolve_passphrase,
    terminal_prompt,
)
from l3io.wp.backup.domain.errors import PassphraseUnavailableError, UsageError

ARG = "argument-passphrase"
ENV = "environment-passphrase"
TYPED = "prompted-passphrase"


def _prompt(value: str = TYPED) -> tuple[list[str], object]:
    calls: list[str] = []

    def prompt(text: str) -> str:
        calls.append(text)
        return value

    return calls, prompt


@pytest.mark.parametrize(
    ("explicit", "environ", "expected", "source"),
    [
        (ARG, {PASSPHRASE_ENV: ENV}, ARG, "argument"),
        (None, {PASSPHRASE_ENV: ENV}, ENV, "environment"),
        ("", {PASSPHRASE_ENV: ENV}, ENV, "environment"),
        (None, {}, TYPED, "prompt"),
        ("", {PASSPHRASE_ENV: ""}, TYPED, "prompt"),
    ],
)
def test_the_first_present_step_wins(
    explicit: str | None, environ: dict[str, str], expected: str, source: str
) -> None:
    calls, prompt = _prompt()
    secrets = SecretRegistry()
    resolved = resolve_passphrase(
        explicit=explicit,
        environ=environ,
        prompt=prompt,  # type: ignore[arg-type]
        secrets=secrets,
    )
    assert resolved is not None
    assert (resolved.value.reveal(), resolved.source) == (expected, source)
    assert len(calls) == (source == "prompt")
    assert secrets.redact(expected) == REDACTED


def test_nothing_resolves_to_none_and_registers_nothing() -> None:
    secrets = SecretRegistry()
    assert (
        resolve_passphrase(explicit=None, environ={}, prompt=None, secrets=secrets)
        is None
    )
    _, empty = _prompt("")
    assert (
        resolve_passphrase(explicit="", environ={}, prompt=empty, secrets=secrets)  # type: ignore[arg-type]
        is None
    )
    assert len(secrets) == 0


@pytest.mark.parametrize("raised", [EOFError, KeyboardInterrupt])
def test_a_closed_prompt_stops_the_chain(raised: type[BaseException]) -> None:
    def prompt(_: str) -> str:
        raise raised

    with pytest.raises(PassphraseUnavailableError) as caught:
        resolve_passphrase(
            explicit=None, environ={}, prompt=prompt, secrets=SecretRegistry()
        )
    assert isinstance(caught.value, UsageError)
    assert PASSPHRASE_ENV in str(caught.value)


def test_a_passphrase_too_short_to_redact_is_refused() -> None:
    secrets = SecretRegistry()
    with pytest.raises(PassphraseUnavailableError) as caught:
        resolve_passphrase(explicit="abc", environ={}, prompt=None, secrets=secrets)
    assert caught.value.reason == "too short to protect"
    assert "abc" not in str(caught.value)
    assert len(secrets) == 0


def test_requested_encryption_with_nothing_and_no_terminal_is_refused() -> None:
    with pytest.raises(PassphraseUnavailableError) as caught:
        require_passphrase(
            explicit=None, environ={}, prompt=None, secrets=SecretRegistry()
        )
    assert PASSPHRASE_ENV in str(caught.value)
    assert "no terminal" in str(caught.value)


def test_repr_shows_only_the_source() -> None:
    resolved = resolve_passphrase(
        explicit=ARG, environ={}, prompt=None, secrets=SecretRegistry()
    )
    assert repr(resolved) == "ResolvedPassphrase(source='argument')"


def test_an_unrequested_passphrase_is_noted_by_name_only() -> None:
    with capture_logs() as logs:
        note_unused_passphrase({PASSPHRASE_ENV: ENV})
        note_unused_passphrase({})
    assert logs == [
        {
            "event": "passphrase_ignored",
            "variable": PASSPHRASE_ENV,
            "message": "encryption was not requested, so the Archive is unencrypted",
            "log_level": "warning",
        }
    ]


# --------------------------------------------------------------------------
# The prompt is bound only on a terminal
# --------------------------------------------------------------------------


@pytest.fixture
def pseudo_tty_stdin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    main_fd, child_fd = pty.openpty()
    stream = os.fdopen(child_fd, "r")
    monkeypatch.setattr(sys, "stdin", stream)
    try:
        yield
    finally:
        stream.close()
        os.close(main_fd)


def test_the_prompt_is_not_bound_without_a_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with (tmp_path / "in").open("w+") as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        assert terminal_prompt() is None


@pytest.mark.usefixtures("pseudo_tty_stdin")
def test_the_prompt_is_getpass_on_a_terminal() -> None:
    assert terminal_prompt() is passphrase_module.getpass.getpass


# --------------------------------------------------------------------------
# One chain: every root holds this function object
# --------------------------------------------------------------------------


def _root_modules() -> list[str]:
    roots = (f"{package.__name__}.cli", f"{package.__name__}.api")
    return [
        info.name
        for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}.")
        if info.name.startswith(roots)
    ]


def test_no_root_defines_a_second_chain() -> None:
    """Only ``cli/passphrase.py`` defines ``resolve_passphrase``."""
    definers = []
    for name in _root_modules():
        module = importlib.import_module(name)
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        if any(
            isinstance(node, ast.FunctionDef) and node.name == "resolve_passphrase"
            for node in ast.walk(ast.parse(source))
        ):
            definers.append(name)
    assert definers == [passphrase_module.__name__]


def test_every_root_that_holds_the_chain_holds_this_one() -> None:
    holders = {
        name: importlib.import_module(name).resolve_passphrase
        for name in _root_modules()
        if hasattr(importlib.import_module(name), "resolve_passphrase")
    }
    assert passphrase_module.__name__ in holders
    assert all(fn is resolve_passphrase for fn in holders.values()), sorted(holders)


def test_the_library_root_holds_the_same_chain_once_it_exists() -> None:
    """Tripwire: Epic 6's ``api/`` must import this function, not copy it."""
    api = [n for n in _root_modules() if n.startswith(f"{package.__name__}.api.")]
    if not api:
        pytest.skip("api/ has no entry point yet (Epic 6)")
    holders = [
        n for n in api if hasattr(importlib.import_module(n), "resolve_passphrase")
    ]
    assert holders, "api/ exists but no module binds resolve_passphrase"
    for name in holders:
        assert importlib.import_module(name).resolve_passphrase is resolve_passphrase


# --------------------------------------------------------------------------
# E008-S01-001 (BL-E004-052, ADR-0024 amendment): the encryption floor
# --------------------------------------------------------------------------


def _plain(value: object) -> object:
    """The text of ``value``, whether a ``str`` or an opaque ``Secret``."""
    reveal = getattr(value, "reveal", None)
    return reveal() if callable(reveal) else value


def _sentinel(length: int) -> str:
    import uuid

    return f"sentinel-pp-{uuid.uuid4().hex}"[:length]


def test_require_passphrase_refuses_eleven_characters() -> None:
    from l3io.wp.backup.cli.passphrase import MIN_ENCRYPTION_PASSPHRASE_LENGTH
    from l3io.wp.backup.domain.errors import PassphraseTooShortError

    assert MIN_ENCRYPTION_PASSPHRASE_LENGTH == 12
    value = _sentinel(11)
    with pytest.raises(PassphraseTooShortError) as caught:
        require_passphrase(
            explicit=value, environ={}, prompt=None, secrets=SecretRegistry()
        )
    assert caught.value.minimum == 12
    assert "12" in str(caught.value)
    assert value not in str(caught.value)
    assert isinstance(caught.value, UsageError)


@pytest.mark.parametrize("source", ["argument", "environment"])
def test_require_passphrase_accepts_twelve_and_asks_nothing(source: str) -> None:
    value = _sentinel(12)
    calls, prompt = _prompt()
    resolved = require_passphrase(
        explicit=value if source == "argument" else None,
        environ={PASSPHRASE_ENV: value} if source == "environment" else {},
        prompt=prompt,  # type: ignore[arg-type]
        secrets=SecretRegistry(),
    )
    assert _plain(resolved.value) == value
    assert resolved.source == source
    assert calls == []


def test_a_prompted_passphrase_is_asked_twice() -> None:
    value = _sentinel(16)
    calls, prompt = _prompt(value)
    resolved = require_passphrase(
        explicit=None,
        environ={},
        prompt=prompt,  # type: ignore[arg-type]
        secrets=SecretRegistry(),
    )
    assert _plain(resolved.value) == value
    assert len(calls) == 2
    assert calls[0] != calls[1]


def test_a_mismatched_confirmation_is_refused() -> None:
    from l3io.wp.backup.domain.errors import PassphraseMismatchError

    first, second = _sentinel(16), _sentinel(16)
    answers = iter([first, second])
    with pytest.raises(PassphraseMismatchError) as caught:
        require_passphrase(
            explicit=None,
            environ={},
            prompt=lambda _label: next(answers),
            secrets=SecretRegistry(),
        )
    assert first not in str(caught.value)
    assert second not in str(caught.value)
    assert isinstance(caught.value, UsageError)


@pytest.mark.parametrize("length", [4, 8, 11])
def test_decryption_still_accepts_a_short_passphrase(length: int) -> None:
    value = _sentinel(length)
    resolved = resolve_passphrase(
        explicit=value, environ={}, prompt=None, secrets=SecretRegistry()
    )
    assert resolved is not None
    assert _plain(resolved.value) == value


def test_a_short_restore_passphrase_is_noted_by_length_only() -> None:
    from l3io.wp.backup.cli.passphrase import note_short_restore_passphrase

    value = _sentinel(8)
    resolved = resolve_passphrase(
        explicit=value, environ={}, prompt=None, secrets=SecretRegistry()
    )
    with capture_logs() as logs:
        note_short_restore_passphrase(resolved)
        note_short_restore_passphrase(None)
        note_short_restore_passphrase(
            resolve_passphrase(
                explicit=_sentinel(12),
                environ={},
                prompt=None,
                secrets=SecretRegistry(),
            )
        )
    assert [r["event"] for r in logs] == ["passphrase_below_encryption_floor"]
    assert logs[0]["log_level"] == "warning"
    assert value not in repr(logs)


def test_the_library_backup_refuses_a_short_passphrase_before_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from l3io.wp.backup import api
    from l3io.wp.backup.api import operations
    from l3io.wp.backup.domain.backup_scope import BackupScope
    from l3io.wp.backup.domain.errors import PassphraseTooShortError

    driven: list[object] = []
    monkeypatch.setattr(operations, "drive_backup", lambda *a, **k: driven.append(a))
    monkeypatch.delenv(PASSPHRASE_ENV, raising=False)
    with pytest.raises(PassphraseTooShortError):
        api.backup(
            tmp_path,
            scope=BackupScope.FILES,
            target=tmp_path / "out.tar.gz",
            encrypt=True,
            passphrase=_sentinel(11),
        )
    assert driven == []
