"""FR-11, FR-29, AD-17 — Epic 7's restore extension (BL-E007-019: its own module).

:func:`restore_extension` is the factory ``cli/restore.py`` imports (the
one-line ownership grant) as ``drive_restore``'s ``extension`` default. It
returns a :class:`WpCliRestoreExtension`, which satisfies
``cli/restore_request.py``'s ``RestoreExtension`` Protocol structurally: it
validates the relocation flags (``core/relocation.py``), fills
``core/restore.py``'s ``RestoreRelocation``, places ``wp-config.php`` by
``core/config_location.py``'s rules over facts it reads here (BL-E007-018),
hands the relocated account to the root as an ``ExplicitCredentialSpec``
and, when a URL is relocated and ``wp`` was found, returns
``adapters/wpcli.py``'s ``WpCliSearchReplace``. No ``l3io.wp.database`` type
is named here.

``restore_extension`` resolves ``wp`` on ``PATH`` once, with
``adapters/preflight.py::locate_tool`` (absolute entries only); see
``adapters/wpcli.py`` for the residual window that leaves.

The factory is annotated with ``core/relocation.py::RelocationRequest``,
because ``adapters/`` may not import ``cli/`` where ``RelocationFlags`` lives.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path, PurePosixPath
from typing import Final

import structlog

from l3io.wp.backup.adapters.database import database_location_from_db_host
from l3io.wp.backup.adapters.preflight import (
    locate_tool,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.wpcli import (
    WpCliSearchReplace,
    check_wp_cli_directory,
    owed_search_replace,
)
from l3io.wp.backup.core.config_location import (
    ConfigFacts,
    ConfigPlacement,
    config_file,
    existing_location,
    place_config,
    recorded_location,
    require_lay_down_free,
    require_stated_config_loaded,
)
from l3io.wp.backup.core.relocation import (
    RelocationRequest,
    relocation_actions,
    relocation_undo,
    required_tools,
    rewrite_values,
    url_relocation,
    validate_relocation,
    validate_relocation_values,
)
from l3io.wp.backup.core.restore import (
    RestoreRelocation,
    planned_cutover_keys,
)
from l3io.wp.backup.domain.archive_format import (
    WP_CONFIG_NAME,
    WP_SETTINGS_NAME,
    ArchiveView,
    ConfigLocation,
    config_member,
)
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase, restores_files
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.ports.search_replace import SearchReplaceCommand
from l3io.wp.backup.ports.site_files import SiteFilesQuery

__all__ = ["RELOCATED_CREDENTIAL_SOURCE", "WpCliRestoreExtension", "restore_extension"]

_log = structlog.get_logger(__name__)

#: How the relocated account's source is labelled in records (never a value).
RELOCATED_CREDENTIAL_SOURCE = "--new-db-user/--new-db-password"
#: The file ``wp-load.php`` looks for, and the one that marks an install.
_CONFIG: Final = WP_CONFIG_NAME
_SETTINGS: Final = WP_SETTINGS_NAME


class WpCliRestoreExtension:
    """What Epic 7 supplies to one restore, for one set of relocation flags."""

    __slots__ = ("_replacer", "_request", "_secrets", "_wp")

    def __init__(
        self,
        request: RelocationRequest,
        *,
        secrets: SecretRegistry | None = None,
        wp: str | None = None,
    ) -> None:
        self._request = request
        self._secrets = secrets if secrets is not None else SecretRegistry()
        self._wp = wp
        self._replacer: WpCliSearchReplace | None = None

    def __repr__(self) -> str:
        return "WpCliRestoreExtension()"

    def required_tools(self) -> tuple[ToolRequirement, ...]:
        """WP-CLI's requirement iff a URL is relocated (``core/relocation.py``).

        This is the one route a relocation's tools reach the plan:
        ``RestoreRelocation.required_tools`` stays ``()``.
        """
        return required_tools(self._request)

    def wordpress_spec(self) -> ExplicitCredentialSpec | None:
        """The relocated account, or ``None`` without the user/password pair."""
        user, password = self._request.new_db_user, self._request.new_db_password
        if user is None or password is None:
            return None
        return ExplicitCredentialSpec(user, password)

    def search_replace(self) -> SearchReplaceCommand | None:
        """WP-CLI's ``search-replace`` when a URL is relocated and ``wp`` was found.

        ``None`` otherwise: with no URL there is no ``SEARCH_REPLACE`` phase,
        and with no ``wp`` the root's ``UnboundSearchReplace`` fails closed
        (story 7.4's preflight refuses first). One instance per extension.
        """
        if url_relocation(self._request) is None or self._wp is None:
            return None
        if self._replacer is None:
            self._replacer = WpCliSearchReplace(self._wp, secrets=self._secrets)
        return self._replacer

    def resolve(
        self,
        *,
        site_root: Path,
        site: SiteDescriptor,
        view: ArchiveView,
        files: SiteFilesQuery,
        mode: RestoreMode,
    ) -> RestoreRelocation:
        """The relocation for ``mode``, or a ``UsageError`` before any effect.

        ``validate_relocation`` first. With ``--new-db-host`` or
        ``--new-db-name``, the load location is parsed once by
        ``database_location_from_db_host`` (the one ``host:port`` splitter),
        defaulting each half to the Site's own; a bad host or name is its
        ``UsageError``. Then the config placement (:meth:`_config_file`).
        """
        request = self._request
        validate_relocation(request, mode=mode)
        load_location = None
        if request.new_db_host is not None or request.new_db_name is not None:
            host_text = (
                request.new_db_host
                if request.new_db_host is not None
                else f"{site.db_host}:{site.db_port}"
            )
            name = (
                request.new_db_name
                if request.new_db_name is not None
                else (site.db_name)
            )
            load_location = database_location_from_db_host(host_text, name)
        prior = {"WP_SITEURL": site.site_url, "WP_HOME": site.home_url}
        urls = url_relocation(request)
        undo = relocation_undo(request, prior=prior)
        if urls is not None:
            check_wp_cli_directory(site_root)
            owed = owed_search_replace(site_root, urls, wp=self._wp)
            undo = (*undo, (RestorePhase.SEARCH_REPLACE, owed))
        laid_down = self._config_file(
            site_root=site_root,
            view=view,
            files=files,
            mode=mode,
            urls_relocated=urls is not None,
        )
        relocation = RestoreRelocation(
            rewrite_values=rewrite_values(request),
            load_database=request.new_db_name,
            load_location=load_location,
            cutover_host=request.new_db_host,
            cutover_user=request.new_db_user,
            cutover_password=request.new_db_password is not None,
            search_replace=urls,
            config_file=laid_down,
            lay_down_config=laid_down is not None,
            required_tools=(),
            actions=(),
            undo=undo,
        )
        relocation = dataclasses.replace(
            relocation, actions=relocation_actions(relocation)
        )
        relocated = dataclasses.replace(
            relocation, config_file=None, lay_down_config=False
        )
        if relocated != RestoreRelocation.none():
            # Logged as the relocation is resolved, before the survey and
            # FR-7's check: a Run refused after this still logged it.
            _log.info(
                "relocation_planned",
                keys=[key for key, _ in relocation.rewrite_values],
                cutover_keys=list(planned_cutover_keys(relocation)),
                db_location=None
                if load_location is None
                else {
                    "host": load_location.host,
                    "port": load_location.port,
                    # The database loaded: None when it is the Run's
                    # staging database on that host (no --new-db-name).
                    "name": request.new_db_name,
                },
                load_target=request.new_db_name or "a staging database",
                credential_source=RELOCATED_CREDENTIAL_SOURCE
                if relocation.cutover_password
                else site.credential_source,
                site_url_changed=request.new_site_url is not None,
                home_url_changed=request.new_home_url is not None,
            )
        return relocation

    def _config_file(
        self,
        *,
        site_root: Path,
        view: ArchiveView,
        files: SiteFilesQuery,
        mode: RestoreMode,
        urls_relocated: bool,
    ) -> PurePosixPath | None:
        """The file to lay ``wp-config.php`` down at, or ``None`` (FR-29).

        ``None`` when the target already has the config WordPress reads (the
        root located it at step 3 and it stands), when ``--wp-config`` named
        one (used as given, overriding the search), or when nothing is laid
        down. A Manifest ``config_path`` outside ``ConfigLocation`` is refused
        whether or not ``--wp-config`` is given. The facts are what
        ``locate_wp_config`` reads: a regular file (symlinks followed) at the
        two places ``wp-load.php`` looks, and anything at
        ``<root>/../wp-settings.php``. A lay-down is refused when anything at
        all (a directory, a dangling symlink) is at its path. ``site_root`` is
        the root's canonical Site Root.

        A URL relocation whose ``--wp-config`` is not the file WordPress would
        load is a ``UsageError``: WP-CLI's ``search-replace`` would bootstrap
        from another config than the one rewritten, a partial relocation
        (FR-13).
        """
        root = PurePosixPath(site_root)
        wanted = recorded_location(view)
        facts = ConfigFacts(
            at_site_root=_regular_file(files, site_root / _CONFIG),
            at_parent=_regular_file(files, site_root.parent / _CONFIG),
            parent_has_wp_settings=files.file_exists(site_root.parent / _SETTINGS),
        )
        recorded = None if wanted is None else wanted.value
        stated = self._request.wp_config
        if stated is not None:
            existing = existing_location(facts)
            read = None if existing is None else config_file(root, _existing(existing))
            loaded = read is not None and _same_file(Path(read), Path(stated))
            require_stated_config_loaded(
                PurePosixPath(stated),
                loaded=loaded,
                read=read,
                site_root=root,
                urls_relocated=urls_relocated,
            )
            _log.info(
                "config_located",
                config_path=os.path.realpath(stated),
                location=existing.value if loaded and existing else None,
                action="use-existing",
                source="stated",
                manifest_config_path=recorded,
            )
            return None
        placement = place_config(
            recorded,
            facts,
            site_root=root,
            restores_files=restores_files(mode),
            archive_has_config=config_member(view) is not None,
        )
        path = config_file(root, placement)
        require_lay_down_free(path, placement, occupied=os.path.lexists(path))
        _log.info(
            "config_located",
            config_path=str(path),
            location=placement.location.value,
            action=placement.action,
            source=placement.source,
            manifest_config_path=recorded,
        )
        return path if placement.action == "lay-down" else None


def _existing(location: ConfigLocation) -> ConfigPlacement:
    return ConfigPlacement(location=location, action="use-existing", source="target")


def _regular_file(files: SiteFilesQuery, path: Path) -> bool:
    """``locate_wp_config``'s test: a regular file, a symlink to one followed."""
    return files.file_exists(path) and os.path.isfile(path)  # noqa: PTH113


def _same_file(left: Path, right: Path) -> bool:
    """Whether two paths name one file, symlinks and ``..`` resolved."""
    return os.path.realpath(left) == os.path.realpath(right)


def restore_extension(
    flags: RelocationRequest, *, secrets: SecretRegistry
) -> WpCliRestoreExtension:
    """``drive_restore``'s ``extension`` default (``RestoreExtensionFactory``).

    ``--new-db-password`` is registered with ``secrets`` here, the one place
    it is (BL-E003-094); the root calls this before it reads the Site or the
    Archive. The
    Mode-independent rules (``validate_relocation_values``) run here, before
    the root reads the Site or the Archive; the Mode rule runs in
    :meth:`WpCliRestoreExtension.resolve`. With a URL relocated, ``wp`` is
    looked up on ``PATH`` here, once, by preflight's own rule
    (:func:`~l3io.wp.backup.adapters.preflight.locate_tool`: relative and
    empty entries ignored), so the path is absolute and is the one checked.
    """
    if flags.new_db_password:
        secrets.register(flags.new_db_password, source="--new-db-password")
    validate_relocation_values(flags)
    wp = None
    if url_relocation(flags) is not None:
        wp = locate_tool("wp")
    return WpCliRestoreExtension(flags, secrets=secrets, wp=wp)
