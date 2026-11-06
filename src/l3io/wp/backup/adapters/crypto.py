"""ADR-0014/ADR-0024, AD-26 — the passphrase payload cipher, via ``l3io-wp-database``.

:class:`PassphraseCipher` implements the ``PayloadCipher`` port by calling
``l3io.wp.database.encryption``'s ``encrypt``/``decrypt`` with ``passphrase=``;
no cipher, KDF or ``age`` code is written or imported here (ADR-0014). The
database import is deferred (AD-8): without the ``database`` extra, or without
database's passphrase backend, it raises ``MissingExtraError``. It is the one
``PayloadCipher`` in the package (BL-E004-042), and every composition root
builds it through :func:`build_payload_cipher` (E004-S01-003).

Database's errors are mapped into this package's hierarchy (AD-14) and kept
distinct: a passphrase that cannot open the payload is
:class:`PassphraseRejectedError`, a damaged, truncated or non-age stream is
:class:`PayloadDamagedError`. Both are ``VERIFICATION_FAILED`` (Shawn,
2026-11-03): ``age`` authenticates every chunk, so either means the payload
could not be authenticated. A passphrase or backend database refuses as
invalid is :class:`EncryptionConfigError` (``USAGE``). No message carries
database's text, and none carries the passphrase.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Mapping
from typing import IO, Final, overload

from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.domain.errors import (
    MissingExtraError,
    PassphraseRejectedError,
    PayloadDamagedError,
    UsageError,
)
from l3io.wp.backup.domain.preflight import ToolRequirement

__all__ = [
    "EncryptionConfigError",
    "PassphraseCipher",
    "build_payload_cipher",
    "passphrase_backend_requirements",
]

_EXTRA = "database"
#: Database's backend switch (its README and ADR-0003).
_BACKEND_ENV: Final = "L3IO_AGE_PASSPHRASE_BACKEND"
_BINDER_MODULE: Final = "l3io_age_passphrase"
#: ``age`` 1.3 is the first release that ships ``age-plugin-batchpass`` (FR-27).
_AGE_MINIMUM: Final = (1, 3)
_NEEDED_FOR: Final = "the encrypted payload (ENCRYPT, VERIFY)"
_REGISTRY_SOURCE: Final = "passphrase"


class EncryptionConfigError(UsageError):
    """Database refused the passphrase or its backend setting as invalid.

    Database's ``InvalidArgumentsError`` from ``check_passphrase``: an empty
    passphrase, an unrecognised ``L3IO_AGE_PASSPHRASE_BACKEND``, or the ``cli``
    backend without ``age`` and ``age-plugin-batchpass`` on ``PATH``. The
    message never carries database's text, which could echo the input.
    """

    def __init__(self) -> None:
        super().__init__(
            "encryption cannot run: the passphrase is empty, or "
            f"{_BACKEND_ENV} names a backend that is unknown or not installed"
        )


def passphrase_backend_requirements(
    *, environ: Mapping[str, str], needed_for: str
) -> tuple[ToolRequirement, ...]:
    """The external tools database's passphrase backend needs (Gate E004 AG-8).

    Restates database's documented selection (README, ADR-0003), because its
    resolver is private: ``binder`` needs no binary; ``auto`` (or unset) needs
    none when the ``l3io-age-passphrase`` binder is importable (detected with
    ``find_spec``, never imported), else ``age`` >= 1.3 and
    ``age-plugin-batchpass``, which ``cli`` always needs. An unrecognised value
    needs nothing here: ``check()`` refuses it, in one place. Reads only
    ``environ``. Replace with a call if database exports a backend query.
    """
    requested = environ.get(_BACKEND_ENV, "auto").strip().lower() or "auto"
    cli = (
        ToolRequirement("age", _AGE_MINIMUM, needed_for),
        ToolRequirement("age-plugin-batchpass", None, needed_for),
    )
    if requested == "cli":
        return cli
    if requested == "auto" and importlib.util.find_spec(_BINDER_MODULE) is None:
        return cli
    return ()


class PassphraseCipher:
    """``PayloadCipher`` over database's ``age`` passphrase streams."""

    __slots__ = ("_passphrase",)

    def __init__(self, passphrase: str) -> None:
        self._passphrase = passphrase

    def __repr__(self) -> str:
        return "PassphraseCipher(passphrase=<redacted>)"

    def required_tools(self) -> tuple[ToolRequirement, ...]:
        """The backend's external tools, for the plan's ``extra_tools``."""
        return passphrase_backend_requirements(
            environ=os.environ, needed_for=_NEEDED_FOR
        )

    def check(self) -> None:
        """Refuse before any write if the passphrase or its backend cannot run.

        ``MissingExtraError`` when database, ``pyrage`` or a usable backend is
        missing; ``EncryptionConfigError`` for an invalid passphrase or backend
        setting.
        """
        try:
            from l3io.wp.database import encryption
            from l3io.wp.database.errors import (
                InvalidArgumentsError,
                MissingDependencyError,
            )
        except ImportError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc
        try:
            encryption.check_passphrase(self._passphrase)
        except MissingDependencyError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc
        except InvalidArgumentsError as exc:
            raise EncryptionConfigError from exc

    def encrypt(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        """Stream ``reader`` into ``writer`` as ``age`` (a sole scrypt recipient)."""
        try:
            from l3io.wp.database import encryption
            from l3io.wp.database.errors import (
                InvalidArgumentsError,
                MissingDependencyError,
            )
        except ImportError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc
        try:
            encryption.encrypt(reader, writer, passphrase=self._passphrase)
        except MissingDependencyError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc
        except InvalidArgumentsError as exc:
            raise EncryptionConfigError from exc

    def decrypt(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        """Stream ``age`` ciphertext into ``writer``, authenticating every chunk."""
        try:
            from l3io.wp.database import encryption
            from l3io.wp.database.errors import (
                CorruptDumpError,
                DecryptionError,
                InvalidArgumentsError,
                MissingDependencyError,
            )
        except ImportError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc
        try:
            encryption.decrypt(reader, writer, passphrase=self._passphrase)
        except MissingDependencyError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc
        except InvalidArgumentsError as exc:
            raise EncryptionConfigError from exc
        except DecryptionError as exc:
            raise PassphraseRejectedError from exc
        except CorruptDumpError as exc:
            raise PayloadDamagedError from exc


@overload
def build_payload_cipher(
    *, passphrase: str, secrets: SecretRegistry
) -> PassphraseCipher: ...
@overload
def build_payload_cipher(*, passphrase: None, secrets: SecretRegistry) -> None: ...
@overload
def build_payload_cipher(
    *, passphrase: str | None, secrets: SecretRegistry
) -> PassphraseCipher | None: ...
def build_payload_cipher(
    *, passphrase: str | None, secrets: SecretRegistry
) -> PassphraseCipher | None:
    """The one way a composition root turns a resolved passphrase into a cipher.

    ``None`` for ``None``: whether a passphrase was required is the root's
    call (``require_passphrase``, ``_require_cipher``, ``--authenticate``).
    Otherwise the passphrase is registered for redaction (idempotent: the
    AD-26 chain registered it already), and the cipher's ``check()`` runs
    before it is returned, so every root refuses identically, Dry Run too.
    """
    if passphrase is None:
        return None
    secrets.register(passphrase, source=_REGISTRY_SOURCE)
    cipher = PassphraseCipher(passphrase)
    cipher.check()
    return cipher
