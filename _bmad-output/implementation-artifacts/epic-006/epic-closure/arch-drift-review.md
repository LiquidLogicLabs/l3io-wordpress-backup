# E006 architectural review (Mode B): built vs planned

Scope: `git diff b321422^1 83d73a7 -- src tests docs pyproject.toml` (26 src files, `docs/api.md`, `pyproject.toml`), the four story files, ADR 0011/0015/0016/0018/0019/0024, spine AD-1/4/10/14/16/22/24/26. Stack: Python (uv). Reviewer: l3io-arch-review, Mode B.

## Executive summary

The surface is built as planned and mechanically held: `uv run lint-imports` keeps all 4 contracts (including the new `FR-24 - cli/ never imports api/`), the docs/surface/parity/no-SDK contract tests pass (43 passed), every `api.__all__` name has a `docs/api.md` heading, and tracing uses `l3io.*` attributes per O-2(a). No blocker. Three MAJOR drifts: `api/` is built on `cli/` drivers with no recorded decision; the library does print to stderr although the API docstring says it never does; and the package's own `unexpected_exception` record carries an unredacted traceback of a dependency exception. Most important action: record the api-on-cli layering decision (ADR) and make the print/doc contradiction one way or the other.

## Findings

| # | Severity | Principle | Location | Finding | Remediation |
|---|----------|-----------|----------|---------|-------------|
| AD-1 | MAJOR | Core §1 Separation of Concerns; Core §10 docs match code; spine AD-1/AD-16 (structural seed: `cli/` and `api/` are peer composition roots) | `src/l3io/wp/backup/api/operations.py:53-82`, `api/__init__.py:45-51`, `api/errors.py:45-52`, `pyproject.toml:388-397` | `api/` is not an independent composition root. It imports the CLI's drivers (`drive_backup/restore/verify`, `BackupRequest`, `RestoreRequest`, `require_database_restored`), `cli.passphrase`, `cli.logging`, `cli.exit.RUN_ID_ATTR`, and re-exports public types defined in `cli/` (`BackupResult`, `RestoreOutcome`, `RelocationFlags`, `RunContext`, `configure_logging`, 4 CLI exception classes). The layers contract `cli : api` only forbids neither direction at the sibling level (a sibling may import a sibling), so the lint passes; the one-way `FR-24` contract forbids only cli->api. No ADR or spine text records "api composes through cli/ drivers"; the seed and AD-22 describe two peers, and ADR-0016 only says `api/` drivers "consume" the Run Context. Consequence: the public contract's real defining modules are CLI-internal (the docs have to say "the defining module is not part of the contract"), and any CLI change (printing, argparse types) is a library-surface change. Reachable: every public entry point goes through it (verified by reading `operations.py` and `lint-imports`). | Write an ADR (Mode C) choosing between (a) accepting `cli/` drivers as the shared root, with a lint contract that bars `api/` from `cli.parser`, `cli.main`/argparse modules and `cli/output`, or (b) moving drivers, `RunContext`, request/result types and passphrase chain to a neutral module that both `cli/` and `api/` import. Update the spine seed either way. |
| AD-2 | MAJOR | Core §10 (docs contradict code); spine AD-10 (library modules do not own output), AD-26 | `api/operations.py:8-9` ("It never prints"), `cli/restore.py:1307-1311`, `cli/restore.py:1434-1443`; `docs/api.md` (silent) | `api.restore()` runs `drive_restore`, which calls `print(..., file=sys.stderr)` for the unauthenticated-archive warning (unencrypted archive, no `--require-encrypted`), the "safety snapshot is NOT encrypted" notice and the "safety snapshot is encrypted with the passphrase" notice. The docstring promises no printing and `docs/api.md` does not mention stderr output from `restore`. AD-26 does require the warning not be log-only, so the behaviour may be right; the claim is wrong. A host using stderr as a protocol channel is surprised. Verified by reading the call path (the three prints are inside `drive_restore`, which `restore` calls); not executed. | Either state in `operations.py` and `docs/api.md` that `restore` writes safety warnings to stderr (and that `test_process_state.py` pins it), or inject a warning sink (default stderr for the CLI, a `warnings`/callback or logger-only for the library) and record the choice in an ADR amending AD-26. |
| AD-3 | MAJOR | Core §9 (secrets in logs), spine AD-14 / ADR-0018 | `api/errors.py:257-265` (`_log.error(..., exc_info=exc)`), `docs/api.md:42-55` | When any non-package exception escapes an entry point, the package itself logs `unexpected_exception` with the original exception's full traceback and text. Redaction is applied only if the host used `configure_logging` (`redact_event` in the chain); a host that configures nothing gets structlog defaults, which print the record to stdout with no redaction (the docs say so). Verified: with a plain structlog chain, `verify()` whose driver raised `RuntimeError("SENTINEL-pw-123")` logged a record whose `exception` field contained the traceback and the sentinel-bearing message, and `__cause__` held the raw exception. ADR-0018 places redaction "where text leaves the program"; this record leaves the program unredacted by default, and a dependency exception (pymysql, boto3, subprocess) is exactly the text most likely to carry a credential. The documented caveat covers other records but not this one, which the package chooses to enrich with a traceback. | In `public_boundary`, drop `exc_info=exc` and log `cause_type` plus a traceback only through a redacting path (use the open Run's registry when `context` is present, else format with `traceback` frames without exception text); or document `__cause__` and this record as unredacted in `docs/api.md` under a heading and add an ADR-0018 amendment accepting it. Add a contract test planting a sentinel in a non-package exception and asserting the default-chain record is clean. |
| AD-4 | MINOR | Core §2 Reuse | `api/operations.py:151-178` | `backup()` builds `BackupRequest` twice, two 11-line blocks identical except `exclusion=`. A new `BackupRequest` field must be added in both places or one path silently diverges from the other. | Build the keyword arguments once (`kwargs = {...}; if exclusion is not None: kwargs["exclusion"] = _exclusion(exclusion)`) or give `BackupRequest` a `None`-means-default constructor path. |
| AD-5 | MINOR | Core §8 GA over beta | `pyproject.toml:114`, `:188` (`opentelemetry-exporter-otlp-json-file>=0.65b0`) | A beta-versioned package is in the `otel` extra and the dev group. ADR-0011 names it "preferred" but records no GA-status justification or exit plan, which §8 requires for any beta dependency. Confined to an optional extra, so not shipped in the base path. | Add to ADR-0011 (or a new ADR) why the beta is accepted, the blast radius (optional extra), and the condition for moving to a GA exporter. |
| AD-6 | MINOR | Core §3 / ADR hygiene | `docs/adr/0011-observability-structlog-plus-api-only-tracing.md` (Status: Proposed; no 2026-11-06 amendment) | AD-24 in the spine carries the 2026-11-06 O-2(a) amendment (`l3io.*` attributes, `l3io.phase`, `l3io.archive.bytes`), but the ADR that records the tracing decision is still "Proposed" and says nothing of the attribute namespace. The decision it records is built and in force. | Mark ADR-0011 Accepted and add the O-2(a) amendment note so the spine and ADR agree. |

