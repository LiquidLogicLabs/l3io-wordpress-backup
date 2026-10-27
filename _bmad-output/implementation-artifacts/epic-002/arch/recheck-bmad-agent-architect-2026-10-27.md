# Re-check — `bmad-agent-architect` (Winston) — E002 arch gate, 2026-10-27

- **Scope:** the BLOCKER and MAJORs from `reviewer-bmad-agent-architect-2026-10-27.md`, which are AG-30 … AG-42 in `arch-gate-review.md`. For each one the only question is whether the change resolves it.
- **Mode:** headless and read-only. This file is the only output.
- **Result:** 13 resolved, 0 not resolved, 2 new MINORs.

## Findings

AG-30: RESOLVED. `SiteLockedError` and `PreflightFailedError` are now defined in sprint 1 (S01-001 §Interface contracts, `domain/errors.py`). S01-004 §Non-goals moves `site_lock`, lock order, lock join and lock refusal to S02-008, and moves `dry_run_adapters` and the selection to S02-007. S01-004's tests no longer mention a lock or a dry-run set. S01-005 §Testability's `PENDING_CASES = {LOCKED: "E002-S02-008"}` fails if an entry outlives its case. S02-008 §Testability adds `LOCKED` and empties `PENDING_CASES`, and S02-007 §Testability adds the preflight `NOT_READY` case. In state, no sprint-1 story has a `depends_on` on a sprint-2 story.

AG-31: RESOLVED. S01-004 §Interface contracts defines `AdapterSet` once, in `ports/adapter_set.py`, with the sprint-1 ports. `open_run` now takes no adapters. `bind_adapters` is write-once and raises `AdaptersNotBoundError`/`AdaptersAlreadyBoundError`. `RunHandle.opened` controls the bind. S02-007 and S02-012 only add fields, and S02-008 adds none. `test_adapter_set_owner.py` checks that only one definition exists. ADR-0016's "Resolution of the Concern" section replaces the body's earlier "owned by E002-S02-007".

AG-32: RESOLVED. `UsageError` moved to S01-001 `domain/errors.py`. S01-006 §Non-goals no longer depends on S01-005's flags or its accepted-and-explained set, which now belong to S02-011. `dump-plan` shows the state already matches the reconciliation table: the S01-003 edges are on S01-004, S01-005 and S01-006, and S01-006 no longer depends on S01-005. The graph has no cycle.

AG-33: RESOLVED. S01-001 §Interface contracts ("Dependency baseline") states v0.2.0 / `48a7da9` / config v0.1.1 once, citing signatures instead of line numbers. A grep of all twelve E002 stories finds no `v0.1.0`, `47384ba`, `< 0.2.0`, `PassphraseEncryptionUnavailableError` or line citation. The one exception is S02-009's untouchable AC block, and a dated correction note sits under it. S01-006 §Testability's passphrase sentinel parameter now drives `resolve_passphrase`. It no longer exercises a refusal path that cannot be reached.

AG-34: RESOLVED. S02-009 §Testability `test_config_writer.py` now reads "ordinary assertions against the pinned v0.1.1, no `xfail` marker" for both the `0640` mode test and the fsync test.

AG-35: RESOLVED. S01-001 §Interface contracts makes `UnsupportedStorageSchemeError(RefusedError)` and adds a distinct `ArchiveExistsError(RefusedError)`, and `ArchiveWriteError` keeps I/O and `ENOSPC`. S01-004 §Interface contracts makes `RunContextActiveError(RefusedError)`. S01-005 §Interface contracts uses the class attribute as its only mechanism, and the first `match` list is gone. S01-005 §Testability asserts `RefusedError` mapping by walking the subclasses.

AG-36: RESOLVED. ADR-0018 Option C applies. S01-001 `BackupError.__str__` does "no redaction". S01-005 `cli/exit.py::render_failure(exc, *, redact)` redacts at the boundary. S01-006 §Interface contracts names the boundaries, and its §Testability `test_single_redaction.py` keeps `domain/`/`core/` free of redaction code. Item 3 of the leak test exercises the surface that redaction no longer covers.

AG-37: RESOLVED. Each signature now appears once across the stories: `write_archive(out, manifest_builder, members, *, cipher)` (S01-010), `site_descriptor_from_wp_config(site_root, *, config_file)` (S01-001), `build_backup_plan(site, scope, target, *, encrypted)` (S01-010, reused by S01-002 §Interface contracts), and `extract_archive(source: StorageTarget, …, exclude=frozenset())` (S01-003). `DumpReadiness` is gone; `Readiness`/`DatabaseLocation` are in S01-001 `domain/database.py`. The `match` mapping is removed. S01-003 §Interface contracts `classify` replaces the v0 `MEMBER_MISSING` rule with the three-shape AD-23 rule.

AG-38: RESOLVED. S01-003 §Interface contracts declares `SUPPORTED_FORMAT_VERSIONS: Final[frozenset[int]] = frozenset({1})` in `domain/archive_format.py`. §Testability `test_supported_formats.py` asserts that it equals the version set derived from the fixtures, asserts the glob is non-empty, and commits a v1 fixture.

