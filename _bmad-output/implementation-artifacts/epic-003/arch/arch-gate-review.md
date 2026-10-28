# Arch gate review — E003 "Restore a site without destroying it"

- **Date:** 2026-10-28
- **Work type:** CODE
- **Reviewers run:** `l3io-arch-review` (Mode B) first, alone; it reported 2 BLOCKER and 10
  MAJOR, so per step-04 §4 the gate widened to `bmad-agent-architect`, run on the same inputs
  without sight of the first report.
- **Scope:** 6 story files, `epic-003/sprint-01` (E003-S01-001, -002, -006) and
  `epic-003/sprint-02` (E003-S02-003, -004, -005) — derived count of `{story_file_paths}`.
- **Spec alignment:** ON; 39 pointed-to ranges (`spec-align sections`); each report ends with
  `Sections read:`.
- **Raw reports:** `reviewer-l3io-arch-review-2026-10-28.md` (2 BLOCKER, 10 MAJOR, 11 MINOR),
  `reviewer-bmad-agent-architect-2026-10-28.md` (2 BLOCKER, 13 MAJOR, 15 MINOR).
- **Verdict:** **BLOCKED** — 3 BLOCKER and 14 MAJOR after merge (8 confirmed by both
  reviewers, 6 single-source; all block per §5). MINORs deferred to the issues file.
- **E002 gate changes (reconciliation record, "Changes Epic 3 must adopt"):** all 8 adopted
  (l3io-arch-review's table). One residue: the AC block of E003-S01-006 still cites
  E002-S01-005's table (MINOR, below).
- **Numbering:** AG-n here is E003's own series, starting at 1; E002's record has its own.

## Findings

| ID | Sev | Source | Finding |
|---|---|---|---|
| AG-1 | BLOCKER | both | After a kill during or after the cutover, `IncompleteRunError.undo` — the last open `phase_started` entry's undo (E003-S02-005 L98–107) — says "drop staging database", which is by then the live database. Cutover and loser drop are steps inside `LOAD_DUMP`, and steps carry no undo (E002-S02-012). L130/L145 describe the right behaviour; the data model cannot produce it, and the kill harness never asserts undo text. AD-18.1 |
| AG-2 | BLOCKER | l3io-arch-review (BLOCKER); architect (MAJOR) | ADR-0017's Concern is owned by no E003 story: an Archive with absolute or out-of-root symlinks passes VERIFY, then `filter="data"` aborts EXTRACT part-way — in `*-clean` modes after CLEAN emptied the Site Root. The Safety Snapshot of such a site carries the same link and cannot be restored (FR-8's reversibility fails), and a Dry Run reports success. Whatever is chosen must run before SNAPSHOT |
| AG-3 | BLOCKER | architect | Sprint 01 cannot meet its own ACs without sprint 02: E003-S01-001's `LOAD_DUMP` and loser removal are E003-S02-005's staging, yet its `test_restore_modes.py` needs every mode working; E003-S01-006 builds the Run Journal (S02-005) and Safety Snapshot (S02-003); sprint 01 would ship EXTRACT without S02-005's never-extract-over-`wp-config.php` rule |
| AG-4 | MAJOR confirmed | both | PREFLIGHT and VERIFY run twice — in the root before the plan exists (E003-S01-006 steps 4–5) and as plan phases (E003-S01-001 L59/L62); `RestorePlan.required_tools`, including the decided `extra_tools` passphrase route, is computed after preflight ran (AD-13); where `begin_run` sits is unstated |
| AG-5 | MAJOR confirmed | both | A Dry Run fails where the real Run succeeds — queries read results of suppressed commands: snapshot `verify_archive` on a never-written path (E003-S02-003 L61); post-load survey of a never-created staging database (E003-S02-005 L70, l3io); `restored` never true so database-mode Dry Runs exit non-OK (E003-S02-004 L68–69, l3io). AD-4 |
| AG-6 | MAJOR confirmed | both | `SnapshotPolicy` (incl. `SKIP`) and the requested snapshot location never reach the plan: `build_restore_plan` takes neither (E003-S01-001 L58/L88; E003-S02-003 L59/L64; E003-S02-005 L102) |
| AG-7 | MAJOR confirmed | both | Journal lifecycle has no owner and no "orderly failure" rule: nobody says who calls `begin_run`/`finish_run` or when the header is built; `finish_run()` files an open Run under non-blocking `failed/`, so a natural `finally:` unblocks a half-restored site (E003-S01-001 L107/L112; E003-S02-005 L96/L146) |
| AG-8 | MAJOR confirmed | both | `--resolve-interrupted-run` cannot be built: E002-S02-012's `RunJournalCommand` has no resolve method or no-op twin (a Dry Run would really resolve), and completing an interrupted cutover needs config ports and the relocated source bound only in step 3, after step 2 (E003-S01-006 L60/L88; E003-S02-005 L108/L144). Remedy touches `ports/*`, Epic 2's |
| AG-9 | MAJOR confirmed | both | E003-S02-003's encryption text is stale: refuses passphrase-only "until 0.2.0" (AC L25, L46, L62, L89) although v0.2.0 is pinned and E003-S01-006 passes `request.cipher` from `build_member_cipher` |
| AG-10 | MAJOR confirmed | both | The cutover and the in-root exclusion use `relocation.config_file or <site_root>/wp-config.php` (E003-S02-005 L78) and ignore `LocatedConfig.path` from E003-S01-006 step 3: before Epic 7, a `PARENT`-config site is wrongly refused or gets a second in-root config WordPress then prefers |
| AG-11 | MAJOR confirmed | l3io-arch-review (MAJOR); architect (carried item) | Run Journal location conflict: E003-S02-003 L65 (snapshot directory, as spine AD-18.1 words it) vs E003-S02-005 L106/L113 (always the per-Site directory). **Shawn's decision** — options below |
| AG-12 | MAJOR single | l3io-arch-review | The FR-7 database guard: E003-S01-002 L236 surveys the live database; E003-S02-005 L127 says it checks `load_location` — a staging name that never exists, so the guard never refuses |
| AG-13 | MAJOR single | l3io-arch-review | `core/restore.py::staging_name` validated with `l3io.wp.database.validate_database_name` (E003-S02-005 L66) imports the database package into `core/`; import-linter treats the external package as opaque and cannot see the `pymysql` behind it (AD-1) |
| AG-14 | MAJOR single | architect | Plan→executor contract incomplete: `RestorePlan(...)` and `execute_restore(plan, ports, take_snapshot, journal)` (E003-S01-001 L58/L61) carry neither the relocation values, the Site Descriptor, the Archive view, the run id nor the journal header the arms use (L92–95; E003-S02-005 L125–129) |
| AG-15 | MAJOR single | architect | After a later phase fails, nothing carries the Safety Snapshot path to the operator: `ExecutionReport` is never returned on failure and `drive_restore` never prints (E003-S01-006 L80/L126; E003-S02-003 L93; E003-S01-001 L112/L123) |
| AG-16 | MAJOR single (MINOR from l3io-arch-review) | architect | `spec-gap`: `files`/`all` restores never restore an existing `wp-config.php` (E003-S02-005 L78), against FR-6 "does exactly what its name says" and E003-S01-001 L153's own "restored parts match the Archive"; the operator is not told |
| AG-17 | MAJOR single | architect | Sprint 02 needs E003-S02-005 before E003-S02-004: S02-004 L207/L215 changes `adapters/staging.py` and the cutover, both created by S02-005 |

