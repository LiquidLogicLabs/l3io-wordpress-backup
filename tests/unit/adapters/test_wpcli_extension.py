"""E007-S01-001 (FR-11, AD-17) — ``adapters/wpcli.py``'s restore extension.

``resolve`` is driven over every ``RestoreMode`` (from the enum) x every
subset of the ``--new-*`` flags (derived from the restore subparser, which
:data:`~l3io.wp.backup.core.relocation.RELOCATION_FLAGS` must match), and
each result is planned through the built ``build_restore_plan``.
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
from pathlib import Path

import pytest
from structlog.testing import capture_logs

pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters import wpcli
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.wpcli import (
    WpCliRestoreExtension,
    WpCliSearchReplace,
    restore_extension,
)
from l3io.wp.backup.cli.parser import (
    NOT_YET_IMPLEMENTED,
    RESTORE_COMMAND,
    build_parser,
)
from l3io.wp.backup.cli.restore import RelocationFlags, RestoreExtensionFactory
from l3io.wp.backup.core.relocation import RELOCATION_FLAGS, RelocationRequest
from l3io.wp.backup.core.restore import (
    CUTOVER_KEYS,
    RestoreRelocation,
    planned_cutover_keys,
    planned_cutover_targets,
)
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.database import DatabaseLocation
from l3io.wp.backup.domain.errors import (
    IncompleteRelocationCredentialsError,
    InvalidRelocationValueError,
    RelocationNeedsDatabaseRestoreError,
    UsageError,
)
from l3io.wp.backup.domain.restore import RestoreMode, RestorePhase, restores_database
from l3io.wp.backup.domain.search_replace import UrlRelocation
from tests.conftest import CredentialSentinel
from tests.restore_builders import SITE, plan, view

#: The real BL-E007-008 check, before the autouse fixture replaces it.
_REAL_STATE_CHECK = wpcli._check_wp_cli_directory  # noqa: SLF001


@pytest.fixture(autouse=True)
def _no_state_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """``resolve``'s BL-E007-008 check reads the real state directory; these
    cases use a synthetic Site Root. The check has its own tests below."""
    monkeypatch.setattr(wpcli, "_check_wp_cli_directory", lambda site_root: None)


PASSWORD = "e007-relocated-password-0"
VALUES = {
    "new_site_url": "https://staging.e007.example",
    "new_home_url": "https://www.staging.e007.example",
    "new_db_host": "db.e007.example:3310",
    "new_db_name": "e007_staging",
    "new_db_user": "e007_user",
    "new_db_password": PASSWORD,
}


def _restore_parser() -> argparse.ArgumentParser:
    for action in build_parser()._actions:  # noqa: SLF001 - argparse has no public API
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return action.choices[RESTORE_COMMAND]  # type: ignore[no-any-return]
    msg = "no subcommands"
    raise AssertionError(msg)


def _new_flags() -> set[str]:
    """Every ``--new-*`` option the restore subparser registers."""
    return {
        option
        for action in _restore_parser()._actions  # noqa: SLF001
        for option in action.option_strings
        if option.startswith("--new-")
        # A source of --new-db-password's value, not a relocation field.
        and option != "--new-db-password-file"
    }


#: The flag → attribute table, inverted from RELOCATION_FLAGS.
ATTRIBUTE = {flag: name for name, flag in RELOCATION_FLAGS.items()}
FLAGS = sorted(_new_flags())
SUBSETS = [
    combo
    for size in range(len(FLAGS) + 1)
    for combo in itertools.combinations(FLAGS, size)
]


def _flags(**set_: object) -> RelocationFlags:
    return dataclasses.replace(RelocationFlags.none(), **set_)  # type: ignore[arg-type]


def _from(combo: tuple[str, ...]) -> RelocationFlags:
    return _flags(**{ATTRIBUTE[flag]: VALUES[ATTRIBUTE[flag]] for flag in combo})


class _NoFiles:
    """A target with no file at all (E007-S01-002's placement asks ``file_exists``)."""

    def file_exists(self, path: Path) -> bool:
        del path
        return False


NO_FILES = _NoFiles()


def _resolve(flags: RelocationFlags, mode: RestoreMode) -> RestoreRelocation:
    extension = restore_extension(flags, secrets=SecretRegistry())
    return extension.resolve(
        site_root=SITE.site_root,  # type: ignore[arg-type]
        site=SITE,
        view=view(),
        files=NO_FILES,  # type: ignore[arg-type]
        mode=mode,
    )


# --------------------------------------------------------------------------
# Scope: the parser, RELOCATION_FLAGS and RelocationFlags agree
# --------------------------------------------------------------------------


def test_every_new_flag_is_known_and_bound() -> None:
    """A ``--new-*`` flag with no entry here fails: none is accepted and discarded."""
    assert FLAGS, "the restore subparser registers no --new-* flag"
    assert set(FLAGS) == set(RELOCATION_FLAGS.values())
    fields = {f.name for f in dataclasses.fields(RelocationFlags)} - {"wp_config"}
    assert set(RELOCATION_FLAGS) == fields
    unbound = [f for f in FLAGS if f"{RESTORE_COMMAND} {f}" in NOT_YET_IMPLEMENTED]
    assert unbound == []
    # E007-S01-002 bound --wp-config: it is a passthrough, not a --new-* flag.
    assert f"{RESTORE_COMMAND} --wp-config" not in NOT_YET_IMPLEMENTED


def test_relocation_flags_are_a_relocation_request() -> None:
    """mypy checks this assignment (and the factory type below) structurally."""
    request: RelocationRequest = RelocationFlags.none()
    factory: RestoreExtensionFactory = restore_extension
    assert request.new_db_password is None
    assert callable(factory)


# --------------------------------------------------------------------------
# resolve: every Mode x every flag subset
# --------------------------------------------------------------------------


@pytest.mark.parametrize("combo", SUBSETS, ids=lambda c: "+".join(c) or "none")
def test_resolve_over_every_mode(combo: tuple[str, ...]) -> None:
    names = {ATTRIBUTE[f] for f in combo}
    paired = ("new_db_user" in names) == ("new_db_password" in names)
    for mode in RestoreMode:
        flags = _from(combo)
        if not paired:
            with pytest.raises(IncompleteRelocationCredentialsError):
                _resolve(flags, mode)
            continue
        if names and not restores_database(mode):
            with pytest.raises(RelocationNeedsDatabaseRestoreError):
                _resolve(flags, mode)
            continue
        relocation = _resolve(flags, mode)
        _check(relocation, names, mode)


def _check(relocation: RestoreRelocation, names: set[str], mode: RestoreMode) -> None:
    urls = [
        (key, VALUES[name])
        for name, key in (("new_site_url", "WP_SITEURL"), ("new_home_url", "WP_HOME"))
        if name in names
    ]
    assert list(relocation.rewrite_values) == urls
    assert not {k for k, _ in relocation.rewrite_values} & set(CUTOVER_KEYS)
    assert relocation.load_database == (
        VALUES["new_db_name"] if "new_db_name" in names else None
    )
    assert relocation.cutover_host == (
        VALUES["new_db_host"] if "new_db_host" in names else None
    )
    assert relocation.cutover_user == (
        VALUES["new_db_user"] if "new_db_user" in names else None
    )
    assert relocation.cutover_password is ("new_db_password" in names)
    # E007-S01-003: SEARCH_REPLACE carries the new URLs iff one is supplied.
    expected_urls = (
        UrlRelocation(
            site_url=VALUES["new_site_url"] if "new_site_url" in names else None,
            home_url=VALUES["new_home_url"] if "new_home_url" in names else None,
        )
        if urls
        else None
    )
    assert relocation.search_replace == expected_urls
    assert relocation.required_tools == ()
    # Every supplied DB_* value reaches the cutover instead of REWRITE_CONFIG.
    keys = planned_cutover_keys(relocation)
    supplied = {
        "new_db_host": "DB_HOST",
        "new_db_user": "DB_USER",
        "new_db_password": "DB_PASSWORD",
    }
    for name, key in supplied.items():
        assert (key in keys) is (name in names)
    load = relocation.load_database or SITE.db_name
    targets = planned_cutover_targets(load, relocation)
    assert PASSWORD not in targets.values()
    if "new_db_name" in names:
        assert targets["DB_NAME"] == VALUES["new_db_name"]
    # The built planner accepts it, and REWRITE_CONFIG appears iff a URL does.
    planned = plan(mode, relocation=relocation)
    assert (RestorePhase.REWRITE_CONFIG in planned.phases) is bool(urls)
    assert (RestorePhase.SEARCH_REPLACE in planned.phases) is bool(urls)
    if RestorePhase.SEARCH_REPLACE in planned.phases:
        # AD-17: wp bootstraps from the config REWRITE_CONFIG and the cutover
        # just wrote, so SEARCH_REPLACE is last of the three.
        order = list(planned.phases)
        at = order.index(RestorePhase.SEARCH_REPLACE)
        assert at > order.index(RestorePhase.REWRITE_CONFIG)
        assert at > order.index(RestorePhase.LOAD_DUMP)
        undo = dict(relocation.undo)[RestorePhase.SEARCH_REPLACE]
        assert "search-replace" in undo
        assert "--skip-columns=guid" in undo
        for _, value in urls:
            assert value in undo
    for action in planned.actions:
        assert PASSWORD not in action.text


def test_the_load_location_defaults_each_half_to_the_site() -> None:
    by_name = _resolve(_flags(new_db_name="e007_only"), RestoreMode.ALL)
    assert by_name.load_location == DatabaseLocation(
        host=SITE.db_host, port=SITE.db_port, name="e007_only"
    )
    by_host = _resolve(_flags(new_db_host="db.e007.example:3311"), RestoreMode.ALL)
    assert by_host.load_location == DatabaseLocation(
        host="db.e007.example", port=3311, name=SITE.db_name
    )
    assert by_host.load_database is None
    # No flag relocates nothing; the target here has no config, so the
    # Archive's is laid down at the default (E007-S01-002), which is no relocation.
    unrelocated = _resolve(RelocationFlags.none(), RestoreMode.ALL)
    assert unrelocated.lay_down_config is True
    assert dataclasses.replace(
        unrelocated, config_file=None, lay_down_config=False
    ) == (RestoreRelocation.none())


@pytest.mark.parametrize(
    "set_",
    [
        {"new_db_host": ""},
        {"new_db_host": "db.example:notaport"},
        {"new_db_host": "db.example:70000"},
        {"new_db_name": "bad name`"},
        {"new_db_name": ""},
    ],
)
def test_an_invalid_host_or_name_is_usage(set_: dict[str, str]) -> None:
    with pytest.raises(UsageError) as caught:
        _resolve(_flags(**set_), RestoreMode.ALL)
    for value in set_.values():
        if value:
            assert value not in str(caught.value)


# --------------------------------------------------------------------------
# The other three seams
# --------------------------------------------------------------------------


def test_the_relocated_account_is_an_explicit_spec_only_as_a_pair() -> None:
    pair = restore_extension(
        _flags(new_db_user="e007_user", new_db_password=PASSWORD),
        secrets=SecretRegistry(),
    )
    assert pair.wordpress_spec() == ExplicitCredentialSpec("e007_user", PASSWORD)
    none = restore_extension(RelocationFlags.none(), secrets=SecretRegistry())
    assert none.wordpress_spec() is None
    assert none.search_replace() is None
    assert none.required_tools() == ()


def test_the_factory_registers_the_password_and_no_repr_holds_it(
    credential_sentinel: CredentialSentinel,
) -> None:
    password = credential_sentinel.relocated_password
    secrets = SecretRegistry()
    flags = _flags(new_db_user="e007_user", new_db_password=password)
    extension = restore_extension(flags, secrets=secrets)
    assert isinstance(extension, WpCliRestoreExtension)
    assert secrets.redact(password) != password
    credential_sentinel.add_repr("WpCliRestoreExtension", extension)
    credential_sentinel.add_repr("wordpress_spec", extension.wordpress_spec())
    with capture_logs() as logs:
        relocation = extension.resolve(
            site_root=SITE.site_root,  # type: ignore[arg-type]
            site=SITE,
            view=view(),
            files=NO_FILES,  # type: ignore[arg-type]
            mode=RestoreMode.ALL,
        )
    credential_sentinel.add_repr("RestoreRelocation", relocation)
    credential_sentinel.add_repr("records", logs)
    credential_sentinel.assert_clean()


def test_relocation_planned_names_keys_and_flags_never_urls() -> None:
    flags = _from(tuple(FLAGS))
    with capture_logs() as logs:
        _resolve(flags, RestoreMode.ALL)
    (record,) = [e for e in logs if e["event"] == "relocation_planned"]
    assert record["keys"] == ["WP_SITEURL", "WP_HOME"]
    assert record["cutover_keys"] == list(CUTOVER_KEYS)
    assert record["db_location"] == {
        "host": "db.e007.example",
        "port": 3310,
        "name": "e007_staging",
    }
    assert record["site_url_changed"] is True
    assert record["home_url_changed"] is True
    text = repr(record)
    for name in ("new_site_url", "new_home_url", "new_db_password"):
        assert VALUES[name] not in text


def test_no_relocation_logs_nothing() -> None:
    with capture_logs() as logs:
        _resolve(RelocationFlags.none(), RestoreMode.ALL)
    assert [e for e in logs if e["event"] == "relocation_planned"] == []


def test_the_factory_refuses_a_bad_value_before_any_resolve() -> None:
    """L5: URL and pair rules run in the factory, before the root reads anything."""
    with pytest.raises(InvalidRelocationValueError):
        restore_extension(
            _flags(new_site_url="staging.example"), secrets=SecretRegistry()
        )
    with pytest.raises(IncompleteRelocationCredentialsError):
        restore_extension(_flags(new_db_user="e007_user"), secrets=SecretRegistry())
    # The Mode rule needs the Mode: the factory accepts what only resolve refuses.
    restore_extension(_flags(new_db_name="e007_db"), secrets=SecretRegistry())


def test_a_host_only_relocation_logs_no_database_name() -> None:
    """The Dump goes to a staging database on the new host, not the Site's name."""
    with capture_logs() as logs:
        _resolve(_flags(new_db_host="db.e007.example:3311"), RestoreMode.ALL)
    (record,) = [e for e in logs if e["event"] == "relocation_planned"]
    assert record["db_location"] == {
        "host": "db.e007.example",
        "port": 3311,
        "name": None,
    }
    assert record["load_target"] == "a staging database"


# --------------------------------------------------------------------------
# E007-S01-003: search_replace() binds WpCliSearchReplace
# --------------------------------------------------------------------------


@pytest.mark.parametrize("combo", SUBSETS, ids=lambda c: "+".join(c) or "none")
@pytest.mark.parametrize("found", ["/opt/bin/wp", None])
def test_search_replace_is_bound_iff_a_url_and_wp(
    combo: tuple[str, ...], found: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = {ATTRIBUTE[f] for f in combo}
    if ("new_db_user" in names) != ("new_db_password" in names):
        return
    lookups: list[str] = []

    def which(name: str) -> str | None:
        lookups.append(name)
        return found

    monkeypatch.setattr(wpcli, "locate_tool", which)
    extension = restore_extension(_from(combo), secrets=SecretRegistry())
    url = bool(names & {"new_site_url", "new_home_url"})
    assert lookups == (["wp"] if url else [])
    bound = extension.search_replace()
    if url and found is not None:
        assert isinstance(bound, WpCliSearchReplace)
        assert repr(bound) == f"WpCliSearchReplace({found!r})"
        assert extension.search_replace() is bound, "one instance per extension"
    else:
        assert bound is None


# --------------------------------------------------------------------------
# E008-S01-001: BL-E007-008 (checked before any effect) and BL-E007-009 (cap)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", wpcli.WP_CLI_CONFIG_FILES)
def test_a_wp_cli_config_above_the_state_directory_refuses_before_any_effect(
    tmp_path: Path, name: str
) -> None:
    from l3io.wp.backup.adapters.journal import journal_directory

    site_root = tmp_path / "www"
    site_root.mkdir()
    state = journal_directory(site_root)
    state.parent.mkdir(parents=True, exist_ok=True)
    planted = state.parent / name
    planted.write_text("require: [x.php]\n", encoding="utf-8")
    with pytest.raises(wpcli.WpCliConfigFoundError) as caught:
        _REAL_STATE_CHECK(site_root)
    assert caught.value.path == planted
    assert int(caught.value.exit_code) == 3


def test_the_state_directory_is_prepared_or_the_run_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_root = tmp_path / "www"
    site_root.mkdir()
    _REAL_STATE_CHECK(site_root)  # prepared, nothing above it
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))
    with pytest.raises(wpcli.WpCliFailedError) as caught:
        _REAL_STATE_CHECK(site_root)
    assert int(caught.value.exit_code) == 1


