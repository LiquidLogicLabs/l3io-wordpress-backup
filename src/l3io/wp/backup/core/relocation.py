"""AD-17, AD-18, FR-11 — what a relocating restore changes, derived from its flags.

Epic 3 built the seam (``core/restore.py::RestoreRelocation``); this module
derives the parts of it that are pure value work, for
``adapters/wpcli.py::WpCliRestoreExtension.resolve``:

* :class:`RelocationRequest` — the seven relocation flags, read-only. It is
  the type ``adapters/`` names, because ``adapters/`` may not import
  ``cli/`` (import-linter, "AD-1 — layers run inward only"), where
  ``RelocationFlags`` lives;
* :func:`validate_relocation` — the URL string rules, the user/password pair
  rule (both also :func:`validate_relocation_values`, run as soon as the
  flags are known) and the Mode rule, in that order, each a ``UsageError``
  before any effect;
* :func:`rewrite_values` — what ``REWRITE_CONFIG`` writes: ``WP_SITEURL`` and
  ``WP_HOME`` only. The ``DB_*`` keys are the cutover's (AD-17 as amended
  2026-10-27), carried by ``RestoreRelocation.cutover_*``;
* :func:`relocation_actions` and :func:`relocation_undo` — the plan's extra
  action line (keys only) and the journal's undo text (the URL keys with
  their prior values, which nothing else records).

The new database password never enters this module beyond the fact that it
was supplied: :func:`validate_relocation` reads whether it is ``None`` or
empty, and nothing here stores, renders or returns it.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from pathlib import PurePath
from types import MappingProxyType
from typing import Final, Protocol

from l3io.wp.backup.core.restore import (
    CONFIG_FILE_NAME,
    RestoreRelocation,
    planned_cutover_keys,
)
from l3io.wp.backup.domain.errors import (
    IncompleteRelocationCredentialsError,
    InvalidRelocationValueError,
    RelocationNeedsDatabaseRestoreError,
)
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase, restores_database

__all__ = [
    "RELOCATION_FLAGS",
    "URL_SCHEMES",
    "RelocationRequest",
    "relocation_actions",
    "relocation_undo",
    "rewrite_values",
    "validate_relocation",
    "validate_relocation_values",
]


class RelocationRequest(Protocol):
    """The relocation flags, read-only: ``cli/restore.py::RelocationFlags``'s shape."""

    @property
    def new_site_url(self) -> str | None: ...

    @property
    def new_home_url(self) -> str | None: ...

    @property
    def new_db_host(self) -> str | None: ...

    @property
    def new_db_name(self) -> str | None: ...

    @property
    def new_db_user(self) -> str | None: ...

    @property
    def new_db_password(self) -> str | None: ...

    @property
    def wp_config(self) -> PurePath | None: ...


#: Each ``--new-*`` attribute of :class:`RelocationRequest` and the flag that
#: sets it, as the operator spells it (errors name the flag). ``wp_config`` is
#: E007-S01-002's and not here. A test checks this against the parser.
RELOCATION_FLAGS: Final = MappingProxyType(
    {
        "new_site_url": "--new-site-url",
        "new_home_url": "--new-site-home-url",
        "new_db_host": "--new-db-host",
        "new_db_name": "--new-db-name",
        "new_db_user": "--new-db-user",
        "new_db_password": "--new-db-password",
    }
)
#: The URL flags and the ``wp-config.php`` constant each sets, in write order.
_URL_KEYS: Final = (("new_site_url", "WP_SITEURL"), ("new_home_url", "WP_HOME"))
#: The schemes a WordPress site or home URL may carry.
URL_SCHEMES: Final = ("http://", "https://")
#: Characters a URL constant may not hold: quotes and a backslash.
_QUOTES: Final = frozenset("'\"\\")
#: Unicode categories refused in a URL: control, format and surrogate.
_CONTROL_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs"})


#: Characters that end a URL's authority (its host and port).
_AUTHORITY_ENDS: Final = "/?#"


def _authority(rest: str) -> str:
    """The text after the scheme up to the first of ``/``, ``?`` or ``#``."""
    cut = min((rest.find(c) for c in _AUTHORITY_ENDS if c in rest), default=len(rest))
    return rest[:cut]


def _url_rule_broken(value: str) -> str | None:
    """The first URL string rule ``value`` breaks, or ``None``. No ``urllib``."""
    if value.startswith("-"):
        return "must not start with '-'"
    if not value.startswith(URL_SCHEMES):
        return "must start with http:// or https://"
    for char in value:
        if char.isspace():
            return "must not contain whitespace"
        if unicodedata.category(char) in _CONTROL_CATEGORIES:
            return "must not contain a control character"
        if char in _QUOTES:
            return "must not contain a quote or a backslash"
    authority = _authority(value.split("://", 1)[1])
    if not authority or authority.startswith((":", "@")) or authority.endswith("@"):
        return "must name a host after the scheme"
    if value.endswith("/"):
        return "must not end with '/' (WordPress stores its URLs without one)"
    return None


