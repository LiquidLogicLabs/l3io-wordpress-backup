"""E007-S01-001 (FR-11, AD-17) — ``core/relocation.py``, pure value derivation.

Every combination of the ``--new-*`` attributes is generated from
:data:`~l3io.wp.backup.core.relocation.RELOCATION_FLAGS` (whose agreement with
the parser is asserted in ``tests/unit/adapters/test_wpcli_extension.py``),
and every Restore Mode from the enum: nothing is typed out twice.
"""

from __future__ import annotations

import dataclasses
import itertools
from pathlib import PurePath

import pytest

from l3io.wp.backup.adapters.preflight import KNOWN_TOOLS
from l3io.wp.backup.core.relocation import (
    RELOCATION_FLAGS,
    WP_CLI_MINIMUM,
    WP_CLI_REQUIREMENT,
    RelocationRequest,
    nested,
    pairs_merged,
    relocation_actions,
    relocation_undo,
    replacement_pairs,
    required_tools,
    requires_search_replace,
    rewrite_values,
    safe_order,
    url_relocation,
    validate_relocation,
    validate_relocation_values,
)
from l3io.wp.backup.core.restore import (
    CUTOVER_KEYS,
    RestoreRelocation,
    planned_cutover_keys,
)
from l3io.wp.backup.domain.errors import (
    IncompleteRelocationCredentialsError,
    InterferingReplacementsError,
    InvalidRelocationValueError,
    RelocationNeedsDatabaseRestoreError,
    UsageError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase, restores_database
from l3io.wp.backup.domain.search_replace import Replacement, StoredUrls, UrlRelocation

#: One distinct, valid value per attribute, so a leak names its source.
VALUES = {
    "new_site_url": "https://site.e007.example",
    "new_home_url": "https://home.e007.example",
    "new_db_host": "db.e007.example:3307",
    "new_db_name": "e007_db",
    "new_db_user": "e007_user",
    "new_db_password": "e007-password-sentinel",
}
DB_MODES = [m for m in RestoreMode if restores_database(m)]
FILE_MODES = [m for m in RestoreMode if not restores_database(m)]


@dataclasses.dataclass(frozen=True)
class _Request:
    new_site_url: str | None = None
    new_home_url: str | None = None
    new_db_host: str | None = None
    new_db_name: str | None = None
    new_db_user: str | None = None
    new_db_password: str | None = None
    wp_config: PurePath | None = None


def _request(**set_: str | None) -> RelocationRequest:
    return _Request(**set_)  # type: ignore[arg-type]


def _subsets() -> list[tuple[str, ...]]:
    names = list(RELOCATION_FLAGS)
    return [
        combo
        for size in range(len(names) + 1)
        for combo in itertools.combinations(names, size)
    ]


SUBSETS = _subsets()


def _paired(combo: tuple[str, ...]) -> bool:
    return ("new_db_user" in combo) == ("new_db_password" in combo)


def test_the_scope_is_every_attribute_and_every_subset() -> None:
    assert set(VALUES) == set(RELOCATION_FLAGS)
    assert len(SUBSETS) == 2 ** len(RELOCATION_FLAGS)
    assert DB_MODES
    assert FILE_MODES


@pytest.mark.parametrize("combo", SUBSETS, ids="+".join)
def test_rewrite_values_hold_only_the_supplied_urls_in_order(
    combo: tuple[str, ...],
) -> None:
    request = _request(**{name: VALUES[name] for name in combo})
    values = rewrite_values(request)
    assert not {key for key, _ in values} & set(CUTOVER_KEYS)
    expected = [
        (key, VALUES[name])
        for name, key in (("new_site_url", "WP_SITEURL"), ("new_home_url", "WP_HOME"))
        if name in combo
    ]
    assert list(values) == expected


@pytest.mark.parametrize("combo", SUBSETS, ids="+".join)
def test_validation_in_a_database_mode_refuses_only_half_a_pair(
    combo: tuple[str, ...],
) -> None:
    request = _request(**{name: VALUES[name] for name in combo})
    for mode in DB_MODES:
        if _paired(combo):
            validate_relocation(request, mode=mode)
        else:
            with pytest.raises(IncompleteRelocationCredentialsError):
                validate_relocation(request, mode=mode)


@pytest.mark.parametrize("mode", list(RestoreMode), ids=lambda m: m.value)
@pytest.mark.parametrize("name", list(RELOCATION_FLAGS))
def test_the_mode_rule_refuses_every_flag_where_no_database_is_restored(
    mode: RestoreMode, name: str
) -> None:
    set_ = {name: VALUES[name]}
    if name in {"new_db_user", "new_db_password"}:
        set_ = {"new_db_user": VALUES["new_db_user"], **set_}
        set_["new_db_password"] = VALUES["new_db_password"]
    request = _request(**set_)
    if restores_database(mode):
        validate_relocation(request, mode=mode)
        return
    with pytest.raises(RelocationNeedsDatabaseRestoreError) as caught:
        validate_relocation(request, mode=mode)
    assert RELOCATION_FLAGS[name] in caught.value.flags
    assert mode.value in str(caught.value)
    assert VALUES[name] not in str(caught.value)


def test_no_flag_is_valid_in_every_mode() -> None:
    for mode in RestoreMode:
        validate_relocation(_request(), mode=mode)
        assert rewrite_values(_request()) == ()
        assert relocation_actions(RestoreRelocation.none()) == ()
        assert relocation_undo(_request(), prior=PRIOR) == ()


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        ("-https://x.example", "start with '-'"),
        ("--help", "start with '-'"),
        ("ftp://x.example", "http:// or https://"),
        ("x.example", "http:// or https://"),
        ("", "http:// or https://"),
        ("HTTPS://x.example", "http:// or https://"),
        ("https://x .example", "whitespace"),
        ("https://x.example\t", "whitespace"),
        ("https://x.example\u0085", "whitespace"),
        ("https://x.example\u2028", "whitespace"),
        ("https://x.example\x00", "control character"),
        ("https://x.example\x7f", "control character"),
        ("https://x​.example", "control character"),
        ("https://x.example/'", "quote"),
        ('https://x.example/"', "quote"),
        ("https://x.example\\a", "backslash"),
        ("https://", "host"),
        ("https:///path", "host"),
        ("https://?p=1", "host"),
        ("https://#x", "host"),
        ("https://:8443", "host"),
        ("https://@", "host"),
        ("https://user@", "host"),
        ("https://x.example/", "end with '/'"),
        ("https://x.example/sub/", "end with '/'"),
    ],
)
@pytest.mark.parametrize("name", ["new_site_url", "new_home_url"])
def test_each_url_rule(name: str, value: str, rule: str) -> None:
    with pytest.raises(InvalidRelocationValueError) as caught:
        validate_relocation(_request(**{name: value}), mode=RestoreMode.ALL)
    assert caught.value.flag == RELOCATION_FLAGS[name]
    assert rule in caught.value.rule
    assert caught.value.exit_code is ExitCode.USAGE


