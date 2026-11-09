"""Owner decision 48 (BL-E008-032) — ``--overwrite-wp-config`` carries source.

The target's ``DB_*`` reach the Archive's ``wp-config.php`` as the source the
target wrote (``get_source``/``set_source``, ``l3io-wordpress-config`` 0.1.6),
so a ``getenv_docker()`` definition stays a lookup. A carried
``getenv_docker()`` needs the Archive's text to declare that function before
the first carried define; ``l3io-wordpress-config`` 0.1.7's
``declares_function(name, before=key)`` decides, and its own tests cover its
lexical limits. Here: the behaviours backup relies on, and that the library's
answer is the one acted on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("l3io.wp.config")

from l3io.wp.config import EnvLookup, WpConfigFile, WpConfigString

from l3io.wp.backup.adapters import noop
from l3io.wp.backup.adapters.config_writer import (
    WpConfigWriter,
    carry_sources,
    refuse_uncarriable,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.domain.errors import (
    ConfigWriteFailedError,
    UndefinedConfigFunctionError,
    UnsupportedConfigValueError,
)
from l3io.wp.backup.domain.exit_codes import ExitCode

KEEP = ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")
TARGET_PASSWORD = "target-secret-4c8e1a"
ARCHIVE_PASSWORD = "archive-secret-2b9f6d"
PLANTED = "planted-source-7d3e0b"

#: The official image's ``wp-config-docker.php``, as it ships its helper and
#: ``DB_*`` (the rest of the file is irrelevant here).
DOCKER_HELPER = (
    "if (!function_exists('getenv_docker')) {\n"
    "\t// https://github.com/docker-library/wordpress/issues/588\n"
    "\tfunction getenv_docker($env, $default) {\n"
    "\t\tif ($fileEnv = getenv($env . '_FILE')) {\n"
    '\t\t\treturn rtrim(file_get_contents($fileEnv), "\\r\\n");\n'
    "\t\t}\n"
    "\t\telse if (($val = getenv($env)) !== false) {\n"
    "\t\t\treturn $val;\n"
    "\t\t}\n"
    "\t\telse {\n"
    "\t\t\treturn $default;\n"
    "\t\t}\n"
    "\t}\n"
    "}\n"
)
DOCKER_DEFINES = {
    "DB_NAME": "getenv_docker('WORDPRESS_DB_NAME', 'wordpress')",
    "DB_USER": "getenv_docker('WORDPRESS_DB_USER', 'example username')",
    "DB_PASSWORD": "getenv_docker('WORDPRESS_DB_PASSWORD', 'example password')",
    "DB_HOST": "getenv_docker('WORDPRESS_DB_HOST', 'mysql')",
}


def _defines(values: dict[str, str]) -> str:
    return "".join(f"define( '{k}', {v} );\n" for k, v in values.items())


def _docker_config(eol: str = "\n") -> str:
    text = "<?php\n" + DOCKER_HELPER + _defines(DOCKER_DEFINES)
    text += "$table_prefix = getenv_docker('WORDPRESS_TABLE_PREFIX', 'wp_');\n"
    return text.replace("\n", eol)


def _literal_config(eol: str = "\n", **values: str) -> str:
    quoted = {k: f"'{v}'" for k, v in values.items()}
    text = "<?php\n" + _defines(quoted) + "$table_prefix = 'wp_';\n"
    text += "require_once ABSPATH . 'wp-settings.php';\n"
    return text.replace("\n", eol)


TARGET_LITERALS = {
    "DB_NAME": "target_db",
    "DB_USER": "target_user",
    "DB_PASSWORD": TARGET_PASSWORD,
    "DB_HOST": "target-host",
}
ARCHIVE_LITERALS = {
    "DB_NAME": "source_db",
    "DB_USER": "source_user",
    "DB_PASSWORD": ARCHIVE_PASSWORD,
    "DB_HOST": "source-host",
}


def _target(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "site" / "wp-config.php"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(text.encode())
    return path


def _replaced_by_set(target: Path, archive: str) -> bytes:
    """What ``replace_file`` wrote before 0.1.6: each kept value through ``set``."""
    working = WpConfigString(archive)
    values = WpConfigFile(target)
    for key in KEEP:
        value = values.get(key)
        assert isinstance(value, str)
        working.set(key, value)
    return working.content.encode()


@pytest.fixture
def writer() -> WpConfigWriter:
    return WpConfigWriter(SecretRegistry())


# --------------------------------------------------------------------------
# A literal target: unchanged from the value-carrying writer
# --------------------------------------------------------------------------


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_a_literal_target_lands_byte_for_byte_as_before(
    tmp_path: Path, writer: WpConfigWriter, eol: str
) -> None:
    config = _target(tmp_path, _literal_config(eol, **TARGET_LITERALS))
    archive = _literal_config(eol, **ARCHIVE_LITERALS)
    expected = _replaced_by_set(config, archive)
    result = writer.replace_file(config, archive, keep=KEEP)
    assert config.read_bytes() == expected
    assert set(result.changed) == set(KEEP)
    assert ARCHIVE_PASSWORD.encode() not in config.read_bytes()


def test_a_literal_in_another_spelling_is_kept_as_the_target_wrote_it(
    tmp_path: Path, writer: WpConfigWriter
) -> None:
    """The one difference from ``set``: the target's quoting, not a re-rendering."""
    text = _literal_config(**TARGET_LITERALS).replace("'target-host'", '"target-host"')
    config = _target(tmp_path, text)
    writer.replace_file(config, _literal_config(**ARCHIVE_LITERALS), keep=KEEP)
    landed = WpConfigFile(config)
    assert landed.get_source("DB_HOST") == '"target-host"'
    assert landed.get_value("DB_HOST") == "target-host"


