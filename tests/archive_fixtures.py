"""Which files in a fixture directory are Archives (E004-S01-002, F-8).

Not a test module.

The one selection rule, used by every test that walks
``tests/fixtures/archives/v*/``: regular files directly in the directory, minus
the scripts that generate them (``*.py``) and minus the sidecars that describe
an Archive rather than being one (:data:`SIDECAR_SUFFIXES`). Directories, such as
``v0/malformed/`` and ``__pycache__/``, are never selected; a caller wanting the
malformed set passes that directory itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

#: Files beside an Archive that are not Archives: the released fixture's
#: provenance record, and E004-S01-003's passphrase for an encrypted fixture.
SIDECAR_SUFFIXES: Final = (".provenance.json", ".passphrase.txt")


def archive_files(directory: Path) -> list[Path]:
    """The Archive fixtures directly in ``directory``, sorted."""
    return sorted(
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix != ".py" and not p.name.endswith(SIDECAR_SUFFIXES)
    )
