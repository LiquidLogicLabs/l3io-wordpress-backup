# ADR-0021: Run Journal location, lifecycle and resolution

- **Status:** Accepted
- **Date:** 2026-10-28
- **Deciders:** Shawn Anderson
- **Epic:** E003
- **Resolves:** arch gate findings AG-11, AG-7 and AG-8 (2026-10-28)
- **Departs from spec:** _bmad-output/planning-artifacts/architecture/architecture-py-wordpress-backup-2026-09-15/ARCHITECTURE-SPINE.md#ad-18--a-killed-run-must-be-recoverable
- **Principle(s) in tension:** AD-18.1 (Run Journal "written to the Safety Snapshot's directory"), AD-4 (a Dry Run changes nothing), ADR-0016 (adapters bound once, after the lock), NFR-1

## Context

Three gaps in the Run Journal blocked the E003 gate.

- **Location (AG-11).** AD-18.1 puts the journal in "the Safety Snapshot's directory", and
  E003-S02-003 followed it. E003-S02-005 kept it in the per-Site state directory always.
  A journal that follows `--snapshot-dir` cannot be found by a later Run without the same
  flag, and has no directory at all when the snapshot is skipped.
- **Lifecycle (AG-7).** Nobody owned `begin_run`/`finish_run` or the header's
  construction. `finish_run` files an open Run under the non-blocking `failed/`, so a
  natural `finally: finish_run()` would unblock a half-restored site.
- **Resolution (AG-8).** `--resolve-interrupted-run` had no port method and no no-op twin,
  so a Dry Run would really resolve. It ran in step 2, before the adapter bind, so
  completing an interrupted cutover had no config port and no relocated credentials.

## Options considered

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. **Location: always the per-Site state directory** | A later Run always finds it, with or without flags; works when the snapshot is skipped | Amends AD-18.1's wording | NFR-1; AD-18.1's intent (durable, found by the next Run) |
| B. Follow `--snapshot-dir` | Matches AD-18.1's words | A Run without the flag misses the journal and proceeds over a half-restored site | Breaks NFR-1 |
| C. Follow `--snapshot-dir`, plus a pointer file in the per-Site directory | Matches the words | Two files must agree; the pointer is the real journal location in all but name | Adds a second durability mechanism |
| D. Make the location not configurable at all | Simplest | Same location as A; A already ignores the flag | Equivalent to A |

Lifecycle and resolution had no competing options worth recording: the gate named the
defects, and the decisions below close them.

## Decision

**Location — option A (Shawn).** The Run Journal is **always** in the per-Site state
directory, `platformdirs.user_state_path("l3io-wp-backup") / "snapshots" /
sha256(canonical Site Root)`. It never follows `--snapshot-dir`. The restore root
(E003-S02-006) computes the directory and passes a path; `core/` receives only paths and
never imports `platformdirs`. AD-18.1 is amended to say so.

**Lifecycle.**

- The root (E003-S02-006) builds the frozen `JournalHeader` before `SNAPSHOT`, with the
  Safety Snapshot path computed in advance, and passes it in `RestoreInputs`.
- The executor writes `begin_run(header)` immediately before `SNAPSHOT`, or before the
  first destructive phase when the snapshot is opted out. `PREFLIGHT` and `VERIFY` leave no
  journal.
- `finish_run()` is called **only** on success, or on an orderly failure as decided by one
  pure predicate, `core/restore.py::is_orderly_failure(entries) -> bool`. It returns true
  only when no `CLEAN`, `EXTRACT` or `REWRITE_CONFIG` phase started, no cutover step
  completed, and the failure is either a failed post-load check whose staging drop
  succeeded, or a cutover read-back failure with the original config intact. The entries
  show which by the named steps of ADR-0020 (`"check_failed"` or
  `"cutover_readback_failed"`, with `"staging_dropped"`).
- Every other failure leaves `current.json` in place, so the next Run is blocked. There is
  no `finally: finish_run()`.

**Resolution.**

- E002-S02-012's `RunJournalCommand` gains `resolve(run_id: str, resolved_by: str) ->
  None`, with a no-op twin on `NoopRunJournal`. A Dry Run therefore resolves nothing; it
  prints what it would resolve.
- Resolution happens **after** the adapter bind (ADR-0016's step 3), not before it, so
  completing a cutover-interrupted set has the config ports and the credentials it needs.
  The incomplete-journal **query** still runs before the bind, keyed by the canonical Site
  Root only.

## Consequences

- Positive: the next Run finds an incomplete journal whatever flags either Run used, and
  whether or not a snapshot was taken.
- Positive: one rule decides when a failed Run stops blocking, and it is a pure function of
  the journal, testable per entry sequence.
- Negative / trade-off accepted: the journal and an operator-chosen snapshot can sit in
  different directories. The header records the snapshot's actual path, and the undo text
  (ADR-0020) names it.
- Negative: a Run that fails in a way the predicate does not recognise blocks the next Run
  even when nothing was changed. That is the safe direction (NFR-1); the operator resolves
  it with `--resolve-interrupted-run`.
- Follow-ups: spine AD-18.1 amended; E002-S02-012 adds `resolve` and its no-op twin;
  E003-S02-003 drops "the snapshot's directory"; E003-S02-005 states the predicate and the
  layout; E003-S02-006 builds the header, moves resolution after the bind, and lists the
  new order.
- **Revisit when:** journals must be shared between hosts (a remote lock backend). The
  location would then follow the lock, not the snapshot.
