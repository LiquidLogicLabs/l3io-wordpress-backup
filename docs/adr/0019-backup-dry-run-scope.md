# ADR-0019: What a backup Dry Run executes

- **Status:** Accepted
- **Date:** 2026-10-27
- **Deciders:** Shawn Anderson
- **Epic:** E002 (story E002-S02-007, Story 2.7 "Rehearse any destructive operation"); E003 (restore Dry Run) is unaffected
- **Resolves:** arch gate finding AG-44 (2026-10-27)
- **Departs from spec:** _bmad-output/planning-artifacts/prds/prd-py-wordpress-backup-2026-09-12/prd.md#56-safety-observability-and-exit-behavior
- **Principle(s) in tension:** FR-19 ("a Dry Run performs Verification"), AD-4 (Dry Run is adapter selection, never a conditional), AD-21 (streaming)

## Context

AD-4 makes a Dry Run a choice of adapters: real queries, no-op commands, and a core that
is byte-identical in both cases. FR-19 lists what a Dry Run exercises: "Verification,
credential resolution, WP-CLI availability checks and relocation planning".

E002-S02-007 defined the backup Dry Run only as "the plans are equal and the no-ops
record `skipped_dry_run`". That left the execution of the later plan steps undefined.
The backup plan is `DUMP`, `COLLECT_SITE_ROOT`, `ENCRYPT` (when encrypting),
`WRITE_ARCHIVE`, `VERIFY`, `PUBLISH`.

- `NoopDatabase.dump_to` writes no Dump. But `WRITE_ARCHIVE` is
  `adapters/tar.py::write_archive`, a plain function rather than a port in the
  `AdapterSet`, so it would try to read and hash a Dump that does not exist. It would also
  stream the whole Site Root into `os.devnull`.
- `VERIFY` has no staged Archive to verify.

A developer would either add an `if dry_run:` (forbidden by AD-4) or hit a crash.

FR-19's Verification clause was written with restore in mind: verify the existing Archive
before planning any destruction. A backup has no Archive until it writes one.

## Options considered

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. Full rehearsal: dump to a discard sink, write and hash into `os.devnull`, verify the discarded stream | Exercises the most code | Reads the whole database and Site Root to produce nothing; a Dry Run on a large site costs as much as a backup; still nothing real to verify | AD-4-compatible but defeats a Dry Run's purpose |
| B. Write a real Archive to a temporary location, verify it, discard it | FR-19's "Verification" taken literally | Writes to disk: the Storage Target's filesystem fills or not, so the Dry Run is not effect-free; full cost | Breaks FR-19's "makes no change" in spirit |
| C. **Run every query for real; bind no-op commands for dump, archive write and publish; report the plan** | Exercises credentials, preflight, database readiness and every query-reachable refusal; costs seconds | No archive is produced, so there is nothing to verify | AD-4 exactly; departs from FR-19's literal Verification clause for backup only |

## Decision

**Option C.** A backup Dry Run:

1. **resolves credentials** from the WordPress credential source (the real source; the
   resolved password is registered for redaction like any Run's);
2. **runs preflight** (AD-13) and `l3io-wp-database`'s **`check_dump`** through
   `DatabaseQuery`. A blocking verdict exits with the code the real Run would use, after
   reporting the actions planned up to that point, as E002-S02-007 already states;
3. runs every other **query** the plan depends on for real, because AD-4 binds the real
   query set. A refusal reachable through a query is reported exactly as the real Run
   would report it. A refusal that only a command can detect is not;
4. **builds the `BackupPlan` and reports it**, in order, on stdout and as `planned_action`
   records;
5. executes the command steps through **no-op command adapters**, each of which records
   `skipped_dry_run` with `bytes=0`. It does **not** dump the database, read or hash Site
   Root file contents, encrypt, write, stage, verify or publish an Archive.

For this to hold without a conditional, every step that writes, hashes or verifies is
reached through a command half of the `AdapterSet`:

- `DUMP` goes through `DatabaseCommand.dump_to` (no-op: `NoopDatabase`);
- the archive write, with collection streaming and `ENCRYPT` inside it, goes through a
  command port wrapping `adapters/tar.py::write_archive` (no-op twin in
  `adapters/noop.py`). `write_archive` stops being called directly by the executor;
  E002-S02-007 names the port;
- `VERIFY` and `PUBLISH` run inside `ArchiveStore.publish`, which already calls its
  injected verifier against the staged file before `os.replace` (E002-S01-001). The no-op
  store does neither.

The plan steps `WRITE_ARCHIVE`, `VERIFY` and `PUBLISH` stay in the plan and in the
report, which says what a real Run would do. They are just bound to no-ops.

**FR-19's "a Dry Run performs Verification" applies to the restore path**: verifying the
existing Archive before planning destruction. For a backup Dry Run there is no Archive to
verify, and none is produced. "Credential resolution" applies to both paths. "WP-CLI
availability checks and relocation planning" are restore-only, because backup invokes no
WP-CLI.

### How AD-4's plan-equality test still holds

The `BackupPlan` is computed by the pure core from the Site Descriptor, the scope, the
target and the results of **queries**. Both adapter sets share the same query instances
(E002-S02-007: `dry_run_adapters` reuses the real set's queries and substitutes only
command halves). So the core gets identical inputs and returns an identical plan.
`tests/unit/core/test_plan_equality.py` asserts that for every `BackupScope`, unchanged.
Execution differs only in which command adapters are bound, never in the core or in any
branch on a flag. AD-4's CI grep for `dry_run`/`what_if` outside the composition roots
also stays as is.

## Consequences

- Positive: a backup Dry Run is cheap, effect-free and still catches the realistic failures
  (bad credentials, an unreachable or unready database, missing tools, and any refusal a
  query can detect) with the same exit codes as a real Run.
- Positive: the "no-op dump, real writer" crash is closed by construction. With no direct
  call to `write_archive` from the executor, there is no unguarded effect for a Dry Run to
  reach.
- Negative / trade-offs accepted: a backup Dry Run cannot detect failures that appear only
  while writing (disk full, a file unreadable mid-stream, a file changing between hashing
  and writing). The report says it is a plan, not a rehearsal of the write.
- Negative: one more command port and no-op twin for E002-S02-007, and
  `tests/contracts/test_port_split.py` finds it by its walk.
- Follow-ups: E002-S02-007 states the five points above, adds the archive-writer command
  port and its no-op, and adds a test that a backup Dry Run against a temporary Site Root
  and a recording database double leaves both unchanged, writes nothing at the Storage
  Target and never calls `write_archive`.
- **Revisit when:** operators need a backup rehearsal that measures size or duration. That
  would be a separate, explicit mode (Option A's discard-sink write), not a change to Dry
  Run.