def test_resolve_runs_the_state_check_only_for_a_url_relocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[Path] = []
    monkeypatch.setattr(wpcli, "_check_wp_cli_directory", checked.append)
    for flags, expected in (
        (_flags(new_site_url="https://new.example"), 1),
        (_flags(new_db_name="e008_db"), 0),
    ):
        checked.clear()
        _resolve(flags, RestoreMode.ALL_CLEAN)
        assert len(checked) == expected, flags


def test_a_wp_child_printing_past_the_cap_is_killed_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-E007-009: the output is bounded; past it the call fails."""
    monkeypatch.setattr(wpcli, "CHILD_OUTPUT_LIMIT", 4096)
    script = tmp_path / "wp"
    script.write_text("#!/bin/sh\nyes WP-CLI 2.12.0\n", encoding="utf-8")
    script.chmod(0o755)
    with pytest.raises(wpcli.ChildOutputLimitError, match="4096"):
        wpcli.run_child([str(script)], cwd=tmp_path, env={"PATH": "/usr/bin:/bin"})
    config = tmp_path / "config.yml"
    config.write_text("", encoding="utf-8")
    session = wpcli._Session(  # noqa: SLF001
        wp=str(script),
        site_root=tmp_path,
        cwd=tmp_path,
        config=config,
        secrets=SecretRegistry(),
        run=wpcli.run_child,
        as_root=False,
    )
    with pytest.raises(wpcli.WpCliFailedError) as caught:
        session.call("version", [str(script)])
    assert "4096" in str(caught.value)
