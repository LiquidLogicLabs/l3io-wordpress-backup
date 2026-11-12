"""Owner decision 60, ADR-0028 — every setting has a flag, a variable and a key.

The one source is ``cli/settings.Settings``: a field's name is its argparse
``dest`` and its config-file key, its variable is ``L3IO_WP_BACKUP_`` + the
upper-cased name, its flag ``--`` + the name with dashes. Derived from it and
from the parser (``build_parser()``, every subcommand walked):

- **each setting has all three spellings**: a parser option with that
  ``dest`` and that flag; the variable, read (set it, load, see it); the key,
  read (write it to a TOML file, load, see it);
- **scope, from the parser**: every option the parser has is a setting or is
  in ``NOT_SETTINGS`` with a reason, and no ``NOT_SETTINGS`` entry is stale;
- **scope, from the source**: every ``L3IO_WP_BACKUP_*`` name written as a
  string anywhere under ``src/`` is a setting's variable or
  ``L3IO_WP_BACKUP_CONFIG``, so a variable read somewhere has a flag and a key
  too.

Each check runs over a planted violation that attacks its scope: an option
added to a subcommand, a field with no flag, a variable named in a new module.

**Not covered:** a variable name built at run time (``PREFIX + name``) is not
seen by the source scan; the settings module itself builds names only that way.
"""

from __future__ import annotations

import argparse
import ast
import re
from collections.abc import Mapping
from pathlib import Path
from typing import get_args

import pytest
from pydantic import SecretStr

from l3io.wp.backup.cli import settings
from l3io.wp.backup.cli.parser import CliParser, build_parser
from l3io.wp.backup.cli.settings import (
    CONFIG_VAR,
    NOT_SETTINGS,
    Settings,
    config_key,
    env_name,
    flag_name,
)

SRC = Path(__file__).resolve().parents[2] / "src"
#: A variable name; one ending in ``*`` is prose about a family (``DB_*``).
_VARIABLE = re.compile(r"L3IO_WP_BACKUP_[A-Z0-9_]+\*?")


def _options(parser: argparse.ArgumentParser) -> dict[str, set[str]]:
    """Every option of ``parser`` and its subcommands: ``dest`` to its flags."""
    found: dict[str, set[str]] = {}
    parsers = [parser]
    for action in parser._actions:  # noqa: SLF001 - argparse has no public API
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            parsers += list(action.choices.values())
    for each in parsers:
        for action in each._actions:  # noqa: SLF001
            if action.option_strings:
                found.setdefault(action.dest, set()).update(action.option_strings)
    return found


def _sample(annotation: object) -> tuple[str, str, object]:
    """A value for a field: as a variable, as a TOML literal, and as loaded."""
    kinds = get_args(annotation)
    if bool in kinds:
        return "true", "true", True
    if list[str] in kinds:
        return '["x-1"]', '["x-1"]', ["x-1"]
    if SecretStr in kinds:
        return "secret-sample-7f", '"secret-sample-7f"', "secret-sample-7f"
    if int in kinds:
        return "7", "7", 7
    return "sample-7f", '"sample-7f"', "sample-7f"


