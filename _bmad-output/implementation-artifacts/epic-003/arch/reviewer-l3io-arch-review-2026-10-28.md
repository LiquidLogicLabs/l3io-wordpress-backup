# Arch review (Mode B) — E003 "Restore a site without destroying it"

- **Reviewer:** `l3io-arch-review`, Mode B (design review of story files; no code exists yet)
- **Date:** 2026-10-28
- **Scope:** 6 stories — `epic-003/sprint-01/stories/E003-S01-{001,002,006}.md`, `epic-003/sprint-02/stories/E003-S02-{003,004,005}.md`
- **Standards loaded:** `standards-core.md` §1–10; `standards-python.md` (stack: `pyproject.toml`, `uv.lock`)
- **Result:** 2 BLOCKER, 10 MAJOR, 11 MINOR

## Executive summary

The stories adopt all eight E002-gate changes, and most of the restore path is now specified against E002's real contracts. The plan as data, the staging cutover, administrator-only existence, the re-entrant lock and the single extraction path all hold up. Two defects can destroy a site as written. (1) After a kill once the cutover has started, the journal's undo text still tells the operator to drop the staging database, which is by then the live one. (2) ADR-0017's absolute or outward symlinks pass `VERIFY` and abort `EXTRACT` after `CLEAN`, and the Safety Snapshot of that same site cannot be restored either. The most important action is to make the undo derivation step-aware, and test the undo text at every kill point.

## Findings

BLOCKER: E003-S02-005 §Interface contracts ("Run Journal" undo table, L98–105; "Refusal on an incomplete journal", L107; "Journaling the cutover", L130) and §Error handling L145. `IncompleteRunError.undo` is defined as "that entry's `undo`", meaning the last open `phase_started` entry. The undo is derived by `undo_for(phase, header)`, a `match` over `RestorePhase`. But the cutover and the loser drop are *steps* inside `LOAD_DUMP`, not phases, and `step_completed` carries no undo. So after a kill between the last cutover write and the loser drop or `phase_completed(LOAD_DUMP)`, the reported undo is `LOAD_DUMP`'s pre-load text, "live site untouched; drop staging database `<staging>`". By then `<staging>` is the database the site runs on. L130 overrides the text only "when the interrupted step is the cutover", and L145's "the undo names the old database" has no derivation behind it. The kill harness (L199–204) asserts the refusal, phase and Run ID, but never the undo text, so nothing catches this. Spec: `ARCHITECTURE-SPINE.md#ad-18--a-killed-run-must-be-recoverable` (AD-18.1, "what would undo it"); NFR-1. Remediation: derive the undo from `(interrupted_phase, last step_completed, header)` in `core/restore.py`, with one row per step boundary: pre-load, loaded, cutover partial, cutover complete, revoke done, drop done. Make the "cutover" and "loser removal" rows reachable. Add a per-*k* assertion on the undo text to `test_kill_mid_restore.py`, with the scope derived from the recording run as it is now.

BLOCKER: The ADR-0017 Concern is not handled by any E003 story. Absolute and outward symlinks pass `VERIFY` (`STRUCTURAL_ONLY` or `VERIFIED`), then `extract_archive`'s `filter="data"` aborts part-way through `EXTRACT`. In `*-clean` modes this happens after `CLEAN` has emptied the Site Root (E003-S01-001 §Interface contracts "Extraction", L63; E003-S02-005 Non-goals L52). The Safety Snapshot of the same site carries the same link (E003-S02-003 L56, "the snapshot *is* a backup"), so restoring the snapshot aborts the same way, and the "a bad restore is reversible" outcome (FR-8) is false for such sites (for example `wp-content/uploads -> /mnt/media/uploads`). A Dry Run also reports "would restore" where the real Run fails (AD-4). Spec: `ARCHITECTURE-SPINE.md#ad-11--an-archive-is-untrusted-input` ("Verification … completes before the first destructive step"), `#ad-4--dry-run-is-adapter-selection-never-a-conditional`, `docs/adr/0017-manifest-v1-member-index.md` §Concern. Remediation: Epic 3 must choose, and record in an ADR, between the options under Carried items below. Every option needs a pure pre-destruction check over `ArchiveView` in `core/restore.py`, run before `SNAPSHOT` and in Dry Run.