def _given(request: RelocationRequest) -> tuple[str, ...]:
    """The ``--new-*`` flags ``request`` sets, in :data:`RELOCATION_FLAGS` order."""
    return tuple(
        flag
        for name, flag in RELOCATION_FLAGS.items()
        if getattr(request, name) is not None
    )


def validate_relocation_values(request: RelocationRequest) -> None:
    """The Mode-independent rules, checkable as soon as the flags are known.

    :class:`InvalidRelocationValueError` for a URL breaking a string rule, or
    an empty ``--new-db-user``/``--new-db-password``; then
    :class:`IncompleteRelocationCredentialsError` for one half of the
    user/password pair. ``--new-db-host`` and ``--new-db-name`` are checked by
    the database package where they are parsed
    (``adapters/database.py::database_location_from_db_host``).
    """
    for name, _ in _URL_KEYS:
        value = getattr(request, name)
        if value is None:
            continue
        broken = _url_rule_broken(value)
        if broken is not None:
            raise InvalidRelocationValueError(RELOCATION_FLAGS[name], broken)
    user, password = request.new_db_user, request.new_db_password
    for name, value in (("new_db_user", user), ("new_db_password", password)):
        if value == "":
            flag = RELOCATION_FLAGS[name]
            raise InvalidRelocationValueError(flag, "must not be empty")
    if (user is None) != (password is None):
        given, missing = "new_db_user", "new_db_password"
        if user is None:
            given, missing = missing, given
        raise IncompleteRelocationCredentialsError(
            given=RELOCATION_FLAGS[given], missing=RELOCATION_FLAGS[missing]
        )


def validate_relocation(request: RelocationRequest, *, mode: RestoreMode) -> None:
    """Refuse a relocation that cannot be carried out as given, before any effect.

    In order: :func:`validate_relocation_values`, then
    :class:`RelocationNeedsDatabaseRestoreError` for any ``--new-*`` flag in a
    Mode that restores no database (it has no cutover, so the value would be
    accepted and discarded).
    """
    validate_relocation_values(request)
    given_flags = _given(request)
    if given_flags and not restores_database(mode):
        raise RelocationNeedsDatabaseRestoreError(flags=given_flags, mode=mode.value)


def rewrite_values(request: RelocationRequest) -> tuple[tuple[str, str], ...]:
    """What ``REWRITE_CONFIG`` sets: ``WP_SITEURL`` then ``WP_HOME``, those supplied.

    Never a :data:`~l3io.wp.backup.core.restore.CUTOVER_KEYS` member.
    """
    return tuple(
        (key, value)
        for name, key in _URL_KEYS
        if (value := getattr(request, name)) is not None
    )


def relocation_actions(
    relocation: RestoreRelocation,
) -> tuple[tuple[RestorePhase, str], ...]:
    """The plan's extra action line: the keys a relocated cutover writes.

    The keys are :func:`~l3io.wp.backup.core.restore.planned_cutover_keys`'s,
    the one derivation the cutover and the journal header also use; the line
    is added only when the relocation names a database, host or account.
    ``REWRITE_CONFIG``'s own line already names its keys. Keys only, never a
    value.
    """
    relocated = (
        relocation.load_database is not None
        or relocation.cutover_host is not None
        or relocation.cutover_user is not None
        or relocation.cutover_password
    )
    if not relocated:
        return ()
    keys = planned_cutover_keys(relocation)
    return (
        (
            RestorePhase.LOAD_DUMP,
            f"relocate: the cutover sets {', '.join(keys)} in {CONFIG_FILE_NAME} "
            "to the relocated database",
        ),
    )


def _restore_step(key: str, prior: str | None) -> str:
    if prior is None:
        return f"remove the {key} define (it was not defined)"
    return f"set {key} back to '{prior}'"


def relocation_undo(
    request: RelocationRequest, *, prior: Mapping[str, str | None]
) -> tuple[tuple[RestorePhase, str], ...]:
    """The journal's undo text for ``REWRITE_CONFIG``, when it writes a URL key.

    Names the keys in write order with their **prior values**, from ``prior``
    (the target's ``WP_SITEURL``/``WP_HOME`` as the Site Descriptor read
    them): no Safety Snapshot is certain to hold the prior ``wp-config.php``
    (a database Mode's snapshot does not cover files, and a snapshot may be
    skipped), and the journal records only ``DB_NAME``/``DB_HOST``/``DB_USER``.
    These are site URLs, never a credential. ``LOAD_DUMP``'s undo stays
    ``core/restore.py::undo_for``'s.
    """
    keys = [key for key, _ in rewrite_values(request)]
    if not keys:
        return ()
    steps = "; ".join(_restore_step(key, prior.get(key)) for key in keys)
    return ((RestorePhase.REWRITE_CONFIG, f"in {CONFIG_FILE_NAME}: {steps}"),)
