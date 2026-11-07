"""``domain/secret.Secret`` equality."""

from __future__ import annotations

from l3io.wp.backup.domain.secret import Secret


def test_a_lone_surrogate_compares_without_raising() -> None:
    lone = "\ud800-sentinel-5a-secret"
    assert Secret(lone) == Secret(lone)
    assert Secret(lone) != Secret("\udc00-sentinel-5a-secret")
    assert Secret(lone) != Secret("sentinel-5a-secret")