MAJOR: Dry Run of a restore fails where the real Run succeeds, because the design has *queries that read the result of a suppressed command*. Three cases:
- E003-S02-003 L61: `take_snapshot` runs `verify_archive` on the published path, which a Dry Run never writes, so it raises `SnapshotFailedError`.
- E003-S02-005 L70: the post-load `survey_target(load_location)` runs against a staging database that `NoopDatabase` never created, so it raises `StagingCheckFailedError`.
- E003-S02-004 L68–69: `restored` can never be true under no-op adapters, so "exit `OK` only if … `restored is True`" fails every database-mode Dry Run.

The Dry Run bullets of S02-003 (L92), S02-004 (L99) and S02-005 (L153) do not address any of this. Nor does E003-S01-001 (L64) or E003-S01-006 step 8 say whether `execute_restore` runs at all in a Dry Run. Spec: `#ad-4--dry-run-is-adapter-selection-never-a-conditional`. Remediation: state that `execute_restore` runs under the dry-run set (AD-4). Give each post-effect check a query whose no-op pairing is defined: for example, the no-op command returns a result that marks it as not performed, and the post-check takes that result as an input rather than re-reading the world. Define `restored` and the exit rule for `dry_run` in `cli/restore.py`, the only place `dry_run` may be named. Extend `test_dry_run_restore.py` to assert exit `OK` for every mode against a clean target.

MAJOR: Ownership of `PREFLIGHT` and `VERIFY` is contradictory, and so is the route of `required_tools` into preflight.
- E003-S01-006 steps 4–5 (L62–63) run `PREFLIGHT` and `VERIFY` in the root *before* the plan exists (step 6).
- E003-S01-001 L59, L62 put `PREFLIGHT` and `VERIFY` in every plan, and has the executor's `VERIFY` phase call `verify_archive`. That means two full archive reads and two `phase_completed` records, and E003-S01-006 L69 forbids the root from naming a phase.
- `RestorePlan.required_tools`, including `extra_tools` (E003-S01-006 L70, "the only route by which the passphrase backend reaches preflight") and `relocation.required_tools`, is computed at step 6, after step 4 has run preflight. The decided 2026-10-28 route is therefore not wired.
- Where `begin_run` sits relative to `PREFLIGHT`, `VERIFY` and `SNAPSHOT` is also unstated. E003-S01-001 L107 implies after `VERIFY`, while `JournalHeader.snapshot_path` needs the snapshot name.

Spec: `#ad-17--restore-phase-order-is-data-not-a-diagram`, `#ad-13--external-tools-are-checked-at-the-port-before-the-first-effect`. Remediation: pick one owner. Either the root performs `PREFLIGHT`/`VERIFY` and the executor skips them, with a stated split, or plan first and let the executor run all phases. The second needs `ArchiveView` before verification, which `verify_archive` returns, so a two-stage plan is the likely shape. Either way, check `plan.required_tools` before `SNAPSHOT`, and state where `begin_run` is written.

MAJOR: The stories disagree on which database the FR-7 database guard checks. E003-S01-002 L236 surveys the *live* database (`site.database_location()`), the one being replaced. E003-S02-005 L127 says "the FR-7 database emptiness check (E003-S01-002)" addresses `load_location`, which without relocation is the staging name. That database never exists, so the guard would never refuse. E003-S02-004 test (c) L135 assumes the live-database reading. Spec: `prd.md#52-restore` FR-7 ("Restoring over an existing database without explicit authorization exits non-zero"). Remediation: state in both stories that FR-7 checks the live database. When `relocation.load_database` is set, it *also* checks that operator-named database. Add a unit case for each in `test_target_guard.py`.

