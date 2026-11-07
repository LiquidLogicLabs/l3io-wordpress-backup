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
  their prior values, which nothing else records);
* :func:`url_relocation`, :func:`requires_search_replace`,
  :func:`replacement_pairs`, :func:`safe_order`, :func:`pairs_merged` and
  :func:`nested` — what
  ``SEARCH_REPLACE`` rewrites (E007-S01-003): the pairs WP-CLI's
  ``search-replace`` is run with, derived from the stored ``siteurl``/``home``
  and the new URLs. No serialized value is read or written here.

The new database password never enters this module beyond the fact that it
was supplied: :func:`validate_relocation` reads whether it is ``None`` or
empty, and nothing here stores, renders or returns it.
"""

from __future__ import annotations

import itertools
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
    InterferingReplacementsError,
    InvalidRelocationValueError,
    RelocationNeedsDatabaseRestoreError,
)
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase, restores_database
from l3io.wp.backup.domain.search_replace import Replacement, StoredUrls, UrlRelocation

__all__ = [
    "RELOCATION_FLAGS",
    "URL_SCHEMES",
    "RelocationRequest",
    "nested",
    "pairs_merged",
    "relocation_actions",
    "relocation_undo",
    "replacement_pairs",
    "requires_search_replace",
    "rewrite_values",
    "safe_order",
    "url_relocation",
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


# --------------------------------------------------------------------------
# SEARCH_REPLACE (E007-S01-003): the pairs, never the serialized values
# --------------------------------------------------------------------------


def url_relocation(request: RelocationRequest) -> UrlRelocation | None:
    """The URLs ``SEARCH_REPLACE`` moves the content to, or ``None`` for neither."""
    if not requires_search_replace(request):
        return None
    return UrlRelocation(site_url=request.new_site_url, home_url=request.new_home_url)


def requires_search_replace(request: RelocationRequest) -> bool:
    """True iff ``--new-site-url`` or ``--new-site-home-url`` is supplied."""
    return request.new_site_url is not None or request.new_home_url is not None


def pairs_merged(stored: StoredUrls, urls: UrlRelocation) -> bool:
    """True when both URLs are relocated but the stored two are one value.

    The content then holds one old URL, so one pair (``home_url``'s) is made
    for both requests (:func:`replacement_pairs`), and the report says so.
    """
    both = urls.site_url is not None and urls.home_url is not None
    return both and stored.siteurl == stored.home


def replacement_pairs(
    stored: StoredUrls, urls: UrlRelocation
) -> tuple[Replacement, ...]:
    """The search-replace pairs, longest ``old`` first, identities dropped.

    One pair per supplied URL: ``stored.siteurl`` → ``urls.site_url`` and
    ``stored.home`` → ``urls.home_url``. When the stored values are equal a
    single pair is made, to ``home_url`` if supplied, else ``site_url``. The
    order is by descending ``len(old)``, so ``https://x.test/wp`` is rewritten
    before ``https://x.test`` would rewrite its prefix.
    """
    if stored.siteurl == stored.home:
        new = urls.home_url if urls.home_url is not None else urls.site_url
        candidates = [] if new is None else [Replacement(stored.home, new)]
    else:
        candidates = [
            Replacement(old, new)
            for old, new in (
                (stored.siteurl, urls.site_url),
                (stored.home, urls.home_url),
            )
            if new is not None
        ]
    kept = [pair for pair in candidates if pair.old != pair.new]
    return tuple(sorted(kept, key=lambda pair: len(pair.old), reverse=True))


def nested(pair: Replacement) -> bool:
    """True when ``pair.new`` contains ``pair.old``: no zero-remaining check then.

    After the replacement the new text legitimately holds the old, so a
    re-count of ``old`` cannot be zero.
    """
    return pair.old in pair.new


def _safe(order: tuple[Replacement, ...]) -> bool:
    """No pair in ``order`` touches what an earlier one wrote or left to a later one.

    For an earlier pair ``e`` and a later pair ``l``: ``l.old`` must not occur
    in ``e.new`` (``l`` would rewrite ``e``'s output), and ``e.old`` must not
    occur in ``l.old`` (``e`` would consume ``l``'s matches first).
    """
    return not any(
        late.old in early.new or early.old in late.old
        for i, early in enumerate(order)
        for late in order[i + 1 :]
    )


def safe_order(pairs: tuple[Replacement, ...]) -> tuple[Replacement, ...]:
    """``pairs`` in an order where no pass rewrites another's output, or a refusal.

    The pairs run as separate ``wp search-replace`` passes, so a later pair
    whose old URL occurs inside an earlier pair's new URL would rewrite it
    (``example.com/wp`` and ``example.com`` moved to ``example.com.au``
    would end as ``example.com.au.au``). The given order (longest ``old``
    first) is kept when it is safe; otherwise the first safe permutation is
    used; when none is, :class:`InterferingReplacementsError` is raised, before
    any replacement. A pair's own nesting (``new`` containing ``old``) is not
    interference: one pass reads each value once.
    """
    for order in itertools.permutations(pairs):
        if _safe(order):
            return order
    raise InterferingReplacementsError(tuple((pair.old, pair.new) for pair in pairs))