## Per-principle walkthrough

Core charter:
- §1 Separation of Concerns: AD-1, AD-2. `domain/` and `core/` pass both forbidden contracts (new `core/config_location.py` imports only `domain` and stdlib pure types); `adapters/tracing.py` imports only `ports/` and the root `__version__`.
- §2 Reuse: AD-4.
- §3 Design by Contract: PASS. `public_boundary` closes the exception set (exported `BackupError` passes through, anything else becomes `UnexpectedError`); `restore`'s required-keyword-only parameters keep AD-5 (no defaults for mode, overrides, snapshot, config_policy); `passphrase` without `encrypt=True` is refused. CLI-guard parity (`--require-encrypted`, `--archive-database`, `--reset-existing-db-user`, `--no-safety-snapshot` via `SnapshotPolicy`, `--whatIf`, `--admin-tls`, relocation, `--overwrite-wp-config`) is present as parameters and goes through the same `drive_*` drivers, so FR-7 guard, lock and snapshot are shared code; `tests/contracts/test_api_parity.py` passes.
- §4 Testability: PASS. Injectable `prompt`, `context`, tracer resolved at call time, `tests/otel_sdk.py` isolates the SDK.
- §5 Brevity: AD-4 only.
- §6 Comments: PASS. Long module docstrings state current behaviour and cite ADRs; no changelog comments seen in the touched headers.
- §7 Dependency selection: PASS apart from AD-5 (the exporter is maintained; OTel API only in the base install, SDK in an extra, umbrella exporter absent).
- §8 GA over beta: AD-5.
- §9 Logging/correlation: AD-3. Tracing coupling is flat `trace_id`/`span_id` through `add_trace_ids` only, as AD-24 specifies; span free-text goes through the registry; `record_exception=False` keeps tracebacks off spans. Library does not configure logging, tracer provider, signals or `os.environ` (no `basicConfig`/`signal`/`atexit`/`set_tracer_provider` hits in `api/` or `adapters/tracing.py`).
- §10 Documentation: AD-2. `docs/api.md` headings match `api.__all__` exactly (checked by script: none missing, only `__version__` extra, which is `l3io.wp.backup.__all__`); `test_api_docs.py` and `test_public_surface.py` enforce it.

