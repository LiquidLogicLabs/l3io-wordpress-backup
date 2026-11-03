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
    assert (resolved.value, resolved.source) == (expected, source)
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
            "log_level": "debug",
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
