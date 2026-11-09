"""Owner decision 47 — ``wp-config.php`` settings read from the environment.

The official ``wordpress`` image defines ``DB_*`` and ``$table_prefix`` with
``getenv_docker('NAME', 'default')``. Backup reads them through
``l3io-wordpress-config``'s ``get_value``/``get_variable_value`` and resolves
an ``EnvLookup`` with the library's ``resolve`` against the Run's ``environ``
(a ``NAME_FILE`` Docker secret included); an ``Expression`` is refused without
being quoted. Each shape, the secret file, an unset or empty variable, the
refusals, and that no resolved secret reaches a message, a log record or a
span-bound field.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

pytest.importorskip("l3io.wp.database")
pytest.importorskip("l3io.wp.config")

from l3io.wp.backup.adapters.config_writer import (
    SECRET_FILE_LIMIT,
    TABLE_PREFIX,
    WpConfigWriter,
    read_settings,
    refuse_environment_lookups,
)
from l3io.wp.backup.adapters.database import (
    require_wp_config_credentials,
    site_descriptor_from_wp_config,
)
from l3io.wp.backup.adapters.redaction import REDACTED, SecretRegistry
from l3io.wp.backup.domain.errors import (
    CredentialsError,
    EnvironmentDrivenTargetError,
    InvalidSiteDescriptorError,
    UnsupportedConfigValueError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode

#: Values planted in the environment; searched for, never printed.
DB_PASSWORD = "env-db-password-6f1c2a9e7b"
FILE_PASSWORD = "file-db-password-91d0c3b8a4"
EXPRESSION_SECRET = "inline-secret-part-5e2d7a"

#: The ``DB_*`` and ``$table_prefix`` lines of the official image's
#: ``wp-config-docker.php``, as it ships them.
DOCKER_CONFIG = """<?php
if (!function_exists('getenv_docker')) {
\tfunction getenv_docker($env, $default) {
\t\tif ($fileEnv = getenv($env . '_FILE')) {
\t\t\treturn rtrim(file_get_contents($fileEnv), "\\r\\n");
\t\t}
\t\telse if (($val = getenv($env)) !== false) {
\t\t\treturn $val;
\t\t}
\t\telse {
\t\t\treturn $default;
\t\t}
\t}
}
define( 'DB_NAME', getenv_docker('WORDPRESS_DB_NAME', 'wordpress') );
define( 'DB_USER', getenv_docker('WORDPRESS_DB_USER', 'example username') );
define( 'DB_PASSWORD', getenv_docker('WORDPRESS_DB_PASSWORD', 'example password') );
define( 'DB_HOST', getenv_docker('WORDPRESS_DB_HOST', 'mysql') );
define( 'DB_CHARSET', getenv_docker('WORDPRESS_DB_CHARSET', 'utf8') );
$table_prefix = getenv_docker('WORDPRESS_TABLE_PREFIX', 'wp_');
"""

SITE_ENV = {
    "WORDPRESS_DB_HOST": "db:3307",
    "WORDPRESS_DB_NAME": "wpdb_site",
    "WORDPRESS_DB_USER": "wpu_site",
    "WORDPRESS_DB_PASSWORD": DB_PASSWORD,
    "WORDPRESS_TABLE_PREFIX": "xy_",
}
KEYS = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", TABLE_PREFIX)


def _config(tmp_path: Path, body: str = DOCKER_CONFIG) -> Path:
    path = tmp_path / "wp-config.php"
    path.write_text(body, encoding="utf-8")
    return path


def _define(tmp_path: Path, key: str, value: str) -> Path:
    return _config(tmp_path, f"<?php\ndefine( '{key}', {value} );\n")


def _read(
    config: Path, environ: dict[str, str], keys: tuple[str, ...] = KEYS
) -> dict[str, str | None]:
    return read_settings(config, keys, environ=environ, label=f"wp-config:{config}")


# --------------------------------------------------------------------------
# Each shape
# --------------------------------------------------------------------------


def test_the_official_image_config_resolves_from_the_environment(
    tmp_path: Path,
) -> None:
    values = _read(_config(tmp_path), SITE_ENV)
    assert values == {
        "DB_HOST": "db:3307",
        "DB_NAME": "wpdb_site",
        "DB_USER": "wpu_site",
        "DB_PASSWORD": DB_PASSWORD,
        TABLE_PREFIX: "xy_",
    }


def test_an_unset_getenv_docker_variable_takes_its_default(tmp_path: Path) -> None:
    values = _read(_config(tmp_path), {})
    assert values["DB_HOST"] == "mysql"
    assert values["DB_NAME"] == "wordpress"
    assert values[TABLE_PREFIX] == "wp_"


def test_a_getenv_docker_variable_set_empty_is_empty_not_its_default(
    tmp_path: Path,
) -> None:
    """PHP's ``getenv()`` returns ``''`` for a variable set empty, not ``false``."""
    values = _read(_config(tmp_path), {"WORDPRESS_DB_USER": ""}, ("DB_USER",))
    assert values == {"DB_USER": ""}


