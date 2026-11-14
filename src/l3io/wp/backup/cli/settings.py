"""Owner decision 60, ADR-0028 — every Run setting as a flag, a variable and a key.

**One setting, three spellings.** Each field of :class:`Settings` is one
setting. Its spellings are derived, never listed:

- the CLI flag ``--<name with dashes>`` (``wp_dir`` is ``--wp-dir``), whose
  argparse ``dest`` is the field name;
- the environment variable :data:`ENV_PREFIX` + the upper-cased name
  (``L3IO_WP_BACKUP_WP_DIR``), read by pydantic-settings' environment source;
- the key of the same name in a TOML config file (``wp_dir = "/srv/www"``),
  read by pydantic-settings' TOML source.

``tests/contracts/test_settings_spellings.py`` derives the mapping from this
model and the parser and fails on a setting missing any spelling, and on a
parser option or an ``L3IO_WP_BACKUP_*`` name in the source that is neither a
setting nor in :data:`NOT_SETTINGS`.

**Precedence (decision 60): command line, then environment, then config
file, then the parser's default.** The flags are parsed by argparse with
every setting's default suppressed, so what the operator typed is known;
pydantic-settings layers the environment over the config files
(:class:`Settings` returns no value of its own), and the parser's default
applies where neither has one. Values from a variable or a file go through the
same argparse ``type`` and ``choices`` as typed ones, so there is one
validation of each.

**Config files.** ``--config PATH`` or :data:`CONFIG_VAR` names one file, which
must exist. Without either, :data:`SYSTEM_CONFIG_FILE` and then the per-user
file (:func:`user_config_file`) are read when present, the user's keys over
the system's. A file holding a secret (a :data:`SECRET_SETTINGS` key) that its
group or others can read is refused, naming the file; an unknown key is
refused, naming the file and the key; a value that does not validate is
refused naming the setting and where it came from, never the value.

**Secrets.** :data:`SECRET_SETTINGS` are read as ``SecretStr`` and handed on
as :class:`~l3io.wp.backup.domain.secret.Secret`; the CLI root registers every
one with the Run's ``SecretRegistry`` before the Run opens (``cli.main``).
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Final, Literal, get_args

import platformdirs
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from l3io.wp.backup.adapters.lock import APP_NAME
from l3io.wp.backup.domain.compression import (
    MAX_COMPRESSION_LEVEL,
    MIN_COMPRESSION_LEVEL,
)
from l3io.wp.backup.domain.errors import UsageError
from l3io.wp.backup.domain.secret import Secret

if TYPE_CHECKING:
    from l3io.wp.backup.cli.parser import CliParser

__all__ = [
    "CONFIG_DEST",
    "CONFIG_VAR",
    "ENV_PREFIX",
    "NOT_SETTINGS",
    "ORIGINS_ATTR",
    "SECRET_SETTINGS",
    "SETTING_NAMES",
    "SYSTEM_CONFIG_FILE",
    "ExplainedType",
    "LayeredArguments",
    "Origin",
    "Settings",
    "config_key",
    "env_name",
    "flag_name",
    "parse_layered",
    "setting_origins",
    "typed_settings",
    "user_config_file",
]

#: Every setting's variable starts with this.
ENV_PREFIX: Final = "L3IO_WP_BACKUP_"
#: The config file to read, as a variable (``--config`` is the flag).
CONFIG_VAR: Final = f"{ENV_PREFIX}CONFIG"
#: ``--config``'s ``dest``: the one option that selects the file itself.
CONFIG_DEST: Final = "config"
_CONFIG_FILE_NAME: Final = "config.toml"
#: The system-wide config file, read first when present.
SYSTEM_CONFIG_FILE: Final = Path("/etc") / APP_NAME / _CONFIG_FILE_NAME
#: Mode bits that make a file readable by its group or by others.
_EXPOSED: Final = stat.S_IRGRP | stat.S_IROTH

#: Where a setting's value came from, as logged and as a refusal names it.
Origin = Literal["command line", "environment", "config file", "default"]
#: The namespace attribute :func:`parse_layered` leaves the origins on.
ORIGINS_ATTR: Final = "setting_origins"


@dataclass(frozen=True, slots=True)
class ExplainedType:
    """A parser ``type`` whose refusal has a reason in this package's own words.

    Called as ``convert``. A flag's refusal is ``convert``'s own
    ``ArgumentTypeError`` (argparse prints it); a variable's or a key's names
    where the value came from, then ``reason(value)`` (:func:`_converted`), a
    pure function of the value, never an exception's text. Any other
    ``type``'s refusal (``int``'s, ``Path``'s, a dependency's) is reported
    without its text. ``reason`` must not echo a secret: no secret setting
    has a ``type``.
    """

    convert: Callable[[str], object]
    reason: Callable[[str], str | None]

    def __call__(self, text: str) -> object:
        return self.convert(text)


def user_config_file() -> Path:
    """The per-user config file: ``<user config dir>/l3io-wp-backup/config.toml``."""
    return platformdirs.user_config_path(APP_NAME) / _CONFIG_FILE_NAME


class Settings(BaseSettings):
    """Every Run setting, ``None`` when neither a variable nor a file sets it.

    The defaults are the parser's (``cli/parser.py``), applied by
    :func:`parse_layered`; nothing here has one of its own. Field types say
    only how a variable or a key is parsed: ``bool`` for a switch, ``list``
    for a repeatable flag (a variable holds a JSON list), ``SecretStr`` for a
    secret, ``int`` for a count (``keep`` and the ``keep_*`` tiers:
    ``keep_daily = 7`` in TOML; ``core/retention.RetentionPolicy`` refuses one
    below 1, which an ``int`` skips the parser's ``type``),
    a bounded ``int`` for ``compression_level`` (its bounds are the domain's,
    the ones the parser's ``type`` checks, so a variable or a key outside
    them is refused here; owner decision 88),
    ``str`` for everything else, which the parser's ``type`` and ``choices``
    then check.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        case_sensitive=False,
        env_ignore_empty=True,
        extra="forbid",
    )

    # The top-level parser.
    log_level: str | None = None
    log_format: str | None = None
    what_if: bool | None = None
    # The Site and the Archive.
    wp_dir: str | None = None
    wp_config: str | None = None
    archive: str | None = None
    archive_dir: str | None = None
    site_name: str | None = None
    archive_subdir: str | None = None
    mode: str | None = None
    # Encryption.
    encrypt_with_passphrase: bool | None = None
    passphrase: SecretStr | None = None
    require_encrypted: bool | None = None
    # What a backup collects.
    exclude: list[str] | None = None
    no_default_excludes: bool | None = None
    # The payload's gzip level, for a backup and a Safety Snapshot (decision 88).
    compression_level: (
        Annotated[int, Field(ge=MIN_COMPRESSION_LEVEL, le=MAX_COMPRESSION_LEVEL)] | None
    ) = None
    # The WordPress account, and the database connections' TLS.
    db_user: str | None = None
    db_password: SecretStr | None = None
    db_credentials_aws_secret_id: str | None = None
    db_credentials_aws_region: str | None = None
    db_tls: str | None = None
    db_tls_ca: str | None = None
    # The restore Mode and its options.
    cleanfirst: bool | None = None
    drop_database: bool | None = None
    force: bool | None = None
    overwrite_wp_config: bool | None = None
    reset_existing_db_user: bool | None = None
    no_safety_snapshot: bool | None = None
    snapshot_dir: str | None = None
    archive_database: str | None = None
    # The database administrator.
    admin_user: str | None = None
    admin_password: SecretStr | None = None
    admin_credentials_aws_secret_id: str | None = None
    admin_credentials_aws_region: str | None = None
    # Relocation.
    new_site_url: str | None = None
    new_site_home_url: str | None = None
    new_db_host: str | None = None
    new_db_name: str | None = None
    new_db_user: str | None = None
    new_db_password: SecretStr | None = None
    new_db_password_file: str | None = None
    # verify.
    authenticate: bool | None = None
    # prune (owner decisions 83, 84): counts, so a TOML file may write them bare.
    keep: int | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
    keep_yearly: int | None = None

    @field_validator("compression_level", mode="before")
    @classmethod
    def _not_a_bool(cls, value: object) -> object:
        """``compression_level = true`` is refused, not read as level 1."""
        if isinstance(value, bool):
            msg = "Input should be a whole number, not a boolean"
            raise ValueError(msg)  # noqa: TRY004 - pydantic reports ValueError
        return value

    def __repr__(self) -> str:
        """The names set, never a value: four of them are secrets."""
        given = sorted(
            n for n in type(self).model_fields if getattr(self, n) is not None
        )
        return f"Settings(set={given})"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """The environment over the config files; no ``.env`` or secrets dir."""
        del init_settings, dotenv_settings, file_secret_settings
        return (env_settings, TomlConfigSettingsSource(settings_cls))


