"""ADR-0018 — ``SecretRegistry``, the one redaction implementation."""

from __future__ import annotations

import json
import logging
import pickle
import threading
from collections import namedtuple

import pytest
import structlog
from structlog.testing import capture_logs

from l3io.wp.backup.adapters.redaction import REDACTED, SecretRegistry

SECRET = "s3cret-value"


@pytest.fixture
def registry() -> SecretRegistry:
    registry = SecretRegistry()
    registry.register(SECRET, source="test")
    return registry


def test_redact_replaces_every_occurrence(registry: SecretRegistry) -> None:
    assert registry.redact(f"a {SECRET} b {SECRET}") == f"a {REDACTED} b {REDACTED}"
    assert registry.redact("nothing here") == "nothing here"


def test_an_empty_registry_returns_text_unchanged() -> None:
    assert SecretRegistry().redact("abc***") == "abc***"


def test_the_longer_of_two_overlapping_secrets_is_redacted_whole() -> None:
    registry = SecretRegistry()
    registry.register("pass", source="short")
    registry.register("password-long", source="long")
    assert registry.redact("x password-long y pass") == f"x {REDACTED} y {REDACTED}"


def test_overlapping_secrets_leave_no_fragment() -> None:
    registry = SecretRegistry()
    registry.register("abcd", source="a")
    registry.register("cdefgh", source="b")
    assert registry.redact("x abcdefgh y") == f"x {REDACTED} y"
    assert registry.redact("abcd cdefgh") == f"{REDACTED} {REDACTED}"


@pytest.mark.parametrize("value", ["pa'ss\\word", 'pa"ss', "pä\nss-word"])
def test_escaped_spellings_are_redacted(value: str) -> None:
    registry = SecretRegistry()
    registry.register(value, source="t")
    multi = ValueError("context", value)  # str() is the args tuple's repr
    texts = [str(multi), f"{value!r}", json.dumps({"p": value}), value]
    assert all(value not in registry.redact(t) for t in texts)
    assert all(REDACTED in registry.redact(t) for t in texts)
    out = registry.redact_event(None, "e", {"error": multi})
    assert REDACTED in out["error"]
    assert len(registry) == 1


def test_regex_metacharacters_are_literal() -> None:
    registry = SecretRegistry()
    registry.register("a.b*c(", source="t")
    assert registry.redact("aXbbc( a.b*c(") == f"aXbbc( {REDACTED}"


def test_redact_argv(registry: SecretRegistry) -> None:
    assert registry.redact_argv(["tool", f"--password={SECRET}"]) == [
        "tool",
        f"--password={REDACTED}",
    ]


@pytest.mark.parametrize("value", ["", "abc"])
def test_a_short_secret_is_refused_with_a_warning_naming_only_the_source(
    value: str,
) -> None:
    registry = SecretRegistry()
    with capture_logs() as logs:
        registry.register(value, source="wp-config:/x")
    assert len(registry) == 0
    assert logs == [
        {
            "event": "secret_too_short_to_redact",
            "source": "wp-config:/x",
            "log_level": "warning",
        }
    ]


def test_registering_twice_counts_once(registry: SecretRegistry) -> None:
    registry.register(SECRET, source="again")
    assert len(registry) == 1


def test_redact_event_walks_nested_structures(registry: SecretRegistry) -> None:
    point = namedtuple("point", "x y")
    event = {
        "event": f"connect {SECRET}",
        SECRET: "as a key",
        "nested": {"list": [SECRET, 1], "tuple": (SECRET,), "set": {SECRET}},
        "named": point(SECRET, 2),
        "number": 3,
        "none": None,
    }
    out = registry.redact_event(None, "info", event)
    assert SECRET not in repr(out)
    assert out["nested"]["tuple"] == (REDACTED,)
    assert out["nested"]["set"] == {REDACTED}
    assert out["named"] == [REDACTED, 2]
    assert out["number"] == 3
    assert out["none"] is None


def test_redact_event_redacts_exception_text(registry: SecretRegistry) -> None:
    try:
        raise ValueError(SECRET)  # noqa: TRY301
    except ValueError as exc:
        caught = exc
        event = {"event": "failed", "exc_info": exc, "error": exc}
    out = registry.redact_event(None, "error", event)
    assert "exc_info" not in out
    assert "ValueError" in out["exception"]
    assert SECRET not in out["exception"]
    assert out["error"] == f"ValueError: {REDACTED}"
    # Always text, so a renderer never repr()s the object and its args.
    assert registry.redact_event(None, "e", {"e": ValueError("x")})["e"] == (
        "ValueError: x"
    )
    assert SECRET in str(caught)  # the exception object itself is untouched


def test_redact_event_emits_no_record(registry: SecretRegistry) -> None:
    with capture_logs() as logs:
        registry.redact_event(None, "info", {"event": SECRET})
    assert logs == []


def test_redact_event_as_a_processor_in_a_real_chain(
    registry: SecretRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    logger = structlog.wrap_logger(
        structlog.PrintLogger(),
        processors=[registry.redact_event, structlog.processors.KeyValueRenderer()],
    )
    logger.info("hello", password=SECRET)
    printed = capsys.readouterr().out
    assert SECRET not in printed
    assert REDACTED in printed


def test_repr_shows_only_a_count(registry: SecretRegistry) -> None:
    assert repr(registry) == "SecretRegistry(<1 secrets>)"


def test_the_registry_is_never_pickled(registry: SecretRegistry) -> None:
    with pytest.raises(TypeError):
        pickle.dumps(registry)


def test_log_registered_emits_one_debug_count(registry: SecretRegistry) -> None:
    with capture_logs() as logs:
        registry.log_registered()
    assert logs == [{"event": "secrets_registered", "count": 1, "log_level": "debug"}]


def test_concurrent_registration_loses_nothing() -> None:
    registry = SecretRegistry()
    values = [f"secret-{i:04d}" for i in range(200)]

    def register(chunk: list[str]) -> None:
        for value in chunk:
            registry.register(value, source="t")

    threads = [
        threading.Thread(target=register, args=(values[i::4],)) for i in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(registry) == len(values)
    assert all(registry.redact(v) == REDACTED for v in values)


def test_stdlib_records_redact_through_processor_formatter(
    registry: SecretRegistry,
) -> None:
    """The shape E002-S01-004's ``configure_logging`` installs for stdlib records."""
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            registry.redact_event,
            structlog.processors.KeyValueRenderer(),
        ],
    )
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "pw %s", (SECRET,), None)
    rendered = formatter.format(record)
    assert SECRET not in rendered
    assert REDACTED in rendered
