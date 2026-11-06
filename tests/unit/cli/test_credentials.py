"""ADR-0015 — ``cli/credentials.py``, the composition roots' credential seam."""

from __future__ import annotations

from pathlib import Path

import pytest

wpdb = pytest.importorskip("l3io.wp.database")

from l3io.wp.backup.adapters.redaction import REDACTED, SecretRegistry  # noqa: E402
from l3io.wp.backup.cli.credentials import (  # noqa: E402
    admin_source,
    admin_spec,
    wordpress_source,
    wordpress_spec,
)
from l3io.wp.backup.domain.credentials import (  # noqa: E402
    AwsSecretCredentialSpec,
    ExplicitCredentialSpec,
)
from l3io.wp.backup.domain.errors import CredentialsError, UsageError  # noqa: E402

PASSWORD = "explicit-pw-value"
WP_PASSWORD = "wp-config-pw-value"


class SpyRegistry(SecretRegistry):
    """Records each registration, in order."""

    __slots__ = ("seen",)

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    def register(self, value: str, *, source: str = "unspecified") -> None:
        self.seen.append(value)
        super().register(value, source=source)


@pytest.fixture
def wp_config(tmp_path: Path) -> object:
    config = tmp_path / "wp-config.php"
    config.write_text(
        "<?php\ndefine('DB_NAME', 'w');\ndefine('DB_USER', 'u');\n"
        f"define('DB_PASSWORD', '{WP_PASSWORD}');\ndefine('DB_HOST', 'h');\n",
        encoding="utf-8",
    )
    return wpdb.WpConfigFile(config)


EXPLICIT = ExplicitCredentialSpec(username="admin", password=PASSWORD)
AWS = AwsSecretCredentialSpec(secret_id="prod/db", region="eu-west-1")


def test_an_explicit_wordpress_password_is_registered_before_return(
    wp_config: object,
) -> None:
    secrets = SpyRegistry()
    source = wordpress_source(
        EXPLICIT, wp_config=wp_config, environ={}, secrets=secrets
    )
    assert secrets.seen == [PASSWORD]
    assert source.resolve().password == PASSWORD  # explicit precedes wp-config
    assert PASSWORD not in repr(source)


def test_a_wp_config_password_is_registered_when_resolved(wp_config: object) -> None:
    secrets = SpyRegistry()
    source = wordpress_source(None, wp_config=wp_config, environ={}, secrets=secrets)
    assert secrets.seen == []
    assert source.resolve().password == WP_PASSWORD
    assert secrets.redact(WP_PASSWORD) == REDACTED


def test_an_admin_password_is_registered_before_return() -> None:
    secrets = SpyRegistry()
    source = admin_source(EXPLICIT, environ={}, secrets=secrets)
    assert secrets.seen == [PASSWORD]
    assert source is not None
    assert source.resolve().username == "admin"


def test_no_admin_spec_is_no_admin_source() -> None:
    assert admin_source(None, environ={}, secrets=SecretRegistry()) is None


def test_an_aws_spec_names_epic_5(wp_config: object) -> None:
    with pytest.raises(UsageError, match="Epic 5"):
        wordpress_source(AWS, wp_config=wp_config, environ={}, secrets=SecretRegistry())
    with pytest.raises(UsageError, match="Epic 5"):
        admin_source(AWS, environ={}, secrets=SecretRegistry())


@pytest.mark.parametrize(
    ("username", "password", "missing"),
    [("admin", None, "--admin-password"), (None, PASSWORD, "--admin-username")],
)
def test_a_lone_half_is_a_usage_error_naming_the_missing_half(
    username: str | None, password: str | None, missing: str
) -> None:
    with pytest.raises(UsageError, match=missing) as caught:
        admin_spec(
            username=username, password=password, aws_secret_id=None, aws_region=None
        )
    assert PASSWORD not in str(caught.value)


def test_admin_spec_builds_each_kind() -> None:
    assert (
        admin_spec(username=None, password=None, aws_secret_id=None, aws_region=None)
        is None
    )
    assert (
        admin_spec(
            username="admin", password=PASSWORD, aws_secret_id=None, aws_region=None
        )
        == EXPLICIT
    )
    assert (
        admin_spec(
            username=None,
            password=None,
            aws_secret_id="prod/db",
            aws_region="eu-west-1",
        )
        == AWS
    )


def test_an_explicit_pair_and_an_aws_secret_together_are_refused() -> None:
    with pytest.raises(UsageError, match="not both"):
        admin_spec(
            username="admin", password=PASSWORD, aws_secret_id="x", aws_region=None
        )


def test_a_region_without_a_secret_id_is_refused() -> None:
    with pytest.raises(UsageError, match="secret-id"):
        wordpress_spec(aws_secret_id=None, aws_region="eu-west-1")
    with pytest.raises(UsageError, match="secret-id"):
        admin_spec(username=None, password=None, aws_secret_id=None, aws_region="r")


def test_wordpress_spec() -> None:
    assert wordpress_spec(aws_secret_id=None, aws_region=None) is None
    assert wordpress_spec(aws_secret_id="prod/db", aws_region="eu-west-1") == AWS


def test_a_source_raising_while_resolving_is_named_never_quoted(
    wp_config: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: object) -> object:
        msg = f"boom {WP_PASSWORD}"
        raise RuntimeError(msg)

    monkeypatch.setattr(wpdb.WpConfigFile, "resolve", broken)
    source = wordpress_source(
        None, wp_config=wp_config, environ={}, secrets=SecretRegistry()
    )
    with pytest.raises(CredentialsError) as caught:
        source.resolve()
    assert caught.value.reason == "RuntimeError"
    assert caught.value.source == source.source
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_source_bound_to_another_registry_is_rewrapped(wp_config: object) -> None:
    from l3io.wp.backup.adapters.database import WpDatabase

    first, second = SecretRegistry(), SecretRegistry()
    source = wordpress_source(None, wp_config=wp_config, environ={}, secrets=first)
    database = WpDatabase(source, secrets=second)
    database._credentials.resolve()  # noqa: SLF001 - what dump would call
    assert first.redact(WP_PASSWORD) == REDACTED
    assert second.redact(WP_PASSWORD) == REDACTED


def test_the_admin_password_variable_supplies_the_password_not_argv() -> None:
    # RT-S02-002: the help promises L3IO_WP_BACKUP_ADMIN_PASSWORD; it must be read.
    spec = admin_spec(
        username="admin",
        password=None,
        aws_secret_id=None,
        aws_region=None,
        environ={"L3IO_WP_BACKUP_ADMIN_PASSWORD": PASSWORD},
    )
    assert spec == ExplicitCredentialSpec(username="admin", password=PASSWORD)


def test_the_admin_flag_wins_over_the_variable() -> None:
    spec = admin_spec(
        username="admin",
        password=PASSWORD,
        aws_secret_id=None,
        aws_region=None,
        environ={"L3IO_WP_BACKUP_ADMIN_PASSWORD": "not-" + PASSWORD},
    )
    assert spec == ExplicitCredentialSpec(username="admin", password=PASSWORD)
