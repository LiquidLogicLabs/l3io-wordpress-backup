"""FR-21 — the exit-code table: the one source of truth for what a Run exits with.

The family codes are those of ``l3io-wp-database``'s ``cli.Exit`` (v0.2.0):
0 OK, 1 FAILED, 2 USAGE, 3 REFUSED, 4 CREDENTIALS, 5 UNREACHABLE and
7 NOT_READY, with **6 reserved for the family** and therefore not a member.
Two codes are this package's own: 8 VERIFICATION_FAILED and 9 LOCKED — the
next free values after the family's 7, skipping the reserved 6 (decided
2026-10-20). If the family later reserves a block above 7, these move before
the first release, never after. ``tests/contracts/test_exit_codes_family.py``
checks the family members against database's enum without an import here.

**Append-only (PRD §10).** A value is never renumbered or reused; a member is
added only for a genuinely new kind of failure, and only by E002.

**How later failures map onto this table**, without a new member:

- a non-empty restore target (FR-7) and an incomplete journal (AD-18) are
  refusals: ``REFUSED``;
- a missing tool — WP-CLI (FR-13), or any FR-27 prerequisite — is
  ``NOT_READY``;
- a decryption failure (Epic 4) is ``CREDENTIALS`` for a missing or wrong key
  and ``VERIFICATION_FAILED`` for corrupt ciphertext — except under ``verify
  --authenticate``, where a wrong passphrase is ``VERIFICATION_FAILED`` too
  (``PassphraseRejectedError``; Shawn, 2026-11-03): the Archive could not be
  authenticated.

Each error class carries its code as the class attribute
``BackupError.exit_code`` (``domain/errors.py``); the CLI reads that attribute
and never matches over classes.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import IntEnum, unique
from types import MappingProxyType
from typing import Final

__all__ = ["DESCRIPTIONS", "RESERVED", "ExitCode"]


@unique
class ExitCode(IntEnum):
    """The process exit code of a ``wpbackup2`` Run."""

    OK = 0
    FAILED = 1
    USAGE = 2
    REFUSED = 3
    CREDENTIALS = 4
    UNREACHABLE = 5
    NOT_READY = 7
    VERIFICATION_FAILED = 8
    LOCKED = 9


#: Values the family reserves; never a member of :class:`ExitCode`.
RESERVED: Final[frozenset[int]] = frozenset({6})

#: One line per member, used by the CLI's stderr message, ``--help`` and README.
DESCRIPTIONS: Final[Mapping[ExitCode, str]] = MappingProxyType(
    {
        ExitCode.OK: "the Run succeeded",
        ExitCode.FAILED: "the Run failed for a reason no other code names",
        ExitCode.USAGE: "the command line was invalid",
        ExitCode.REFUSED: "a check found a reason not to act; nothing was done",
        ExitCode.CREDENTIALS: "credentials were missing, unusable or rejected",
        ExitCode.UNREACHABLE: "the database server could not be reached",
        ExitCode.NOT_READY: "a required tool or optional extra is not available",
        ExitCode.VERIFICATION_FAILED: "the archive failed verification",
        ExitCode.LOCKED: "another Run holds the site lock",
    }
)
