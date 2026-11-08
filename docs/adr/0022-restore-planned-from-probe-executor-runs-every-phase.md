# ADR-0022: A restore is planned from a probe, and the executor runs every phase, in a Dry Run too

- **Status:** Accepted
- **Date:** 2026-10-28
- **Deciders:** Shawn Anderson
- **Epic:** E003
- **Resolves:** arch gate findings AG-4 and AG-5 (2026-10-28)
- **Departs from spec:** n/a
- **Principle(s) in tension:** AD-13 (preflight covers every tool the *plan* requires), AD-17 (phase order is data), AD-4 (Dry Run is adapter selection; it refuses wherever the real Run would and nowhere else)

## Context

**AG-4.** The restore root (then E003-S01-006) ran `PREFLIGHT` and `VERIFY` itself, before
the plan existed, and E003-S01-001's plan contained both again as phases. So both ran
twice, and the root's preflight could not cover `RestorePlan.required_tools` — including
the decided `extra_tools` route for the passphrase backend and Epic 7's relocation tools —
because the plan was built after it. AD-13 requires preflight over the plan's tools.

**AG-5.** A Dry Run binds no-op commands (AD-4). Three checks that follow an effect
re-read the world instead of using that effect's result, so in a Dry Run they read what the
suppressed command never produced, and fail where the real Run succeeds:

- E003-S02-003 verifies the Safety Snapshot at a path that was never written;
- E003-S02-005 surveys a staging database that was never created;
- E003-S02-004's `restored` is never true, so a Dry Run of a database Mode exits non-OK.

## Options considered

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. Root runs `PREFLIGHT`/`VERIFY` before planning; drop them from the plan | One run of each | Preflight cannot see the plan's tools; the phases leave the AD-17 enum's data | Breaks AD-13 and AD-17 |
| B. **Probe, plan, then the executor runs every phase including `PREFLIGHT` and `VERIFY`** | Each phase runs once, in AD-17 order; preflight sees the plan's tools; nothing is checked twice | Planning reads unverified headers | AD-13 and AD-17 as written |
| C. Dry Run checks branch on a dry-run flag | Small | `if dry_run` in `adapters/` | Forbidden by AD-4 |
| D. **Checks that follow an effect take the effect's result; no-op results say "not performed"** | No conditional; the check's input is the effect it checks | Result types gain a field | AD-4 exactly |

## Decision

**B and D.**

**Two stages.**

1. **Probe.** The root reads the Archive's headers and Manifest through `ArchiveProbe`
   (`member_index`, E002-S01-003), and `classify` produces the `ArchiveView`. No checksum
   is verified at this stage. A malformed Archive refused here exits with the classifier's
   error (`UnrecognisedArchiveError`, `UnsupportedFormatVersionError`) and changes nothing.
2. **Plan.** The root surveys the target through query ports and builds the `RestorePlan`.

**The executor runs every phase** in AD-17 order: `PREFLIGHT` over `plan.required_tools`
(Epic 3's own, `extra_tools` and the relocation tools), then `VERIFY` (the full
`verify_archive`), then `SNAPSHOT`, and so on. The root no longer runs its own `PREFLIGHT`
or `VERIFY` steps. AD-13 and AD-17 hold without amendment.

**Dry Run.**

- `execute_restore` runs under the dry-run adapter set (AD-4), exactly as in a real Run.
- Each check that follows an effect takes that effect's **result** as input; it does not
  re-read the world.
- A no-op command returns a result marked as not performed (`performed=False`). A check
  given a not-performed result reports "would verify" or "would check" and does not fail.
- This covers three cases:
  - verifying the Safety Snapshot after publishing (E003-S02-003);
  - the post-load survey of the staging database (E003-S02-005);
  - `restored` (E003-S02-004).
- The Dry Run exit rule lives in `cli/restore.py`, the only place `dry_run` may be named. A
  Dry Run that completes its plan without a refusal exits `OK`.

## Consequences

- Positive: preflight checks exactly the tools the plan needs, once, before any effect.
- Positive: a Dry Run refuses wherever the real Run would refuse, through the same queries,
  and never fails because a check looked for something a no-op did not make.
