# E002 arch gate re-check — `l3io-arch-review` findings

- **Date:** 2026-10-27
- **Reviewer:** `l3io-arch-review` (re-validation, /l3io-execute step 04 §6; not a full review)
- **Original record:** `reviewer-l3io-arch-review-2026-10-27.md` (8 MAJOR), consolidated as AG-30 (first half), AG-31–AG-36, AG-43, AG-44
- **Resolution record:** `_bmad-output/planning-artifacts/gate-reconciliation-2026-10-27.md`

## Findings

AG-30: RESOLVED — S01-001 §Interface contracts defines `SiteLockedError` and `PreflightFailedError` in sprint 1 ("raised by E002-S02-008/-007"); S01-004 §Non-goals hands `site_lock`, lock acquisition, lock tests, `BackupRequest.dry_run` and `dry_run_adapters` to S02-008/S02-007, and S01-004 §Interface contracts/§Testability no longer name `SiteLock`, `LockHolder` or a dry-run set; S01-005 §Testability's `PENDING_CASES = {LOCKED: "E002-S02-008"}` fails if it outlives its case, and S02-008 §Testability removes it. No sprint-1 section I read needs a sprint-2 type.

AG-31: RESOLVED — ADR-0016 (Option D, plus its "Resolution of the Concern") and S01-004 §Interface contracts: `AdapterSet` defined once in `ports/adapter_set.py` with sprint-1 ports; `open_run` takes no adapters; `bind_adapters` is write-once with `AdaptersNotBoundError`/`AdaptersAlreadyBoundError`; `join_or_open` returns `RunHandle(ctx, opened)` and only the opener binds. S02-007 and S02-012 §Interface contracts add fields only; S02-008 adds none and its `test_lock_order.py` asserts lock → `wp-config.php` read → source → `bind_adapters` → first phase. `test_adapter_set_owner.py` enforces the single definition.

AG-32: RESOLVED — `UsageError` is in S01-001 §Interface contracts (`domain/errors.py`); S01-006 §Non-goals moves flag registration to S02-011 and S01-006 no longer reads S01-005's accepted-and-explained set; the reconciliation's final graph has S01-005 → S01-006 one way and no cycle. Carried condition, not a reopen: the record states the `state/planned/epic-002` `depends_on` YAML was **not** edited (and S01-010, S02-011, S02-012 are not yet in it); the listed edge changes must be applied to state before sprint dispatch, or the executor will order sprint 1 without S01-010.

AG-33: RESOLVED — S01-001 §Interface contracts "Dependency baseline" states v0.2.0 (rev `48a7da9`, `>=0.2.0,<0.3`) and config v0.1.1 once, signatures not line numbers; `stored_siteurl` re-checked at v0.2.0 (`Manifest.source`); S01-006 §Non-goals/§Testability drop the "< 0.2.0 refused" path and point the passphrase sentinel at `resolve_passphrase`. A grep of all E002 stories finds no `v0.1.0`, `47384ba` or line-number citation except S02-009's immutable AC block, which carries the dated correction note.

AG-34: RESOLVED — S02-009 §Testability: `test_config_writer.py` is "ordinary assertions against the pinned v0.1.1, no `xfail` marker" for the `0640` mode and the fsync spy; no `<release>` placeholder remains.

AG-35: RESOLVED — S01-001 §Interface contracts makes `UnsupportedStorageSchemeError`, new `ArchiveExistsError` (separate from `ArchiveWriteError`, which keeps I/O and `ENOSPC`) subclasses of `RefusedError`; S01-004 makes `RunContextActiveError(RefusedError)`; S01-005 §Interface contracts states one mechanism (class attribute) and the code table once, with "no `match` over classes", and §Testability asserts `RefusedError` subclasses by walk and forbids `match`/`isinstance` chains under `cli/`.

AG-36: RESOLVED — ADR-0018 (Option C); S01-001 §Interface contracts: `BackupError.__str__` does no redaction; S01-005 §Interface contracts/§Security: `render_failure(exc, *, redact=...)` redacts at the boundary; S01-006 §Interface contracts names the three boundary call sites, and §Testability adds leak-test item 3 (`str`/`repr`/`format_exception` of every driver exception, unredacted) and keeps `test_single_redaction.py` as the guard that `domain/` holds no redaction code.

