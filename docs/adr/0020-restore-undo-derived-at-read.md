# ADR-0020: Restore undo text is derived when the journal is read

- **Status:** Accepted
- **Date:** 2026-10-28
- **Deciders:** Shawn Anderson
- **Epic:** E003
- **Resolves:** arch gate finding AG-1 (2026-10-28); MINOR BL-E003-001
- **Departs from spec:** n/a
- **Principle(s) in tension:** AD-18.1 (the Run Journal names what would undo the interrupted phase), AD-1 (the core computes, adapters record), Core §3 single ownership of a contract

## Context

E002-S02-012's `JournalEntry` stored an `undo` string on each `phase_started` entry, and
E003-S02-005 reported the undo of the last open `phase_started` entry as
`IncompleteRunError.undo`. The cutover and the removal of the database that loses it are
steps inside `LOAD_DUMP`, and steps carried no undo. So a Run killed during or after the
cutover reported `LOAD_DUMP`'s undo, "drop staging database `<staging>`" — by then the
live database. Following that instruction destroys the site.

The derivation also had two signatures: `undo_for(phase, header, relocation)` in
E003-S01-001 and `undo_for(phase, header)` in E003-S02-005. The kill harness asserted
the refusal at every kill point, but never the undo text.

## Options considered

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. Store undo text on every `step_completed` entry too | Small change to the entry type | Two places produce undo text; text frozen at write time can still be wrong for a later state; every writer must remember to supply it | Duplicates a derivation; the defect class stays open |
| B. **Store facts only; derive the undo text from the journal when it is read** | One pure function produces every undo; the text always matches the recorded state; testable per prefix of entries | Every step that changes what "undo" means must be journaled as a named step | AD-1 (pure derivation), AD-18.1, single ownership |

## Decision

**Option B.**

- Undo text is **not stored**. `JournalEntry` (E002-S02-012) loses its `undo` field, and
  `RunJournalCommand.phase_started(phase, target)` loses its `undo` parameter.
- `core/restore.py::undo_for(interrupted: RestorePhase | None, last_step: str | None,
  header: JournalHeader) -> str` is the **one** derivation. It is pure, a `match` over
  every `RestorePhase` ending in `assert_never`. `interrupted` is
  `interrupted_phase(entries)`; `last_step` is the `step` of the last `step_completed`
  entry inside that phase, or `None`. `IncompleteRunError.undo` comes from it and from
  nowhere else. This is the signature E003-S01-001 and E003-S02-005 both cite.
- The steps inside `LOAD_DUMP` that change what undo means are journaled as
  `step_completed(LOAD_DUMP, <named step>)`: each cutover key write as
  `"cutover:<KEY>"`, and the removal of the database that lost the cutover as
  `"loser_dropped"`. Error-path steps the orderly-failure rule needs (ADR-0021) are named
  the same way: `"check_failed"`, `"cutover_readback_failed"`, `"staging_dropped"`. The
  config steps of `REWRITE_CONFIG` under `--overwrite-wp-config` are named by ADR-0023.
  The step names are constants in `core/restore.py`.
- With these, `undo_for` tells a pre-cutover kill from a post-cutover one. Before the first
  `"cutover:*"` step, the undo names the staging database to drop. After it, the undo
  **never** names the now-live database as something to drop: a partial cutover names the
  written and pending keys and `--resolve-interrupted-run`; a completed cutover says the
  site runs on `header.load_database` and names the previous database as retained (or, after
  `"loser_dropped"`, as dropped).
- Text that only Epic 7 knows (`REWRITE_CONFIG` and `SEARCH_REPLACE` under relocation) is
  copied into the `JournalHeader` at plan time (`phase_undo`, from `RestoreRelocation.undo`),
  so `undo_for` needs no `relocation` argument when a later Run reads the journal.
- The kill harness (E003-S02-005) asserts the **reported undo text** at every kill point
  *k*, not only the refusal.

## Consequences

- Positive: the undo the operator reads is computed from what the journal proves happened,
  so no state can be paired with a stale instruction. The derivation is unit-tested against
  every prefix of a recorded entry sequence.
- Positive: one signature, one owner (E003-S02-005 states the table; other stories cite it).
- Negative / trade-off accepted: the executor must journal every named step, and a new step
  that changes the undo needs a new name and a new arm. The harness derives its kill points
  from a recorded run, so an unjournaled step shows up as a wrong undo at some *k*.
- Follow-ups: E002-S02-012 changes `JournalEntry` and `phase_started`, and adds
  `phase_undo` to `JournalHeader`; E003-S02-005 states the undo table and extends the
  harness; E003-S01-001 cites the signature.
- **Revisit when:** a phase gains internal steps whose undo differs (for example staged file
  extraction). They become named steps under this rule; the rule itself does not change.