#: Every setting's name: its ``dest``, its key, and its variable's suffix.
SETTING_NAMES: Final[frozenset[str]] = frozenset(Settings.model_fields)
#: The settings whose value is a secret: registered, never shown.
SECRET_SETTINGS: Final[frozenset[str]] = frozenset(
    name
    for name, field in Settings.model_fields.items()
    if SecretStr in get_args(field.annotation)
)

#: Parser options that are not settings, and why. Each has a flag only.
NOT_SETTINGS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "help": "prints help and exits",
        "version": "prints the version and exits",
        CONFIG_DEST: (
            "selects the config file itself: a flag and L3IO_WP_BACKUP_CONFIG, "
            "never a key in the file it selects"
        ),
        "list_default_excludes": "prints the default exclusion set; not a Run",
        "resolve_interrupted_run": "names one interrupted Run, for one invocation",
        "json": "doctor's output form, for one interactive diagnosis",
        "only_appointed_in_asg": "removed; accepted only to say so",
        "admin_tls": "deprecated spelling of --db-tls",
        "admin_tls_ca": "deprecated spelling of --db-tls-ca",
        "allow_unencrypted": "deprecated; has no effect (owner decision 59)",
    }
)


def env_name(setting: str) -> str:
    """``setting``'s variable: ``wp_dir`` is ``L3IO_WP_BACKUP_WP_DIR``."""
    return f"{ENV_PREFIX}{setting.upper()}"


