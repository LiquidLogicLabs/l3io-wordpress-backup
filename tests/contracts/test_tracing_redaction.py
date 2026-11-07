"""AD-24/ADR-0018 — a span is as public as a log line: no planted secret reaches one.

With ``credential_sentinel`` planted as the database password (in
``wp-config.php``), as the passphrase (``L3IO_WP_BACKUP_PASSPHRASE``) and as
the administrator password of a restore, and with the host process's
``sys.argv`` carrying the sentinel after ``--admin-password``, every span the
API exports is searched — name, attributes, events, status description — and
the sentinel is in none. The API places no command line on a span at all
(``command_args=None``; a library call has none of its own, and the host's
argv may carry secrets the Run's registry cannot know).

On the CLI path the no-op tracer is bound: no ``l3io.wp.backup.*`` span is
exported.

Not marked ``integration``: the server is ``FakeDatabase`` and the restore is
the restore root's rig, so the check runs in every tier.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from l3io.wp.backup import api
from l3io.wp.backup.cli import main
from l3io.wp.backup.domain.backup_scope import BackupScope
from l3io.wp.backup.domain.credentials import ExplicitCredentialSpec
from l3io.wp.backup.domain.restore import ConfigPolicy, RestoreMode, SnapshotPolicy
from tests.conftest import CredentialSentinel
from tests.otel_sdk import otel_spans  # noqa: F401 - fixture
from tests.restore_root_rig import ALL_WAIVED, Rig
from tests.unit.cli.conftest import fake_database  # noqa: F401 - fixture

PREFIX = "l3io.wp.backup."
BACKEND_ENV = "L3IO_AGE_PASSPHRASE_BACKEND"

_host = trace.get_tracer("tests.host")


def _texts(span: ReadableSpan) -> list[str]:
    found = [span.name, str(span.status.description)]
    found += [f"{k}={v}" for k, v in (span.attributes or {}).items()]
    for event in span.events:
        found += [
            event.name,
            *(f"{k}={v}" for k, v in (event.attributes or {}).items()),
        ]
    return found


def _carrying(spans: Sequence[ReadableSpan], secret: str) -> list[str]:
    return [s.name for s in spans if any(secret in t for t in _texts(s))]


@pytest.mark.usefixtures("fake_database")
@pytest.mark.parametrize("credential_sentinel", [{"passphrase": True}], indirect=True)
def test_no_planted_secret_reaches_an_api_span(
    credential_sentinel: CredentialSentinel,
    tmp_path: Path,
    otel_spans: InMemorySpanExporter,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = credential_sentinel.secrets
    assert credential_sentinel.passphrase is not None
    monkeypatch.setenv(BACKEND_ENV, "binder")
    monkeypatch.setattr(
        sys,
        "argv",
        ["host-app", "--admin-password", credential_sentinel.admin_password],
    )
    with _host.start_as_current_span("host"):
        api.backup(
            credential_sentinel.site,
            scope=BackupScope.ALL,
            target=tmp_path / "out.tar.gz",
            encrypt=True,
        )
        rig = Rig(tmp_path / "restore", monkeypatch)
        api.restore(
            rig.site_root,
            archive=rig.archive_path,
            mode=RestoreMode.ALL,
            overrides=ALL_WAIVED,
            snapshot=SnapshotPolicy.SKIP,
            config_policy=ConfigPolicy.KEEP,
            admin=ExplicitCredentialSpec("root", credential_sentinel.admin_password),
            allow_unencrypted=True,
            dry_run=True,
        )
    spans = otel_spans.get_finished_spans()
    ours = [s for s in spans if s.name.startswith(PREFIX)]
    assert len([s for s in ours if s.name == PREFIX + "run"]) == 2
    assert [
        s.name for s in ours if "process.command_args" in (s.attributes or {})
    ] == []
    for secret in secrets:
        assert _carrying(spans, secret) == []


def test_the_search_sees_a_planted_secret(
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    secret = "sentinel-search-0123456789abcdef"
    with _host.start_as_current_span(PREFIX + "run") as span:
        span.set_attribute("process.command_args", ["--admin-password", secret])
    with _host.start_as_current_span(PREFIX + "dump") as span:
        span.add_event("x", {"detail": f"near {secret}"})
    assert _carrying(otel_spans.get_finished_spans(), secret) == [
        PREFIX + "run",
        PREFIX + "dump",
    ]


@pytest.mark.usefixtures("fake_database")
def test_the_cli_exports_no_span_of_its_own(
    credential_sentinel: CredentialSentinel,
    tmp_path: Path,
    otel_spans: InMemorySpanExporter,  # noqa: F811
) -> None:
    final = tmp_path / "cli.tar.gz"
    argv = [
        "backup",
        "--wp-dir",
        str(credential_sentinel.site),
        "--archive",
        str(final),
    ]
    with _host.start_as_current_span("host"):
        assert main(argv) == 0
    spans = otel_spans.get_finished_spans()
    assert [s.name for s in spans] == ["host"]
