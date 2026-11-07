"""A library caller, type-checked by ``tests/contracts/test_api_typing.py``.

Never collected and never run (no ``test_`` prefix, and ``main`` is not
called): it exists so ``mypy --strict`` proves a consumer can call every
entry point with the package's own types and read every result without a
cast or an ignore.
"""

from __future__ import annotations

from pathlib import Path

from l3io.wp.backup.api import (
    AdminTls,
    BackupResult,
    BackupScope,
    ConfigPolicy,
    ExclusionSet,
    ExplicitCredentialSpec,
    Override,
    OverrideSet,
    RelocationFlags,
    RestoreMode,
    RestoreOutcome,
    SnapshotPolicy,
    VerificationResult,
    backup,
    enumerate_defaults,
    restore,
    verify,
)


def main(site: Path, archive: Path) -> tuple[int, bool, str, tuple[str, ...]]:
    made: BackupResult = backup(
        site,
        scope=BackupScope.ALL,
        target=archive,
        credentials=ExplicitCredentialSpec(username="wp", password="placeholder"),
        exclusion=ExclusionSet.from_patterns(["*.log"], include_defaults=True),
        dry_run=False,
    )
    checked: VerificationResult = verify(str(made.path), authenticate=False)
    outcome: RestoreOutcome = restore(
        str(site),
        archive=archive,
        mode=RestoreMode.ALL,
        overrides=OverrideSet(waived=frozenset({Override.NONEMPTY_FILES})),
        snapshot=SnapshotPolicy.TAKE,
        config_policy=ConfigPolicy.KEEP,
        admin_tls=AdminTls(mode="required", ca_file=None),
        relocation=RelocationFlags.none(),
        prompt=input,
    )
    return made.bytes, checked.passed, outcome.run_id, enumerate_defaults()