def config_key(setting: str) -> str:
    """``setting``'s config-file key: its own name."""
    return setting


def flag_name(setting: str) -> str:
    """``setting``'s flag: ``wp_dir`` is ``--wp-dir``."""
    return "--" + setting.replace("_", "-")


@dataclass(frozen=True, slots=True)
class LayeredArguments:
    """The parsed arguments, every setting resolved, and where each came from.

    ``namespace`` is what argparse would return had every value been typed;
    a secret setting holds a :class:`Secret`. ``origins`` maps each setting
    of the chosen command to its :data:`Origin`; ``config_files`` are the
    files read, in order; ``unknown_variables`` the ``L3IO_WP_BACKUP_*``
    names set in the environment that no setting has (logged by the root).
    ``secrets`` are every secret read, by name, whichever command uses it, so
    the root registers each one (``repr`` shows only the names).
    """

    namespace: argparse.Namespace
    origins: Mapping[str, Origin]
    config_files: tuple[Path, ...]
    unknown_variables: tuple[str, ...]
    secrets: Mapping[str, Secret] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        """The command and the files, never a value: some are secrets."""
        command = getattr(self.namespace, "action", None)
        files = [str(path) for path in self.config_files]
        return f"LayeredArguments(command={command!r}, config_files={files})"

    def given(self, origin: Origin) -> frozenset[str]:
        """The settings whose value came from ``origin``."""
        return frozenset(name for name, o in self.origins.items() if o == origin)


def setting_origins(values: Mapping[str, object]) -> Mapping[str, Origin]:
    """Each setting's :data:`Origin`, from ``vars(args)``; empty if not layered."""
    origins = values.get(ORIGINS_ATTR)
    return origins if isinstance(origins, Mapping) else MappingProxyType({})


def typed_settings(values: Mapping[str, object]) -> frozenset[str] | None:
    """The settings typed on the command line; ``None`` when not layered here."""
    if ORIGINS_ATTR not in values:
        return None
    return frozenset(
        name
        for name, origin in setting_origins(values).items()
        if origin == "command line"
    )


def _parsers(parser: CliParser) -> list[argparse.ArgumentParser]:
    return [parser, *parser.commands.values()]