Python overlay:
- Packaging (uv, lockfile, `requires-python`): PASS (unchanged by the diff).
- Quality toolchain / structured logging: PASS apart from AD-3 and the stderr prints in AD-2.

Spine and ADR conformance (only sections the diff touches):
- AD-1: pure-layer contracts KEPT; see AD-1 above for the api/cli question the contract does not examine.
- AD-4: PASS. `dry_run` appears only in composition roots and CLI/api modules; adapters/core mention it in comments or `skipped_dry_run` outcome strings only; `api` passes `dry_run=` to the drivers and never branches on it.
- AD-10: PASS with AD-3. `public_boundary` acquires a bound logger and configures nothing.
- AD-14: PASS. One base, structured fields, `raise ... from`; `UnexpectedError` message names the type, not the text (the leak is the log record and `__cause__`, AD-3).
- AD-16: PASS. `context=None` opens one Run, a `RunContext` is joined, a second active Run raises `RunContextActiveError`; the registry comes from the joined context.
- AD-22/FR-24: PASS. Public surface is `__all__` in the two roots, `cli/` cannot import `api/`.
- AD-24 (as amended): PASS. `opentelemetry-api` hard dependency, SDK in the `otel` extra, `l3io.*` attributes, standard `process.*`/`error.type`/`db.name` kept, no `TRACEPARENT`.
- AD-26: PASS for the chain (argument, environment, prompt, in `cli/passphrase.py`, shared); stderr part see AD-2.
- ADR-0015/0016/0018/0019/0024: PASS. Credentials stay delegated to the database package (`CredentialSpec` values only); Run joined not re-created and adapters bound once by the driver; redaction through the registry with the floor amendment not touched by this diff; backup Dry Run selects the no-op adapters inside the driver; passphrase-only payload (no recipient/identity parameters on `backup`/`restore`/`verify`). ADR-0011 hygiene is AD-6.

## Gate

- BLOCKER: none. MAJOR: AD-1, AD-2, AD-3 must be resolved or recorded as an accepted ADR before epic sign-off. MINOR: AD-4, AD-5, AD-6 defer to the backlog.

## Recommended ADRs

1. api/ composition through `cli/` drivers, or extraction of a neutral root layer (AD-1); amend the spine's structural seed.
2. Library stderr warnings from `restore` (amends AD-26: sink or documented behaviour) (AD-2).
3. Handling of the original exception in `unexpected_exception` and on `__cause__` (amendment to ADR-0018) (AD-3).
4. Beta exporter in the `otel` extra and ADR-0011 status/amendment (AD-5, AD-6).

Blocker: 0, Major: 3, Minor: 3

Sections read:
- /tmp/e006-brief.md L1-23
- .claude/skills/l3io-arch-review/references/standards-core.md L1-169; standards-python.md L1-43; review-report.md L1-31
- ARCHITECTURE-SPINE.md L44-51 (AD-1), L83-89 (AD-4), L152-157 (AD-10), L178-183 (AD-14), L211-218 (AD-16), L259-268 (AD-22), L279-292 (AD-24), L300-305 (AD-26), L387-432 (structural seed); heading list of the whole file
- spec-index.md L1-60 (index-picked: structural seed for the api/cli hunk)
- docs/adr/0011 (full, cut to 900 chars/line), 0016 (full, cut), 0018 (L1-~110 cut), 0019 (L1-60 cut), 0015 and 0024 (amendment headings only)
- pyproject.toml L32-38, L100-120, L187-188, L350-406
- src/l3io/wp/backup/api/__init__.py (full), api/operations.py (full), api/errors.py (full), adapters/tracing.py (full), cli/restore.py L1290-1315, L1420-1446, core/config_location.py imports, grep over cli/adapters/core for dry_run/print/stderr/signal/atexit/basicConfig
- docs/api.md L36-58 and heading/`__all__` cross-check; cli/logging.py grep of the chain
- Commands run: `uv run lint-imports` (4 kept); `pytest tests/contracts/test_api_docs.py test_public_surface.py test_api_parity.py test_tracing_no_sdk.py` (43 passed); two `uv run python -I` probes (sentinel exception log record; credential spec repr/pickle)
- Story files and per-story review files were not opened (diff and code were read instead).
