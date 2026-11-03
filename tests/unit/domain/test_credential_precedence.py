"""AD-9 — the two credential precedence orders and the backup-owned spec."""

from __future__ import annotations

import pytest

from l3io.wp.backup.domain.credentials import (
    ADMIN_CREDENTIAL_PRECEDENCE,
    WORDPRESS_CREDENTIAL_PRECEDENCE,
    AwsSecretCredentialSpec,
    CredentialOrigin,
    ExplicitCredentialSpec,
)


@pytest.mark.parametrize(
    "order", [WORDPRESS_CREDENTIAL_PRECEDENCE, ADMIN_CREDENTIAL_PRECEDENCE]
)
def test_no_origin_appears_twice(order: tuple[CredentialOrigin, ...]) -> None:
    assert len(set(order)) == len(order)


def test_the_administrator_never_falls_back_to_wp_config() -> None:
    assert CredentialOrigin.WP_CONFIG not in ADMIN_CREDENTIAL_PRECEDENCE


def test_the_wordpress_order_ends_with_wp_config_and_covers_every_origin() -> None:
    assert WORDPRESS_CREDENTIAL_PRECEDENCE[-1] is CredentialOrigin.WP_CONFIG
    assert set(WORDPRESS_CREDENTIAL_PRECEDENCE) == set(CredentialOrigin)


def test_both_orders_start_with_an_explicit_source() -> None:
    assert WORDPRESS_CREDENTIAL_PRECEDENCE[0] is CredentialOrigin.EXPLICIT
    assert ADMIN_CREDENTIAL_PRECEDENCE[0] is CredentialOrigin.EXPLICIT


def test_the_explicit_spec_repr_hides_the_password() -> None:
    spec = ExplicitCredentialSpec(username="admin", password="hunter2-long")
    assert "hunter2-long" not in repr(spec)
    assert "admin" in repr(spec)


def test_the_aws_spec_holds_a_name_not_a_secret() -> None:
    spec = AwsSecretCredentialSpec(secret_id="prod/db", region=None)
    assert spec.region is None