AG-39: RESOLVED. ADR-0017 adds `MemberKind`, `ManifestMember(…, kind, …, link_target)`, the hash rule for each kind, mode and mtime carried only in the tar header, and the single `manifest.json` exemption. These appear in S01-001 §Interface contracts. S01-010 §Interface contracts gives the writer's rules for directories (empty ones included), symlinks, hard links and special files. S01-003 §Interface contracts/§Error handling gives the verifier's reasons `KIND_MISMATCH`, `LINK_TARGET_MISMATCH`, `UNSUPPORTED_MEMBER_TYPE` and a second `manifest.json` → `UNDECLARED_MEMBER`.

AG-40: RESOLVED. S02-012 §Interface contracts binds `UnboundAdapterError` placeholders for the restore-half database methods, `site_files_*` and `config_*` "until E002-S02-009". S02-009 §Interface contracts "Wiring" replaces them, and S02-009 now depends on S02-012. No "proposed story" pointer is left in any E002 story.

AG-41: RESOLVED (for E002). S02-012 §Interface contracts declares `ports/journal.py` (`RunJournalQuery.incomplete`, `RunJournalCommand.begin_run/phase_started/step_completed/phase_completed/finish_run`), the `domain/journal.py` record types and `NoopRunJournal`. It also reconciles `load_dump(location, source) -> LoadResult` with the reason for each dropped argument. One part is still open: E003-S02-005/E003-S01-006 contradict this until Epic 3's owner applies the eight items in the reconciliation record's "Changes Epic 3 must adopt". Epic 3's own gate should check that. It is not an E002 defect.

AG-42: RESOLVED. S01-001 is now declarations (seven small `domain/` modules, four `ports/`) plus the backup half of the database adapter. S01-010 takes the writer, store, workspace, phase runner and root. It is still the largest story in sprint 1, but it is one coherent "write and publish" slice. S01-005 keeps only exit codes, rendering and generated docs. S02-011 holds the restore vocabulary, the AD-25 table, the flags, `doctor` and the frozen-surface test, which is the split I asked for. S02-007 keeps the port-split mechanism, the archive-writer port, the no-op set, preflight and backup Dry Run. S02-012 holds only port and value declarations.

## New

NEW MINOR: S01-005 §Interface contracts says "`phase()` (E002-S01-010) … take[s] [its] `exit_code` from `exit_code_for`". `phase()` lives in `adapters/phase.py`, but `exit_code_for` lives in `cli/exit.py`, and `adapters/` may not import `cli/`. S01-004's tracing-hooks bullet states that rule for this same module. The text invites the layer break that AG-36 removed elsewhere. State that `phase()` reads `type(exc).exit_code` (a `domain/` attribute, falling back to `ExitCode.FAILED`), and that `cli/exit.py::exit_code_for` is for the CLI boundary only. This sentence is new in this pass.

NEW MINOR: S01-006 §Testability points forward to S01-004, which runs after S01-006 in the reconciled order (`S01-004 depends_on S01-006`):
- the second check in `test_credential_sources_built_once.py` walks the hints of `BackupRequest` and `VerifyRequest`;
- item 3 of `credential_sentinel` covers "every exception the drivers raise".

Both the request types and the drivers are created by S01-004. Sprint 1 still closes green, but S01-006 cannot be merged green on its own as written. Say that S01-004 extends both checks when it adds the types and drivers, or have S01-006's check walk whatever request types exist and assert that the set is non-empty once S01-004 lands. Both references are new in this pass.

Sections read:
- `epic-002/arch/reviewer-bmad-agent-architect-2026-10-27.md` (findings, and the "Previous findings" header); `epic-002/arch/arch-gate-review.md` (AG-30…AG-44 rows only)
- `planning-artifacts/gate-reconciliation-2026-10-27.md` (all)
- `docs/adr/0016-…` (all); `docs/adr/0017-…` (§Decision); `docs/adr/0018-…` (§Decision)
- read in full for AG-42: E002-S01-001, E002-S01-010, E002-S01-005, E002-S02-011, E002-S02-007, E002-S02-012
- E002-S01-002 §Interface contracts
- E002-S01-003 §Non-goals, §Interface contracts, §Error and edge case handling, §Testability
- E002-S01-004 §Non-goals, §Interface contracts, §Error and edge case handling, §Testability
- E002-S01-006 §Non-goals, §Interface contracts, §Testability
- E002-S02-008 §Interface contracts, §Testability
- E002-S02-009 AC block and its note, §Interface contracts, §Testability
- `pm-status.py dump-plan`: `depends_on` for E002 stories
- grep across all E002 stories for stale versions, line citations, "proposed story", `DumpReadiness`, `xfail` and signature variants
- `git show 4130fc2^` of S01-005 and S01-006 (only to confirm the two NEW items were introduced in this pass)

DONE — [13 resolved / 0 not resolved / 2 new]