@pytest.mark.parametrize(
    "value",
    [
        "http://x.example",
        "https://x.example",
        "https://x.example:8443",
        "https://x.example/blog",
        "https://bücher.example/straße",
        "https://x.example/?p=1&q=$2",
    ],
)
def test_a_valid_url_passes(value: str) -> None:
    validate_relocation(
        _request(new_site_url=value, new_home_url=value), mode=RestoreMode.ALL
    )


@pytest.mark.parametrize("name", ["new_db_user", "new_db_password"])
def test_an_empty_credential_half_is_invalid(name: str) -> None:
    set_ = {"new_db_user": "u", "new_db_password": "p" * 12, name: ""}
    with pytest.raises(InvalidRelocationValueError) as caught:
        validate_relocation(_request(**set_), mode=RestoreMode.ALL)
    assert caught.value.flag == RELOCATION_FLAGS[name]


@pytest.mark.parametrize(
    ("given", "missing"),
    [("new_db_user", "new_db_password"), ("new_db_password", "new_db_user")],
)
def test_half_a_pair_names_both_halves(given: str, missing: str) -> None:
    with pytest.raises(IncompleteRelocationCredentialsError) as caught:
        validate_relocation(_request(**{given: VALUES[given]}), mode=RestoreMode.ALL)
    assert caught.value.given == RELOCATION_FLAGS[given]
    assert caught.value.missing == RELOCATION_FLAGS[missing]
    assert VALUES[given] not in str(caught.value)