def _parse_typed(
    parser: CliParser, argv: Sequence[str] | None
) -> tuple[argparse.Namespace, dict[str, tuple[object, bool]]]:
    """argparse with every setting's default suppressed: only what was typed.

    Returns the namespace and each layered ``dest``'s original default and
    ``required``, which are restored on the actions before returning.
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    if "-h" in argv or "--help" in argv:
        # Help is printed by the parser as declared, required options included.
        parser.parse_args(argv)
    saved: dict[argparse.Action, tuple[object, bool]] = {}
    for each in _parsers(parser):
        for action in each._actions:  # noqa: SLF001 - argparse has no public API
            if action.dest in SETTING_NAMES or action.dest == CONFIG_DEST:
                saved[action] = (action.default, action.required)
                action.default = argparse.SUPPRESS
                action.required = False
    try:
        typed = parser.parse_args(argv)
    finally:
        for action, (default, required) in saved.items():
            action.default, action.required = default, required
    originals: dict[str, tuple[object, bool]] = {}
    command = parser.commands.get(getattr(typed, "action", None) or "")
    for each in (parser, command) if command is not None else (parser,):
        for action in each._actions:  # noqa: SLF001
            if action in saved:
                default, required = saved[action]
                # The subcommand's own default wins over the top level's
                # (``--whatIf``'s SUPPRESS on a subcommand defers to it).
                if default != argparse.SUPPRESS or action.dest not in originals:
                    originals[action.dest] = (default, required)
    return typed, originals


def _config_files(typed: Mapping[str, object]) -> tuple[Path, ...]:
    """``--config``, else :data:`CONFIG_VAR`, else the default files present."""
    flag = typed.get(CONFIG_DEST)
    named = str(flag) if flag else os.environ.get(CONFIG_VAR) or None
    if named is not None:
        path = Path(named)
        if not path.is_file():
            source = "--config" if flag else CONFIG_VAR
            msg = f"{source}: config file {path} does not exist or is not a file"
            raise UsageError(msg, arguments=(CONFIG_DEST,))
        return (path,)
    return tuple(p for p in (SYSTEM_CONFIG_FILE, user_config_file()) if p.is_file())


def _check_file(path: Path) -> None:
    """Refuse a file that cannot be read, has an unknown key, or exposes a secret."""
    try:
        with path.open("rb") as handle:
            mode = os.fstat(handle.fileno()).st_mode
            document = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        # Neither the parser's text nor the file's: a value may be a secret.
        msg = f"config file {path} is not valid TOML"
        raise UsageError(msg, arguments=(CONFIG_DEST,)) from exc
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        msg = f"config file {path} could not be read: {reason}"
        raise UsageError(msg, arguments=(CONFIG_DEST,)) from exc
    unknown = sorted(set(document) - SETTING_NAMES)
    if unknown:
        msg = (
            f"config file {path}: unknown setting {unknown[0]!r} (a key is a "
            "setting's name, as --wp-dir is wp_dir)"
        )
        raise UsageError(msg, arguments=(CONFIG_DEST,))
    held = sorted(set(document) & SECRET_SETTINGS)
    if held and mode & _EXPOSED:
        msg = (
            f"config file {path} holds a secret ({', '.join(held)}) and is "
            f"readable by its group or others; make it readable by its owner "
            f"only (chmod 600 {path})"
        )
        raise UsageError(msg, arguments=(CONFIG_DEST,))


def _environment_names() -> frozenset[str]:
    """The setting variables set, non-empty, in the environment (upper case)."""
    return frozenset(name.upper() for name, value in os.environ.items() if value)


def _load(files: tuple[Path, ...]) -> Settings:
    """pydantic-settings' environment over ``files``; ``UsageError`` if invalid."""

    class _FromFiles(Settings):
        model_config = SettingsConfigDict(toml_file=list(files))

    try:
        return _FromFiles()
    except ValidationError as exc:
        error = exc.errors(include_input=False, include_url=False)[0]
        name = str(error["loc"][0]) if error["loc"] else "?"
        where = (
            env_name(name)
            if env_name(name) in _environment_names()
            else f"key {config_key(name)!r} in {', '.join(map(str, files))}"
        )
        msg = f"setting {name} from {where}: {error['msg']}"
        raise UsageError(msg, arguments=(name,)) from None


