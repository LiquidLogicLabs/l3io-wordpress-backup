"""FR-11, AD-17, ADR-0015 — Epic 7's restore extension.

:func:`restore_extension` is the factory ``cli/restore.py`` imports as
``RESTORE_EXTENSION`` (the one-line ownership grant). It returns a
:class:`WpCliRestoreExtension`, which satisfies ``cli/restore.py``'s
``RestoreExtension`` Protocol structurally:

* :meth:`~WpCliRestoreExtension.resolve` validates the relocation flags
  (``core/relocation.py``) and fills ``core/restore.py``'s
  ``RestoreRelocation``: the URL keys ``REWRITE_CONFIG`` writes, and the
  ``DB_*`` values ``LOAD_DUMP``'s cutover writes with the relocated load
  location;
* :meth:`~WpCliRestoreExtension.wordpress_spec` hands the relocated account to
  the composition root as an ``ExplicitCredentialSpec``; the root registers
  and resolves it. No ``l3io.wp.database`` type is named here;
* :meth:`~WpCliRestoreExtension.search_replace` and
  :meth:`~WpCliRestoreExtension.required_tools` are E007-S01-003's and
  E007-S01-004's: until they land, no content rewrite and no extra tool.

The factory is annotated with ``core/relocation.py::RelocationRequest``,
because ``adapters/`` may not import ``cli/`` where ``RelocationFlags`` lives.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import structlog

from l3io.wp.backup.adapters.database import database_location_from_db_host
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.core.relocation import (
    RelocationRequest,
    relocation_actions,
    relocation_undo,
    rewrite_values,
    validate_relocation,
    validate_relocation_values,
)
from l3io.wp.backup.core.restore import (
    RestoreRelocation,
    planned_cutover_keys,
)
from l3io.wp.backup.domain.archive_format import ArchiveView
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.preflight import ToolRequirement
from l3io.wp.backup.domain.restore import RestoreMode
from l3io.wp.backup.domain.site import SiteDescriptor
from l3io.wp.backup.ports.search_replace import SearchReplaceCommand
from l3io.wp.backup.ports.site_files import SiteFilesQuery

__all__ = ["WpCliRestoreExtension", "restore_extension"]

_log = structlog.get_logger(__name__)

#: How the relocated account's source is labelled in records (never a value).
RELOCATED_CREDENTIAL_SOURCE = "--new-db-user/--new-db-password"


class WpCliRestoreExtension:
    """What Epic 7 supplies to one restore, for one set of relocation flags."""

    __slots__ = ("_request",)

    def __init__(self, request: RelocationRequest) -> None:
        self._request = request

    def __repr__(self) -> str:
        return "WpCliRestoreExtension()"

    def required_tools(self) -> tuple[ToolRequirement, ...]:
        """None yet: E007-S01-004 registers WP-CLI's requirement."""
        return ()

    def wordpress_spec(self) -> ExplicitCredentialSpec | None:
        """The relocated account, or ``None`` without the user/password pair."""
        user, password = self._request.new_db_user, self._request.new_db_password
        if user is None or password is None:
            return None
        return ExplicitCredentialSpec(user, password)

    def search_replace(self) -> SearchReplaceCommand | None:
        """None yet: E007-S01-003 binds WP-CLI's ``search-replace``."""
        return None

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
        ``UsageError``.
        """
        del site_root, view, files
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
        relocation = RestoreRelocation(
            rewrite_values=rewrite_values(request),
            load_database=request.new_db_name,
            load_location=load_location,
            cutover_host=request.new_db_host,
            cutover_user=request.new_db_user,
            cutover_password=request.new_db_password is not None,
            search_replace=None,
            config_file=None,
            lay_down_config=False,
            required_tools=(),
            actions=(),
            undo=relocation_undo(request, prior=prior),
        )
        relocation = dataclasses.replace(
            relocation, actions=relocation_actions(relocation)
        )
        if relocation != RestoreRelocation.none():
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


def restore_extension(
    flags: RelocationRequest, *, secrets: SecretRegistry
) -> WpCliRestoreExtension:
    """``cli/restore.py``'s ``RESTORE_EXTENSION`` (``RestoreExtensionFactory``).

    ``--new-db-password`` is registered with ``secrets`` again here, so a root
    that skipped it still redacts the value (registration is idempotent). The
    Mode-independent rules (``validate_relocation_values``) run here, before
    the root reads the Site or the Archive; the Mode rule runs in
    :meth:`WpCliRestoreExtension.resolve`.
    """
    if flags.new_db_password:
        secrets.register(flags.new_db_password, source="--new-db-password")
    validate_relocation_values(flags)
    return WpCliRestoreExtension(flags)