AG-43: RESOLVED — ADR-0015 Amendment; S01-001 §Interface contracts: only `adapters/database.py` (and Epic 5's `credential_providers.py`) constructs database credential classes, and `site_descriptor_from_wp_config` returns the `WpConfigFile` source; S01-006 §Interface contracts adds `CredentialSpec` in `domain/credentials.py`, builders in `adapters/database.py`, and `cli/credentials.py` delegating; S01-004 `BackupRequest.credentials: CredentialSpec | None`; S01-006 §Testability `test_credential_sources_built_once.py` walks the package with a planted violation and checks public request annotations for `l3io.wp.database` types. S02-012's `real_adapters` sources are "built by E002-S01-006's `cli/credentials.py`, never by the root".

AG-44: RESOLVED — ADR-0019 (Option C) and S02-007 §Outcome/§Non-goals/§Interface contracts: `ArchiveWriterCommand.write(...)` with `TarArchiveWriter`/`NoopArchiveWriter`; `execute_backup_plan` stops calling `write_archive`/`collect_site_root`; each step's binding stated; no-op store's `publish()` calls no verifier; FR-19 Verification is restore-only. §Testability `test_backup_dry_run_effects.py` spies `write_archive`, `collect_site_root`, the verifier and Site Root reads, plus an AST check of `cli/backup.py`.

## New

NEW MINOR: [Core §3 contract coherence; ADR-0018] E002-S01-006 §Interface contracts specifies `ExplicitCredentialSpec.__repr__` "shows `password='***'`" in `domain/credentials.py`, while S01-006 §Testability's `test_single_redaction.py` fails on any module other than `adapters/redaction.py` containing the literal `"***"`, and `domain/` may not import `adapters/redaction.REDACTED`. The two contracts in the same story cannot both hold. The likely dev fix (exempting `domain/credentials.py`) weakens the guard ADR-0018 relies on to keep redaction out of `domain/`. Remediation: have the `repr` omit the password field (e.g. `ExplicitCredentialSpec(username='u', password=<hidden>)` without the `***` literal), and keep the guard without exemptions.

## Gate

9 of 9 re-checked findings RESOLVED. 1 new MINOR (auto-defers to the backlog under the step-04 policy). One carried condition on AG-32: the `depends_on` edges in the reconciliation record must be written to `state/planned/epic-002/` before sprint 1 is dispatched.

Sections read:
- `_bmad-output/implementation-artifacts/epic-002/arch/reviewer-l3io-arch-review-2026-10-27.md` (all)
- `_bmad-output/planning-artifacts/gate-reconciliation-2026-10-27.md` (all)
- `docs/adr/0016-run-context-binds-adapters-once.md` (all), `0018-redaction-at-the-boundary.md` (all), `0019-backup-dry-run-scope.md` (all), `0015-credentials-delegated-to-database.md` §Amendment — 2026-10-27
- E002-S01-001 §Non-goals, §Interface contracts (grep-filtered: baseline, `domain/errors.py`, `adapters/database.py`, `Manifest`, `WpConfigFile`)
- E002-S01-004 frontmatter, §Non-goals, §Interface contracts, §Error and edge case handling, §Testability
- E002-S01-005 §Non-goals, §Interface contracts, §Security, §Testability
- E002-S01-006 frontmatter, §Non-goals, §Interface contracts, §Testability
- E002-S02-007 §Outcome, §Non-goals, §Interface contracts, §Testability
- E002-S02-008 §Interface contracts, §Testability
- E002-S02-009 AC block and correction note, §Interface contracts (grep-filtered: restore halves, Wiring), §Testability
- E002-S02-012 §Interface contracts (`load_dump`, `AdapterSet`, `real_adapters` bullets)
- Greps over all E002 stories: stale baseline (`v0.1.0`, `47384ba`, line numbers, `xfail`); cross-story references; sprint-1 mentions of `SiteLock`/`dry_run`/lock and preflight errors
