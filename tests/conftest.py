"""Fixtures shared by every test tier.

``credential_sentinel`` is AD-7 items 1-4 as a reusable fixture (ADR-0018).
It plants a unique sentinel password in a temporary ``wp-config.php`` and
captures every surface a credential could leak through; a test drives a Run
and calls :meth:`CredentialSentinel.assert_clean`. Epic 3 story 3.1 applies it
to a full restore; Epic 4 (E004-S01-003) applies its ``passphrase`` parameter
to an encrypted Run.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import subprocess
import tempfile
import traceback
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

#: AD-26's environment step; spelled out so the fixture does not import the CLI.
PASSPHRASE_ENV = "L3IO_WP_BACKUP_PASSPHRASE"


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


@dataclass(repr=False)
class CredentialSentinel:
    """A planted secret, and everything captured while a Run used it."""

    password: str
    passphrase: str | None
    site: Path
    config: Path
    workdir: Path
    environ_before: dict[str, str]
    caplog: pytest.LogCaptureFixture
    logs: list[dict[str, Any]]
    monkeypatch: pytest.MonkeyPatch = field(repr=False)
    popen: list[tuple[object, object]] = field(default_factory=list)
    texts: list[tuple[str, str]] = field(default_factory=list)

    def __repr__(self) -> str:
        # pytest prints a failing frame's arguments: never the sentinels.
        return f"CredentialSentinel(<{len(self.texts)} captured texts>)"

    @property
    def secrets(self) -> tuple[str, ...]:
        extra = () if self.passphrase is None else (self.passphrase,)
        return (self.password, *extra)

    def add(self, surface: str, text: str | bytes) -> None:
        """Capture ``text`` (CLI output, Manifest bytes, …) under ``surface``."""
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        self.texts.append((surface, text))

    def add_exception(self, surface: str, exc: BaseException) -> None:
        """``str``, ``repr`` and the formatted traceback, with no redaction."""
        self.add(f"{surface}: str", str(exc))
        self.add(f"{surface}: repr", repr(exc))
        self.add(f"{surface}: traceback", "".join(traceback.format_exception(exc)))

    def add_repr(self, surface: str, value: object) -> None:
        self.add(f"{surface}: repr", repr(value))

    def watch(self, owner: object, name: str) -> None:
        """Wrap ``owner.name`` so each call's arguments and result are captured."""
        real: Callable[..., Any] = getattr(owner, name)

        def spy(*args: object, **kwargs: object) -> object:
            for i, arg in enumerate(args):
                self.add_repr(f"{name} arg {i}", arg)
            for key, arg in kwargs.items():
                self.add_repr(f"{name} {key}=", arg)
            result = real(*args, **kwargs)
            self.add_repr(f"{name} result", result)
            return result

        self.monkeypatch.setattr(owner, name, spy)

    def surfaces(self) -> list[tuple[str, str]]:
        found = list(self.texts)
        renderer = structlog.processors.KeyValueRenderer()
        for event in self.logs:
            found.append(("structlog event", repr(event)))
            found.append(("structlog rendered", str(renderer(None, "", dict(event)))))
        formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
        for record in self.caplog.records:
            found.append(("stdlib record", formatter.format(record)))
            found.append(("stdlib record fields", repr(vars(record))))
        found.append(("caplog text", self.caplog.text))
        for argv, env in self.popen:
            found.append(("Popen argv", repr(argv)))
            found.append(("Popen env", repr(env)))
        # The variable the fixture itself planted is the one expected holder.
        planted = PASSPHRASE_ENV if self.passphrase is not None else None
        environ = {k: v for k, v in os.environ.items() if k != planted}
        found.append(("os.environ after", repr(environ)))
        return found

    def leaks(self) -> list[str]:
        """Each surface holding a sentinel, named with its fingerprint only."""
        return [
            f"{surface} holds sha256:{_fingerprint(secret)}"
            for surface, text in self.surfaces()
            for secret in self.secrets
            if secret in text
        ]

    def assert_clean(self) -> None:
        leaks = self.leaks()
        assert not leaks, f"credential reached {len(leaks)} surface(s): {leaks}"
        after, before = _environ(), self.environ_before
        changed = sorted(
            k for k in after.keys() | before.keys() if after.get(k) != before.get(k)
        )
        # Names only: the environment can hold real secrets (CI's DB_PASSWORD).
        assert not changed, f"the Run changed os.environ: {changed}"
        assert not any(
            isinstance(env, Mapping) and "MYSQL_PWD" in env for _, env in self.popen
        ), "MYSQL_PWD was put in a child's environment"


def _environ() -> dict[str, str]:
    """``os.environ`` without the variable pytest itself rewrites per phase."""
    return {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}


def write_wp_config(path: Path, *, password: str, user: str | None = None) -> None:
    """A ``wp-config.php`` for the CI database (or ``db.invalid``)."""
    host = os.environ.get("DB_HOST", "db.invalid")
    user = user or os.environ.get("DB_USER", "wp")
    path.write_text(
        "<?php\n"
        f"define('DB_NAME', {os.environ.get('DB_NAME', 'wptest')!r});\n"
        f"define('DB_USER', {user!r});\n"
        f"define('DB_PASSWORD', {password!r});\n"
        f"define('DB_HOST', {host!r});\n"
        "$table_prefix = 'wp_';\n",
        encoding="utf-8",
    )


@pytest.fixture
def credential_sentinel(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[CredentialSentinel]:
    """A planted sentinel password, with every leak surface under capture.

    Parameter (``indirect=True``, optional): ``{"passphrase": True}`` plants a
    sentinel in ``L3IO_WP_BACKUP_PASSPHRASE``. The password is always
    generated, never taken from the environment, so it is unique. It is
    plain ASCII: the wp-config reader does not unescape a quoted PHP string,
    so escaped spellings are covered by ``tests/unit/adapters/test_redaction.py``.
    """
    params: dict[str, Any] = getattr(request, "param", None) or {}
    password = f"sentinel-pw-{uuid.uuid4().hex}"
    passphrase = f"sentinel-pp-{uuid.uuid4().hex}" if params.get("passphrase") else None
    if passphrase is not None:
        monkeypatch.setenv(PASSPHRASE_ENV, passphrase)
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.php").write_text("<?php\n", encoding="utf-8")
    config = site / "wp-config.php"
    write_wp_config(config, password=password)
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(workdir))
    monkeypatch.setattr(tempfile, "tempdir", None)
    caplog.set_level(1)
    with capture_logs() as logs:
        sentinel = CredentialSentinel(
            password=password,
            passphrase=passphrase,
            site=site,
            config=config,
            workdir=workdir,
            environ_before=_environ(),
            caplog=caplog,
            logs=logs,
            monkeypatch=monkeypatch,
        )
        real_popen = subprocess.Popen
        signature = inspect.signature(real_popen)

        class SpyPopen(real_popen):  # type: ignore[misc, valid-type]
            def __init__(self, *args: object, **kwargs: object) -> None:
                bound = signature.bind(*args, **kwargs).arguments
                sentinel.popen.append((bound.get("args"), bound.get("env")))
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", SpyPopen)
        planted = {
            k: v for k, v in sentinel.environ_before.items() if k != PASSPHRASE_ENV
        }
        inputs = (repr(planted), str(tmp_path))
        assert not any(
            secret in text for secret in sentinel.secrets for text in inputs
        ), "a planted sentinel already occurs in the fixture's own inputs"
        yield sentinel