def test_literals_are_read_as_written(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        "<?php\ndefine('DB_HOST', 'localhost');\ndefine('DB_NAME', \"site\");\n"
        "define('DB_USER', 'u');\ndefine('DB_PASSWORD', 'literal-password-1');\n"
        "$table_prefix = 'ab_';\n",
    )
    assert _read(config, SITE_ENV) == {
        "DB_HOST": "localhost",
        "DB_NAME": "site",
        "DB_USER": "u",
        "DB_PASSWORD": "literal-password-1",
        TABLE_PREFIX: "ab_",
    }


def test_an_undefined_key_is_none(tmp_path: Path) -> None:
    config = _define(tmp_path, "DB_NAME", "'site'")
    assert _read(config, {}, ("DB_HOST", TABLE_PREFIX)) == {
        "DB_HOST": None,
        TABLE_PREFIX: None,
    }


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"HOST_VAR": "db.example"}, "db.example"),
        ({"HOST_VAR": ""}, "fallback"),
        ({"HOST_VAR": "0"}, "fallback"),
        ({}, "fallback"),
    ],
    ids=["set", "empty", "zero", "unset"],
)
def test_getenv_with_a_default_falls_back_as_php_does(
    tmp_path: Path, environ: dict[str, str], expected: str
) -> None:
    config = _define(tmp_path, "DB_HOST", "getenv('HOST_VAR') ?: 'fallback'")
    assert _read(config, environ, ("DB_HOST",)) == {"DB_HOST": expected}


def test_a_bare_getenv_reads_the_variable(tmp_path: Path) -> None:
    config = _define(tmp_path, "DB_HOST", "getenv('HOST_VAR')")
    assert _read(config, {"HOST_VAR": "db.example"}, ("DB_HOST",)) == {
        "DB_HOST": "db.example"
    }


@pytest.mark.parametrize(
    ("key", "error"),
    [("DB_HOST", InvalidSiteDescriptorError), ("DB_PASSWORD", CredentialsError)],
)
def test_a_bare_getenv_of_an_unset_variable_is_refused_naming_it(
    tmp_path: Path, key: str, error: type[Exception]
) -> None:
    """PHP defines ``false``; no usable setting, so it is refused, not guessed."""
    config = _define(tmp_path, key, "getenv('SOME_VAR')")
    with pytest.raises(error) as caught:
        _read(config, {}, (key,))
    assert "SOME_VAR" in str(caught.value)
    assert key in str(caught.value)


# --------------------------------------------------------------------------
# NAME_FILE: Docker secrets
# --------------------------------------------------------------------------


