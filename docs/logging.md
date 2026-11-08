# Logging and Run correlation

`wpbackup2` writes one structured log stream to **stderr**. Every record a Run
emits — this package's, `l3io.wp.database`'s and `l3io.wp.config`'s — carries
the same `l3io_run_id`, so one Run can be pulled out of a week of logs:

```sh
wpbackup2 --log-format json backup --wp-dir /srv/www 2> backup.log
jq -c 'select(.l3io_run_id == "4b0e…")' backup.log
jq -c 'select(.event == "phase_completed") | {phase, duration_ms, bytes, exit_code}' backup.log
```

stdout carries only the command's result (the Archive path, or the verify
verdict), never a log record.

## Flags

| Flag | Values | Default |
|---|---|---|
| `--log-level` | stdlib level names, case-insensitive: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | `INFO` |
| `--log-format` | `console`, `json` | `console` when stderr is a terminal, else `json` |

Both are top-level flags, given before the subcommand. They match
`l3io-wp-database` v0.2.0's CLI. v0.2.21's `--log-level` defaulted to
`CRITICAL`; the default is now `INFO` (triage E-14).

**Human-readable output off a terminal (cron mail).** Off a terminal stderr is
strict JSON lines by default, one object per line, warnings and failures
included: no plain-text line is mixed in, so a log collector can parse every
line (owner decision 2026-11-07, BL-E002-103 UX-5). Where a person reads the
output instead, such as a cron job whose output is mailed, pass
`--log-format console`:

```sh
# crontab: the mailed output is human-readable
0 3 * * * wpbackup2 --log-format console backup --wp-dir /srv/www --archive /var/backups/site.tar.gz
```

Under `console` the notices that `json` carries only as WARNING records (an
unauthenticated Archive, an unencrypted Safety Snapshot, a password on the
command line) are also printed as plain sentences.

## The record schema

This schema is part of the CLI contract (PRD §8). Log message wording is not.

<!-- schema:record -->
| Field | Type | On | Meaning |
|---|---|---|---|
| `l3io_run_id` | string (UUIDv4) | every record emitted inside a Run | The Run's identifier; random, so it leaks neither time nor path |
| `l3io_dry_run` | boolean | every record emitted inside a Run | `true` when the Run is a Dry Run (`--whatIf`): real queries, no-op commands |
| `timestamp` | string (ISO 8601, UTC, `Z`) | every record | When the record was emitted |
| `level` | string | every record | `debug`, `info`, `warning`, `error`, `critical` |
| `logger` | string | every record | The emitting logger, e.g. `l3io.wp.backup.adapters.phase`, `l3io.wp.database`, `l3io.wp.config.adapters.config_file` |
| `event` | string | every record | What happened, e.g. `run_started`, `phase_completed`, `run_finished` |
<!-- /schema:record -->

Every `phase_completed` record also carries exactly these phase fields, plus
any step-specific fields (verification adds `format_version`, `integrity`,
`dump_present`, `passed`, `authenticated`, and on failure `reason` and
`member`). `authenticated` is `false` for every keyless verification — the
checksums it checks live in the unprotected Manifest, so a pass means
consistent, not authentic — and `true` only when `verify --authenticate`
decrypted the whole payload with its passphrase:

<!-- schema:phase -->
| Field | Type | Meaning |
|---|---|---|
| `phase` | string | The step, upper case with underscores. A backup's: `DUMP`, `COLLECT_SITE_ROOT`, `ENCRYPT`, `WRITE_ARCHIVE`, `VERIFY`, `PUBLISH`; a restore's: `PREFLIGHT`, `VERIFY`, `SNAPSHOT`, `CLEAN`, `EXTRACT`, `REWRITE_CONFIG`, `LOAD_DUMP`, `SEARCH_REPLACE` (owner decision 2026-11-07, BL-E002-109; the Run Journal file keeps its own lower-case spelling) |
| `duration_ms` | number | Monotonic time the step took |
| `bytes` | integer | Bytes the step produced or read; `0` when not applicable |
| `exit_code` | integer | `0` on success; on failure the error's code from the [exit-code table](../README.md#exit-codes), the same one `run_finished` carries; `null` when the step was interrupted by `KeyboardInterrupt`, whose exit status is the interpreter's, not the table's |
| `outcome` | string | `ok` or `failed`; `skipped_dry_run` (with `bytes` `0`) for a step a Dry Run did not perform |
<!-- /schema:phase -->