MAJOR: E003-S02-005 L96 and L146 never define when a failure is "orderly". `finish_run()` (E002's, no argument) files a Run with an open `phase_started` under `failed/`, which does not block. The post-load-check failure is to be filed there. Every failure after a destructive step must stay blocking (E003-S01-001 L112). Nothing says who decides, so the natural `finally: journal.finish_run()` unblocks a half-extracted site. Spec: `#ad-18--a-killed-run-must-be-recoverable`. Remediation: state the rule in `core/restore.py` as a pure predicate over the entries. A failure is orderly only if no `CLEAN`/`EXTRACT`/`REWRITE_CONFIG` phase started, no cutover step completed, and the staging drop succeeded. The executor calls `finish_run` on failure only when the predicate holds. Add a test that a failed `EXTRACT` leaves `current.json` in place.

MAJOR: Resolving an interrupted journal cannot be built as written.
- E002-S02-012's `RunJournalCommand` has no resolve method, so moving `current.json` to `resolved/` (E003-S02-005 L108) has no port method and no `NoopRunJournal` twin. E003-S01-006 step 2 (L60) performs it with the real pair, built *before* the dry-run wrap, so `--whatIf --resolve-interrupted-run` would really resolve.
- Completing a cutover-interrupted set (E003-S01-006 L88; E003-S02-005 L144) needs `ConfigQuery`/`ConfigCommand` and the relocated WordPress source. Both are bound in step 3, after step 2 has already stopped the Run.

Spec: `#ad-4--dry-run-is-adapter-selection-never-a-conditional`, `#ad-18--a-killed-run-must-be-recoverable`. Remediation: E002 adds `RunJournalCommand.resolve(run_id, resolved_by)` with its no-op twin, which is a cross-epic request since `ports/*` is Epic 2's. Alternatively, Epic 3 records why resolution is exempt from AD-4. Move resolution after the step-3 bind, or give the cutover-completion path its own bind. State that Dry Run resolves nothing.

MAJOR: The Safety Snapshot opt-out and location never reach the plan. E003-S02-003 L64 says "`SKIP` removes `SNAPSHOT` from the plan", with `SnapshotPolicy` having no default. `build_restore_plan(site, mode, overrides, view, target, *, relocation, extra_tools)` (E003-S01-001 L58, L88) and E003-S01-006 step 6 take no `SnapshotPolicy`, no requested location, and no `config_dir` for `snapshot_location` (E003-S02-003 L59). A builder will likely skip `take_snapshot` in the executor instead, leaving `SNAPSHOT` in the plan and in the Dry Run's printed actions. Spec: `#ad-12--every-run-owns-a-private-workspace`; `prd.md#52-restore` FR-8. Remediation: add `snapshot: SnapshotPolicy` (keyword-required, no default) and the resolved snapshot location to `build_restore_plan` and `RestorePlan`. Extend `tools/check_mode_defaults.py`'s planted case to `SnapshotPolicy`.

MAJOR: E003-S02-003's encryption text is stale against the pinned dependency and E003-S01-006. AC L25 and §Interface contracts L62/L89 refuse a passphrase-only configuration with `EncryptionUnavailableError` "until `l3io-wp-database` 0.2.0 lands". But `pyproject.toml` already pins `l3io-wordpress-database>=0.2.0,<0.3`. E003-S01-006 L57/L65 passes `cipher=request.cipher`, built by Epic 4's `build_member_cipher` (including passphrase mode), and the encryption flags stay `NOT_YET_IMPLEMENTED` until E008-S01-001. As written, a builder would add a refusal path that contradicts the AD-26 chain. Spec: `#ad-26--one-passphrase-resolution-chain-or-no-encryption-at-all`; ADR-0014. Remediation: delete the `EncryptionUnavailableError` path. State that the snapshot is encrypted exactly when `request.cipher is not None`, and that in Epic 3 `cipher` is always `None`.

MAJOR: Before Epic 7, the cutover's config path diverges from the descriptor's. E003-S01-006 step 3 builds the Site Descriptor from `locate_wp_config(site_root, …)`, which finds WordPress's one-level-up `../wp-config.php`. E003-S02-005 L78 uses "`relocation.config_file` or `<site_root>/wp-config.php`" for the cutover, the exclusion and `NoConfigForCutoverError`, and with `RestoreRelocation.none()`, `config_file` is `None`. On a site whose config sits one level up, a `database` restore is wrongly refused. An `all` restore of an in-root Archive gets a *second*, in-root `wp-config.php` extracted and cut over, which WordPress then prefers. Spec: `#ad-3--one-immutable-site-descriptor-parsed-once-at-the-edge`; `prd.md#53-relocation` (FR-29 placement). Remediation: take the cutover, exclusion and journal `wp_config_path` from the descriptor's located config file whenever `relocation.config_file is None`. Add an integration case for a one-level-up config.

MAJOR: E003-S02-005 L66 checks `core/restore.py::staging_name` "with `l3io.wp.database.validate_database_name`". That puts an import of the database package, which carries `pymysql`, into `core/`. The import-linter AD-1 contract cannot see it, because with `include_external_packages = true` it treats `l3io.wp.database` as an opaque node and never follows it to the forbidden `pymysql`. The guard would pass while the rule is broken. Spec: `#ad-1--the-core-computes-plans-only-adapters-perform-effects`; ADR-0015 amendment (no `l3io.wp.database` type in `core/`). Remediation: keep `staging_name` pure, with only the length rule in `core/`. Call `validate_database_name` in `adapters/staging.py`. Optionally add `l3io.wp.database` and `l3io.wp.config` to the AD-1 forbidden list, so the guard reaches the case.

MAJOR: The Run Journal location is a spec-conflict between two stories and the spine (carried OPEN). E003-S02-003 L65 writes the journal "to the snapshot's directory (AD-18)". E003-S02-005 L106/L113 always uses the per-Site default directory, even when `--snapshot-dir` overrides it. Spine AD-18.1 says "the Safety Snapshot's directory". E003-S02-005's reading is buildable and safe, but the two stories cannot both be implemented, and the spine text disagrees for the override case. Remediation: Shawn decides; the options are under Carried items. Then align E003-S02-003 L65 and, if needed, amend the AD-18.1 wording.

MINOR: The `undo_for` signature differs between stories: `undo_for(phase, header, relocation)` (E003-S01-001 L89) and `undo_for(phase, header)` (E003-S02-005 L98). Remediation: one signature, stated once in E003-S02-005. This folds into the BLOCKER's step-aware rewrite.

MINOR: The unencrypted-snapshot WARNING has two event names: `safety_snapshot_unencrypted` (E003-S01-006 L65) and `safety_snapshot_not_encrypted` (E003-S02-003 L102). Remediation: pick one, since tests assert on these names.

MINOR: E003-S01-006 §Security considerations L133 says the archive-config path "extracts only `MemberRole.CONFIG` into the `0700` workspace through `extract_archive`", and calls it "the binding-order OPEN". §Interface contracts L67 decided `probe.read_member(..., max_bytes=1 MiB)`, which writes a `0600` file, and the OPEN is closed. Remediation: rewrite L133 to match L67.

MINOR: Two single-ownership gaps:
- `adapters/restore_runner.py` (E003-S01-001 L56) is absent from Epic 3's "Owns exclusively" in `epics.md#epic-list`, which is recorded only "on precedent".
- E003-S01-006 L68 edits `cli/parser.py::NOT_YET_IMPLEMENTED`, Epic 2's module. Gate change 6 directs this, but the ownership notes do not grant it.

Remediation: add both to the Epic List ownership notes.

MINOR: Cross-story naming residue:
- E003-S02-004 L68 says `RestoreOutcome` is "story 3.1's". It is E003-S01-006's; 3.1 returns `ExecutionReport`.
- The E003-S01-006 AC L26 and `epics.md` story 3.6 still cite "E002-S01-005's table". The note at L32 corrects it, but the AC block is the text a dev reads first.

Remediation: fix the S02-004 sentence and the `epics.md` 3.6 AC line.

MINOR: E003-S02-005 L78 excludes the target's in-root `wp-config.php` from `EXTRACT` and keeps it through `CLEAN` in every mode. A `files` or `files-clean` restore therefore never restores the Archive's `wp-config.php`. That is a defensible choice, but FR-6 says each mode "does exactly what its name says", the operator is not told, and nothing reports it. This is a spec-gap against `prd.md#52-restore` FR-6. Remediation: emit a `planned_action` and a `config_preserved` INFO record naming the kept file, and state the behaviour in the mode help.

MINOR: E003-S02-005 L196 runs the kill harness "through the library entry point", but Epic 6's `api/` lands in a later plan phase (`plan-2026-10-27-v2.yaml`: E006 follows E003). Remediation: drive `drive_restore` directly. A test-opened Run with a bound wrapped adapter set, joined via `ctx=`, gives the injection seam.

MINOR: E003-S02-003 L59 gives the default snapshot directory as `platformdirs.user_state_path(...)` inside the `core/restore.py` bullet. `platformdirs` reads the environment. Remediation: state that the root computes the default and passes it as `requested`, and that `core/` receives only paths.

MINOR: E003-S01-006 step 3 (L61) and L86 bind the extension's `search_replace` and the relocated WordPress source only `if opened`. A joined restore (`ctx` given by an outer root) with relocation uses the outer set, which nothing obliges to carry those seams. Remediation: either refuse relocation on a joined Run, or state the outer root's obligation.

MINOR: Documentation (core §10) has two gaps. No story carries a flow diagram for `drive_restore`'s 8 steps plus the executor. The spine's restore sequence diagram (`#structural-seed`) still shows the cutover in `REWRITE_CONFIG` after `LOAD_DUMP`, against AD-17's two amendments. Remediation: add a Mermaid sequence diagram to E003-S01-006, and flag the stale spine diagram to its owner.

MINOR: E003-S01-002 L236 defines `site_root_empty` as "no entry other than those the Archive's own `wp-config.php` placement rule excludes", then says it is computed by `SiteFilesQuery.is_empty`, which takes no such exclusion. Remediation: state plainly that `is_empty` means no directory entries, or name the excluded entry and the port argument that carries it.

## Changes Epic 3 must adopt

| Change | Status | Evidence |
|---|---|---|
| 1 `load_dump(location, source) -> LoadResult`, staging addressed via `location.with_name(...)`, no `Connection`/`replace`/`identities`, decrypted upstream | adopted | E003-S02-005 L31 (AC note), L64 "The load contract", L67 "Load location", L69 "Load" (`MemberCipher.decrypt` upstream); `test_no_load_replace.py` cited L212 |
| 2 `ensure_database(location)`, `survey_target(location, table_prefix=, wordpress_user=)`, `drop_database(location, confirm=)` | adopted | E003-S02-005 L68, L70, L82, L86; E003-S02-004 L65, L67; E003-S01-002 L236 |
| 3 Journal types are E002's; Epic 3 implements only `adapters/journal.py`; `JournalRecord(header, entries)`; `interrupted_phase` from `entries` in `core/restore`; header refuses `DB_PASSWORD` | adopted | E003-S02-005 L62, L89, L107, L130; E003-S01-001 L49, L61 |
| 4 `ctx.bind_adapters(...)` only when opened, after the lock and the `wp-config.php` read; `journal=(query, command)`, `admin_source=`; `UnboundAdapterError` | adopted | E003-S01-006 step 3 L61; errors L106–107; test L140 |
| 5 Credentials via `cli/credentials.admin_spec/admin_source/wordpress_source`; `cli/restore.py` constructs no database credential class | adopted | E003-S01-006 L57, L61, L87; E003-S02-004 L66, L117 |
| 6 `RunContextActiveError` is a `RefusedError`; `NOT_YET_IMPLEMENTED` entries removed by E003-S01-006 | adopted | E003-S01-006 L68, L105; E003-S02-003 L91 (ownership of the `cli/parser.py` edit is a MINOR above) |
| 7 Restore vocabulary and flags from E002-S02-011 | adopted (AC-block residue) | E003-S01-001 L49, L66; E003-S01-002 L221, L233; E003-S02-003 L64, L67; E003-S02-005 L109. Residue: E003-S01-006 AC L26 still says E002-S01-005, corrected by its note L32 |
| 8 Port/Protocol owners → E002-S02-012 (declaration) / E002-S02-009 (implementation) | adopted | E003-S01-001 L69, L96; E003-S02-004 L63, L77; E003-S02-005 L62, L78. Remaining E002-S02-007 citations are `dry_run_adapters`, port-split and `ToolRequirement`, which the reconciliation keeps |

The reconciliation's OPEN on binding order for a target with no `wp-config.php` (`gate-reconciliation-2026-10-27.md` "Epic 3 adoption → OPEN") is closed in E003-S01-006 L67 (`read_member` → digest → `site_descriptor_from_archive_config`, compared at `VERIFY`). That is sound. Only the stale Security bullet remains (MINOR above).

## Carried items

**Run Journal location under `--snapshot-dir` (E003-S02-005 L113 vs E003-S02-003 L65).**
- **Does it block building as written?** No. E003-S02-005 specifies one concrete, safe behaviour: always the per-Site default directory, keyed by the canonical Site Root hash, with the snapshot's real path in `JournalHeader.snapshot_path`. Sprint 01 does not touch it. What it does block is a consistent sign-off: E003-S02-003 L65 says the opposite, and spine AD-18.1 ("the Safety Snapshot's directory") matches E003-S02-005 only when no override is given. The decision is Shawn's. Options:
  - **(A) Journal always in the per-Site state directory** (E003-S02-005's reading). A later Run finds it with no flag. Cost: amend the AD-18.1 wording to "the per-Site state directory, which is the default Safety Snapshot directory", and fix E003-S02-003 L65.
  - **(B) Journal follows `--snapshot-dir`** (AD-18.1's literal text). A later Run without the same flag cannot see the incomplete journal and proceeds over a half-restored site, which conflicts with NFR-1 unless it is combined with (C).
  - **(C) Journal follows `--snapshot-dir`, plus a pointer file in the per-Site state directory** naming the journal path, which the incomplete query reads first. This is literal AD-18.1 compliance, at the cost of a second durable write per Run and a dangling-pointer case to handle.

**ADR-0017 Concern: absolute or out-of-root symlinks under AD-11's extraction filter.**
- **Do the E003 stories handle it?** No. No E003 story mentions archive symlinks at all; the only symlink text concerns the Site Root path and the snapshot location. E002-S01-003 L42 and E004-S01-002 hand the decision to Epic 3 explicitly. Consequences as written: `VERIFY` passes, then `EXTRACT` aborts part-way, after `CLEAN` in `*-clean` modes. The Safety Snapshot carries the same link and cannot be restored either, and the Dry Run says "would restore". This is filed as a BLOCKER above. Options for Epic 3 to record in an ADR:
  - **(1) Refuse at restore planning.** A pure scan of `ArchiveView` for absolute or outward link targets raises a `RefusedError` before `SNAPSHOT`, so Dry Run refuses too. It is simplest and safest, but such sites cannot be restored by this tool at all.
  - **(2) Restore-side filter wrapping `data_filter`** that skips and reports those links (`planned_action` and a WARNING listing each one) and extracts everything else. The links need recreating by hand, and the restore is no longer "exactly the Archive", so FR-6 reporting is needed.
  - **(3) Refuse at backup time.** That is Epic 2's or Epic 4's module, not Epic 3's, and it does not help with Archives that already exist, so it is insufficient alone.

  Whichever is chosen, the check must run before the first destructive phase (AD-11). It needs a fixture Archive with `uploads -> /abs/path` in `tests/contracts/test_extraction.py`'s sibling restore test, plus a snapshot round-trip case.

## Per-principle walkthrough

| Principle | Result |
|---|---|
| Core §1 Separation of concerns / AD-1 layering | MAJOR (`staging_name` imports `l3io.wp.database` in `core/`); MINOR (`platformdirs` default). Plan/executor/root split otherwise sound; `RestoreOutcome` kept in `cli/` for the `dry_run` name. |
| Core §2 Reuse | PASS. Snapshot is the backup path joined (AD-16); single `extract_archive`; single config writer. |
| Core §3 Design by contract | MAJOR (`build_restore_plan` lacks `SnapshotPolicy`; resolve has no port; FR-7 target contradictory); MINOR (`undo_for` signature, `site_root_empty`). |
| Core §4 Testability | MAJOR (Dry Run post-effect queries; kill harness never asserts undo text, which is part of the BLOCKER); MINOR (harness entry point). Scope-derived parametrisation from enums and `FLAG_MAP` is good. |
| Core §5 Brevity | PASS. |
| Core §6 Comments | Not applicable (no code). |
| Core §7 Dependencies | PASS. `l3io-wordpress-database>=0.2.0,<0.3` and `l3io-wordpress-config>=0.1.1,<0.2` match the stories; no new dependency added. MAJOR on stale 0.2.0 encryption text. |
| Core §8 GA over beta | PASS. |
| Core §9 Logging and correlation | MINOR (event-name mismatch). One `run_id` across the joined snapshot is specified and tested. |
| Core §10 Documentation | MINOR (no restore flow diagram; spine diagram stale). |
| Python overlay | PASS. `mypy --strict` with `assert_never`, `from __future__ import annotations`, `__all__`, pytest markers. |
| Safety (NFR-1, AD-11/17/18/19) | 2 BLOCKER (undo after cutover; symlink extraction), plus MAJOR on the orderly-failure rule and the config path. |
| Single ownership (Epic List, Epic 7 seams) | MINOR (`restore_runner`, the `parser.py` edit). The Epic 7 one-line static-import grant is recorded in `epics.md` and enforced by E003-S01-006's contract test. |
| Security | PASS. Credentials by explicit call, `DB_PASSWORD` resolved just before its write and refused by `JournalHeader`, `credential_sentinel` on the full restore, journal and snapshot, DDL scan, revoke before drop. |

## Gate

The BLOCKER and MAJOR findings must be resolved in the story text, or recorded as accepted ADRs, before sign-off. MINORs defer to the backlog.

## Recommended ADRs

- Restore handling of absolute and outward symlinks (ADR-0017 Concern): option (1), (2) or (3) above.
- Run Journal location under `--snapshot-dir` (amend AD-18.1 or adopt C).
- If resolution stays outside the journal port: why `--resolve-interrupted-run` is exempt from AD-4.

Sections read:
- `.claude/skills/l3io-arch-review/SKILL.md` (loaded by the skill), `references/review-report.md` (all), `references/standards-core.md` L1–150
- `_bmad-output/implementation-artifacts/epic-003/sprint-01/stories/E003-S01-001.md` (all, L1–179), `E003-S01-002.md` (all, L1–120), `E003-S01-006.md` (all, L1–160)
- `_bmad-output/implementation-artifacts/epic-003/sprint-02/stories/E003-S02-003.md` (all, L1–147), `E003-S02-004.md` (all, L1–149), `E003-S02-005.md` (all, L1–239)
- `/tmp/wpb-tools/e003-sections.txt` (all)
- `ARCHITECTURE-SPINE.md` L44–51, L83–96, L157–170, L210–251, L299–304, L342–361, L386–431
- `prd.md` L154–220, L472–491
- `epics.md` L124–203, L384–508 (headings only elsewhere)
- `gate-reconciliation-2026-10-27.md` L60–74, L89–115, L149–175 (plus a grep of lines naming E003)
- `epic-002/arch/arch-gate-review.md` L1–25 (header, and the "Open for Epic 3's gate" line)
- `docs/adr/0017-manifest-v1-member-index.md` L161–174 (Concern), plus grep hits at L53 and L112
- `pyproject.toml` `[tool.importlinter]` block and dependency lines 82–83, 111–113
- E002 contracts, cited-section excerpts only: `E002-S02-012.md` L36, L54, L56–57, L59–62, L102; `E002-S01-003.md` L42, L61, L68, L83, L120–121, L123 (grep excerpts); `E002-S01-004.md` L35, L56–57, L63, L65, L110 (grep excerpts); `E002-S02-008.md` L68, L100 (grep excerpts)