def test_a_name_file_secret_is_read_and_registered(tmp_path: Path) -> None:
    secret = tmp_path / "db_password"
    secret.write_text(FILE_PASSWORD + "\n", encoding="utf-8")
    config = _config(tmp_path)
    environ = SITE_ENV | {"WORDPRESS_DB_PASSWORD_FILE": str(secret)}
    secrets = SecretRegistry()
    with capture_logs() as logs:
        values = read_settings(
            config, ("DB_PASSWORD",), environ=environ, label="l", secrets=secrets
        )
    assert values == {"DB_PASSWORD": FILE_PASSWORD}
    assert secrets.redact(f"x {FILE_PASSWORD} y") == f"x {REDACTED} y"
    assert FILE_PASSWORD not in repr(logs)
    assert DB_PASSWORD not in repr(logs)
    [record] = [r for r in logs if r["event"] == "config_value_from_environment"]
    assert record["variable"] == "WORDPRESS_DB_PASSWORD"
    assert record["key"] == "DB_PASSWORD"


def test_an_environment_password_is_registered(tmp_path: Path) -> None:
    secrets = SecretRegistry()
    read_settings(
        _config(tmp_path),
        ("DB_PASSWORD",),
        environ=SITE_ENV,
        label="l",
        secrets=secrets,
    )
    assert secrets.redact(DB_PASSWORD) == REDACTED


@pytest.mark.parametrize(
    ("key", "error"),
    [("DB_PASSWORD", CredentialsError), ("DB_NAME", InvalidSiteDescriptorError)],
)
def test_an_unreadable_name_file_is_refused_unchained(
    tmp_path: Path, key: str, error: type[Exception]
) -> None:
    """A reader's failure (a ``UnicodeDecodeError`` holds the file's bytes) is
    named by class and never chained."""
    secret = tmp_path / "secret"
    secret.write_bytes(b"\xff\xfe" + FILE_PASSWORD.encode())
    variable = "WORDPRESS_" + key
    environ = {f"{variable}_FILE": str(secret)}
    with pytest.raises(error) as caught:
        _read(_config(tmp_path), environ, (key,))
    text = str(caught.value)
    assert f"{variable}_FILE" in text
    assert "UnicodeDecodeError" in text
    assert FILE_PASSWORD not in text
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_missing_name_file_is_refused(tmp_path: Path) -> None:
    environ = {"WORDPRESS_DB_PASSWORD_FILE": str(tmp_path / "absent")}
    with pytest.raises(CredentialsError, match="FileNotFoundError"):
        _read(_config(tmp_path), environ, ("DB_PASSWORD",))


def test_an_oversized_name_file_is_refused(tmp_path: Path) -> None:
    secret = tmp_path / "huge"
    secret.write_text("x" * (SECRET_FILE_LIMIT + 1), encoding="utf-8")
    environ = {"WORDPRESS_DB_PASSWORD_FILE": str(secret)}
    with pytest.raises(CredentialsError, match="ValueError"):
        _read(_config(tmp_path), environ, ("DB_PASSWORD",))


# --------------------------------------------------------------------------
# An Expression is refused, never quoted
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"])
def test_an_expression_is_refused_without_its_source(tmp_path: Path, key: str) -> None:
    config = _define(tmp_path, key, f"'abc' . '{EXPRESSION_SECRET}'")
    with pytest.raises(UnsupportedConfigValueError) as caught:
        _read(config, {}, (key,))
    assert caught.value.key == key
    assert caught.value.path == config
    assert type(caught.value).exit_code is ExitCode.REFUSED
    assert key in str(caught.value)
    assert EXPRESSION_SECRET not in str(caught.value)
    assert "abc" not in str(caught.value)


def test_a_table_prefix_expression_is_refused_naming_the_variable(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "<?php\n$table_prefix = PREFIX . '_';\n")
    with pytest.raises(UnsupportedConfigValueError) as caught:
        _read(config, {}, (TABLE_PREFIX,))
    assert caught.value.key == "$table_prefix"


# --------------------------------------------------------------------------
# The Site Descriptor and the wp-config.php credential source
# --------------------------------------------------------------------------


