"""AD-24 — with no SDK configured, tracing is inert: no output, no ids, no cost.

Each case runs a full ``api.backup`` (``FakeDatabase`` for the server) in a
fresh interpreter, because OpenTelemetry lets the global provider be set only
once per process and the rest of the suite installs one. The child configures
logging through ``configure_logging(..., trace_processors=(add_trace_ids,))``
— the slot filled, so an absent id is the processor's decision, not a missing
processor — and writes its findings to a file, keeping stdout for the check.

Without an SDK: the provider is the API's proxy, stdout is empty, stderr is
only JSON records, no record carries ``trace_id``, and a spy registry handed
to ``run_span(command_args=...)`` and ``set_attributes(db_name=...)`` is asked
for no redaction (E006 arch gate D7 — the deterministic form of "no
measurable cost"). The same backup under an SDK logs the same
``phase_completed`` records, differing only by ``trace_id`` and ``span_id``.
A provider installed only after the package was imported is honoured: the
tracer is resolved per call.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CHILD = r"""
import json, sys, tempfile
from pathlib import Path

with_sdk = sys.argv[1] == "sdk"
out = Path(sys.argv[2])
work = Path(sys.argv[3])
tempfile.tempdir = str(work)

from opentelemetry import trace
if with_sdk:
    from opentelemetry.sdk.trace import TracerProvider
    trace.set_tracer_provider(TracerProvider())

from l3io.wp.backup import api
from l3io.wp.backup.adapters.redaction import SecretRegistry
from l3io.wp.backup.adapters.tracing import OpenTelemetryTracer
from l3io.wp.backup.cli import adapter_sets
from l3io.wp.backup.domain.backup_scope import BackupScope
from tests.unit.cli.conftest import FakeDatabase

exported = None
if sys.argv[1] == "late":
    # Installed only now, after the package was imported.
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    exported = InMemorySpanExporter()
    late = TracerProvider()
    late.add_span_processor(SimpleSpanProcessor(exported))
    trace.set_tracer_provider(late)

database = FakeDatabase()
adapter_sets.WpDatabase = lambda credentials, **kwargs: database

site = work / "www"
site.mkdir()
(site / "wp-config.php").write_text(
    "<?php\ndefine('DB_NAME', 'wptest');\ndefine('DB_USER', 'wp');\n"
    "define('DB_PASSWORD', 'not-a-real-secret');\ndefine('DB_HOST', 'db.invalid');\n"
    "$table_prefix = 'wp_';\n",
    encoding="utf-8",
)
(site / "index.php").write_text("<?php\n", encoding="utf-8")

api.configure_logging("INFO", "json", trace_processors=(api.add_trace_ids,))
result = api.backup(site, scope=BackupScope.ALL, target=work / "out.tar.gz")

SENTINEL = "sentinel-admin-pw-0123456789abcdef"

class Spy:
    def __init__(self):
        self.inner = SecretRegistry()
        self.inner.register(SENTINEL, source="--admin-password")
        self.calls = []
    def redact(self, text):
        self.calls.append("redact")
        return self.inner.redact(text)
    def redact_argv(self, argv):
        self.calls.append("redact_argv")
        return self.inner.redact_argv(argv)

spy = Spy()
tracer = OpenTelemetryTracer()
with tracer.run_span(
    run_id="r", operation="backup", attributes={},
    command_args=["--admin-password", SENTINEL], secrets=spy,
) as run:
    run.set_attributes(db_name="wptest", archive_bytes=1)
    with tracer.phase_span("DUMP", secrets=spy) as step:
        step.set_outcome(exit_code=0, archive_bytes=1)
    run.set_outcome(exit_code=0, error_type=None)

out.write_text(json.dumps({
    "provider": type(trace.get_tracer_provider()).__name__,
    "performed": result.performed,
    "redactions": spy.calls,
    "spans": [x.name for x in exported.get_finished_spans()] if exported else [],
}), encoding="utf-8")
"""


def _run(tmp_path: Path, mode: str) -> tuple[dict[str, Any], str, str]:
    work = tmp_path / mode
    work.mkdir()
    out = work / "findings.json"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("OTEL_") and k != "L3IO_WP_BACKUP_PASSPHRASE"
    }
    done = subprocess.run(  # noqa: S603 - the test's own interpreter and script
        [sys.executable, "-c", CHILD, mode, str(out), str(work)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(out.read_text(encoding="utf-8")), done.stdout, done.stderr


def _records(stderr: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stderr.splitlines() if line.strip()]


@pytest.fixture(scope="module")
def no_sdk(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[dict[str, Any], str, str]:
    return _run(tmp_path_factory.mktemp("no-sdk"), "none")


@pytest.fixture(scope="module")
def with_sdk(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[dict[str, Any], str, str]:
    return _run(tmp_path_factory.mktemp("sdk"), "sdk")


def test_the_provider_is_the_api_proxy(no_sdk: tuple[dict[str, Any], str, str]) -> None:
    findings, _, _ = no_sdk
    assert findings["provider"] == "ProxyTracerProvider"
    assert findings["performed"] is True


def test_nothing_is_written_beyond_the_log_records(
    no_sdk: tuple[dict[str, Any], str, str],
) -> None:
    _, stdout, stderr = no_sdk
    assert stdout == ""
    records = _records(stderr)  # every stderr line parses as a record
    assert records
    assert all("event" in r for r in records)


def test_no_record_carries_a_trace_id(no_sdk: tuple[dict[str, Any], str, str]) -> None:
    records = _records(no_sdk[2])
    assert [r for r in records if "trace_id" in r or "span_id" in r] == []


def test_no_redaction_is_done_for_a_non_recording_span(
    no_sdk: tuple[dict[str, Any], str, str],
) -> None:
    assert no_sdk[0]["redactions"] == []


def test_the_sdk_run_did_redact_so_the_spy_is_live(
    with_sdk: tuple[dict[str, Any], str, str],
) -> None:
    assert with_sdk[0]["provider"] == "TracerProvider"
    assert "redact_argv" in with_sdk[0]["redactions"]


def test_a_provider_installed_after_import_is_honoured(
    tmp_path: Path,
) -> None:
    findings, _, _ = _run(tmp_path, "late")
    assert findings["spans"].count("l3io.wp.backup.run") == 2  # backup, then direct
    assert "l3io.wp.backup.dump" in findings["spans"]


def _phases(stderr: str) -> list[dict[str, Any]]:
    return [r for r in _records(stderr) if r["event"] == "phase_completed"]


def test_the_phase_records_differ_only_by_the_ids(
    no_sdk: tuple[dict[str, Any], str, str],
    with_sdk: tuple[dict[str, Any], str, str],
) -> None:
    plain, traced = _phases(no_sdk[2]), _phases(with_sdk[2])
    assert plain
    assert [set(t) - set(p) for p, t in zip(plain, traced, strict=True)] == [
        {"trace_id", "span_id"}
    ] * len(plain)
    assert all(set(p) <= set(t) for p, t in zip(plain, traced, strict=True))
    fixed = ("phase", "outcome", "exit_code")
    assert [[p[k] for k in fixed] for p in plain] == [
        [t[k] for k in fixed] for t in traced
    ]
    assert all({"phase", "duration_ms", "exit_code", "bytes"} <= set(p) for p in plain)
