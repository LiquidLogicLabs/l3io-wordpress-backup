"""Owner decision 87 — the command's up-front access check, before a Run opens.

Each root calls :func:`require_run_access` with the paths its command needs,
after any switch to the Site Root's owner (owner decisions 58, 67), so the
check is made as the user the Run runs as. A path that user cannot read or
write is refused with ``PathNotAccessibleError`` (``REFUSED``), naming the
path, the user's ids, the path's owner, group and mode, and the operator's
fix; nothing is changed, a Dry Run too. ``adapters/access.require_access``
makes each check.

What each command needs:

- the per-user state directory (the Site Lock, the Run Journal, the default
  Safety Snapshots), written by ``backup``, ``restore`` and ``prune``;
- ``TMPDIR`` when it is set, written by ``backup`` and ``restore`` (the Run
  workspace holds the plaintext Dump; Python would otherwise fall back to
  another temporary directory without a word);
- the Archive directory, written by ``backup`` and ``prune``;
- a backup's Site Root, read;
- a restore's ``--snapshot-dir``, written.

The Archive a ``restore`` or ``verify`` reads is checked where it is opened
(``adapters/access.archive_open_error``); a restore's Site Root by its target
survey (``TargetNotWritableError``).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from l3io.wp.backup.adapters.access import require_access
from l3io.wp.backup.adapters.lock import state_root

__all__ = ["require_run_access"]


def require_run_access(
    *,
    environ: Mapping[str, str],
    workspace: bool,
    archive_dir: Path | None = None,
    site_root: Path | None = None,
    snapshot_dir: Path | None = None,
) -> None:
    """Refuse the first path the Run's user cannot use; see the module docstring.

    ``workspace``: the Run makes a workspace under ``TMPDIR``. The state
    directory is always checked.
    """
    require_access(state_root(), role="state directory", access="write")
    tmpdir = environ.get("TMPDIR")
    if workspace and tmpdir:
        require_access(
            Path(tmpdir), role="temporary directory (TMPDIR)", access="write"
        )
    if snapshot_dir is not None:
        require_access(snapshot_dir, role="Safety Snapshot directory", access="write")
    if archive_dir is not None:
        require_access(archive_dir, role="Archive directory", access="write")
    if site_root is not None:
        require_access(site_root, role="Site Root", access="read")