### MINORs — deferred to the issues file (§5)

Confirmed by both: `undo_for` signature differs (S01-001 L89 vs S02-005 L98; folds into
AG-1); unencrypted-snapshot event named twice (`safety_snapshot_unencrypted` S01-006 L65 vs
`safety_snapshot_not_encrypted` S02-003 L102); E003-S01-006 L133 stale "binding-order OPEN" /
`extract_archive` text vs L67's `read_member`; ownership gaps (`adapters/restore_runner.py`,
and S01-006's edit of Epic 2's `cli/parser.py::NOT_YET_IMPLEMENTED`, absent from the Epic
List); `site_root_empty` not testable as written (S01-002 L236); the kill harness runs through
Epic 6's library entry point, which lands later (S02-005 L196); naming residue (S01-006 AC L26
and epics.md 3.6 cite E002-S01-005's table; S02-004 L68 calls `RestoreOutcome` 3.1's).

Single-source: S01-006 L87 says `RootPreflight` ("P3") adds the passphrase backend, against
L70's decided `extra_tools`; v0 config read uses a second read instead of ADR-0016's stored
digest — record as accepted residual; `NonEmptyTargetError.part` should be `parts`
(S01-002 L234/L251); "story 3.1's OPEN on `cli/restore.py`" is resolved (S01-002 L231,
S02-003 L54); a Dry Run refusal cannot print a plan that was never built (S01-001 L113);
`restore_mode_resolved`'s raw flags never reach `drive_restore` (S01-001 L130); files-only
journal headers have no value for `live_database_before`/`load_database` (E002-S02-012); FR-6
"drops database first" vs AD-18.3 — note the supersession (S01-001 L59); `SnapshotResult`'s
module unstated (must be `core/` or `domain/`); `platformdirs` default computed in `core/`
(S02-003 L59); a joined Run with relocation has no obligation to carry the seams (S01-006
L61/L86); no flow diagram for `drive_restore`, and the spine's restore sequence diagram still
shows the cutover in `REWRITE_CONFIG` (flag to the spine's owner).

## Carried items — Shawn's

- **Run Journal location (AG-11).** Both reviewers: E003-S02-005's reading is buildable and
  safe; the conflict blocks a consistent sign-off. Options: (A) always the per-Site state
  directory, amending AD-18.1's wording — the only option under which a later Run without the
  flag still finds the journal, and the only one that works when the snapshot is skipped;
  (B) follow `--snapshot-dir` — unsafe alone; (C) follow `--snapshot-dir` plus a pointer file
  in the per-Site directory; (D, architect) make the journal location not configurable.
- **ADR-0017 symlinks (AG-2).** Options: (1) refuse at restore planning; (2) skip and report
  the links through E002's one extraction function; (3) refuse at backup time — Epic 2/4's
  module, insufficient alone because existing Archives remain; (4, architect) refuse by
  default with an explicit opt-in to skip. Any choice must run before SNAPSHOT.
- **AG-8 crosses into Epic 2:** a `RunJournalCommand.resolve` with a no-op twin changes
  E002-S02-012, a gated (PASSED) but unbuilt story.

## Resolution

MINORs filed as BL-E003-001…019 in `state/issues.yaml`.

Not started. Step-04 §6 prescribes one ADR per blocking finding (17), reserved in one
`adr-reserve` batch, then a re-check of only what changed. Route is Shawn's.
