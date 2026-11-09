"""BL-E003-032 — credential hygiene with every secret on the database side.

Review F-11 (E003-S01-001): the hygiene cases never put a secret where the
database restore and the cutover use it. Here a real restore over the CI
database holds three unique sentinels on that side at once:

* the Archive's own ``wp-config.php`` ``DB_PASSWORD`` (and the target's, the
  live account's password: ``credential_sentinel.password``);
* the administrator's password (``admin=``, ``credential_sentinel.admin_password``);
* the relocated account's password (``RelocationFlags.new_db_password``,
  ``credential_sentinel.relocated_password``), which the cutover writes.

It is driven through ``api.restore`` — which composes through
``drive_restore`` — with the SDK tracer installed, so the surfaces are every
log record, stdout/stderr, the outcome's ``repr``, each span's name,
attributes, events and status, and (in the failing case) the exception's
text, ``repr`` and traceback. A second case fails on the database side (the
administrator is rejected) with the same secrets planted.

Uniquely named throwaway databases and accounts; needs the CI database and
skips when ``DB_HOST`` is unset.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("l3io.wp.database")
pytest.importorskip("pymysql")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from l3io.wp.backup import api
from l3io.wp.backup.cli.restore_request import RelocationFlags
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.errors import BackupError
from l3io.wp.backup.domain.restore import (
    ConfigPolicy,
    Override,
    OverrideSet,
    RestoreMode,
    SnapshotPolicy,
)
from tests.conftest import CredentialSentinel
from tests.integration.live_cli import CliSite, admin_account, cli_site
from tests.integration.live_restore import (
    host,
    live_site,
    port,
    read_keys,
    root_connection,
    sha,
)
from tests.otel_sdk import otel_spans

__all__ = ["admin_account", "cli_site", "live_site", "otel_spans"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "DB_HOST" not in os.environ, reason="needs the CI database (DB_HOST unset)"
    ),
]

MODE = RestoreMode.DATABASE
WAIVED = OverrideSet(waived=frozenset({Override.NONEMPTY_DATABASE}))


def _relocation(site: CliSite, password: str) -> tuple[RelocationFlags, str, str]:
    new_name = f"{site.site.database}_hyg"
    new_user = f"wph_{uuid.uuid4().hex[:10]}"
    site.site.extra_users.append(new_user)
    flags = RelocationFlags(
        new_site_url=None,
        new_home_url=None,
        new_db_host=f"{host()}:{port()}",
        new_db_name=new_name,
        new_db_user=new_user,
        new_db_password=password,
        wp_config=None,
    )
    return flags, new_name, new_user


def _spans(sentinel: CredentialSentinel, spans: InMemorySpanExporter) -> None:
    finished = spans.get_finished_spans()
    assert finished, "no span was exported: the span surface was not checked"
    for span in finished:
        sentinel.add(f"span {span.name}: name", span.name)
        sentinel.add_repr(f"span {span.name}: attributes", dict(span.attributes or {}))
        sentinel.add_repr(
            f"span {span.name}: events",
            [(e.name, dict(e.attributes or {})) for e in span.events],
        )
        sentinel.add_repr(
            f"span {span.name}: status",
            (span.status.status_code, span.status.description),
        )


def _restore(site: CliSite, admin_user: str, flags: RelocationFlags) -> object:
    return api.restore(
        site.root,
        archive=site.archive(),
        mode=MODE,
        overrides=WAIVED,
        snapshot=SnapshotPolicy.TAKE,
        config_policy=ConfigPolicy.KEEP,
        snapshot_dir=site.snapshots,
        admin=ExplicitCredentialSpec(admin_user, site.admin_password),
        relocation=flags,
    )


def test_a_relocating_restore_emits_no_database_side_secret(
    cli_site: CliSite,
    credential_sentinel: CredentialSentinel,
    otel_spans: InMemorySpanExporter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    s = credential_sentinel
    flags, new_name, new_user = _relocation(cli_site, s.relocated_password)
    outcome = _restore(cli_site, cli_site.admin_user, flags)
    # The secrets were used, not merely carried: the cutover wrote the new
    # account into wp-config.php and the Dump loaded into the new database.
    keys = read_keys(cli_site.site.config)
    assert keys["DB_NAME"] == new_name
    assert keys["DB_USER"] == new_user
    # Compared by digest: a failing assertion must not print the sentinel.
    assert sha(keys["DB_PASSWORD"]) == sha(s.relocated_password)
    with root_connection(new_name).cursor() as cursor:
        cursor.execute("SHOW TABLES")
        assert "wp_restored" in {row[0] for row in cursor.fetchall()}
    captured = capsys.readouterr()
    s.add("stdout", captured.out)
    s.add("stderr", captured.err)
    s.add_repr("outcome", outcome)
    _spans(s, otel_spans)
    s.assert_clean()


def test_a_restore_failing_on_the_database_side_emits_no_secret(
    cli_site: CliSite,
    credential_sentinel: CredentialSentinel,
    otel_spans: InMemorySpanExporter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The administrator is rejected (no such account), with the secrets planted."""
    s = credential_sentinel
    flags, _, _ = _relocation(cli_site, s.relocated_password)
    before = cli_site.site.config.read_text(encoding="utf-8")
    with pytest.raises(BackupError) as caught:
        _restore(cli_site, f"wpnobody_{uuid.uuid4().hex[:8]}", flags)
    assert cli_site.site.config.read_text(encoding="utf-8") == before
    captured = capsys.readouterr()
    s.add("stdout", captured.out)
    s.add("stderr", captured.err)
    s.add_exception("restore error", caught.value)
    _spans(s, otel_spans)
    s.assert_clean()
