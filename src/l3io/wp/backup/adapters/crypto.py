"""ADR-0014/ADR-0024 — the passphrase payload cipher, through ``l3io-wp-database``.

:class:`PassphraseCipher` implements the ``PayloadCipher`` port by calling
``l3io.wp.database.encryption``'s ``encrypt``/``decrypt`` with ``passphrase=``;
no cipher, KDF or ``age`` code is written or imported here (ADR-0014). The
database import is deferred (AD-8): without the ``database`` extra, or without
database's passphrase backend, it raises ``MissingExtraError``.

Database's errors are mapped into this package's hierarchy (AD-14) and kept
distinct: a passphrase that cannot open the payload is
:class:`PassphraseRejectedError`, a damaged, truncated or non-age stream is
:class:`PayloadDamagedError`. Both are ``VERIFICATION_FAILED`` (Shawn,
2026-11-03): ``age`` authenticates every chunk, so either means the payload
could not be authenticated. Neither message carries database's text.

``verify --authenticate`` is the first caller (BL-E002-060). E004-S01-003 binds
the same cipher into backup.
"""

from __future__ import annotations

from typing import IO

from l3io.wp.backup.domain.errors import (
    MissingExtraError,
    PassphraseRejectedError,
    PayloadDamagedError,
)

__all__ = ["PassphraseCipher"]

_EXTRA = "database"


class PassphraseCipher:
    """``PayloadCipher`` over database's ``age`` passphrase streams."""

    __slots__ = ("_passphrase",)

    def __init__(self, passphrase: str) -> None:
        self._passphrase = passphrase

    def __repr__(self) -> str:
        return "PassphraseCipher(passphrase=<redacted>)"

    def check(self) -> None:
        """Raise ``MissingExtraError`` before any write if no backend can run."""
        try:
            from l3io.wp.database import encryption
            from l3io.wp.database.errors import MissingDependencyError
        except ImportError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc
        try:
            encryption.check_passphrase(self._passphrase)
        except MissingDependencyError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc

    def encrypt(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        """Stream ``reader`` into ``writer`` as ``age`` (a sole scrypt recipient)."""
        try:
            from l3io.wp.database import encryption
            from l3io.wp.database.errors import MissingDependencyError
        except ImportError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc
        try:
            encryption.encrypt(reader, writer, passphrase=self._passphrase)
        except MissingDependencyError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc

    def decrypt(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        """Stream ``age`` ciphertext into ``writer``, authenticating every chunk."""
        try:
            from l3io.wp.database import encryption
            from l3io.wp.database.errors import (
                CorruptDumpError,
                DecryptionError,
                MissingDependencyError,
            )
        except ImportError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc
        try:
            encryption.decrypt(reader, writer, passphrase=self._passphrase)
        except MissingDependencyError as exc:
            raise MissingExtraError(extra=_EXTRA) from exc
        except DecryptionError as exc:
            raise PassphraseRejectedError from exc
        except CorruptDumpError as exc:
            raise PayloadDamagedError from exc
