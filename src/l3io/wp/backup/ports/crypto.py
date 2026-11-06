"""ADR-0024 — the payload cipher port.

Applied once to the whole payload, streaming from a reader to a writer (AD-21).
A transform, neither query nor command. Epic 4's passphrase-only
``adapters/crypto.PassphraseCipher`` implements it (E004-S01-003), built only by
``adapters/crypto.build_payload_cipher``. ``restore`` and ``verify
--authenticate`` bind a real cipher whenever a passphrase resolves; ``backup``
binds ``None`` until the encryption flag ships (E008).
"""

from __future__ import annotations

from typing import IO, Protocol

__all__ = ["PayloadCipher"]


class PayloadCipher(Protocol):
    """Encrypts or decrypts the payload as a stream."""

    def check(self) -> None:
        """Raise before any write if the cipher cannot run."""
        ...

    def encrypt(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        """Read plaintext from ``reader`` and write ciphertext to ``writer``."""
        ...

    def decrypt(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        """Read ciphertext from ``reader`` and write plaintext to ``writer``."""
        ...
