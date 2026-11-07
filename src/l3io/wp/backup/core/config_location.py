"""FR-29, AD-6, AD-11 — where a restore's ``wp-config.php`` is, or is laid down.

WordPress's ``wp-load.php`` reads ``<root>/wp-config.php``, else
``<root>/../wp-config.php`` when ``<root>/..`` holds no ``wp-settings.php``
(a parent holding one is itself a WordPress install, whose config WordPress
would not read for this Site). Those are the two
:class:`~l3io.wp.backup.domain.archive_format.ConfigLocation` values. The
Archive's Manifest records which one its Site used (``config_path``); that
untrusted value only *selects* a member of the enum and is never joined to a
path (AD-11).

:func:`existing_location` is ``wp-load.php``'s rule over what the target
already has, the same rule ``adapters/database.py::locate_wp_config`` applies
to the filesystem (a test checks that they agree). :func:`place_config`
decides, for a target with no such file, where the Archive's ``config``-role
member is laid down; :func:`config_file` anchors the result to the canonical
Site Root.

**Residual window.** The facts are read before the plan, and the lay-down
happens in ``EXTRACT``. The Site Lock covers the Site Root, not its parent,
so a file created at ``<root>/../wp-config.php`` in between is not detected:
the lay-down overwrites it, and it is in neither FR-7's check nor the Safety
Snapshot (a time-of-check/time-of-use window). Nothing claims exclusivity
over the parent directory.

Pure (AD-1): booleans in, a placement out; paths are ``PurePosixPath``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from l3io.wp.backup.domain.archive_format import ConfigLocation
from l3io.wp.backup.domain.errors import (
    ConfigLocationConflictError,
    UnsupportedConfigLocationError,
)

__all__ = [
    "ConfigFacts",
    "ConfigPlacement",
    "PlacementAction",
    "PlacementSource",
    "config_file",
    "existing_location",
    "manifest_location",
    "place_config",
]

#: ``use-existing``: the target's own file is used. ``lay-down``: the
#: Archive's config is placed there (its ``config``-role member, else, for a
#: v0 Archive, the tree's copy as ``EXTRACT`` writes the Site Root). ``none``:
#: nothing is laid down (a Mode that restores no files, or no config in the
#: Archive).
PlacementAction = Literal["use-existing", "lay-down", "none"]
#: Where the location came from: the target, the Manifest, or the default.
PlacementSource = Literal["target", "manifest", "v0-default"]

_SETTINGS = "wp-settings.php"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfigFacts:
    """What the target already has, at the two places WordPress looks."""

    at_site_root: bool
    at_parent: bool
    parent_has_wp_settings: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfigPlacement:
    """Which ``ConfigLocation`` a restore uses, and what it does there."""

    location: ConfigLocation
    action: PlacementAction
    source: PlacementSource


def existing_location(facts: ConfigFacts) -> ConfigLocation | None:
    """The file WordPress would read on the target, or ``None`` (``wp-load.php``)."""
    if facts.at_site_root:
        return ConfigLocation.SITE_ROOT
    if facts.at_parent and not facts.parent_has_wp_settings:
        return ConfigLocation.PARENT
    return None


def config_file(site_root: PurePosixPath, placement: ConfigPlacement) -> PurePosixPath:
    """``placement``'s file, anchored to the canonical ``site_root``; no ``..``."""
    name = PurePosixPath(placement.location.value).name
    if placement.location is ConfigLocation.PARENT:
        return site_root.parent / name
    return site_root / name


def manifest_location(value: str) -> ConfigLocation:
    """The ``ConfigLocation`` a Manifest ``config_path`` names, or a refusal.

    :class:`UnsupportedConfigLocationError` for any other text (AD-11): the
    value selects an enum member and is never joined to a path.
    """
    try:
        return ConfigLocation(value)
    except ValueError:
        allowed = tuple(member.value for member in ConfigLocation)
        raise UnsupportedConfigLocationError(value, allowed=allowed) from None


def place_config(
    manifest_config_path: str | None,
    facts: ConfigFacts,
    *,
    site_root: PurePosixPath,
    restores_files: bool,
    archive_has_config: bool,
) -> ConfigPlacement:
    """Where the restore's ``wp-config.php`` is, raising a refusal before any effect.

    ``manifest_config_path`` is the Manifest's ``config_path`` when the
    Archive has a ``config``-role member, else ``None`` (a v0 Archive, whose
    view names a member, not a location). In order:

    1. a value that is not a ``ConfigLocation`` is
       :class:`UnsupportedConfigLocationError`, whatever the target has;
    2. the file WordPress would read on the target (:func:`existing_location`)
       is used, unless the Manifest places the config at the *other* location
       and a file WordPress could read is already there
       (:class:`ConfigLocationConflictError`, naming it: BL-E003-056);
    3. with no recorded location (a v0 Archive, whose config is in its tree,
       or a v1 one whose Site's config was at a stated path), the default
       ``SITE_ROOT``: laid down there when ``restores_files`` and
       ``archive_has_config``, else nothing;
    4. the recorded location, laid down only when ``restores_files``; a
       ``PARENT`` whose directory holds ``wp-settings.php`` is
       :class:`ConfigLocationConflictError`, naming the directory.
    """
    wanted = (
        None
        if manifest_config_path is None
        else manifest_location(manifest_config_path)
    )
    existing = existing_location(facts)
    if existing is not None:
        if (
            wanted is ConfigLocation.PARENT
            and existing is ConfigLocation.SITE_ROOT
            and facts.at_parent
            and not facts.parent_has_wp_settings
        ):
            placement = ConfigPlacement(
                location=wanted, action="lay-down", source="manifest"
            )
            raise ConfigLocationConflictError(
                config_file(site_root, placement),
                "is where the Archive's Manifest places wp-config.php, but "
                f"WordPress reads {config_file(site_root, _at(existing))} first; "
                "remove one of the two",
            )
        return _at(existing)
    if wanted is None:
        lays = restores_files and archive_has_config
        return ConfigPlacement(
            location=ConfigLocation.SITE_ROOT,
            action="lay-down" if lays else "none",
            source="v0-default",
        )
    if wanted is ConfigLocation.PARENT and facts.parent_has_wp_settings:
        raise ConfigLocationConflictError(
            site_root.parent,
            f"holds {_SETTINGS}: it is another WordPress install, and WordPress "
            "would not read a wp-config.php placed there for this Site",
        )
    return ConfigPlacement(
        location=wanted,
        action="lay-down" if restores_files else "none",
        source="manifest",
    )


def _at(location: ConfigLocation) -> ConfigPlacement:
    return ConfigPlacement(location=location, action="use-existing", source="target")