# --------------------------------------------------------------------------
# A getenv_docker() target
# --------------------------------------------------------------------------


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_a_getenv_docker_target_keeps_its_lookups(
    tmp_path: Path, writer: WpConfigWriter, eol: str
) -> None:
    config = _target(tmp_path, _docker_config(eol))
    archive = _docker_config(eol).replace("'mysql'", "'archive-default'")
    archive = archive.replace("<?php" + eol, "<?php" + eol + "// archive" + eol)
    result = writer.replace_file(config, archive, keep=KEEP)
    landed = config.read_bytes().decode()
    # The Archive's text, with the target's definitions in place: CRLF kept.
    assert landed == archive.replace("'archive-default'", "'mysql'")
    assert result.changed == ("DB_HOST",)
    if eol == "\r\n":
        assert landed.count("\r\n") == landed.count("\n")
    parsed = WpConfigString(landed)
    for key, source in DOCKER_DEFINES.items():
        value = parsed.get_value(key)
        assert isinstance(value, EnvLookup), key
        assert value.function == "getenv_docker"
        assert parsed.get_source(key) == source


def test_a_getenv_docker_target_into_a_literal_archive_with_the_helper(
    tmp_path: Path, writer: WpConfigWriter
) -> None:
    """An Archive from a Docker site whose DB_* were set as literals."""
    config = _target(tmp_path, _docker_config())
    archive = _literal_config(**ARCHIVE_LITERALS).replace(
        "<?php\n", "<?php\n" + DOCKER_HELPER
    )
    writer.replace_file(config, archive, keep=KEEP)
    parsed = WpConfigFile(config)
    assert parsed.get_value("DB_HOST") == EnvLookup(
        name="WORDPRESS_DB_HOST", default="mysql", function="getenv_docker"
    )
    assert ARCHIVE_PASSWORD not in config.read_text()


@pytest.mark.parametrize("dry_run", [False, True])
def test_an_archive_without_getenv_docker_is_refused_and_nothing_changes(
    tmp_path: Path, writer: WpConfigWriter, dry_run: bool
) -> None:
    config = _target(tmp_path, _docker_config())
    before = config.read_bytes()
    archive = _literal_config(**ARCHIVE_LITERALS)
    adapter = noop.NoopConfigWriter(noop.ActionLog()) if dry_run else writer
    with pytest.raises(UndefinedConfigFunctionError) as caught:
        adapter.replace_file(config, archive, keep=KEEP)
    error = caught.value
    assert error.function == "getenv_docker"
    assert set(error.keys) == set(KEEP)
    assert type(error).exit_code is ExitCode.REFUSED
    assert "Call to undefined function getenv_docker()" in str(error)
    assert "--overwrite-wp-config" in str(error)
    assert ARCHIVE_PASSWORD not in str(error)
    assert config.read_bytes() == before


def test_the_early_check_refuses_and_reads_the_archive_only_when_needed(
    tmp_path: Path,
) -> None:
    reads: list[str] = []

    def archive() -> str:
        reads.append("read")
        return _literal_config(**ARCHIVE_LITERALS)

    literal = _target(tmp_path, _literal_config(**TARGET_LITERALS))
    refuse_uncarriable(literal, KEEP, archive_content=archive)
    assert reads == []
    docker = _target(tmp_path, _docker_config())
    with pytest.raises(UndefinedConfigFunctionError):
        refuse_uncarriable(docker, KEEP, archive_content=archive)
    assert reads == ["read"]


def test_the_early_check_refuses_an_expression(tmp_path: Path) -> None:
    config = _target(
        tmp_path,
        _literal_config(**TARGET_LITERALS).replace("'target-host'", "'db' . ':3307'"),
    )
    with pytest.raises(UnsupportedConfigValueError) as caught:
        refuse_uncarriable(config, KEEP, archive_content=lambda: "<?php\n")
    assert caught.value.key == "DB_HOST"