def _loaded(value: object) -> object:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def missing_spellings(
    model: type[Settings],
    parser: argparse.ArgumentParser,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """``<setting>: <spelling>`` for every spelling a setting lacks."""
    options = _options(parser)
    missing: list[str] = []
    for name, field in model.model_fields.items():
        if flag_name(name) not in options.get(name, set()):
            missing.append(f"{name}: no flag {flag_name(name)} with dest {name}")
        as_env, as_toml, expected = _sample(field.annotation)
        with monkeypatch.context() as patch:
            patch.setenv(env_name(name), as_env)
            if _loaded(getattr(model(), name)) != expected:
                missing.append(f"{name}: {env_name(name)} not read")
        config = tmp_path / f"{name}.toml"
        config.write_text(f"{config_key(name)} = {as_toml}\n", encoding="utf-8")

        class _File(model):  # type: ignore[valid-type,misc]
            model_config = settings.SettingsConfigDict(toml_file=[config])

        if _loaded(getattr(_File(), name)) != expected:
            missing.append(f"{name}: key {config_key(name)!r} not read")
    return missing


def unclassified_options(
    parser: argparse.ArgumentParser, excluded: Mapping[str, str] = NOT_SETTINGS
) -> list[str]:
    """Options that are neither a setting nor in ``excluded``, and stale entries."""
    dests = set(_options(parser))
    unknown = sorted(dests - set(Settings.model_fields) - set(excluded))
    stale = sorted(set(excluded) - dests)
    return [f"option {d} is not a setting" for d in unknown] + [
        f"NOT_SETTINGS entry {d} names no option" for d in stale
    ]


def unknown_variables(root: Path) -> list[str]:
    """``L3IO_WP_BACKUP_*`` names written under ``root`` that no setting has."""
    known = {env_name(name) for name in Settings.model_fields} | {CONFIG_VAR}
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            for name in _VARIABLE.findall(node.value):
                if not name.endswith("*") and name not in known:
                    found.append(f"{path.relative_to(root)}: {name}")
    return found


# --------------------------------------------------------------------------
# The rule, over the real model, parser and source
# --------------------------------------------------------------------------


def test_every_setting_has_a_flag_a_variable_and_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert missing_spellings(Settings, build_parser(), tmp_path, monkeypatch) == []


def test_every_option_is_a_setting_or_excluded_with_a_reason() -> None:
    assert unclassified_options(build_parser()) == []
    assert all(reason.strip() for reason in NOT_SETTINGS.values())


def test_every_variable_the_source_names_is_a_settings() -> None:
    assert unknown_variables(SRC) == []


def test_the_scope_reaches_what_it_should() -> None:
    """The derived sets hold what the owner's decision names."""
    names = set(Settings.model_fields)
    assert {
        "wp_dir",
        "archive_dir",
        "require_encrypted",
        "db_tls",
        "db_tls_ca",
        "passphrase",
        "mode",
        "site_name",  # owner decision 82
        "keep",  # owner decision 83
        "keep_daily",  # owner decision 84
        "keep_weekly",
        "keep_monthly",
        "keep_yearly",
        "archive_subdir",  # owner decision 86
    } <= names
    variables = {v.split(": ")[1] for v in unknown_variables(SRC)} | {
        env_name(n) for n in names
    }
    for existing in (
        "L3IO_WP_BACKUP_PASSPHRASE",
        "L3IO_WP_BACKUP_DB_USER",
        "L3IO_WP_BACKUP_DB_PASSWORD",
        "L3IO_WP_BACKUP_ADMIN_USER",
        "L3IO_WP_BACKUP_ADMIN_PASSWORD",
        "L3IO_WP_BACKUP_NEW_DB_PASSWORD",
    ):
        assert existing in variables
    assert {
        "passphrase",
        "db_password",
        "admin_password",
        "new_db_password",
    } == settings.SECRET_SETTINGS


# --------------------------------------------------------------------------
# Planted violations: each attacks a check's scope
# --------------------------------------------------------------------------


def _planted_parser() -> CliParser:
    parser = build_parser()
    parser.commands["restore"].add_argument("--later-option", default=None)
    return parser


def test_an_option_added_to_a_subcommand_is_caught() -> None:
    assert unclassified_options(_planted_parser()) == [
        "option later_option is not a setting"
    ]


def test_a_field_with_no_flag_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Planted(Settings):
        unflagged_setting: str | None = None

    assert missing_spellings(_Planted, build_parser(), tmp_path, monkeypatch) == [
        "unflagged_setting: no flag --unflagged-setting with dest unflagged_setting"
    ]


def test_a_flag_whose_dest_differs_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parser = build_parser()
    parser.commands["backup"].add_argument("--odd-name", dest="other_dest")

    class _Planted(Settings):
        odd_name: str | None = None

    assert missing_spellings(_Planted, parser, tmp_path, monkeypatch) == [
        "odd_name: no flag --odd-name with dest odd_name"
    ]


def test_a_variable_named_in_a_new_module_is_caught(tmp_path: Path) -> None:
    deep = tmp_path / "pkg" / "deeper"
    deep.mkdir(parents=True)
    (deep / "later.py").write_text(
        'TOKEN = "L3IO_WP_BACKUP_LATER_TOKEN"\n'
        'KNOWN = "L3IO_WP_BACKUP_WP_DIR"\n'
        'HELP = f"set L3IO_WP_BACKUP_CONFIG or {TOKEN}"\n',
        encoding="utf-8",
    )
    assert unknown_variables(tmp_path) == [
        "pkg/deeper/later.py: L3IO_WP_BACKUP_LATER_TOKEN"
    ]


def test_a_stale_exclusion_is_caught() -> None:
    planted = {**NOT_SETTINGS, "gone_option": "removed"}
    assert unclassified_options(build_parser(), planted) == [
        "NOT_SETTINGS entry gone_option names no option"
    ]
