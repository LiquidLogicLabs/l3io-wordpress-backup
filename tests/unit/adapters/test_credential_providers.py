"""FR-14/AD-9 — ``adapters/credential_providers.py``'s two chain builders.

Database's real source classes over a temporary ``wp-config.php``; AWS is
stubbed by patching ``boto3.client`` with a ``botocore`` ``Stubber`` client,
never a call to AWS. The precedence cases are derived from the domain tuples.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

wpdb = pytest.importorskip("l3io.wp.database")

from l3io.wp.database import CredentialSource, ResolvedCredentials  # noqa: E402

from l3io.wp.backup.adapters import credential_providers  # noqa: E402
from l3io.wp.backup.adapters.credential_providers import (  # noqa: E402
    ADMIN_PASSWORD_VAR,
    ADMIN_USER_VAR,
    WORDPRESS_PASSWORD_VAR,
    WORDPRESS_USER_VAR,
    admin_credentials,
    wordpress_credentials,
)
from l3io.wp.backup.adapters.database import (  # noqa: E402
    WpDatabase,
    admin_source,
    require_wp_config_credentials,
    wordpress_source,
)
from l3io.wp.backup.adapters.redaction import SecretRegistry  # noqa: E402
from l3io.wp.backup.domain.credentials import (  # noqa: E402
    ADMIN_CREDENTIAL_PRECEDENCE,
    WORDPRESS_CREDENTIAL_PRECEDENCE,
    AwsSecretCredentialSpec,
    CredentialOrigin,
    CredentialSpec,
    ExplicitCredentialSpec,
)
from l3io.wp.backup.domain.database import DatabaseLocation  # noqa: E402
from l3io.wp.backup.domain.errors import (  # noqa: E402
    CredentialsError,
    MissingExtraError,
)
from tests.conftest import CredentialSentinel  # noqa: E402

MODULE = Path(credential_providers.__file__)
ALL_VARS = (
    WORDPRESS_USER_VAR,
    WORDPRESS_PASSWORD_VAR,
    ADMIN_USER_VAR,
    ADMIN_PASSWORD_VAR,
)
SECRET_ID = "prod/wordpress-db"
AWS_LABEL = f"aws-secrets-manager:{SECRET_ID}"
WP_ENV_LABEL = f"environment:{WORDPRESS_USER_VAR},{WORDPRESS_PASSWORD_VAR}"
ADMIN_ENV_LABEL = f"environment:{ADMIN_USER_VAR},{ADMIN_PASSWORD_VAR}"
LOCATION = DatabaseLocation(host="db.invalid", port=3306, name="wptest")


class SpyRegistry(SecretRegistry):
    """Records each registration, in order."""

    __slots__ = ("seen",)

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    def register(self, value: str, *, source: str = "unspecified") -> None:
        self.seen.append(value)
        super().register(value, source=source)


class Recording:
    """A ``CredentialSource`` stand-in counting its ``resolve()`` calls."""

    def __init__(self, label: str, *, fail: bool = False) -> None:
        self.label = label
        self.fail = fail
        self.calls = 0

    @property
    def source(self) -> str:
        return self.label

    def resolve(self) -> ResolvedCredentials:
        self.calls += 1
        if self.fail:
            msg = "this source does I/O"
            raise AssertionError(msg)
        return wpdb.ResolvedCredentials("wp-user", "wp-config-value", self.label)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def wp_config(tmp_path: Path) -> CredentialSource:
    config = tmp_path / "wp-config.php"
    config.write_text(
        "<?php\ndefine('DB_NAME', 'w');\ndefine('DB_USER', 'wp-user');\n"
        "define('DB_PASSWORD', 'wp-config-value');\ndefine('DB_HOST', 'h');\n",
        encoding="utf-8",
    )
    return wpdb.WpConfigFile(config)


def _environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def _set_pair(
    monkeypatch: pytest.MonkeyPatch, user_var: str, password_var: str
) -> None:
    monkeypatch.setenv(user_var, "env-user")
    monkeypatch.setenv(password_var, "env-value")


# --------------------------------------------------------------------------
# Precedence, derived from the domain tuples
# --------------------------------------------------------------------------

#: How each origin is made present. Keyed by every origin the domain declares,
#: so a new origin without a case fails ``test_every_origin_has_a_case``.
WORDPRESS_CASES: dict[CredentialOrigin, str] = {
    CredentialOrigin.EXPLICIT: "explicit",
    CredentialOrigin.ENVIRONMENT: WP_ENV_LABEL,
    CredentialOrigin.WP_CONFIG: "wp-config:",
}
ADMIN_CASES: dict[CredentialOrigin, str] = {
    CredentialOrigin.EXPLICIT: "explicit",
    CredentialOrigin.ENVIRONMENT: ADMIN_ENV_LABEL,
}


def test_every_origin_has_a_case() -> None:
    assert set(WORDPRESS_CASES) == set(WORDPRESS_CREDENTIAL_PRECEDENCE)
    assert set(ADMIN_CASES) == set(ADMIN_CREDENTIAL_PRECEDENCE)
    assert CredentialOrigin.WP_CONFIG not in ADMIN_CREDENTIAL_PRECEDENCE


@pytest.mark.parametrize("winner", range(len(WORDPRESS_CREDENTIAL_PRECEDENCE)))
def test_each_wordpress_step_wins_when_the_earlier_ones_are_absent(
    winner: int, wp_config: CredentialSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    present = WORDPRESS_CREDENTIAL_PRECEDENCE[winner:]
    spec = (
        ExplicitCredentialSpec("explicit-user", "explicit-value")
        if CredentialOrigin.EXPLICIT in present
        else None
    )
    if CredentialOrigin.ENVIRONMENT in present:
        _set_pair(monkeypatch, WORDPRESS_USER_VAR, WORDPRESS_PASSWORD_VAR)
    chain = wordpress_credentials(
        spec, wp_config=wp_config, environ=_environ(), secrets=SecretRegistry()
    )
    resolved = chain.resolve()
    assert resolved.source.startswith(
        WORDPRESS_CASES[WORDPRESS_CREDENTIAL_PRECEDENCE[winner]]
    )


@pytest.mark.parametrize("winner", range(len(ADMIN_CREDENTIAL_PRECEDENCE)))
def test_each_admin_step_wins_when_the_earlier_ones_are_absent(
    winner: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    present = ADMIN_CREDENTIAL_PRECEDENCE[winner:]
    spec = (
        ExplicitCredentialSpec("admin", "explicit-value")
        if CredentialOrigin.EXPLICIT in present
        else None
    )
    if CredentialOrigin.ENVIRONMENT in present:
        _set_pair(monkeypatch, ADMIN_USER_VAR, ADMIN_PASSWORD_VAR)
    chain = admin_credentials(spec, environ=_environ(), secrets=SecretRegistry())
    assert chain is not None
    assert chain.resolve().source == ADMIN_CASES[ADMIN_CREDENTIAL_PRECEDENCE[winner]]


def test_the_wordpress_chain_orders_every_step_by_the_domain_tuple(
    wp_config: CredentialSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pair(monkeypatch, WORDPRESS_USER_VAR, WORDPRESS_PASSWORD_VAR)
    spec = AwsSecretCredentialSpec(SECRET_ID, "eu-west-1")
    chain = wordpress_credentials(
        spec, wp_config=wp_config, environ=_environ(), secrets=SecretRegistry()
    )
    assert chain.source == f"chain({AWS_LABEL}, {WP_ENV_LABEL}, {wp_config.source})"


# --------------------------------------------------------------------------
# Failure stops the chain
# --------------------------------------------------------------------------


@pytest.mark.parametrize("set_var", [WORDPRESS_USER_VAR, WORDPRESS_PASSWORD_VAR])
def test_a_half_set_pair_stops_the_chain_before_wp_config(
    set_var: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = f"half-pair-{uuid.uuid4().hex}"
    monkeypatch.setenv(set_var, planted)
    recording = Recording("wp-config:/srv/site/wp-config.php")
    source = wordpress_source(
        None, wp_config=recording, environ=_environ(), secrets=SecretRegistry()
    )
    with pytest.raises(CredentialsError) as caught:
        require_wp_config_credentials(source)
    assert recording.calls == 0
    other = {WORDPRESS_USER_VAR, WORDPRESS_PASSWORD_VAR} - {set_var}
    assert caught.value.missing_key == other.pop()
    assert planted not in str(caught.value)


def test_no_wordpress_source_with_an_answer_names_every_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "wp-config.php"
    config.write_text("<?php\ndefine('DB_NAME', 'w');\n", encoding="utf-8")
    source = wordpress_source(
        None,
        wp_config=wpdb.WpConfigFile(config),
        environ=_environ(),
        secrets=SecretRegistry(),
    )
    with pytest.raises(CredentialsError) as caught:
        require_wp_config_credentials(source)
    assert caught.value.source == source.source
    assert "defines neither" in caught.value.reason
    # The variables that would also have answered are named (review F-6).
    assert WORDPRESS_USER_VAR in caught.value.reason
    assert WORDPRESS_PASSWORD_VAR in caught.value.reason


@pytest.mark.parametrize("set_var", [ADMIN_USER_VAR, ADMIN_PASSWORD_VAR])
def test_a_half_set_admin_pair_is_a_failure_not_absence(
    set_var: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(set_var, f"half-pair-{uuid.uuid4().hex}")
    chain = admin_credentials(None, environ=_environ(), secrets=SecretRegistry())
    assert chain is not None
    with pytest.raises(wpdb.CredentialsUnobtainableError):
        chain.resolve()


# --------------------------------------------------------------------------
# The administrator builder, every combination
# --------------------------------------------------------------------------

_SPECS: dict[str, CredentialSpec | None] = {
    "none": None,
    "explicit": ExplicitCredentialSpec("admin", "explicit-value"),
    "aws": AwsSecretCredentialSpec(SECRET_ID, "eu-west-1"),
}
_ENVS: dict[str, tuple[str, ...]] = {
    "unset": (),
    "half-user": (ADMIN_USER_VAR,),
    "half-password": (ADMIN_PASSWORD_VAR,),
    "set": (ADMIN_USER_VAR, ADMIN_PASSWORD_VAR),
}
_SPEC_LABEL = {"none": None, "explicit": "explicit", "aws": AWS_LABEL}


@pytest.mark.parametrize("env", list(_ENVS))
@pytest.mark.parametrize("kind", list(_SPECS))
def test_the_admin_builder_over_every_combination(
    kind: str, env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in _ENVS[env]:
        monkeypatch.setenv(name, "x")
    # The WordPress account's variables are set too: they must never appear.
    _set_pair(monkeypatch, WORDPRESS_USER_VAR, WORDPRESS_PASSWORD_VAR)
    chain = admin_credentials(
        _SPECS[kind], environ=_environ(), secrets=SecretRegistry()
    )
    expected = tuple(
        label
        for label in (_SPEC_LABEL[kind], ADMIN_ENV_LABEL if _ENVS[env] else None)
        if label is not None
    )
    if not expected:
        assert chain is None
        return
    assert chain is not None
    assert chain.source == f"chain({', '.join(expected)})"
    assert "wp-config" not in chain.source
    assert WORDPRESS_USER_VAR not in chain.source


# --------------------------------------------------------------------------
# AD-9: building is not resolving
# --------------------------------------------------------------------------


def test_the_builders_resolve_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("boto3")
    _set_pair(monkeypatch, WORDPRESS_USER_VAR, WORDPRESS_PASSWORD_VAR)
    _set_pair(monkeypatch, ADMIN_USER_VAR, ADMIN_PASSWORD_VAR)
    raising = Recording("wp-config:/srv/site/wp-config.php", fail=True)
    calls: list[str] = []

    def boto_client(*args: object, **kwargs: object) -> object:
        calls.append("boto3.client")
        raise AssertionError

    monkeypatch.setattr("boto3.client", boto_client)
    aws = AwsSecretCredentialSpec(SECRET_ID, "eu-west-1")
    wordpress = wordpress_source(
        aws, wp_config=raising, environ=_environ(), secrets=SecretRegistry()
    )
    admin = admin_source(aws, environ=_environ(), secrets=SecretRegistry())
    assert admin is not None
    # Reading the labels does no I/O either.
    assert raising.label in wordpress.source
    assert ADMIN_ENV_LABEL in admin.source
    assert raising.calls == 0
    assert calls == []


def test_each_builder_logs_its_chain_by_label(
    wp_config: CredentialSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pair(monkeypatch, WORDPRESS_USER_VAR, WORDPRESS_PASSWORD_VAR)
    with capture_logs() as logs:
        wordpress_credentials(
            None, wp_config=wp_config, environ=_environ(), secrets=SecretRegistry()
        )
        admin_credentials(None, environ=_environ(), secrets=SecretRegistry())
    built = [e for e in logs if e["event"] == "credential_chain_built"]
    assert built[0]["role"] == "wordpress"
    assert built[0]["sources"] == (WP_ENV_LABEL, wp_config.source)
    assert built[1] == {
        "event": "credential_chain_built",
        "log_level": "debug",
        "role": "administrator",
        "sources": (),
        "none_configured": True,
    }
    assert "env-value" not in repr(logs)


# --------------------------------------------------------------------------
# Registration, through adapters/database.py
# --------------------------------------------------------------------------


def test_an_explicit_password_is_registered_once_at_construction(
    wp_config: CredentialSource,
) -> None:
    for build in (
        lambda s, r: wordpress_source(s, wp_config=wp_config, environ={}, secrets=r),
        lambda s, r: admin_source(s, environ={}, secrets=r),
    ):
        registry = SpyRegistry()
        build(ExplicitCredentialSpec("u", "explicit-value"), registry)
        assert registry.seen == ["explicit-value"]


def test_an_environment_password_is_registered_when_resolved(
    wp_config: CredentialSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pair(monkeypatch, WORDPRESS_USER_VAR, WORDPRESS_PASSWORD_VAR)
    _set_pair(monkeypatch, ADMIN_USER_VAR, ADMIN_PASSWORD_VAR)
    registry = SpyRegistry()
    wordpress = wordpress_source(
        None, wp_config=wp_config, environ=_environ(), secrets=registry
    )
    admin = admin_source(None, environ=_environ(), secrets=registry)
    assert admin is not None
    assert registry.seen == []
    with capture_logs() as logs:
        assert wordpress.resolve().password == "env-value"
        assert admin.resolve().password == "env-value"
    assert registry.seen == ["env-value", "env-value"]
    selected = [e for e in logs if e["event"] == "credential_source_selected"]
    assert [e["credential_source"] for e in selected] == [WP_ENV_LABEL, ADMIN_ENV_LABEL]
    assert selected[0]["log_level"] == "info"
    assert "env-value" not in repr(logs)


def test_a_wp_config_password_is_registered_when_resolved(
    wp_config: CredentialSource,
) -> None:
    registry = SpyRegistry()
    source = wordpress_source(None, wp_config=wp_config, environ={}, secrets=registry)
    assert source.resolve().password == "wp-config-value"
    assert registry.seen == ["wp-config-value"]


# --------------------------------------------------------------------------
# AWS, stubbed below boto3.client
# --------------------------------------------------------------------------


@pytest.fixture
def stub_aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Patch ``boto3.client`` to a Secrets Manager client with a Stubber."""
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber

    real_client = boto3.client
    stubbers: list[Stubber] = []

    def arm(*, payload: str | None = None, error: str | None = None) -> None:
        client = real_client(
            "secretsmanager",
            region_name="eu-west-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        stubber = Stubber(client)
        if error is not None:
            stubber.add_client_error("get_secret_value", service_error_code=error)
        else:
            stubber.add_response(
                "get_secret_value",
                {"SecretString": payload, "Name": SECRET_ID},
                {"SecretId": SECRET_ID},
            )
        stubber.activate()
        stubbers.append(stubber)
        monkeypatch.setattr(boto3, "client", lambda *a, **k: client)

    yield arm
    for stubber in stubbers:
        stubber.deactivate()


def _aws_source(
    registry: SecretRegistry, wp_config: CredentialSource
) -> CredentialSource:
    return wordpress_source(
        AwsSecretCredentialSpec(SECRET_ID, "eu-west-1"),
        wp_config=wp_config,
        environ={},
        secrets=registry,
    )


def test_a_found_aws_secret_wins_and_is_registered(
    stub_aws: Callable[..., None], wp_config: CredentialSource
) -> None:
    stub_aws(payload=json.dumps({"username": "aws-user", "password": "aws-value"}))
    registry = SpyRegistry()
    resolved = _aws_source(registry, wp_config).resolve()
    assert (resolved.username, resolved.source) == ("aws-user", AWS_LABEL)
    assert registry.seen == ["aws-value"]


@pytest.mark.parametrize(
    "arming",
    [
        {"payload": "not json"},
        {"payload": json.dumps({"username": "aws-user"})},
        {"error": "AccessDeniedException"},
    ],
    ids=["malformed", "no-password", "access-denied"],
)
def test_an_unusable_aws_secret_stops_the_chain(
    arming: dict[str, str],
    stub_aws: Callable[..., None],
    tmp_path: Path,
) -> None:
    stub_aws(**arming)
    recording = Recording("wp-config:/srv/site/wp-config.php")
    source = _aws_source(SecretRegistry(), recording)
    with pytest.raises(wpdb.CredentialsUnobtainableError) as caught:
        source.resolve()
    assert recording.calls == 0
    assert SECRET_ID in str(caught.value)


def test_boto3_absent_is_the_aws_extra_from_the_dump_handler(
    wp_config: CredentialSource, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "boto3", None)
    database = WpDatabase(
        _aws_source(SecretRegistry(), wp_config), secrets=SecretRegistry()
    )
    with pytest.raises(MissingExtraError) as caught:
        database.dump_to(LOCATION, tmp_path / "dump.sql")
    assert caught.value.extra == "aws"


@pytest.fixture
def no_aws_region(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pytest.importorskip("boto3")
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-creds"))


@pytest.mark.usefixtures("no_aws_region")
def test_no_aws_region_names_the_db_region_flag_from_the_dump_handler(
    wp_config: CredentialSource, tmp_path: Path
) -> None:
    source = wordpress_source(
        AwsSecretCredentialSpec(SECRET_ID, None),
        wp_config=wp_config,
        environ={},
        secrets=SecretRegistry(),
    )
    database = WpDatabase(source, secrets=SecretRegistry())
    with pytest.raises(CredentialsError) as caught:
        database.dump_to(LOCATION, tmp_path / "dump.sql")
    assert "--db-credentials-aws-region" in caught.value.reason
    assert AWS_LABEL in caught.value.source


@pytest.mark.usefixtures("no_aws_region")
def test_no_aws_region_names_the_admin_region_flag_from_the_survey_handler(
    wp_config: CredentialSource,
) -> None:
    admin = admin_source(
        AwsSecretCredentialSpec(SECRET_ID, None), environ={}, secrets=SecretRegistry()
    )
    wordpress = wordpress_source(
        None, wp_config=wp_config, environ={}, secrets=SecretRegistry()
    )
    database = WpDatabase(wordpress, secrets=SecretRegistry(), admin=admin)
    with pytest.raises(CredentialsError) as caught:
        database.survey_target(LOCATION, table_prefix="wp_", wordpress_user="u")
    assert "--admin-credentials-aws-region" in caught.value.reason


# --------------------------------------------------------------------------
# No sentinel reaches any surface
# --------------------------------------------------------------------------


def test_environment_passwords_reach_no_surface(
    credential_sentinel: CredentialSentinel,
) -> None:
    s = credential_sentinel
    s.monkeypatch.setenv(WORDPRESS_USER_VAR, "env-user")
    s.monkeypatch.setenv(WORDPRESS_PASSWORD_VAR, s.password)
    s.monkeypatch.setenv(ADMIN_USER_VAR, "env-admin")
    s.monkeypatch.setenv(ADMIN_PASSWORD_VAR, s.admin_password)
    registry = SecretRegistry()
    wordpress = wordpress_source(
        None,
        wp_config=wpdb.WpConfigFile(s.config),
        environ=_environ(),
        secrets=registry,
    )
    admin = admin_source(None, environ=_environ(), secrets=registry)
    assert admin is not None
    for name, source in (("wordpress", wordpress), ("admin", admin)):
        s.add_repr(f"{name} source", source)
        s.add_repr(f"{name} resolved", source.resolve())
        s.add(f"{name} label", source.source)
    assert registry.redact(s.password) != s.password
    assert registry.redact(s.admin_password) != s.admin_password
    structlog.get_logger("probe").info("probe", value=registry.redact(s.password))
    for name in (WORDPRESS_USER_VAR, WORDPRESS_PASSWORD_VAR, ADMIN_USER_VAR):
        s.monkeypatch.delenv(name)
    s.monkeypatch.delenv(ADMIN_PASSWORD_VAR)
    s.assert_clean()


# --------------------------------------------------------------------------
# The module itself: AD-8, AD-1, the repr contract
# --------------------------------------------------------------------------


def _tree() -> ast.Module:
    return ast.parse(MODULE.read_text(encoding="utf-8"))


def _database_imports(tree: ast.Module) -> list[tuple[ast.AST, list[ast.AST]]]:
    """Each import of ``l3io.wp.database``, with its ancestors."""
    found: list[tuple[ast.AST, list[ast.AST]]] = []

    def walk(node: ast.AST, ancestors: list[ast.AST]) -> None:
        for child in ast.iter_child_nodes(node):
            names: list[str] = []
            if isinstance(child, ast.Import):
                names = [a.name for a in child.names]
            elif isinstance(child, ast.ImportFrom):
                module = child.module or ""
                names = [module, *(f"{module}.{a.name}" for a in child.names)]
            if any(
                n == "l3io.wp.database" or n.startswith("l3io.wp.database.")
                for n in names
            ):
                found.append((child, ancestors))
            walk(child, [*ancestors, child])

    walk(tree, [])
    return found


def _guarded(ancestors: list[ast.AST]) -> bool:
    for node in ancestors:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return True
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            return True
    return False


def test_database_is_imported_only_inside_functions_or_type_checking() -> None:
    imports = _database_imports(_tree())
    assert imports, "the scan found no import of l3io.wp.database at all"
    unguarded = [node.lineno for node, ancestors in imports if not _guarded(ancestors)]  # type: ignore[attr-defined]
    assert unguarded == []


def test_the_import_guard_sees_a_module_scope_import() -> None:
    planted = ast.parse(
        "from l3io.wp.database import CredentialChain\n"
        "def f():\n    from l3io.wp import database\n"
    )
    imports = _database_imports(planted)
    assert [_guarded(a) for _, a in imports] == [False, True]


def test_the_module_imports_without_the_database_extra(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import importlib.abc, sys

        class Refuse(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name == "l3io.wp.database" or name.startswith("l3io.wp.database."):
                    raise ImportError(name)
                return None

        sys.meta_path.insert(0, Refuse())
        import l3io.wp.backup.adapters.credential_providers as m
        assert "l3io.wp.database" not in sys.modules
        print(m.WORDPRESS_USER_VAR)
        """
    )
    done = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == WORDPRESS_USER_VAR


def test_the_module_defines_no_class() -> None:
    # The walked __repr__ contract covers classes holding a CredentialSource or
    # a password; this module has none, so it adds no repr to check.
    classes = [n.name for n in ast.walk(_tree()) if isinstance(n, ast.ClassDef)]
    assert classes == []


# --------------------------------------------------------------------------
# Review fixes: restore's WordPress account (F-1), the install hint (F-2)
# --------------------------------------------------------------------------


def test_a_restore_keeps_wp_configs_account_when_the_variables_are_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The restored account is the one wp-config.php names: the variables are a
    # backup's override and must not choose another account to provision.
    from tests.restore_root_rig import Rig

    planted = f"restore-env-{uuid.uuid4().hex}"
    monkeypatch.setenv(WORDPRESS_USER_VAR, "env-user")
    monkeypatch.setenv(WORDPRESS_PASSWORD_VAR, planted)
    rig = Rig(tmp_path, monkeypatch)
    rig.drive()
    source = rig.adapter_calls[-1]["wordpress_source"]
    assert "environment:" not in source.source
    resolved = source.resolve()
    assert resolved.source.startswith("wp-config:")
    assert resolved.password != planted


def test_a_missing_database_dependency_names_this_packages_extra(
    wp_config: CredentialSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    from l3io.wp.backup.adapters.database import site_descriptor_from_wp_config

    monkeypatch.setitem(sys.modules, "l3io.wp.config", None)
    config = Path(wp_config.source.removeprefix("wp-config:"))
    with pytest.raises(MissingExtraError) as caught:
        site_descriptor_from_wp_config(config.parent, config_file=config)
    assert caught.value.extra == "database"


def test_every_database_extra_maps_to_an_extra_this_package_offers() -> None:
    from importlib.metadata import metadata

    from l3io.wp.backup.adapters import database

    ours = set(metadata("l3io-wordpress-backup").get_all("Provides-Extra") or ())
    theirs = set(metadata("l3io-wordpress-database").get_all("Provides-Extra") or ())
    assert theirs, "l3io-wordpress-database declares no extra"
    mapped = {database._backup_extra(extra) for extra in theirs}  # noqa: SLF001
    assert mapped <= ours, sorted(mapped - ours)
    assert database._backup_extra("aws") == "aws"  # noqa: SLF001
