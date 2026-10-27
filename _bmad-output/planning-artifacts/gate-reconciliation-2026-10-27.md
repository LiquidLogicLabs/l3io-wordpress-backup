---
friendly_name: "wordpress-backup hardening: doctor + handoff reconcile"
session_id: cmvgs1vhh0gy7tf01cfd5vtqt
date: 2026-10-27
gate: _bmad-output/implementation-artifacts/epic-002/arch/arch-gate-review.md (2026-10-27 re-run, AG-30…AG-44)
---

# E002 gate reconciliation — 2026-10-27

Story files under `_bmad-output/implementation-artifacts/epic-002/sprint-0*/stories/`. This pass
edited in place: superseded text was rewritten to the current decision and the old version
deleted, so each contract is stated once. Original `**Acceptance Criteria:**` blocks and
frontmatter of existing stories are untouched (notes added beneath the block in E002-S01-001
and E002-S02-009). The three new stories (E002-S01-010, E002-S02-011, E002-S02-012) have full
frontmatter, user story, `**Implements:**`, an AC block and all ten ac-dimension sections.

## Findings → changes

| ID | What changed | Where |
|---|---|---|
| AG-30 | `SiteLockedError` and `PreflightFailedError` defined in sprint 1 in `domain/errors.py`; S02-008 / S02-007 only raise them. `real_adapters` is S01-004's (sprint 1); `dry_run_adapters`, `BackupRequest.dry_run` and the selection moved to S02-007. `RunContext.site_lock`, `open_run(site_root, site_lock)` and every lock-order / lock-join / lock-refusal assertion (incl. "root span before lock", "`set_outcome` on lock refusal", "backup acquires no lock when joined") moved to S02-008. S01-005's end-to-end exit-code test covers the sprint-1-reachable codes and carries `PENDING_CASES = {LOCKED: "E002-S02-008"}` (skipped, and failing if it outlives its case); S02-008 adds the `LOCKED` case and empties it; S02-007 adds the preflight `NOT_READY` case. | S01-001 §Interface contracts (errors); S01-004 §Non-goals, §Interface contracts, §Testability; S01-005 §Non-goals, §Testability; S02-007 §Interface contracts ("Selection"), §Testability; S02-008 §Interface contracts, §Testability |
| AG-31 | ADR-0016: `AdapterSet` defined once in S01-004 (`ports/adapter_set.py`, sprint-1 ports + `tracer`); S02-007 adds `archive_writer`, S02-012 adds the restore/journal fields, S02-008 adds none (the lock precedes the bind). `open_run` creates the context without adapters; `ctx.bind_adapters(...)` once, only by the root that opened the Run, after the lock and the `wp-config.php` read; `AdaptersNotBoundError` / `AdaptersAlreadyBoundError` named. `join_or_open` returns `RunHandle(ctx, opened)`. A contract test finds exactly one `class AdapterSet`. | S01-004 §Interface contracts, §Error handling, §Testability; S02-007 §Interface contracts; S02-008 §Interface contracts; S02-012 §Interface contracts |
| AG-32 | Cycle broken: `UsageError` moved to S01-001 `domain/errors.py`; S01-006 raises `UsageError` directly and no longer reads S01-005's accepted-and-explained set (which moved to S02-011) or needs S01-005's flags (moved to S02-011). True direction: S01-005 → S01-006 (rendering uses the Run's `redact`). S01-003 edges added where real (S01-004 `drive_verify`; S01-005 maps S01-003's errors; S01-006's sentinel runs `verify`). Final graph below. | S01-001, S01-005, S01-006 |
| AG-33 | Baseline is `l3io-wordpress-database` v0.2.0 (rev `48a7da9`, `>=0.2.0,<0.3`) and `l3io-wordpress-config` v0.1.1, stated once in S01-001; all v0.1.0 / `47384ba` / line-number citations removed; signatures re-checked against `git show v0.2.0:src/l3io/wp/database/{transfer,inspection,provision,cli,wpconfig}.py` (`dump(..., passphrase=)`, `load(..., replace=, identities=, passphrase=)`, `survey`, `ensure`, `revoke`, `drop_database`, `cli.Exit`, `WpConfigFile`, `--log-level`/`--log-format`). `stored_siteurl` re-checked: still not exposed at v0.2.0. S01-006's "< 0.2.0 refused" path and its sentinel variant deleted; the passphrase sentinel parameter now drives `resolve_passphrase`, and E004-S01-003 applies it to an encrypted Run. | S01-001 §Interface contracts, §Existing-library; S01-004; S01-005; S01-006 §Non-goals, §Interface contracts, §Testability; S02-007; S02-009 (note under AC block) |
| AG-34 | Config-writer mode and fsync tests are ordinary assertions against v0.1.1; the `xfail(strict=True, …v0.1.0…)` text is gone. | S02-009 §Testability |
| AG-35 | `UnsupportedStorageSchemeError(RefusedError)`, new `ArchiveExistsError(RefusedError)` (distinct from `ArchiveWriteError`, which keeps I/O failures incl. `ENOSPC`), `RunContextActiveError(RefusedError)`. S01-005's first `match`-based mapping list deleted; the class attribute is the one mechanism and the code table is stated once in S01-005. | S01-001 §Interface contracts; S01-004 §Interface contracts; S01-005 §Interface contracts, §Testability |
| AG-36 | ADR-0018: `BackupError.__str__` does no redaction; redaction at `cli/exit.py::render_failure` and `redact_event` (and Epic 6's tracing adapter). Leak test gains item 3: `str()`/`repr()`/`traceback.format_exception()` of every exception the drivers raise, unredacted. | S01-001 (errors); S01-005 §Interface contracts, §Security, §Testability; S01-006 §Interface contracts, §Testability |
| AG-37 | One signature each: `write_archive(out, manifest_builder, members, *, cipher) -> Manifest` (`manifest_builder` defined) in S01-010; `site_descriptor_from_wp_config(site_root, *, config_file)` in S01-001; `build_backup_plan(site, scope, target, *, encrypted)` in S01-010 (S01-002 reuses it; S02-007 adds a field, not a parameter); `extract_archive(source: StorageTarget, destination, view, *, roles, exclude=frozenset()) -> ExtractResult` in S01-003; `DumpReadiness` removed — `Readiness` and `DatabaseLocation` defined in S01-001 `domain/database.py`, backup port methods keyed on `DatabaseLocation`; the `match` exit mapping removed; S01-003's v0 `MEMBER_MISSING` rule replaced by the AD-23 three-shape rule. | S01-001, S01-002, S01-003, S01-005, S01-010 |
| AG-38 | `SUPPORTED_FORMAT_VERSIONS: Final[frozenset[int]] = frozenset({1})` in `domain/archive_format.py`; `test_supported_formats.py` asserts equality with the fixture-derived set (glob non-empty; v0 reported accepted-without-fixture). | S01-003 §Interface contracts, §Testability |
| AG-39 | ADR-0017 member index: `MemberKind` (`file`/`dir`/`symlink`), `ManifestMember(path, role, kind, size, sha256, encrypted, link_target)`, per-kind rules, mode/mtime in the tar header only, `manifest.json` the single exemption. Writer rules (dirs incl. empty, symlinks never followed, hard links stored as files, special files skipped, outward-symlink WARNING) in S01-010; verifier checks (`KIND_MISMATCH`, `LINK_TARGET_MISMATCH`, `UNSUPPORTED_MEMBER_TYPE`, second `manifest.json`) in S01-003; v1 fixture contains an empty dir, a relative symlink and a hard-linked pair. | S01-001, S01-010, S01-003 |
| AG-40 | "proposed story P1" references removed. After the split the restore halves are S02-012's: `real_adapters` binds `UnboundAdapterError` placeholders for restore-half database methods, `site_files_*` and `config_*` until **E002-S02-009** replaces them; S02-009 depends on S02-012, not the reverse. S02-007 no longer has a restore half. | S02-012 §Interface contracts; S02-009 §Interface contracts ("Wiring") |
| AG-41 | S02-012 declares `ports/journal.py` (`RunJournalQuery.incomplete`, `RunJournalCommand.begin_run/phase_started/step_completed/phase_completed/finish_run`), `domain/journal.py` (`JournalHeader` with E003-S02-005's fields incl. the 2026-10-27 additions, `JournalEntry`, `JournalRecord`) and `NoopRunJournal`; and the reconciled `load_dump(location: DatabaseLocation, source: IO[bytes] \| Path) -> LoadResult`. | S02-012 §Interface contracts |
| AG-42 | Splits done: S01-001 (domain + ports + the database adapter's backup half) / **S01-010** (writer, store, workspace, plan, phase runner, backup root); S01-005 (exit codes, `exit_code_for`, rendering, docs) / **S02-011** (`domain/restore.py`, AD-25 table, all restore/encryption/exclusion/credential flags, `doctor`, accepted-and-explained set, frozen-surface test); S02-007 (backup Dry Run, preflight, archive-writer port) / **S02-012** (restore halves, `SiteFiles*`, `Config*`, `SearchReplace`, Run Journal ports, no-op twins). E002-S01-001's AC block gained the split note; E002-S01-005's and E002-S02-007's AC blocks hold no criterion that moved, so no note. | S01-001, S01-010, S01-005, S02-011, S02-007, S02-012 |
| AG-43 | ADR-0015 amendment: only `adapters/database.py` (and Epic 5's `credential_providers.py`) construct database credential classes; S01-006 adds `CredentialSpec` (`ExplicitCredentialSpec` / `AwsSecretCredentialSpec`) in `domain/credentials.py`, builders in `adapters/database.py`, and `cli/credentials.py` turns flags into specs and delegates. `BackupRequest.credentials: CredentialSpec \| None` (the separate `db_credentials_aws` field is gone). Contract test walks the whole package; a second check forbids database types in public request annotations. | S01-001 §Interface contracts; S01-004 §Interface contracts; S01-006 §Interface contracts, §Testability; S02-009 §Interface contracts |
| AG-44 | ADR-0019: the five-point backup Dry Run scope; `ArchiveWriterCommand.write(...)` with `TarArchiveWriter` / `NoopArchiveWriter`; the executor stops calling `write_archive`/`collect_site_root`; VERIFY/PUBLISH inside the no-op store do nothing; FR-19's Verification is restore-only; effects test (no Site Root content read, no `write_archive` call). | S02-007 §Outcome, §Non-goals, §Interface contracts, §Testability |
| ADR-0017 Concern | One line each: outward/absolute symlinks are stored and verify, refusal at extraction is an Epic 3 / AD-11 decision. | S01-003 §Non-goals; S01-010 §Non-goals |
| MINORs cleared in rewritten text | Timestamps: `Z`, stated once (S01-001), referenced by S01-004 and S02-008. Stale P3 / "proposed story" → Epic 8 (E008-S01-001) / E002-S02-009 / E003-S01-006. Duplicated Spec lines removed everywhere. Also, because the text was being rewritten: `parse_storage_target(scheme, path)` with the split in `cli/`; `Manifest.source` a `MappingProxyType`; VERIFY/PUBLISH emitted once each from `publish()`; S01-010's interim `structural_check` publish verifier until S01-003; `LockHandle.site_root`; `UnboundAdapterError` instead of a bare `RuntimeError`; S01-002's stale reclassification note and restore `--mode` owner; `read_constants` limited to the restore target's config. | S01-001, S01-002, S01-004, S01-010, S02-008, S02-012 |

## E002 story dependencies after reconciliation

`depends_on` within E002 (none to other epics). Sprint 1 order: 001 → 010 → {002, 003} → 006 → 004 → 005. Sprint 2 order: 007 → {008, 011} → 012 → 009. No sprint-1 story depends on a sprint-2 story; no cycle.

| Story | depends_on |
|---|---|
| E002-S01-001 | — |
| E002-S01-010 | E002-S01-001 |
| E002-S01-002 | E002-S01-001, E002-S01-010 |
| E002-S01-003 | E002-S01-001, E002-S01-010 |
| E002-S01-006 | E002-S01-001, E002-S01-002, E002-S01-003, E002-S01-010 |
| E002-S01-004 | E002-S01-001, E002-S01-003, E002-S01-006, E002-S01-010 |
| E002-S01-005 | E002-S01-001, E002-S01-002, E002-S01-003, E002-S01-004, E002-S01-006, E002-S01-010 |
| E002-S02-007 | E002-S01-001, E002-S01-002, E002-S01-004, E002-S01-005, E002-S01-006, E002-S01-010 |
| E002-S02-008 | E002-S01-001, E002-S01-004, E002-S01-005, E002-S02-007 |
| E002-S02-011 | E002-S01-001, E002-S01-002, E002-S01-005, E002-S01-006, E002-S01-010, E002-S02-007 |
| E002-S02-012 | E002-S01-001, E002-S01-003, E002-S01-004, E002-S01-006, E002-S02-007, E002-S02-011 |
| E002-S02-009 | E002-S01-001, E002-S01-003, E002-S01-006, E002-S02-012 |

Differences from the current state YAML (not edited here): S01-004 drops nothing but gains S01-003, S01-010; S01-005 drops nothing, gains S01-003, S01-010; S01-006 gains S01-002, S01-003, S01-010; S01-002/S01-003 gain S01-010; S02-007 drops S01-003, gains S01-005, S01-010; S02-009 drops S01-004 and S02-007, gains S02-012; S01-010, S02-011, S02-012 are new.

## Changes Epic 3 must adopt

Epic 2's contracts govern; these E003 texts differ and must be edited by Epic 3's owner.

1. **`load_dump`** (E003-S02-005 §Interface contracts, "Load"): `DatabaseCommand.load_dump(location: DatabaseLocation, source: IO[bytes] | Path) -> LoadResult` — address the staging database as `location.with_name(<staging or load name>)`; drop the `Connection(...)`, `wordpress`, `replace=None` and `identities=...` arguments (the WordPress source is bound into `AdapterSet.restore_database_command`; the adapter passes `replace=None, identities=None, passphrase=None`; an encrypted Dump member is decrypted upstream through `MemberCipher.decrypt`).
2. **Other restore-half calls** (E003-S02-005 "Creation", "Post-load check", "Failure before the cutover"): `ensure_database(location)`, `survey_target(location, table_prefix=..., wordpress_user=...)`, `drop_database(location, confirm=...)` — no `Connection`, no `admin`/`wordpress` arguments.
3. **Run Journal types**: `ports/journal.py`, `domain/journal.py` and `NoopRunJournal` are Epic 2's (E002-S02-012); Epic 3 implements only `adapters/journal.py`. `JournalRecord` is `(header: JournalHeader, entries: tuple[JournalEntry, ...])`; `interrupted_phase` is derived in `core/restore` from `entries`. `JournalHeader` refuses `DB_PASSWORD` in `cutover_targets`/`prior_constants`.
4. **Adapter binding** (E003-S01-006 `drive_restore`): bind adapters with `ctx.bind_adapters(...)` only when the Run was opened by `drive_restore`, after the Site Lock and the `wp-config.php` read (ADR-0016) — not as step 1; pass `journal=(query, command)` and `admin_source=` to `real_adapters`; unsupplied ports raise `UnboundAdapterError`, not `RuntimeError`.
5. **Credentials**: build administrator and relocated-WordPress sources through E002-S01-006's `cli/credentials.admin_spec/admin_source` and `wordpress_source` from a `CredentialSpec`; `cli/restore.py` constructs no database credential class.
6. **Exit codes**: `RunContextActiveError` is a `RefusedError` (exit `REFUSED`); restore flags not yet bound are listed in E002-S02-011's `NOT_YET_IMPLEMENTED`, and E003-S01-006 removes the entries it binds.
7. **Restore vocabulary and flags** now come from E002-S02-011 (not E002-S01-005): update E003 references to `RestoreMode`, `RestorePhase`, `OverrideSet`, `SnapshotPolicy`, `resolve_flags`, `--drop-database`, `--no-safety-snapshot`, `--snapshot-dir`, `--resolve-interrupted-run`.
8. **Port and Protocol owner references** to "E002-S02-007" for `SiteFiles*`, `Config*`, restore database halves and `AdapterSet.restore_database_*` now read E002-S02-012 (declaration) and E002-S02-009 (implementation).

Other epics, for their owners: Epic 6 — `BackupRequest.credentials` is a `CredentialSpec`, `BackupRequest.dry_run` is added by E002-S02-007, `api/` must construct no database credential class. Epic 4 — `AdapterSet` carries no `cipher`/`exclusion` field; both are arguments of `backup(...)` and `BackupRequest`.

## Decisions taken in this pass (not in the brief) — assumptions, not OPEN

- `adapters/database.py`'s backup half (descriptor construction, `locate_wp_config`, `check_dump`, `dump_to`) stays in E002-S01-001; E002-S01-010 holds the writer/store/workspace/plan/root, per the brief's list.
- The cipher and exclusion policy are per-call arguments of `backup(...)`, not `AdapterSet` fields, because E003-S01-006 already passes them per call (the snapshot uses `INCLUDE_EVERYTHING`); one source for each value.
- `VerificationResult`/`VerificationFailure`/`VerificationReason` live in `domain/archive_format.py`, because `VerificationFailedError` in `domain/errors.py` carries the result and `domain/` cannot import `core/`.
- New names introduced to state existing decisions: `AdaptersNotBoundError`, `AdaptersAlreadyBoundError`, `RunHandle`, `UnboundAdapterError`, `ArchiveExistsError`, `LoadFailedError` (already in S02-009), `UNSUPPORTED_MEMBER_TYPE`, `LINK_TARGET_MISMATCH`, `TarMemberType`, `ArchiveWriterCommand.write(...)`, `PENDING_CASES`, `NOT_YET_IMPLEMENTED`, `cli/adapter_sets.py`, `structural_check`.
- Preflight mapping of a `check_dump` verdict: `UNREACHABLE` → `DatabaseUnreachableError`, any other blocker → `PreflightFailedError` (same in real and Dry Run, so codes match).
- `--wp-config` on `backup` is registered by E002-S01-010 (in effect at once); on `restore` by E002-S02-011.

## Residuals the validation grep still shows (intended)

- `E002-S02-009` AC block: "`l3io-wordpress-database` v0.1.0" — the block may not be edited; a dated correction note sits under it.
- `E002-S02-011` §Testability: `xfail(strict=True, …)` for `python -m wpbackup2` — Shawn's 2026-10-20 decision for AD-22's open legacy-tree item; it states a current fact (module invocation does not yet work).

## OPEN

None.