def test_the_descriptor_and_credentials_follow_the_environment(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    secrets = SecretRegistry()
    with capture_logs() as logs:
        site, source = site_descriptor_from_wp_config(
            tmp_path, config_file=config, environ=SITE_ENV, secrets=secrets
        )
        require_wp_config_credentials(source, environ=SITE_ENV)
        resolved = source.resolve()
    assert (site.db_host, site.db_port, site.db_name) == ("db", 3307, "wpdb_site")
    assert site.table_prefix == "xy_"
    assert (resolved.username, resolved.password) == ("wpu_site", DB_PASSWORD)
    assert source.source == f"wp-config:{config}"
    assert secrets.redact(DB_PASSWORD) == REDACTED
    assert DB_PASSWORD not in repr(logs)
    assert DB_PASSWORD not in repr(source)
    assert DB_PASSWORD not in repr(site)


def test_the_descriptor_takes_the_defaults_with_nothing_set(tmp_path: Path) -> None:
    site, _ = site_descriptor_from_wp_config(
        tmp_path, config_file=_config(tmp_path), environ={}, secrets=SecretRegistry()
    )
    assert (site.db_host, site.db_name, site.table_prefix) == (
        "mysql",
        "wordpress",
        "wp_",
    )


def test_a_half_environment_pair_names_the_missing_key(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        "<?php\ndefine('DB_HOST', 'localhost');\ndefine('DB_NAME', 's');\n"
        "define('DB_USER', getenv('U_VAR'));\n",
    )
    _, source = site_descriptor_from_wp_config(
        tmp_path,
        config_file=config,
        environ={"U_VAR": "someone"},
        secrets=SecretRegistry(),
    )
    with pytest.raises(CredentialsError) as caught:
        require_wp_config_credentials(source, environ={})
    assert caught.value.missing_key == "DB_PASSWORD"


def test_a_descriptor_refusing_the_environment_names_keys_and_variables(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with pytest.raises(EnvironmentDrivenTargetError) as caught:
        site_descriptor_from_wp_config(
            tmp_path,
            config_file=config,
            environ=SITE_ENV,
            secrets=SecretRegistry(),
            refuse_environment="a database restore",
        )
    error = caught.value
    assert type(error).exit_code is ExitCode.REFUSED
    assert set(error.keys) == {"DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"}
    assert "WORDPRESS_DB_NAME" in error.variables
    assert error.operation == "a database restore"
    assert "docs/adr/0027" in str(error)
    for value in SITE_ENV.values():
        assert value not in str(error)


def test_refusing_literals_refuses_nothing(tmp_path: Path) -> None:
    config = _define(tmp_path, "DB_NAME", "'site'")
    refuse_environment_lookups(
        config, ("DB_NAME", "DB_HOST"), operation="a database restore", label="l"
    )


# --------------------------------------------------------------------------
# ConfigQuery.read_constants: a restore target's constants
# --------------------------------------------------------------------------


def test_read_constants_resolves_with_the_writer_environ(tmp_path: Path) -> None:
    secrets = SecretRegistry()
    writer = WpConfigWriter(secrets, environ=SITE_ENV)
    read = writer.read_constants(_config(tmp_path), ("DB_NAME", "DB_PASSWORD"))
    assert read == {"DB_NAME": "wpdb_site", "DB_PASSWORD": DB_PASSWORD}
    assert secrets.redact(DB_PASSWORD) == REDACTED


def test_read_constants_never_hands_on_an_expression_as_a_value(
    tmp_path: Path,
) -> None:
    """BL-E008-032: ``get()`` returned an expression's text as a ``str``."""
    config = _define(tmp_path, "DB_NAME", "'site_' . PREFIX")
    with pytest.raises(UnsupportedConfigValueError):
        WpConfigWriter(SecretRegistry()).read_constants(config, ("DB_NAME",))


def test_read_constants_keeps_a_literal_type(tmp_path: Path) -> None:
    config = _config(tmp_path, "<?php\ndefine('WP_DEBUG', true);\ndefine('N', 3);\n")
    read = WpConfigWriter(SecretRegistry()).read_constants(config, ("WP_DEBUG", "N"))
    assert read == {"WP_DEBUG": True, "N": 3}