- Negative / trade-off accepted: the plan is built from unverified headers. Nothing
  destructive runs before `VERIFY`, so a tampered header can only make the Run abort at
  `VERIFY`, as under the ADR-0016 amendment. *(Narrowed 2026-11-06 by ADR-0016's amendment of
  that date: this holds for an encrypted Archive, which `age` authenticates. For an
  unencrypted or v0 Archive, `VERIFY` proves consistency, not authenticity. A forger who
  rewrites a header and its checksums together passes `VERIFY`, and the restore proceeds
  with WARNING `archive_unauthenticated`.)*
- Negative: the command results the checks consume carry `performed: bool`:
  `EnsureOutcome`, `LoadResult` and `ConfigWriteResult` (E002-S02-012), `BackupResult`
  (E002-S01-004; the no-op store's `publish()` reports it, E002-S02-007), and Epic 3's
  `SnapshotResult` and `DatabaseRestoreReport`.
- Follow-ups: E003-S02-006 removes its `PREFLIGHT`/`VERIFY` steps and owns the exit rule;
  E003-S01-001's executor runs them; E003-S02-003, E003-S02-004 and E003-S02-005 each add
  a Dry Run test case for their check.
- **Revisit when:** a check must observe the world after a no-op (for example to measure
  real free space). That would be a query run before the effect, not a re-read after it.

## Amendment — 2026-11-06: the Dump runs with the WordPress account's rights (Shawn)

Red team RT-S02-005 (BL-E003-071). `LOAD_DUMP` runs the Dump's SQL into the staging database
as the WordPress account, which at that point still holds its existing grants: on the live
database, and on any other database it was given. A tampered Dump can contain `USE` or
database-qualified statements, so it can change those databases during the load, before the
post-load check and the cutover.

**Decided: accept, document; no confinement (option C).**

The remaining exposure:
- **The live database of the site being restored:** recoverable from the Safety Snapshot,
  unless `--no-safety-snapshot` was given.
- **Any other database the account can reach:** not covered.

The limits on that exposure are:
- the operator's own grants: a site account limited to its own database, the textbook
  setup, leaves nothing else reachable;
- the Archive's provenance: encryption (`age`, ADR-0024) authenticates it;
- the identity confirmation, `--archive-database` (BL-E002-114, RT-S02-003);
- `--binary-mode`, which stops client commands in the Dump (RT-S02-001) but not SQL.

`docs/restore.md` tells the operator this.

Rejected:
- **(A) A temporary load account, granted only on the staging database.** It would confine
  the load, but it needs a create-and-drop account feature in `l3io-wordpress-database`,
  and an account to clean up after a kill.
- **(B) Scanning the Dump for statements that reach outside it.** That is a hand-written SQL
  parser, and easy to evade.

**Revisit when:** an operator restores Archives from sources they do not control, or site
accounts commonly hold grants beyond their own database. Option A is then the fix.

## Amendment — 2026-11-07: `CLEAN`'s target check is a heuristic, recorded (Shawn, owner decision 40)

Red team RT-006 (BL-E003-126). Before `CLEAN` empties the Site Root,
`adapters/site_files.py::refuse_unsafe_clean_target` refuses (`UnsafeCleanTargetError`, exit 3)
a Site Root that resolves to the filesystem root or the user's home directory, or that holds
**neither** `wp-config.php` **nor** `wp-includes/` (`_SITE_MARKERS`). The Dry Run's no-op
`clean` calls the same check, so it refuses where the real Run would (AD-4).

**Decided: keep the heuristic as it is; it is not the control.** One marker suffices; requiring
both was considered and not taken (a Site whose `wp-config.php` sits one level up, FR-29, has
only `wp-includes/` in its Site Root). The check catches the shapes a mistyped `--wp-dir`
usually takes. It does not prove the directory is a WordPress Site: a parent directory, or any
directory, that holds either marker passes, and the root is resolved once before the walk (a
narrow time-of-check window, accepted). The control is FR-7: a clean Mode needs the explicit
`--cleanfirst`, a non-empty Site Root needs `--force`, and a Safety Snapshot is taken first
unless `--no-safety-snapshot` says otherwise. `docs/restore.md` tells the operator so.

**Revisit when:** a clean restore removes a directory that was not a Site in practice, or the
Site Root gains a marker this package writes itself (then check that marker instead).