def _converted(action: argparse.Action, name: str, value: object, where: str) -> object:
    """A variable's or a key's value through the parser's own ``type``/``choices``.

    ``where`` names the variable or the config file key, never the value of a
    secret (no secret setting has a ``type`` or ``choices``). An
    :class:`ExplainedType` adds its ``reason``; any other refusal is reported
    without its text, which may be a dependency's.
    """
    if isinstance(value, SecretStr):
        return Secret(value.get_secret_value())
    if isinstance(value, str) and callable(action.type):
        try:
            value = action.type(value)
        except (argparse.ArgumentTypeError, TypeError, ValueError):
            msg = f"{where}: {value!r} is not a valid {flag_name(name)} value"
            if isinstance(action.type, ExplainedType):
                reason = action.type.reason(str(value))
                msg += "" if reason is None else f": {reason}"
            raise UsageError(msg, arguments=(name,)) from None
    if action.choices is not None and value not in action.choices:
        choices = ", ".join(map(str, action.choices))
        msg = f"{where}: invalid choice {value!r} (choose from {choices})"
        raise UsageError(msg, arguments=(name,))
    return value


def _actions_by_dest(
    parser: CliParser, command: str | None
) -> dict[str, argparse.Action]:
    """The chosen command's actions by ``dest``, over the top level's."""
    found: dict[str, argparse.Action] = {}
    chosen = parser.commands.get(command or "")
    for each in (parser, chosen) if chosen is not None else (parser,):
        for action in each._actions:  # noqa: SLF001
            found[action.dest] = action
    return found


def _unknown_variables() -> tuple[str, ...]:
    known = {env_name(name) for name in SETTING_NAMES} | {CONFIG_VAR}
    return tuple(
        sorted(
            name
            for name in os.environ
            if name.upper().startswith(ENV_PREFIX) and name.upper() not in known
        )
    )


def parse_layered(
    parser: CliParser, argv: Sequence[str] | None = None
) -> LayeredArguments:
    """Parse ``argv`` and resolve every setting of the chosen command.

    argparse's own usage errors exit as before (``SystemExit(2)``); a config
    file or a value that cannot be used is :class:`UsageError`. A required
    option is checked here, after layering, with argparse's own message, so a
    variable or a key satisfies it as a flag does.
    """
    typed, originals = _parse_typed(parser, argv)
    command = getattr(typed, "action", None)
    try:
        return _layer(parser, vars(typed), originals, command)
    except UsageError as exc:
        parser.for_command(command).error(str(exc))


def _layer(
    parser: CliParser,
    typed: Mapping[str, object],
    originals: Mapping[str, tuple[object, bool]],
    command: str | None,
) -> LayeredArguments:
    """:func:`parse_layered` after argparse: the files, the variables, defaults."""
    files = _config_files(typed)
    for path in files:
        _check_file(path)
    loaded = _load(files)
    set_in_environment = _environment_names()
    actions = _actions_by_dest(parser, command)
    namespace = argparse.Namespace(**typed)
    origins: dict[str, Origin] = {}
    missing: list[str] = []
    for dest, (default, required) in originals.items():
        if dest == CONFIG_DEST:
            continue
        action = actions[dest]
        if dest in typed:
            value = typed[dest]
            if dest in SECRET_SETTINGS and isinstance(value, str):
                value = Secret(value)
            origins[dest] = "command line"
        elif getattr(loaded, dest) is not None:
            from_environment = env_name(dest) in set_in_environment
            where = (
                env_name(dest)
                if from_environment
                else f"config file key {config_key(dest)!r}"
            )
            value = _converted(action, dest, getattr(loaded, dest), where)
            origins[dest] = "environment" if from_environment else "config file"
        else:
            value = default
            origins[dest] = "default"
            if required:
                missing.append("/".join(action.option_strings))
        setattr(namespace, dest, value)
    setattr(namespace, CONFIG_DEST, files[-1] if files else None)
    setattr(namespace, ORIGINS_ATTR, MappingProxyType(origins))
    secrets: dict[str, Secret] = {}
    for name in sorted(SECRET_SETTINGS):
        value = getattr(namespace, name, None)
        loaded_value = getattr(loaded, name)
        if isinstance(value, Secret):
            secrets[name] = value
        elif isinstance(loaded_value, SecretStr):
            secrets[name] = Secret(loaded_value.get_secret_value())
    if missing:
        usage = parser.for_command(command)
        usage.error(f"the following arguments are required: {', '.join(missing)}")
    return LayeredArguments(
        namespace=namespace,
        origins=MappingProxyType(origins),
        config_files=files,
        unknown_variables=_unknown_variables(),
        secrets=MappingProxyType(secrets),
    )