def test_a_helper_declared_after_the_use_is_refused(
    tmp_path: Path, writer: WpConfigWriter
) -> None:
    """Declared inside a condition, it exists only once PHP has run past it."""
    config = _target(tmp_path, _docker_config())
    archive = _literal_config(**ARCHIVE_LITERALS) + DOCKER_HELPER
    with pytest.raises(UndefinedConfigFunctionError):
        writer.replace_file(config, archive, keep=KEEP)


def test_a_key_the_archive_lacks_is_added_before_a_later_helper_and_refused(
    tmp_path: Path,
) -> None:
    """``set_source`` adds a missing define after ``<?php``, before the helper."""
    config = _target(tmp_path, _docker_config())
    archive = _docker_config().replace(
        "define( 'DB_HOST', getenv_docker('WORDPRESS_DB_HOST', 'mysql') );\n", ""
    )
    with pytest.raises(UndefinedConfigFunctionError) as caught:
        carry_sources(config, archive, KEEP)
    assert caught.value.keys == ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")


# --------------------------------------------------------------------------
# A source the library will not write verbatim
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_planted_unsafe_source_is_refused_without_its_text(
    tmp_path: Path,
    writer: WpConfigWriter,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    config = _target(tmp_path, _literal_config(**TARGET_LITERALS))
    before = config.read_bytes()
    real = WpConfigFile.get_source

    def planted(self: WpConfigFile, key: str) -> object:
        if key == "DB_USER":
            return f"'{PLANTED}'); system('id'); //"
        return real(self, key)

    monkeypatch.setattr(WpConfigFile, "get_source", planted)
    adapter = noop.NoopConfigWriter(noop.ActionLog()) if dry_run else writer
    with pytest.raises(ConfigWriteFailedError) as caught:
        adapter.replace_file(config, _literal_config(**ARCHIVE_LITERALS), keep=KEEP)
    error = caught.value
    assert error.key == "DB_USER"
    assert error.reason == (
        "its definition cannot be carried into the Archive's wp-config.php as "
        "written; the file was not changed"
    )
    assert PLANTED not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__
    assert config.read_bytes() == before


# --------------------------------------------------------------------------
# The declaration, decided by l3io-wordpress-config's declares_function (0.1.7)
# --------------------------------------------------------------------------


def _archive_with(helper: str) -> str:
    """A literal Archive config with ``helper`` before its defines."""
    return _literal_config(**ARCHIVE_LITERALS).replace("<?php\n", "<?php\n" + helper)


@pytest.mark.parametrize(
    "helper",
    [
        DOCKER_HELPER,
        "function &getenv_docker($env, $default) { return $default; }\n",
    ],
    ids=["official", "by-reference"],
)
def test_a_declared_helper_is_accepted(tmp_path: Path, helper: str) -> None:
    config = _target(tmp_path, _docker_config())
    text, _ = carry_sources(config, _archive_with(helper), KEEP)
    assert WpConfigString(text).get_value("DB_HOST") == EnvLookup(
        name="WORDPRESS_DB_HOST", default="mysql", function="getenv_docker"
    )


@pytest.mark.parametrize(
    "helper",
    [
        "",
        "/*\nfunction getenv_docker($env, $default) {}\n*/\n",
        "// function getenv_docker($env, $default) {}\n",
    ],
    ids=["plain", "block-comment", "line-comment"],
)
def test_an_undeclared_helper_is_refused(tmp_path: Path, helper: str) -> None:
    config = _target(tmp_path, _docker_config())
    with pytest.raises(UndefinedConfigFunctionError):
        carry_sources(config, _archive_with(helper), KEEP)


@pytest.mark.parametrize("answer", [True, False])
def test_the_librarys_answer_is_the_one_acted_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: bool
) -> None:
    """The decision is ``declares_function``'s: backup reads no text itself."""
    asked: list[tuple[str, str | None]] = []

    def declares(self: WpConfigString, name: str, before: str | None = None) -> bool:
        asked.append((name, before))
        return answer

    monkeypatch.setattr(WpConfigString, "declares_function", declares)
    config = _target(tmp_path, _docker_config())
    # The text says the opposite of the library's (stubbed) answer.
    archive = _archive_with("" if answer else DOCKER_HELPER)
    if answer:
        carry_sources(config, archive, KEEP)
    else:
        with pytest.raises(UndefinedConfigFunctionError):
            carry_sources(config, archive, KEEP)
    assert asked
    assert {name for name, _ in asked} == {"getenv_docker"}
    assert {before for _, before in asked} <= set(KEEP)