def test_the_rules_run_in_their_stated_order() -> None:
    """URL rules, then the pair rule, then the Mode rule."""
    bad_url_half_pair = _request(new_site_url="x", new_db_user="u")
    with pytest.raises(InvalidRelocationValueError):
        validate_relocation(bad_url_half_pair, mode=RestoreMode.FILES)
    with pytest.raises(IncompleteRelocationCredentialsError):
        validate_relocation(_request(new_db_user="u"), mode=RestoreMode.FILES)


def test_every_error_is_a_usage_error() -> None:
    for error in (
        InvalidRelocationValueError,
        IncompleteRelocationCredentialsError,
        RelocationNeedsDatabaseRestoreError,
    ):
        assert issubclass(error, UsageError)
        assert error.exit_code is ExitCode.USAGE


PRIOR = {"WP_SITEURL": "https://old.e007.example", "WP_HOME": None}


def _relocation(combo: tuple[str, ...]) -> RestoreRelocation:
    """``combo`` as ``WpCliRestoreExtension.resolve`` carries it (no location)."""
    request = _request(**{name: VALUES[name] for name in combo})
    return dataclasses.replace(
        RestoreRelocation.none(),
        rewrite_values=rewrite_values(request),
        load_database=request.new_db_name,
        cutover_host=request.new_db_host,
        cutover_user=request.new_db_user,
        cutover_password=request.new_db_password is not None,
    )


@pytest.mark.parametrize("combo", SUBSETS, ids="+".join)
def test_actions_name_the_cutovers_keys_never_values(combo: tuple[str, ...]) -> None:
    relocation = _relocation(combo)
    actions = relocation_actions(relocation)
    for _, text in actions:
        for value in VALUES.values():
            assert value not in text
    database = set(combo) - {"new_site_url", "new_home_url"}
    assert [p for p, _ in actions] == ([RestorePhase.LOAD_DUMP] if database else [])
    if database:
        ((_, text),) = actions
        # The one derivation the cutover and the journal header use.
        assert ", ".join(planned_cutover_keys(relocation)) in text


@pytest.mark.parametrize("combo", SUBSETS, ids="+".join)
def test_undo_names_each_url_key_with_its_prior_value(combo: tuple[str, ...]) -> None:
    request = _request(**{name: VALUES[name] for name in combo})
    undo = relocation_undo(request, prior=PRIOR)
    urls = [k for k, _ in rewrite_values(request)]
    assert [p for p, _ in undo] == ([RestorePhase.REWRITE_CONFIG] if urls else [])
    for _, text in undo:
        for value in VALUES.values():
            assert value not in text  # the new values, and never the password
        if "WP_SITEURL" in urls:
            assert "set WP_SITEURL back to 'https://old.e007.example'" in text
        if "WP_HOME" in urls:
            assert "remove the WP_HOME define (it was not defined)" in text


def test_the_mode_independent_rules_need_no_mode() -> None:
    """``validate_relocation_values``: URL and pair rules, and nothing about Modes."""
    with pytest.raises(InvalidRelocationValueError):
        validate_relocation_values(_request(new_site_url="x.example"))
    with pytest.raises(IncompleteRelocationCredentialsError):
        validate_relocation_values(_request(new_db_user="u"))
    validate_relocation_values(_request(new_db_name="e007_db"))


# --------------------------------------------------------------------------
# E007-S01-003: SEARCH_REPLACE's pairs
# --------------------------------------------------------------------------