`run_started` carries `operation` (`backup`, `verify`), `started_at` and the
package `version`; `run_finished` carries `exit_code` (the integer from the
[exit-code table](../README.md#exit-codes)), `exit_reason` (its name, such as
`VERIFICATION_FAILED`), `duration_ms` and, on failure, `error` (the error's
class name) — a verification that fails is a failure here too. A Run ended by
`KeyboardInterrupt` carries `error` but `null` `exit_code` and `exit_reason`:
the process exits with the interpreter's status (130 for Ctrl-C), which is not
in the table. They are the first and last records of every Run.

Before preflight, a backup logs one `planned_action` record per action it
plans, in order — `ordinal`, `step`, the rendered `action` line and the step's
parameters (the collection line adds `exclusion_source` and
`exclusion_patterns`) — in a real Run and in a Dry Run alike.

## Failures

A failed command always leaves one `command_failed` record at `error`, naming
the error class in `error`, the operator message in `message`, and the code in
`exit_code` and `exit_reason` — an Archive that did not verify included
(`VerificationFailedError`, exit 8). An unexpected exception (a defect) also
carries its traceback, redacted like every other field. This holds for a failure
before any Run opened (a missing `--wp-dir`, a bad `--archive`), which has no
`run_finished` and no `l3io_run_id`. It does not hold for command-line usage errors
(exit 2): argparse prints plain text before logging is configured. With `--log-format console` the plain `wpbackup2: ...` line is
printed to stderr as well; with `--log-format json` it is not, so stderr stays
one JSON object per line and `jq` over it never stops at a failed Run.

## Correlating with your own identifier

A Python caller can bind its own correlation ID before calling the library; it
appears on every record of the Run, beside `l3io_run_id`:

```python
import structlog

structlog.contextvars.bind_contextvars(correlation_id="job-1234")
```

The Run's keys are prefixed `l3io_` (owner decision 2026-11-07, BL-E006-019):
`l3io_run_id` and `l3io_dry_run` are what the Run binds in structlog's context
variables, so a host's own `run_id` or `dry_run` is never overwritten during a
call. No record the package emits has a `run_id` or `dry_run` field of its own,
which would replace a host's bound value in that record: `run_started` states
`l3io_run_id` and `l3io_dry_run`, and `run_finished` `l3io_run_id`, as explicit
fields too, so a host that does not merge context variables still sees each
Run's boundaries named. `tests/unit/cli/test_log_schema.py` checks every record
of a whole backup, real and Dry Run, and every record a Run emits under a host
that bound its own `run_id` and `dry_run`.

## What is and is not covered

- Records emitted before the Run opens (argument translation, the passphrase
  note) carry no `l3io_run_id`. A Run opens after the Site Root is checked.
  `command_failed` carries the `l3io_run_id` of the Run that failed, when one was open.
- A record from a thread started without `contextvars.copy_context().run`
  carries no `l3io_run_id` (contextvars do not cross a raw `threading.Thread`). It
  is still redacted: the redaction step applies every open Run's registry.
- Credentials never appear at any level, `DEBUG` included (FR-20, ADR-0018);
  the workspace path is logged only at `DEBUG` (`workspace_not_removed` is a
  WARNING without it, followed by a `DEBUG` record that names it; a Run that
  had otherwise succeeded then fails with `WorkspaceNotRemovedError`, exit 1).
- A host that configures logging itself gets none of this: the configuration
  lives in `l3io.wp.backup.cli.logging.configure_logging`, which a library
  module never calls (AD-10).