#: The URL attributes of ``RelocationRequest``, from ``dataclasses.fields``.
_URL_NAMES = ("new_site_url", "new_home_url")


@pytest.mark.parametrize("combo", _subsets(), ids=lambda c: "+".join(c) or "none")
def test_requires_search_replace_iff_a_url_is_supplied(combo: tuple[str, ...]) -> None:
    names = {f.name for f in dataclasses.fields(_Request)} - {"wp_config"}
    assert set(combo) <= names
    request = _request(**{name: VALUES[name] for name in combo})
    wanted = bool(set(combo) & set(_URL_NAMES))
    assert requires_search_replace(request) is wanted
    urls = url_relocation(request)
    if not wanted:
        assert urls is None
        return
    assert urls == UrlRelocation(
        site_url=VALUES["new_site_url"] if "new_site_url" in combo else None,
        home_url=VALUES["new_home_url"] if "new_home_url" in combo else None,
    )


# --------------------------------------------------------------------------
# E007-S01-004: WP-CLI is required iff a URL is relocated
# --------------------------------------------------------------------------


@pytest.mark.parametrize("with_config", [False, True], ids=["", "wp_config"])
@pytest.mark.parametrize("combo", _subsets(), ids=lambda c: "+".join(c) or "none")
def test_wp_cli_is_required_iff_a_url_is_supplied(
    combo: tuple[str, ...], *, with_config: bool
) -> None:
    """Every subset of every request field (``dataclasses.fields``), config too."""
    names = {f.name for f in dataclasses.fields(_Request)}
    assert names == {*RELOCATION_FLAGS, "wp_config"}
    assert set(combo) <= names
    given: dict[str, object] = {name: VALUES[name] for name in combo}
    if with_config:
        given["wp_config"] = PurePath("/srv/wp-config.php")
    request = _request(**given)  # type: ignore[arg-type]
    wanted = bool(set(combo) & set(_URL_NAMES))
    assert required_tools(request) == ((WP_CLI_REQUIREMENT,) if wanted else ())


def test_the_wp_cli_requirement_says_why_and_where_to_get_it() -> None:
    assert WP_CLI_REQUIREMENT.name == "wp"
    assert WP_CLI_REQUIREMENT.minimum_version == WP_CLI_MINIMUM
    assert all(isinstance(part, int) for part in WP_CLI_MINIMUM)
    for text in ("SEARCH_REPLACE", "FR-12", "--new-site-url", "--new-site-home-url"):
        assert text in WP_CLI_REQUIREMENT.needed_for
    assert WP_CLI_REQUIREMENT.install_hint == "https://wp-cli.org/#installing"
    assert WP_CLI_REQUIREMENT.install_hint == KNOWN_TOOLS["wp"].install_hint


OLD_SITE = "https://old.e007.example/wp"
OLD_HOME = "https://old.e007.example"
NEW_SITE = "https://new.e007.example/wp"
NEW_HOME = "https://new.e007.example"


def test_pairs_are_longest_old_first() -> None:
    stored = StoredUrls(siteurl=OLD_HOME, home=OLD_SITE)
    pairs = replacement_pairs(
        stored, UrlRelocation(site_url=NEW_HOME, home_url=NEW_SITE)
    )
    assert pairs == (Replacement(OLD_SITE, NEW_SITE), Replacement(OLD_HOME, NEW_HOME))
    stored = StoredUrls(siteurl=OLD_SITE, home=OLD_HOME)
    pairs = replacement_pairs(
        stored, UrlRelocation(site_url=NEW_SITE, home_url=NEW_HOME)
    )
    assert [len(p.old) for p in pairs] == sorted(
        (len(p.old) for p in pairs), reverse=True
    )
    assert pairs[0] == Replacement(OLD_SITE, NEW_SITE)


def test_identity_pairs_are_dropped() -> None:
    stored = StoredUrls(siteurl=OLD_SITE, home=OLD_HOME)
    urls = UrlRelocation(site_url=OLD_SITE, home_url=NEW_HOME)
    assert replacement_pairs(stored, urls) == (Replacement(OLD_HOME, NEW_HOME),)
    same = UrlRelocation(site_url=OLD_SITE, home_url=OLD_HOME)
    assert replacement_pairs(stored, same) == ()


@pytest.mark.parametrize(
    ("site_url", "home_url", "new", "merged"),
    [
        (NEW_SITE, NEW_HOME, NEW_HOME, True),
        (NEW_SITE, None, NEW_SITE, False),
        (None, NEW_HOME, NEW_HOME, False),
    ],
)
def test_equal_stored_values_make_one_pair(
    site_url: str | None, home_url: str | None, new: str, merged: bool
) -> None:
    stored = StoredUrls(siteurl=OLD_HOME, home=OLD_HOME)
    urls = UrlRelocation(site_url=site_url, home_url=home_url)
    assert replacement_pairs(stored, urls) == (Replacement(OLD_HOME, new),)
    assert pairs_merged(stored, urls) is merged


@pytest.mark.parametrize(
    ("site_url", "home_url", "expected"),
    [
        (NEW_SITE, None, (Replacement(OLD_SITE, NEW_SITE),)),
        (None, NEW_HOME, (Replacement(OLD_HOME, NEW_HOME),)),
    ],
)
def test_only_the_supplied_url_is_paired(
    site_url: str | None, home_url: str | None, expected: tuple[Replacement, ...]
) -> None:
    stored = StoredUrls(siteurl=OLD_SITE, home=OLD_HOME)
    urls = UrlRelocation(site_url=site_url, home_url=home_url)
    assert replacement_pairs(stored, urls) == expected
    assert pairs_merged(stored, urls) is False


@pytest.mark.parametrize(
    ("old", "new", "is_nested"),
    [
        ("https://a.example", "https://a.example/blog", True),
        ("https://a.example", "https://a.example", True),
        ("https://a.example/blog", "https://a.example", False),
        ("https://a.example", "https://b.example", False),
    ],
)
def test_nested(old: str, new: str, is_nested: bool) -> None:
    assert nested(Replacement(old, new)) is is_nested


def _apply(text: str, pairs: tuple[Replacement, ...]) -> str:
    """Sequential passes, as WP-CLI runs them: each pair over the whole text."""
    for pair in pairs:
        text = text.replace(pair.old, pair.new)
    return text


@pytest.mark.parametrize(
    ("stored", "urls"),
    [
        (
            ("https://example.com/wp", "https://example.com"),
            ("https://example.com.au/wp", "https://example.com.au"),
        ),
        (
            ("https://a.test/wp", "https://a.test"),
            ("https://a.test/blog", "https://b.test"),
        ),
    ],
    ids=["cctld", "home-moves-host"],
)
def test_interfering_pairs_have_no_safe_order(
    stored: tuple[str, str], urls: tuple[str, str]
) -> None:
    pairs = replacement_pairs(StoredUrls(*stored), UrlRelocation(*urls))
    # Without the check the passes corrupt: shown, then refused.
    content = f"{stored[0]}/x {stored[1]}/y"
    assert _apply(content, pairs) != f"{urls[0]}/x {urls[1]}/y"
    with pytest.raises(InterferingReplacementsError) as caught:
        safe_order(pairs)
    assert caught.value.exit_code is ExitCode.REFUSED


def test_a_safe_order_is_kept_or_found() -> None:
    a = Replacement("https://a.test/wp", "https://b.test/wp")
    b = Replacement("https://b.test", "https://c.test")
    assert safe_order((a, b)) == (b, a)
    assert _apply("https://a.test/wp https://b.test", (b, a)) == (
        "https://b.test/wp https://c.test"
    )
    longest = replacement_pairs(
        StoredUrls(OLD_SITE, OLD_HOME),
        UrlRelocation("https://n.test/wp", "https://n.test"),
    )
    assert safe_order(longest) == longest
    nested_pair = (Replacement(OLD_HOME, OLD_HOME + "/blog"),)
    assert safe_order(nested_pair) == nested_pair
    assert safe_order(()) == ()
