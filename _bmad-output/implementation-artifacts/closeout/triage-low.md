# LOW backlog triage (close-out)

Scope: all 286 `severity: Low` entries in `state/issues.yaml`. Resolutions were applied with `pm-status resolve-issue` (session b74a242a-6a71-45d8-8ece-7b06b152e53c); no code, tests, docs, README or pyproject were edited and nothing was committed.

## Summary

- Resolved: **78** (fixed 68, duplicate 5, obsolete 3, wontfix 2)
- Still open: **208** (5a cheap mechanical 120, 5b design-settled code 61, 5c possible owner decision 27)
- Caveat: BL-E002-007 was closed on weaker evidence (the stale `P3` references are gone; the 'old sprint-split wording' part rests on `gate-reconciliation-2026-10-27.md:37`). Re-open it if that matters.
- Bundle items (e.g. BL-E002-066, -071, -075, -096, -105, BL-E003-040) stay open unless every sub-finding was verified closed; the per-item evidence lists the sub-findings checked.
- `src/` file fixes below overlap with the High/Medium fixer's tree; batch by file after that pass lands.

## Resolved items

| Key | Outcome | Evidence |
|---|---|---|
| BL-E002-004 | fixed | tools/gen_cli_docs.py --check run by tests/contracts/test_cli_docs.py:1-30 (CI contracts job); README usage generated (commit d3e0b2b) |
| BL-E002-006 | fixed | domain/storage_target.py:1-33 takes (scheme,path); cli/backup.py:66 does the urlsplit; domain imports no urllib (commit 825c31f) |
| BL-E002-007 | fixed | no P3 refs in epic-002 stories; E002-S02-007.md:43 points at E002-S02-012/009; gate-reconciliation-2026-10-27.md:37 (commit 4130fc2) |
| BL-E002-009 | fixed | tests/unit/cli/test_lock_order.py:1-10 asserts lock then wp-config read; E002-S01-001.md:81 states locate runs after the lock (commit 5b44ca5) |
| BL-E002-010 | fixed | cli/context.py:121 site_lock: LockHandle \| None, one type; S01-004 no longer types it as the port (commit 5b44ca5) |
| BL-E002-011 | fixed | E002-S01-004.md:83 states the fallback: raise against l3io-wp-database rather than patch here (commit 4130fc2) |
| BL-E002-012 | fixed | tests/integration/test_site_lock_signals.py:1-8 uses a database double that blocks inside dump and signals via a ready file (commit 5b44ca5) |
| BL-E002-013 | fixed | adapters/store.py:158-163 verify is a required keyword; E002-S01-010.md:65 names structural_check as the interim verifier until S01-003 (commit 4130fc2) |
| BL-E002-014 | fixed | no duplicate Spec: line inside any block of epic-002 stories (awk scan over sprint-01/02 stories found none) (commit 4130fc2) |
| BL-E002-015 | fixed | E002-S01-010.md:100 states VERIFY and PUBLISH are emitted from inside publish(), once each (commit e74b298) |
| BL-E002-017 | fixed | E002-S02-012.md:57 limits ConfigQuery.read_constants to the restore target's config; callers pass explicit keys (cli/restore.py:1011,1127) (commit 4130fc2) |
| BL-E002-018 | fixed | docs/adr/0015-credentials-delegated-to-database.md:27,45,70-76 corrected: replace= is a confirmation token, load uses replace=None (commit 8131002) |
| BL-E002-019 | fixed | domain/errors.py:888 UnboundAdapterError replaces the bare RuntimeError; used by cli/adapter_sets.py:48 (commit d08ec47) |
| BL-E002-020 | fixed | domain/archive_format.py:251,266 Manifest.source wrapped in MappingProxyType in __post_init__ (commit 825c31f) |
| BL-E002-021 | fixed | domain/credentials.py:77-78 repr omits the password with no literal; tests/contracts/test_single_redaction.py exempts only domain/secret.py (SECRET_REPR) (commit 0c1569e) |
| BL-E002-022 | fixed | adapters/phase.py:13-15,39 reads the exit code via domain.errors.exit_code_of, not cli/exit_code_for (commit d3e0b2b) |
| BL-E002-025 | fixed | core/verify.py:88, tests/unit/core/test_verify.py:213, d8a52c5 |
| BL-E002-030 | fixed | cli/context.py:315,355; 2d9987c |
| BL-E002-035 | fixed | adapters/database.py:817-819, 825c31f |
| BL-E002-036 | fixed | core/verify.py:106 is_safe_member_path, d8a52c5 |
| BL-E002-039 | fixed | cli/adapter_sets.py:168-169, cli/restore.py:1461, 5a3aadb |
| BL-E002-085 | obsolete | resolved obsolete: test replaced by AST test, 14029e8 |
| BL-E002-113 | fixed | fixed 3f29b0d: cryptography dropped from dependencies |
| BL-E002-119 | duplicate | duplicate of BL-E002-081 |
| BL-E002-121 | fixed | fixed 02b9752: cli/restore.py:897 _bind_archive copies Archive to workspace |
| BL-E003-005 | fixed | E003-S01-002.md:61 states site_root_empty means no directory entries beyond the config placement exclusion; ports/site_files.py:38 is_empty (commit b037d9f) |
| BL-E003-010 | fixed | core/restore.py:331-347 NonEmptyTargetError carries parts: tuple[RestorePart, ...] (commit 10193bf) |
| BL-E003-011 | fixed | "story 3.1's OPEN on cli/restore.py" no longer present in E003-S01-002.md or E003-S02-003.md (only Resolved notes remain) (commit e926410) |
| BL-E003-012 | fixed | docs/restore.md:104-106 states a Dry Run refuses the same way and logs restore_refused; docs/adr/0022:64-65 Dry Run exit rule (commit 02b9752) |
| BL-E003-022 | fixed | tests/integration/test_config_outside_root.py:195 restores a PARENT wp-config one level up; test_config_parent_resolve.py:71 (commit c4059b6) |
| BL-E003-024 | fixed | E002-S01-003.md:85 now says a hard link is refused in any position by the filter callable, since data_filter allows in-root ones; tests/contracts/test_extraction.py:465 (commit 8f95820) |
| BL-E003-025 | fixed | ports/restore.py:26-39 declares RestorePorts fields once; restore_runner.py:232,285 carries PayloadCipher; secrets via run.secrets; MemberCipher gone (ADR-0024) (commit ffe6319) |
| BL-E003-028 | fixed | ports/restore.py:26-56 RestorePorts declared with all fields; cutover_credentials bound at cli/restore.py:1054; E003-S01-001.md:230 Path->PurePath in core (commit ffe6319) |
| BL-E003-029 | fixed | tests/unit/core/test_snapshot_location.py:38-40 passes its own explicit default path (commit ffe6319) |
| BL-E003-043 | fixed | fixed e767a66: staging.py:412-413 |
| BL-E003-062 | fixed | fixed 14029e8: test_not_yet_implemented.py:317,325 |
| BL-E003-065 | fixed | fixed e767a66: restore.py:1524-1527 |
| BL-E003-092 | fixed | adapters/restore_runner.py:836 _archive_config reads in memory, unlink gone; 91f8222 |
| BL-E003-093 | fixed | cli/restore.py:1160-1166 on_warning + docstring; 14029e8 |
| BL-E003-097 | wontfix | ADR-0025 (owner 2026-11-07, option a); revisit BL-E006-024 |
| BL-E003-110 | wontfix | ADR-0025 records driver location; no api->cli contract by decision |
| BL-E003-112 | fixed | cli/restore.py:1160-1166 docstring + on_warning; 14029e8 |
| BL-E004-002 | fixed | prd.md:139 amendment backlink; e44ac5b |
| BL-E004-003 | obsolete | story text gone; ADR-0024 rewrite e44ac5b |
| BL-E004-004 | fixed | tests/contracts/test_no_committed_age_key.py:1-12,35-42; 3a1d1eb |
| BL-E004-005 | fixed | E004-S01-002.md:41; test_supported_formats.py; 6defdbe |
| BL-E004-006 | fixed | E004-S01-001.md:48, E004-S01-003.md:177; dee8395 |
| BL-E004-007 | fixed | E005-S01-002.md:163,203; e44ac5b |
| BL-E004-009 | fixed | cli/verify.py:94-102; 53ae80c |
| BL-E004-010 | fixed | adapters/tar.py:513; 3a1d1eb |
| BL-E004-011 | fixed | adapters/crypto.py:234; 3a1d1eb |
| BL-E004-013 | fixed | test_archive_format_owner.py AST scan; 6defdbe |
| BL-E004-014 | fixed | E004-S01-002.md:139,157, released fixture committed; 6defdbe |
| BL-E004-017 | fixed | E004-S01-003.md:94; e44ac5b |
| BL-E004-018 | fixed | tests/contracts/test_exclusions_pure.py:9-28; 6f3c5c4 |
| BL-E004-019 | duplicate | duplicate of BL-E004-016; second gap fixed core/exclusions.py:164 |
| BL-E004-020 | fixed | adapters/tar.py:640; 14029e8 |
| BL-E004-022 | fixed | test_v0_fixtures.py:78, test_tar_probe.py:261; 6defdbe |
| BL-E004-023 | fixed | test_archive_format_owner.py:141; 6defdbe |
| BL-E004-027 | fixed | docs/restore.md:498-501; fea5a21 |
| BL-E004-029 | fixed | E004-S01-003.md:312; e44ac5b |
| BL-E004-032 | fixed | ci.yml:242-245; 3a1d1eb |
| BL-E004-033 | fixed | state/archived/epic-004/sprint-01/E004-S01-003.yaml:8; e0902df |
| BL-E004-037 | fixed | cli/restore.py:993-1005; adapters/tar.py:1338; 02b9752 |
| BL-E004-038 | fixed | api/operations.py:135; cli/backup.py:228; 778989b |
| BL-E004-039 | obsolete | CI step/comment gone; TestPyPI decision, 98b65d9 |
| BL-E004-040 | fixed | E002-S02-009.md:97; 98b65d9 |
| BL-E004-044 | fixed | api/operations.py:132-135, cli/backup.py:218-228 fresh wrapper per call; 14029e8 |
| BL-E004-066 | fixed | 14029e8: preflight.py:207-214 registers sources; README.md:37 lists age 1.3 |
| BL-E005-001 | duplicate | duplicate of BL-E005-008 (database.py:656) |
| BL-E005-002 | duplicate | duplicate of BL-E005-007 (database.py:558) |
| BL-E005-011 | fixed | 14029e8: parser.py:93-97 flags bound; database.py:639-640 names flag and spec field |
| BL-E005-012 | fixed | 8dbdb82: E008-S01-001.md:140-142 supersedes BinaryPreflight(environ=); built cwd= (cli/preflight.py:197) |
| BL-E006-001 | fixed | 14029e8: api/operations.py:296-305 uses shared new_db_password(explicit_source=...) |
| BL-E006-011 | fixed | target required: api/operations.py:144; 14029e8 |
| BL-E006-012 | fixed | context= removed; docs/api.md:558; 14029e8 |
| BL-E006-017 | fixed | Secret.__reduce__ domain/secret.py:51; specs hold Secret (credentials.py:62-67, restore.py:687-693, crypto.py:128); 14029e8 |
| BL-E006-021 | duplicate | duplicate of BL-E006-024 (fuller item, open) |

## Still-real items

### 5a - cheap mechanical fix (120)

Grouped by file (an item touching several files appears under each).

- `.github/workflows/ci.yml`
  - BL-E002-111: AD-4 CI grep gate misses _dry_run/isDryRun/whatIf - ci.yml:103 grep still \b(dry_run\|what_if)\b whole-word only
  - BL-E003-108: AD-4 grep gate misses compound names - ci.yml:103 pattern still \b(dry_run\|what_if)\b; \b misses skipped_dry_run
- `.gitignore`
  - BL-E007-012: Generated diff patch committed; .codex/.claude worktrees not ignored - patch still tracked (git ls-files); .gitignore lacks .codex and .claude/worktrees; git status shows ?? .codex
- `README.md`
  - BL-E004-041: no published binder wheel behaviour - ADR-0014:99,116 say CLI fallback only; no operator docs on no-wheel platform
  - BL-E004-068: AD-6: README claims restore of 0.2.21 Archives; no restore Run covers it - README.md:28 still says restore restores 0.2.21 Archives; no restore Run over released-0.2.21 fixture (test_wpcli_config_placement.py:207 only resolves config)
- `_bmad-output/implementation-artifacts/epic-002/sprint-01/stories/E002-S01-001.md`
  - BL-E002-029: Story dependency versions drift from pyproject - S01-001:146 still config v0.1.1, S01-006:118 and S02-009:106 still v0.2.0/old config
- `_bmad-output/implementation-artifacts/epic-002/sprint-01/stories/E002-S01-002.md`
  - BL-E002-016: S01-002 says Epic 3 adds restore's --mode; S01-005 (decided) registers it in Epi - E002-S01-002.md:42 still says restore --mode is Epic 3 story 3.2; cli/parser.py:292 registered in Epic 2 (S02-011)
  - BL-E002-045: Files-scope test uses DB_HOST=db.invalid; record in story - review asks only for a story dev-note recording the db.invalid substitution; tests/integration/test_backup_scope.py docstring records it, story not amended
- `_bmad-output/implementation-artifacts/epic-002/sprint-01/stories/E002-S01-003.md`
  - BL-E002-028: S01-003 non-goal L42 stale symlink text - S01-003:42 still says data filter refuses symlinks, Epic 3's call
- `_bmad-output/implementation-artifacts/epic-002/sprint-01/stories/E002-S01-004.md`
  - BL-E002-031: S01-004 says run_verify resolves passphrase/cipher - S01-004:65 still says run_verify resolves passphrase and cipher
- `_bmad-output/implementation-artifacts/epic-002/sprint-01/stories/E002-S01-006.md`
  - BL-E002-023: E002-S01-006: two tests cover BackupRequest/VerifyRequest and every driver excep - S01-006 still checks "every exception the drivers raise" (created by S01-004) with no statement that S01-004 extends the check
  - BL-E002-029: Story dependency versions drift from pyproject - S01-001:146 still config v0.1.1, S01-006:118 and S02-009:106 still v0.2.0/old config
  - BL-E002-052: ExplicitCredentialSpec AC wording password='***' - S01-006:55 still says __repr__ shows password='***'
- `_bmad-output/implementation-artifacts/epic-002/sprint-01/stories/E002-S01-010.md`
  - BL-E002-026: Phase for inner-payload write, ENCRYPT placement unstated - S01-010:64 still does not say which phase brackets inner-payload write / where ENCRYPT is emitted
  - BL-E002-027: S01-010 AC L26-27 old archive layout - S01-010:26-27 still state the gzipped-tar layout; only a note supersedes
  - BL-E002-033: S01-010 L66 publish() return value unstated - S01-010:66 publish() contract still gives no return value (code store.py:126 returns bool)
- `_bmad-output/implementation-artifacts/epic-002/sprint-02/stories/E002-S02-009.md`
  - BL-E002-029: Story dependency versions drift from pyproject - S01-001:146 still config v0.1.1, S01-006:118 and S02-009:106 still v0.2.0/old config
- `_bmad-output/implementation-artifacts/epic-002/sprint-02/stories/E002-S02-012.md`
  - BL-E002-029: Story dependency versions drift from pyproject - S01-001:146 still config v0.1.1, S01-006:118 and S02-009:106 still v0.2.0/old config
- `_bmad-output/implementation-artifacts/epic-003/sprint-01/stories/E003-S01-001.md`
  - BL-E003-015: E003-S01-001 L59: note that AD-18.3 supersedes FR-6's 'drops database first' - E003-S01-001.md:59 mode semantics still has no note that AD-18.3 supersedes FR-6 "drops database first"
  - BL-E003-023: E003-S01-001 L62: two executor sentences sit under the RestoreInputs bullet — mo - E003-S01-001.md:71 RestoreInputs bullet still carries the executor sentences (no run/what_if, take_snapshot/load_database callables)
- `_bmad-output/implementation-artifacts/epic-003/sprint-02/stories/E003-S02-006.md`
  - BL-E003-007: Naming residue: E003-S02-006 AC L26 and epics.md 3.6 cite E002-S01-005's table;  - epics.md Story 3.6 (~L507) and E003-S02-006.md:26 still cite E002-S01-005's table (now E002-S02-011)
- `_bmad-output/implementation-artifacts/epic-004/sprint-01/stories/E004-S01-001.md`
  - BL-E004-015: Formerly OPEN blocks false text - E004-S01-001.md:92,94 Formerly OPEN text still present
- `_bmad-output/implementation-artifacts/epic-004/sprint-01/stories/E004-S01-002.md`
  - BL-E004-026: extract_archive without payload= - E004-S01-002.md:82 extract_archive signature lacks payload= (tar.py:1267)
- `_bmad-output/implementation-artifacts/epic-004/sprint-01/stories/E004-S01-003.md`
  - BL-E004-008: v0.1.0 refusal text beside note - E004-S01-003.md:30 and epics.md Story 4.3 AC still carry v0.1.0/passphrase-refused line
  - BL-E004-025: database.sql intact / per-member names - E004-S01-003.md:317 still 'database.sql intact'; event member_decryption_failed kept at tar.py:1374
  - BL-E004-028: epics 3.3/4.3 per-member text - epics.md Stories 3.3/4.3 have no ADR-0024 note (grep empty)
- `_bmad-output/implementation-artifacts/epic-007/sprint-01/closure/diff-E007-S01-004.patch`
  - BL-E007-012: Generated diff patch committed; .codex/.claude worktrees not ignored - patch still tracked (git ls-files); .gitignore lacks .codex and .claude/worktrees; git status shows ?? .codex
- `_bmad-output/implementation-artifacts/epic-007/sprint-01/stories/E007-S01-004.md`
  - BL-E007-017: WP_CLI_REQUIREMENT location differs from story pin; locate_tool alias - WP_CLI_REQUIREMENT still core/relocation.py:325; locate_tool alias preflight.py:451
- `_bmad-output/implementation-artifacts/state/archived/epic-004/sprint-01/E004-S01-001.yaml`
  - BL-E004-031: story depends_on omit E002-S01-004 - depends_on in both yaml still omit E002-S01-004 (bookkeeping, archived)
- `_bmad-output/implementation-artifacts/state/archived/epic-004/sprint-01/E004-S01-003.yaml`
  - BL-E004-031: story depends_on omit E002-S01-004 - depends_on in both yaml still omit E002-S01-004 (bookkeeping, archived)
- `_bmad-output/planning-artifacts/architecture/architecture-py-wordpress-backup-2026-09-15/ARCHITECTURE-SPINE.md`
  - BL-E003-019: Docs: no flow diagram for drive_restore + executor; spine restore sequence diagr - no diagram of drive_restore + executor in docs/; spine ARCHITECTURE-SPINE.md:430 sequence still shows cutover in REWRITE_CONFIG (AD-18.3 amendment L230)
  - BL-E004-035: Spine AD-6 encrypted members - spine AD-6 rule text still says members encrypted/checksums (note only at L109)
- `_bmad-output/planning-artifacts/epics.md`
  - BL-E002-008: epics.md gives Epic 2 cli/* exclusively but cli/restore.py is Epic 3's; Epic 2's - epics.md:128 Epic 2 row still says cli/* and omits adapters workspace/redaction/noop/site_files; cli/restore.py is Epic 3 (L157)
  - BL-E003-004: epics.md Epic List: add adapters/restore_runner.py and E003-S02-006's edit of cl - epics.md:130 Epic 3 row still lacks adapters/restore_runner.py and the parser.py NOT_YET_IMPLEMENTED edit
  - BL-E003-007: Naming residue: E003-S02-006 AC L26 and epics.md 3.6 cite E002-S01-005's table;  - epics.md Story 3.6 (~L507) and E003-S02-006.md:26 still cite E002-S01-005's table (now E002-S02-011)
  - BL-E004-001: epics.md Epic 4 header (L513) claims the v0 classifier in domain/archive_format; - epics.md:519 Epic 4 header still lists domain/archive_format (v0 classifier only) while the Epic List gives it to Epic 2 alone
  - BL-E004-008: v0.1.0 refusal text beside note - E004-S01-003.md:30 and epics.md Story 4.3 AC still carry v0.1.0/passphrase-refused line
  - BL-E004-028: epics 3.3/4.3 per-member text - epics.md Stories 3.3/4.3 have no ADR-0024 note (grep empty)
- `_bmad-output/planning-artifacts/plan-2026-10-27-v2.yaml`
  - BL-E004-012: plan phase 3 omits E004 - plan-2026-10-27-v2.yaml phase 3 dependencies still omit E004 (state epic.yaml for E006 has it)
- `_bmad-output/planning-artifacts/prds/prd-py-wordpress-backup-2026-09-12/prd.md`
  - BL-E004-034: PRD FR-3 per-member checksums - prd.md:83,128 still per-member/Dump checksums; no inner-tar vs index check
- `docs/adr/0014-age-encryption-via-database.md`
  - BL-E004-041: no published binder wheel behaviour - ADR-0014:99,116 say CLI fallback only; no operator docs on no-wheel platform
- `docs/adr/0016-run-context-binds-adapters-once.md`
  - BL-E003-009: E003-S02-006 L67: v0 Archive config read uses a second read instead of ADR-0016' - ADR-0016 amendment (L113-124) says VERIFY compares stored digest, but restore_runner.py:836 and cli/restore.py:1000 just read twice (ADR-0024 dropped the digest); record residual
- `docs/adr/0017-manifest-v1-member-index.md`
  - BL-E008-030: ADR-0017 MANIFEST_MAX_BYTES amendment missing - no 256 MiB mention in ADR-0017 (grep); cap at archive_format.py:123
- `docs/adr/0024-one-encrypted-payload-passphrase-only.md`
  - BL-E004-030: ADR-0024 AG-7 consequence line - ADR-0024 L125 still says AG-7 reduces to one header check
- `docs/adr/0025-api-composes-through-the-cli-drivers.md`
  - BL-E008-029: ADR-0025 Consequences stale - ADR-0025:35 still says drive_restore prints three stderr notices
- `docs/api.md`
  - BL-E006-023: Document restore mode/overrides forwarding risk - no host-forwarding note in docs/restore.md or api.md (grep)
  - BL-E008-031: Bare TypeError undocumented; docstring typo - operations.py:22 still 'open or open the Run'; api.md:64 mentions TypeError only for missing target; Errors section/AD-14 note unchanged
- `docs/exclusions.md`
  - BL-E004-046: exclusions.md hand-written GENERATED table - docs/exclusions.md:28 'BEGIN GENERATED' with no generator in tools/
  - BL-E004-062: AR-9: exclusions.md says GENERATED but no generator exists - docs/exclusions.md:28,35 still say GENERATED; no generator
- `docs/restore.md`
  - BL-E003-019: Docs: no flow diagram for drive_restore + executor; spine restore sequence diagr - no diagram of drive_restore + executor in docs/; spine ARCHITECTURE-SPINE.md:430 sequence still shows cutover in REWRITE_CONFIG (AD-18.3 amendment L230)
  - BL-E003-061: --archive required for --resolve-interrupted-run - cli/__init__.py:112 still requires --archive for resolve; help/docs silent
  - BL-E003-095: docs/restore.md table or view wording - docs/restore.md:88 still 'table or view'
  - BL-E003-103: docs exit table omits 7 NOT_READY - docs/restore.md:68-79 exit table omits 7 NOT_READY
  - BL-E006-023: Document restore mode/overrides forwarding risk - no host-forwarding note in docs/restore.md or api.md (grep)
- `pyproject.toml`
  - BL-E004-024: FR-26 inventory gaps - pathspec licence at pyproject.toml:46 but no pyrage/l3io-age-passphrase licence+exit-plan entry
- `src/l3io/wp/backup/adapters/database.py`
  - BL-E002-053: DumpFailedError chains TransferError; hygiene case - database.py:~855 still chains from exc; test_credential_hygiene.py:91 message lacks stderr line
  - BL-E003-063: TLS mode order and --admin-tls choices hand-kept - database.py:161-162 hand-kept ADMIN_TLS_MODES/_TLS_ORDER; parser.py:397 literal choices
  - BL-E003-098: staging.py imports l3io.wp.database directly - adapters/staging.py:206 still imports l3io.wp.database in _validate_name
  - BL-E005-006: AWS failure keeps chained JSONDecodeError - database.py:550-559 still 'raise CredentialsError ... from exc' keeping chain
  - BL-E005-007: unset_partner reads os.environ in adapter - database.py:558 unset_partner(os.environ,...) still
  - BL-E005-008: credential_source_selected logged per resolve - database.py:656 logs on every resolve
- `src/l3io/wp/backup/adapters/journal.py`
  - BL-E002-005: Timestamp spelling differs: Manifest created_at uses +00:00 (S01-001), log recor - Manifest created_at (cli/backup.py:432) and journal/restore stamps use isoformat() (+00:00); convention (E002-S01-001.md:61) and logs use Z
  - BL-E003-045: corrupt current.json has no operator resolution path - journal.py:101-103 message lacks manual recovery; :319 del site_root
- `src/l3io/wp/backup/adapters/lock.py`
  - BL-E002-082: Lock refusal can name a dead holder - lock.py:310 refusal reads record unchanged; no dead-pid wording
- `src/l3io/wp/backup/adapters/preflight.py`
  - BL-E007-017: WP_CLI_REQUIREMENT location differs from story pin; locate_tool alias - WP_CLI_REQUIREMENT still core/relocation.py:325; locate_tool alias preflight.py:451
  - BL-E008-019: Dead nullcontext in run_bounded - preflight.py:324 contextlib.nullcontext() remains
- `src/l3io/wp/backup/adapters/restore_runner.py`
  - BL-E002-005: Timestamp spelling differs: Manifest created_at uses +00:00 (S01-001), log recor - Manifest created_at (cli/backup.py:432) and journal/restore stamps use isoformat() (+00:00); convention (E002-S01-001.md:61) and logs use Z
  - BL-E003-049: ADV-5 check ignores load_database slot's ports - cli/restore.py:1513 binds slot over same ports, but no test asserts slot ports are run.ports
  - BL-E003-090: tree_bytes lower bound vs 'upper bound' comment - restore_runner.py:552 still says 'upper bound'; site_files.py:81 says lower bound
  - BL-E003-096: snapshot_result bytes shadows builtin - restore_runner.py:675 bytes param with noqa A002
  - BL-E004-065: AR-12: unreachable manifest_text-is-None branches under pragma no cover - restore_runner.py:597 pragma no cover manifest_text None branch still present
- `src/l3io/wp/backup/adapters/site_files.py`
  - BL-E003-090: tree_bytes lower bound vs 'upper bound' comment - restore_runner.py:552 still says 'upper bound'; site_files.py:81 says lower bound
- `src/l3io/wp/backup/adapters/staging.py`
  - BL-E003-044: _drop_staging misreports journal failure as drop failure - staging.py:307-316 one try wraps drop_database and step_completed
  - BL-E003-098: staging.py imports l3io.wp.database directly - adapters/staging.py:206 still imports l3io.wp.database in _validate_name
  - BL-E003-111: ADR-0015 no-database-type-in-cli unchecked - no contract test for no-database-type-in-cli; staging.py:206 import remains
- `src/l3io/wp/backup/adapters/store.py`
  - BL-E002-040: LocalArchiveStore._rename check-then-replace race - store.py:115-124 _rename still check-then-replace; no residual-race docstring
- `src/l3io/wp/backup/adapters/tar.py`
  - BL-E002-073: Stale parser docstring; tar uses time.time - SD-01-3: parser.py:5 still claims python -m wpbackup2 works, :98 'Triage E-14' history note; SD-01-5: tar.py:490,533 still time.time()
  - BL-E002-080: R12 collection failure stages then discards, untested - no test/note that a Site Root collection failure stages then discards (tar.py:647 ArchiveWriterCommand)
  - BL-E004-025: database.sql intact / per-member names - E004-S01-003.md:317 still 'database.sql intact'; event member_decryption_failed kept at tar.py:1374
- `src/l3io/wp/backup/adapters/wpcli.py`
  - BL-E007-013: wp child gets unfiltered PATH - wpcli.py:210 wp_cli_environment copies os.environ PATH unfiltered while wp located over absolute entries only
  - BL-E007-014: wp stderr tail in exception with control characters unstripped - wpcli.py:462-464 only secrets.redact; errors.py:943-949 no control-char stripping
  - BL-E008-007: wp version from first stdout line only - wpcli.py:608-609 whole stdout stripped and prefix-removed; a PHP notice first yields junk version (preflight.py:393 uses first line)
- `src/l3io/wp/backup/api/operations.py`
  - BL-E006-004: api.verify(passphrase=) without authenticate silently ignores it - api/operations.py verify() (~224-251) no UsageError for passphrase without authenticate
  - BL-E006-005: api/operations.py duplicate _NO_ENVIRONMENT and BackupRequest branches - operations.py:117 _NO_ENVIRONMENT and two BackupRequest branches (183-205) remain
  - BL-E006-016: Passphrase empty/verify edge cases - passphrase.py:102 'if explicit:' falls through on empty; verify ignores passphrase without authenticate
  - BL-E006-020: backup() builds BackupRequest twice - two near-identical BackupRequest blocks remain: operations.py:~190-215
  - BL-E008-031: Bare TypeError undocumented; docstring typo - operations.py:22 still 'open or open the Run'; api.md:64 mentions TypeError only for missing target; Errors section/AD-14 note unchanged
- `src/l3io/wp/backup/cli/__init__.py`
  - BL-E002-077: Top-level --whatIf ignored by verify - restore honours --whatIf (restore.py:1789) but verify/doctor silently ignore it; no refusal in main()
  - BL-E003-061: --archive required for --resolve-interrupted-run - cli/__init__.py:112 still requires --archive for resolve; help/docs silent
- `src/l3io/wp/backup/cli/adapter_sets.py`
  - BL-E002-087: adapter_sets.py module-wide noqa ARG002 - adapter_sets.py:1 still has module-wide ruff noqa ARG002
- `src/l3io/wp/backup/cli/backup.py`
  - BL-E002-005: Timestamp spelling differs: Manifest created_at uses +00:00 (S01-001), log recor - Manifest created_at (cli/backup.py:432) and journal/restore stamps use isoformat() (+00:00); convention (E002-S01-001.md:61) and logs use Z
  - BL-E002-043: --archive relative bare path with colon parsed as URL - cli/backup.py:512 urlsplit(text) still treats 'a:b' first-segment bare path as scheme
  - BL-E002-076: execute_backup_plan exclusion agreement unchecked - cli/backup.py:~360-373 no check that plan exclusion set matches writer's policy
  - BL-E005-007: unset_partner reads os.environ in adapter - database.py:558 unset_partner(os.environ,...) still
- `src/l3io/wp/backup/cli/credentials.py`
  - BL-E008-005: Empty new-db-password falls through to env - credentials.py:175 'explicit or None', :181 'or None' still treat empty as not given
  - BL-E008-006: --new-db-password-file no size limit - credentials.py:143-144 readline() unbounded
  - BL-E008-016: password file keeps UTF-8 BOM - credentials.py:143 encoding='utf-8' (not utf-8-sig); no mode check
  - BL-E008-024: --admin-password no runtime warning - password_on_command_line only for new-db-password (credentials.py:179)
- `src/l3io/wp/backup/cli/parser.py`
  - BL-E002-073: Stale parser docstring; tar uses time.time - SD-01-3: parser.py:5 still claims python -m wpbackup2 works, :98 'Triage E-14' history note; SD-01-5: tar.py:490,533 still time.time()
  - BL-E002-077: Top-level --whatIf ignored by verify - restore honours --whatIf (restore.py:1789) but verify/doctor silently ignore it; no refusal in main()
  - BL-E003-061: --archive required for --resolve-interrupted-run - cli/__init__.py:112 still requires --archive for resolve; help/docs silent
  - BL-E003-063: TLS mode order and --admin-tls choices hand-kept - database.py:161-162 hand-kept ADMIN_TLS_MODES/_TLS_ORDER; parser.py:397 literal choices
  - BL-E003-102: --archive required but shown optional - cli/parser.py:281-284 --archive required=False
  - BL-E003-107: --resolve-interrupted-run help lacks stop note - cli/parser.py:411-414 help unchanged
- `src/l3io/wp/backup/cli/passphrase.py`
  - BL-E006-016: Passphrase empty/verify edge cases - passphrase.py:102 'if explicit:' falls through on empty; verify ignores passphrase without authenticate
- `src/l3io/wp/backup/cli/restore.py`
  - BL-E002-005: Timestamp spelling differs: Manifest created_at uses +00:00 (S01-001), log recor - Manifest created_at (cli/backup.py:432) and journal/restore stamps use isoformat() (+00:00); convention (E002-S01-001.md:61) and logs use Z
  - BL-E003-085: web-served guard skipped with --no-safety-snapshot - core/restore.py:1478-1481 snapshot_location only if TAKE; saved_config still built at cli/restore.py:1410
  - BL-E003-094: Epic 7 scaffolding / double registration - cli/restore.py:751 RESTORE_EXTENSION, _NoExtension :754, new_db_password registered :1206 and :692; require_database_restored still two outcome params (:436)
  - BL-E003-096: snapshot_result bytes shadows builtin - restore_runner.py:675 bytes param with noqa A002
  - BL-E003-105: snapshot path stdout, error stderr - cli/restore.py:1696,1715 emit_result(safety snapshot) to stdout on failure
  - BL-E003-106: Database report raw bytes / ordering - cli/restore.py:400 'loaded {bytes} bytes'
  - BL-E004-054: RT-6: restore prints symlink path/target raw to stderr - cli/restore.py:1675 still prints link.path -> link.link_target raw to stderr
  - BL-E005-009: Restore pins wp-config creds (environ={}) with no guard test - No test asserts restore passes environ={} to wordpress_source (cli/restore.py:1261); spy in test_new_db_password.py ignores kwargs
  - BL-E007-011: Dead factory-is-None branch and mid-file import in cli/restore.py - cli/restore.py:122,751 (noqa E402), 1186-1189 factory-is-None branch + RELOCATION_UNAVAILABLE remain
  - BL-E008-009: safety_snapshot_encrypted has no log record - restore.py:1496 emits only the RunWarning; unencrypted case logs at :1491
- `src/l3io/wp/backup/cli/verify.py`
  - BL-E004-065: AR-12: unreachable manifest_text-is-None branches under pragma no cover - restore_runner.py:597 pragma no cover manifest_text None branch still present
- `src/l3io/wp/backup/core/relocation.py`
  - BL-E007-010: --new-site-url userinfo passes validation, reaches argv/logs - core/relocation.py:143-167 _url_rule_broken checks only leading/trailing @ of authority; user:pw@host passes
  - BL-E007-017: WP_CLI_REQUIREMENT location differs from story pin; locate_tool alias - WP_CLI_REQUIREMENT still core/relocation.py:325; locate_tool alias preflight.py:451
- `src/l3io/wp/backup/core/restore.py`
  - BL-E003-085: web-served guard skipped with --no-safety-snapshot - core/restore.py:1478-1481 snapshot_location only if TAKE; saved_config still built at cli/restore.py:1410
  - BL-E003-091: undo_for 'copy None back' - core/restore.py:1031 prints header.saved_config unguarded (None)
  - BL-E007-004: F-10: NoConfigForCutoverError names only one searched path - core/restore.py:1471-1475 NoConfigForCutoverError(str(config_file)) names one path
- `src/l3io/wp/backup/domain/archive_format.py`
  - BL-E002-034: manifest_from_json on_unknown defaults to None - archive_format.py:355 on_unknown still defaults to None; callers :662,:899 pass none
- `src/l3io/wp/backup/domain/errors.py`
  - BL-E007-014: wp stderr tail in exception with control characters unstripped - wpcli.py:462-464 only secrets.redact; errors.py:943-949 no control-char stripping
- `src/l3io/wp/backup/domain/journal.py`
  - BL-E003-014: E002-S02-012 JournalHeader: live_database_before/load_database required but file - JournalHeader (domain/journal.py:85-110) docstring does not say what live_database_before/load_database hold for files-only modes (cli/restore.py:1427 always sets site.db_name)
- `src/l3io/wp/backup/domain/preflight.py`
  - BL-E005-004: Preflight too-old message omits minimum - domain/preflight.py:117-123 found=version only, no minimum in message
- `src/l3io/wp/backup/domain/secret.py`
  - BL-E008-017: Secret.__eq__ raises on lone surrogate - secret.py:49 .encode() with default strict utf-8
- `src/l3io/wp/backup/ports/crypto.py`
  - BL-E004-051: ports/crypto.py docstring stale (backup binds None clause remains) - AgePayloadCipher mention gone (ports/crypto.py:3-8, b424f7c) but line 7-8 still says backup binds None until E008; backup.py:70 now builds a cipher
- `tests/adapters/test_backup_executor.py`
  - BL-E002-042: Missing tests: cipher fail, empty Dump, wp-config-only - no executor-level cipher-failure/empty-dump/wp-config-only tests found (only test_tar_writer.py:364 writer-level)
  - BL-E002-046: No test for early credential check in db/all - cli/backup.py:637-638 early check present; no test asserts it for db/all
  - BL-E002-047: Failed --mode db test: wrong error, no exit code - test_backup_executor.py:534-546 still DumpFailedError, no exit code assert
- `tests/adapters/test_crypto.py`
  - BL-E004-050: test_crypto.py patches database private _passphrase.binder_available - tests/adapters/test_crypto.py:211 still patches l3io.wp.database._passphrase.binder_available
- `tests/adapters/test_descriptor.py`
  - BL-E002-038: Planted-password test lacks fixture-contains assertion - test_descriptor.py: no assert PLANTED_PASSWORD in fixture text
- `tests/adapters/test_tar_probe.py`
  - BL-E004-048: CLI v0 Archive without wp-root dir not verified e2e - review F6 optional: no directory-less v0 case in CLI test; classifier test remains
- `tests/adapters/test_tar_writer.py`
  - BL-E002-042: Missing tests: cipher fail, empty Dump, wp-config-only - no executor-level cipher-failure/empty-dump/wp-config-only tests found (only test_tar_writer.py:364 writer-level)
- `tests/contracts/`
  - BL-E003-111: ADR-0015 no-database-type-in-cli unchecked - no contract test for no-database-type-in-cli; staging.py:206 import remains
- `tests/contracts/test_api_base_install.py`
  - BL-E006-003: API tests use fixed passwords not unique sentinels - api_consumer.py:39 password='pw'; test_api_base_install.py:62 'not-a-real-secret', no stdout check
- `tests/contracts/test_archive_format_owner.py`
  - BL-E002-037: AD-6 owner grep matches role prefix only at start - test_archive_format_owner.py:~72 still matches PREFIXES only via startswith
- `tests/contracts/test_credential_hygiene.py`
  - BL-E002-053: DumpFailedError chains TransferError; hygiene case - database.py:~855 still chains from exc; test_credential_hygiene.py:91 message lacks stderr line
- `tests/contracts/test_exclusions_pure.py`
  - BL-E004-043: Exclusions purity guard scope stops at 'tree' - test_exclusions_pure.py:28-31 still filters names containing 'tree'
- `tests/contracts/test_existence_by_admin.py`
  - BL-E003-052: existence_by_admin guard ignores AccessDenied 1044 - test_existence_by_admin.py:41-42 triggers only AuthenticationFailed/1045; no AccessDenied/1044
- `tests/contracts/test_extraction.py`
  - BL-E002-051: One-extractor guard misses shutil.unpack_archive - no unpack_archive in tests/contracts/test_extraction.py
- `tests/contracts/test_no_dry_run_outside_roots.py`
  - BL-E003-108: AD-4 grep gate misses compound names - ci.yml:103 pattern still \b(dry_run\|what_if)\b; \b misses skipped_dry_run
- `tests/contracts/test_restore_docs.py`
  - BL-E007-001: _mode_rows reads every table in docs/restore.md - test_restore_docs.py:41-48 _mode_rows still scans every table line
- `tests/contracts/test_tracing_no_sdk.py`
  - BL-E006-006: no-SDK test compares only phase/outcome/exit_code of records - test_tracing_no_sdk.py:214-217 compares only phase/outcome/exit_code
- `tests/contracts/test_v0_fixtures_reproducible.py`
  - BL-E004-021: byte-identical tar.gz zlib - test_v0_fixtures_reproducible.py:36-42 still compares raw .tar.gz bytes
- `tests/integration`
  - BL-E004-068: AD-6: README claims restore of 0.2.21 Archives; no restore Run covers it - README.md:28 still says restore restores 0.2.21 Archives; no restore Run over released-0.2.21 fixture (test_wpcli_config_placement.py:207 only resolves config)
- `tests/integration/test_api_roundtrip.py`
  - BL-E006-002: test_api_roundtrip uses importorskip against no-skip rule - test_api_roundtrip.py:23-24 still importorskip
- `tests/integration/test_credential_sources.py`
  - BL-E005-008: credential_source_selected logged per resolve - database.py:656 logs on every resolve
- `tests/integration/test_dry_run_restore.py`
  - BL-E003-030: Dry Run restore test snapshot/journal assertions cannot fail - test_dry_run_restore.py:1-8 docstring claims snapshot/journal coverage; :44 assertions cannot fail
- `tests/integration/test_restore_cli.py`
  - BL-E003-066: end-to-end phase-order assertion vacuous - test_restore_cli.py:145 sorted-phases check true for empty/omitting list
- `tests/integration/test_verify_cli.py`
  - BL-E004-048: CLI v0 Archive without wp-root dir not verified e2e - review F6 optional: no directory-less v0 case in CLI test; classifier test remains
- `tests/restore_harness.py`
  - BL-E003-031: per-Mode drops-nothing assertion checks fake slot - test_restore_runner.py:129 compares harness fake with itself
- `tests/typing/api_consumer.py`
  - BL-E006-003: API tests use fixed passwords not unique sentinels - api_consumer.py:39 password='pw'; test_api_base_install.py:62 'not-a-real-secret', no stdout check
- `tests/unit/adapters/test_restore_runner.py`
  - BL-E003-031: per-Mode drops-nothing assertion checks fake slot - test_restore_runner.py:129 compares harness fake with itself
- `tests/unit/adapters/test_staging.py`
  - BL-E003-042: Undo rows for failure paths only partly asserted - check_failed/staging_dropped covered at test_restore_journal.py:207; readback_failed, operator-named, fallback rows not undo-asserted
- `tests/unit/adapters/test_tar.py`
  - BL-E002-080: R12 collection failure stages then discards, untested - no test/note that a Site Root collection failure stages then discards (tar.py:647 ArchiveWriterCommand)
- `tests/unit/cli/test_backup_dry_run_effects.py`
  - BL-E002-078: Dry-run test doesn't observe workspace while active - test_backup_dry_run_effects.py:163 only checks workspace after removal
  - BL-E002-079: --what-if alias has no behavioural test (backup) - --what-if only tested on restore subparser (test_restore_root.py:803), not top-level/backup
- `tests/unit/cli/test_exit_mapping.py`
  - BL-E002-057: Literal exit-code guard misses return/SystemExit/sys.exit - test_exit_mapping.py:138-157 literal_exit_codes still only ExitCode(int)/exit_code= kw
- `tests/unit/cli/test_new_db_password.py`
  - BL-E005-009: Restore pins wp-config creds (environ={}) with no guard test - No test asserts restore passes environ={} to wordpress_source (cli/restore.py:1261); spy in test_new_db_password.py ignores kwargs
- `tests/unit/cli/test_restore_root.py`
  - BL-E003-049: ADV-5 check ignores load_database slot's ports - cli/restore.py:1513 binds slot over same ports, but no test asserts slot ports are run.ports
  - BL-E003-067: RestorePhase-naming guard misses aliases/iteration - test_restore_root.py:872-892 _named_phases sees only Attribute nodes
- `tests/unit/core/test_restore_journal.py`
  - BL-E003-042: Undo rows for failure paths only partly asserted - check_failed/staging_dropped covered at test_restore_journal.py:207; readback_failed, operator-named, fallback rows not undo-asserted
- `tests/unit/domain/test_archive_format.py`
  - BL-E002-044: Golden test omits DUMP_RELATIVE/CONFIG_RELATIVE - test_archive_format.py:150-153 still literals, no DUMP_RELATIVE/CONFIG_RELATIVE reference

### 5b - real fix needing design-settled code (61)

Grouped by file (an item touching several files appears under each).

- `.github/workflows/ci.yml`
  - BL-E002-096: Remaining AD-5..AD-12, CR-1, CR-2 lows - unresolved: AD-5 (parser.py:152 'change nothing'), AD-9 (noop.py:45 imports database ADMIN_FLAGS), AD-10 (check_mode_defaults.py:53 hand list), AD-12 (ci.yml:103 pipe gate); CR-1 StoredUrls now used (core/relocation.py:60)
- `README.md`
  - BL-E004-056: RT-8: plaintext workspace survives SIGKILL; no sweep; document TMPDIR - workspace.py:39 prefix only, no stale-dir sweep; no TMPDIR-on-tmpfs doc in README/docs (grep)
- `_bmad-output/implementation-artifacts/epic-003/sprint-01/stories/E003-S01-001.md`
  - BL-E003-013: E003-S01-001 L130: restore_mode_resolved's raw CLI flags never reach drive_resto - cli/restore.py:1211 restore_mode_resolved logs mode/overrides/snapshot_policy but not the raw CLI flags the story (L148) requires
- `_bmad-output/implementation-artifacts/epic-003/sprint-02/stories/E003-S02-006.md`
  - BL-E003-018: E003-S02-006 L61/L86: a joined Run with relocation has no obligation to carry se - cli/restore.py:1274 binds relocation seams only if handle.opened; a joined Run with relocation is neither refused nor obliged to carry them
- `docs/logging.md`
  - BL-E002-105: Arch MINOR SD-02-3..SD-02-12 - SD-02-8 still real (check_mode_defaults.py:53 GUARDED_TYPES hand-kept); SD-02-6 ADMIN_FLAGS in adapters still
- `docs/restore.md`
  - BL-E003-040: Low closure findings ADV-12..14, CR-1..4, RT-004..007, SD-01-2..4, UX-8..11 - unresolved incl. SD-01-3 (core/restore.py now 1517 lines), ADV-13, UX-8..11 not verified closed
  - BL-E004-056: RT-8: plaintext workspace survives SIGKILL; no sweep; document TMPDIR - workspace.py:39 prefix only, no stale-dir sweep; no TMPDIR-on-tmpfs doc in README/docs (grep)
- `src/l3io/wp/backup/adapters/crypto.py`
  - BL-E002-120: verify --authenticate writes decrypted site to disk - verify.py:88 _AUTHENTICATED_PAYLOAD still written to workspace
  - BL-E008-026: age CLI backend inherits whole environment - no env scrubbing for age CLI in adapters/crypto.py (grep environ)
- `src/l3io/wp/backup/adapters/database.py`
  - BL-E002-066: Redteam lows L1-L5 (race, redaction, Ctrl-C, hardlinks, symlink) - bundle; L1 unresolved (store.py:115-124 check-then-replace, = BL-E002-040); L2-L5 not individually verified closed
  - BL-E002-096: Remaining AD-5..AD-12, CR-1, CR-2 lows - unresolved: AD-5 (parser.py:152 'change nothing'), AD-9 (noop.py:45 imports database ADMIN_FLAGS), AD-10 (check_mode_defaults.py:53 hand list), AD-12 (ci.yml:103 pipe gate); CR-1 StoredUrls now used (core/relocation.py:60)
  - BL-E002-105: Arch MINOR SD-02-3..SD-02-12 - SD-02-8 still real (check_mode_defaults.py:53 GUARDED_TYPES hand-kept); SD-02-6 ADMIN_FLAGS in adapters still
  - BL-E006-015: UsageError.arguments mixed vocabulary - flag spellings still used: restore.py:627,633,1184,1191; database.py:251-270
- `src/l3io/wp/backup/adapters/exclusions.py`
  - BL-E004-045: path_excluded count vs Manifest excluded - cli/backup.py:192-216 _SkipStaged.decide excludes staged file without logging path_excluded
- `src/l3io/wp/backup/adapters/journal.py`
  - BL-E008-027: State dir accepted without owner/mode check - only _DIR_MODE at creation (lock.py:57, journal.py:75); no st_uid/mode check
- `src/l3io/wp/backup/adapters/lock.py`
  - BL-E008-008: fcntl imported at module import (Windows) - lock.py:29 import fcntl at module level
  - BL-E008-027: State dir accepted without owner/mode check - only _DIR_MODE at creation (lock.py:57, journal.py:75); no st_uid/mode check
- `src/l3io/wp/backup/adapters/noop.py`
  - BL-E002-096: Remaining AD-5..AD-12, CR-1, CR-2 lows - unresolved: AD-5 (parser.py:152 'change nothing'), AD-9 (noop.py:45 imports database ADMIN_FLAGS), AD-10 (check_mode_defaults.py:53 hand list), AD-12 (ci.yml:103 pipe gate); CR-1 StoredUrls now used (core/relocation.py:60)
  - BL-E002-105: Arch MINOR SD-02-3..SD-02-12 - SD-02-8 still real (check_mode_defaults.py:53 GUARDED_TYPES hand-kept); SD-02-6 ADMIN_FLAGS in adapters still
- `src/l3io/wp/backup/adapters/phase.py`
  - BL-E002-056: KeyboardInterrupt run_finished claims exit 1 - context.py:270-278 exit_code_for(error) applied to any BaseException incl. KeyboardInterrupt
- `src/l3io/wp/backup/adapters/preflight.py`
  - BL-E008-018: run_bounded signals pgroup after reap - preflight.py:276 os.killpg after leader wait (:355)
- `src/l3io/wp/backup/adapters/redaction.py`
  - BL-E002-066: Redteam lows L1-L5 (race, redaction, Ctrl-C, hardlinks, symlink) - bundle; L1 unresolved (store.py:115-124 check-then-replace, = BL-E002-040); L2-L5 not individually verified closed
  - BL-E002-071: Adversarial lows AR-07..AR-16 - bundle; AR-10 unresolved (store.py has file fsync only, no dir fsync), AR-07 unresolved (no bytes handling in _walk); AR-16 gen_cli_docs.py first-marker not verified closed
- `src/l3io/wp/backup/adapters/restore_runner.py`
  - BL-E003-040: Low closure findings ADV-12..14, CR-1..4, RT-004..007, SD-01-2..4, UX-8..11 - unresolved incl. SD-01-3 (core/restore.py now 1517 lines), ADV-13, UX-8..11 not verified closed
  - BL-E003-068: restore errors name deleted workspace copy of Archive - restore_runner.py:440,588,849 errors use str(inputs.archive.path) (workspace copy)
- `src/l3io/wp/backup/adapters/site_files.py`
  - BL-E002-088: clean keeps nested keep path's whole top-level ancestor - site_files.py clean docstring documents ancestor-keep but still keeps whole ancestor; no refusal
- `src/l3io/wp/backup/adapters/staging.py`
  - BL-E003-041: Load-DB creation not journaled - created_database only in memory (staging.py:304,537,606); not journaled
  - BL-E003-100: staging.py reads monotonic_ns not Clock port - adapters/staging.py:409,457 time.monotonic_ns direct
  - BL-E003-114: RunTerminated swallowed by except Exception - domain/errors.py:612 RunTerminated(BackupError); staging.py:310,380,416 except Exception
- `src/l3io/wp/backup/adapters/store.py`
  - BL-E002-066: Redteam lows L1-L5 (race, redaction, Ctrl-C, hardlinks, symlink) - bundle; L1 unresolved (store.py:115-124 check-then-replace, = BL-E002-040); L2-L5 not individually verified closed
  - BL-E002-071: Adversarial lows AR-07..AR-16 - bundle; AR-10 unresolved (store.py has file fsync only, no dir fsync), AR-07 unresolved (no bytes handling in _walk); AR-16 gen_cli_docs.py first-marker not verified closed
- `src/l3io/wp/backup/adapters/tar.py`
  - BL-E002-024: Manifest.payload required before payload - placeholder ManifestPayload invented at cli/backup.py:156, tar.py:572,589,637
  - BL-E002-048: Publish gate accepts STRUCTURAL_ONLY archive - tar.py:1031-1035 verify_or_raise checks only result.passed, not Integrity.VERIFIED
  - BL-E002-049: extract/decrypt_payload OSError mapping wrong - tar.py:~1360-1385 decrypt_payload BaseException branch still lets raw OSError escape
  - BL-E002-050: read_member returns first duplicate inner member - tar.py:943-954 read_member returns first match without refusing repeats
  - BL-E002-066: Redteam lows L1-L5 (race, redaction, Ctrl-C, hardlinks, symlink) - bundle; L1 unresolved (store.py:115-124 check-then-replace, = BL-E002-040); L2-L5 not individually verified closed
  - BL-E002-071: Adversarial lows AR-07..AR-16 - bundle; AR-10 unresolved (store.py has file fsync only, no dir fsync), AR-07 unresolved (no bytes handling in _walk); AR-16 gen_cli_docs.py first-marker not verified closed
  - BL-E003-088: Skipped-role members decompressed unbounded - adapters/tar.py _ExtractFilter still reads skipped-role members unbounded
  - BL-E004-060: AR-6: extraction does not require read_inner_manifest; add AST contract - No AST contract pairing extract_archive/decrypt_payload with read_inner_manifest (tests/contracts only lists names in test_streaming.py:132-141)
- `src/l3io/wp/backup/adapters/workspace.py`
  - BL-E002-041: run_workspace ignores rmtree errors, exit 0 - workspace.py:58-61 rmtree(ignore_errors) only WARNs; run still exits 0
  - BL-E004-056: RT-8: plaintext workspace survives SIGKILL; no sweep; document TMPDIR - workspace.py:39 prefix only, no stale-dir sweep; no TMPDIR-on-tmpfs doc in README/docs (grep)
- `src/l3io/wp/backup/adapters/wpcli.py`
  - BL-E007-005: F-11: wp-config.php name defined in several modules - wp-config.php defined at archive_format.py:135, core/restore.py:149, cli/restore.py:260, wpcli.py:198, exclusions.py:48
  - BL-E007-018: Config-placement decision logic sits in adapter - _config_file still at adapters/wpcli.py:855
  - BL-E007-019: adapters/wpcli.py ~900 lines mixes concerns - adapters/wpcli.py is 983 lines, still mixes runner/session/restore extension
  - BL-E007-020: RunFacts hidden shared mutable state - RunFacts still shared between resolve and replacer: wpcli.py:353,580,727,782
  - BL-E008-023: Config keeps Archive mode before DB password - not verified closed in current tree; left open
- `src/l3io/wp/backup/api/errors.py`
  - BL-E006-018: Library misuse becomes UnexpectedError - only signature.bind check (errors.py:283-289); no type/enum validation at the API edge
- `src/l3io/wp/backup/api/operations.py`
  - BL-E006-018: Library misuse becomes UnexpectedError - only signature.bind check (errors.py:283-289); no type/enum validation at the API edge
- `src/l3io/wp/backup/cli/__init__.py`
  - BL-E002-066: Redteam lows L1-L5 (race, redaction, Ctrl-C, hardlinks, symlink) - bundle; L1 unresolved (store.py:115-124 check-then-replace, = BL-E002-040); L2-L5 not individually verified closed
  - BL-E002-081: Second SIGTERM during Run cleanup interrupts workspace removal - _terminate (cli/__init__.py:61-63) still raises on every SIGTERM; no SIG_IGN one-shot
  - BL-E003-115: Only SIGTERM handled; SIGHUP leaves residue - cli/__init__.py:72 only SIGTERM handled; no SIGHUP anywhere in src
- `src/l3io/wp/backup/cli/backup.py`
  - BL-E002-024: Manifest.payload required before payload - placeholder ManifestPayload invented at cli/backup.py:156, tar.py:572,589,637
  - BL-E004-045: path_excluded count vs Manifest excluded - cli/backup.py:192-216 _SkipStaged.decide excludes staged file without logging path_excluded
- `src/l3io/wp/backup/cli/context.py`
  - BL-E002-056: KeyboardInterrupt run_finished claims exit 1 - context.py:270-278 exit_code_for(error) applied to any BaseException incl. KeyboardInterrupt
- `src/l3io/wp/backup/cli/credentials.py`
  - BL-E002-071: Adversarial lows AR-07..AR-16 - bundle; AR-10 unresolved (store.py has file fsync only, no dir fsync), AR-07 unresolved (no bytes handling in _walk); AR-16 gen_cli_docs.py first-marker not verified closed
  - BL-E008-025: password file no regular-file/owner/mode check - _read_password_file credentials.py:140-149 opens path directly
- `src/l3io/wp/backup/cli/doctor.py`
  - BL-E008-020: doctor blockers only exit-code name - doctor.py:72 blockers=(exit_code_for(exc).name,)
- `src/l3io/wp/backup/cli/exit.py`
  - BL-E002-075: UX lows UX-01-11..15,17 - bundle; UX-01-17 unresolved (errors.py:198 still says 'pass passphrase='), UX-01-11 parser.py has no log-level metavar
- `src/l3io/wp/backup/cli/output.py`
  - BL-E002-075: UX lows UX-01-11..15,17 - bundle; UX-01-17 unresolved (errors.py:198 still says 'pass passphrase='), UX-01-11 parser.py has no log-level metavar
  - BL-E003-104: STRUCTURAL_ONLY plain line between JSON logs - cli/output.py:64 emit_warning prints plain text to stderr under JSON logs
  - BL-E008-022: Raising on_warning aborts restore after snapshot - emit_warning docstring: sink exception propagates (output.py:55-64)
- `src/l3io/wp/backup/cli/parser.py`
  - BL-E002-058: Argparse usage errors don't name USAGE meaning - parser.py has no ArgumentParser.error override; end-to-end test still exempts USAGE
  - BL-E002-075: UX lows UX-01-11..15,17 - bundle; UX-01-17 unresolved (errors.py:198 still says 'pass passphrase='), UX-01-11 parser.py has no log-level metavar
  - BL-E002-096: Remaining AD-5..AD-12, CR-1, CR-2 lows - unresolved: AD-5 (parser.py:152 'change nothing'), AD-9 (noop.py:45 imports database ADMIN_FLAGS), AD-10 (check_mode_defaults.py:53 hand list), AD-12 (ci.yml:103 pipe gate); CR-1 StoredUrls now used (core/relocation.py:60)
  - BL-E003-101: Restore usage errors print top-level usage - ran 'wpbackup2 restore --wp-dir /tmp/x': prints top-level usage
- `src/l3io/wp/backup/cli/restore.py`
  - BL-E003-013: E003-S01-001 L130: restore_mode_resolved's raw CLI flags never reach drive_resto - cli/restore.py:1211 restore_mode_resolved logs mode/overrides/snapshot_policy but not the raw CLI flags the story (L148) requires
  - BL-E003-018: E003-S02-006 L61/L86: a joined Run with relocation has no obligation to carry se - cli/restore.py:1274 binds relocation seams only if handle.opened; a joined Run with relocation is neither refused nor obliged to carry them
  - BL-E003-068: restore errors name deleted workspace copy of Archive - restore_runner.py:440,588,849 errors use str(inputs.archive.path) (workspace copy)
  - BL-E003-099: RestoreRequest carries AdminTls; global seam - cli/restore.py:74 AdminTls from adapters.database in RestoreRequest:814; RESTORE_EXTENSION global :751
  - BL-E003-104: STRUCTURAL_ONLY plain line between JSON logs - cli/output.py:64 emit_warning prints plain text to stderr under JSON logs
  - BL-E003-109: Split core/restore.py and cli/restore.py - core/restore.py now 1517 lines, cli/restore.py 1811
  - BL-E004-059: AR-5: restore reads wp-config before inner-Manifest check, decrypts twice - cli/restore.py:982-1000 _archive_config still decrypts separately from VERIFY (comment says decrypts again)
  - BL-E004-061: AR-8: plain Archive restore gated on encryption backend - cli/restore.py:~1759-1790 builds cipher and extra_tools=cipher.required_tools() whenever a passphrase resolves, regardless of view.encrypted
  - BL-E006-015: UsageError.arguments mixed vocabulary - flag spellings still used: restore.py:627,633,1184,1191; database.py:251-270
  - BL-E007-005: F-11: wp-config.php name defined in several modules - wp-config.php defined at archive_format.py:135, core/restore.py:149, cli/restore.py:260, wpcli.py:198, exclusions.py:48
  - BL-E008-021: restore flag combos accepted silently - allow_unencrypted/require_encrypted resolved without table check (restore.py:1332,1791)
  - BL-E008-022: Raising on_warning aborts restore after snapshot - emit_warning docstring: sink exception propagates (output.py:55-64)
- `src/l3io/wp/backup/cli/verify.py`
  - BL-E002-055: Failed verify no longer names Archive path - errors.py:655-670 VerificationFailedError message still has no archive path
  - BL-E002-120: verify --authenticate writes decrypted site to disk - verify.py:88 _AUTHENTICATED_PAYLOAD still written to workspace
- `src/l3io/wp/backup/core/config_location.py`
  - BL-E007-018: Config-placement decision logic sits in adapter - _config_file still at adapters/wpcli.py:855
- `src/l3io/wp/backup/core/exclusions.py`
  - BL-E004-016: ExclusionSource one type - three types remain: ports/exclusion.py:22 str, domain/exclusion.py:48 Literal, core/exclusions.py:51 StrEnum
- `src/l3io/wp/backup/core/restore.py`
  - BL-E003-040: Low closure findings ADV-12..14, CR-1..4, RT-004..007, SD-01-2..4, UX-8..11 - unresolved incl. SD-01-3 (core/restore.py now 1517 lines), ADV-13, UX-8..11 not verified closed
  - BL-E003-041: Load-DB creation not journaled - created_database only in memory (staging.py:304,537,606); not journaled
  - BL-E003-109: Split core/restore.py and cli/restore.py - core/restore.py now 1517 lines, cli/restore.py 1811
  - BL-E004-061: AR-8: plain Archive restore gated on encryption backend - cli/restore.py:~1759-1790 builds cipher and extra_tools=cipher.required_tools() whenever a passphrase resolves, regardless of view.encrypted
  - BL-E007-005: F-11: wp-config.php name defined in several modules - wp-config.php defined at archive_format.py:135, core/restore.py:149, cli/restore.py:260, wpcli.py:198, exclusions.py:48
- `src/l3io/wp/backup/domain/`
  - BL-E003-099: RestoreRequest carries AdminTls; global seam - cli/restore.py:74 AdminTls from adapters.database in RestoreRequest:814; RESTORE_EXTENSION global :751
- `src/l3io/wp/backup/domain/archive_format.py`
  - BL-E002-024: Manifest.payload required before payload - placeholder ManifestPayload invented at cli/backup.py:156, tar.py:572,589,637
  - BL-E004-036: ArchiveView.config_path dual meaning - archive_format.py:744,935 config_path still dual meaning
  - BL-E007-005: F-11: wp-config.php name defined in several modules - wp-config.php defined at archive_format.py:135, core/restore.py:149, cli/restore.py:260, wpcli.py:198, exclusions.py:48
- `src/l3io/wp/backup/domain/errors.py`
  - BL-E002-055: Failed verify no longer names Archive path - errors.py:655-670 VerificationFailedError message still has no archive path
  - BL-E002-075: UX lows UX-01-11..15,17 - bundle; UX-01-17 unresolved (errors.py:198 still says 'pass passphrase='), UX-01-11 parser.py has no log-level metavar
  - BL-E003-114: RunTerminated swallowed by except Exception - domain/errors.py:612 RunTerminated(BackupError); staging.py:310,380,416 except Exception
- `src/l3io/wp/backup/domain/exclusion.py`
  - BL-E004-016: ExclusionSource one type - three types remain: ports/exclusion.py:22 str, domain/exclusion.py:48 Literal, core/exclusions.py:51 StrEnum
- `src/l3io/wp/backup/domain/secret.py`
  - BL-E008-015: Secret unhashable/not copyable - secret.py: __hash__ = None, __reduce__ raises
- `src/l3io/wp/backup/ports/exclusion.py`
  - BL-E004-016: ExclusionSource one type - three types remain: ports/exclusion.py:22 str, domain/exclusion.py:48 Literal, core/exclusions.py:51 StrEnum
- `src/l3io/wp/backup/ports/site_files.py`
  - BL-E002-088: clean keeps nested keep path's whole top-level ancestor - site_files.py clean docstring documents ancestor-keep but still keeps whole ancestor; no refusal
- `tests/adapters/test_site_files.py`
  - BL-E002-088: clean keeps nested keep path's whole top-level ancestor - site_files.py clean docstring documents ancestor-keep but still keeps whole ancestor; no refusal
- `tests/adapters/test_tar_writer.py`
  - BL-E002-048: Publish gate accepts STRUCTURAL_ONLY archive - tar.py:1031-1035 verify_or_raise checks only result.passed, not Integrity.VERIFIED
- `tests/adapters/test_trace_ids_processor.py`
  - BL-E006-007: No test that concurrent Runs never share trace_id/span_id - No concurrency test in tests/adapters tracing tests (grep Thread/concurren: none)
- `tests/adapters/test_tracing.py`
  - BL-E006-007: No test that concurrent Runs never share trace_id/span_id - No concurrency test in tests/adapters tracing tests (grep Thread/concurren: none)
- `tests/adapters/test_wpcli.py`
  - BL-E007-002: Replay real WP-CLI 2.12.0 output fixtures; execute_restore-level case - Deferred on CI capture of real WP-CLI 2.12.0 output; test_wpcli.py uses canned output, no execute_restore-level case found
- `tests/contracts/test_admin_tls_only_by_restore_root.py`
  - BL-E003-064: admin_tls-only-by-restore-root not guarded - no AST contract on admin_tls= call sites (grep tests: none)
- `tests/contracts/test_credential_hygiene.py`
  - BL-E003-032: credential-clean test lacks database/cutover secret - test_credential_hygiene.py:640 has no cutover_credentials/database-side secret
- `tests/contracts/test_credential_sources_built_once.py`
  - BL-E002-090: WpConfigFile guard exemption is module-wide - _config_bindings (test_credential_sources_built_once.py:62) still module-wide; no rebinding handling
- `tests/contracts/test_streaming.py`
  - BL-E004-060: AR-6: extraction does not require read_inner_manifest; add AST contract - No AST contract pairing extract_archive/decrypt_payload with read_inner_manifest (tests/contracts only lists names in test_streaming.py:132-141)
- `tests/data/wp-config.php`
  - BL-E004-064: AR-11: fixture wp-config carries pass@word1, not a unique sentinel - tests/data/wp-config.php:25 still DB_PASSWORD 'pass@word1'
- `tests/fixtures/archives/v0`
  - BL-E004-064: AR-11: fixture wp-config carries pass@word1, not a unique sentinel - tests/data/wp-config.php:25 still DB_PASSWORD 'pass@word1'
- `tests/integration/test_config_outside_root.py`
  - BL-E007-003: F-09: no rig test of SITE_ROOT v1 Archive laid down by role - No rig test for SITE_ROOT v1 role lay-down into config-less root found in test_config_outside_root.py
- `tests/integration/test_dry_run_restore.py`
  - BL-E003-021: E003: no restore-level test for skipped symlinks (exit OK, WARNING lines, Safety - predicate-vs-filter agreement is tested (tests/unit/adapters/test_tar.py:351) but no restore-level test of skipped symlinks (exit OK, WARNING lines, snapshot round trip)
- `tests/integration/test_encrypted_backup.py`
  - BL-E004-049: Right-passphrase drive_restore runs FILES only; no Dump-loading mode - test_encrypted_backup.py:100-109 right-passphrase restore still RestoreMode.FILES only
- `tests/integration/test_exit_codes_end_to_end.py`
  - BL-E002-055: Failed verify no longer names Archive path - errors.py:655-670 VerificationFailedError message still has no archive path
  - BL-E002-058: Argparse usage errors don't name USAGE meaning - parser.py has no ArgumentParser.error override; end-to-end test still exempts USAGE
- `tests/integration/test_restore_modes.py`
  - BL-E003-021: E003: no restore-level test for skipped symlinks (exit OK, WARNING lines, Safety - predicate-vs-filter agreement is tested (tests/unit/adapters/test_tar.py:351) but no restore-level test of skipped symlinks (exit OK, WARNING lines, snapshot round trip)
- `tests/integration/test_search_replace.py`
  - BL-E007-002: Replay real WP-CLI 2.12.0 output fixtures; execute_restore-level case - Deferred on CI capture of real WP-CLI 2.12.0 output; test_wpcli.py uses canned output, no execute_restore-level case found
- `tests/integration/test_site_lock_signals.py`
  - BL-E002-081: Second SIGTERM during Run cleanup interrupts workspace removal - _terminate (cli/__init__.py:61-63) still raises on every SIGTERM; no SIG_IGN one-shot
- `tools/check_mode_defaults.py`
  - BL-E002-096: Remaining AD-5..AD-12, CR-1, CR-2 lows - unresolved: AD-5 (parser.py:152 'change nothing'), AD-9 (noop.py:45 imports database ADMIN_FLAGS), AD-10 (check_mode_defaults.py:53 hand list), AD-12 (ci.yml:103 pipe gate); CR-1 StoredUrls now used (core/relocation.py:60)
  - BL-E002-105: Arch MINOR SD-02-3..SD-02-12 - SD-02-8 still real (check_mode_defaults.py:53 GUARDED_TYPES hand-kept); SD-02-6 ADMIN_FLAGS in adapters still
- `tools/gen_cli_docs.py`
  - BL-E002-071: Adversarial lows AR-07..AR-16 - bundle; AR-10 unresolved (store.py has file fsync only, no dir fsync), AR-07 unresolved (no bytes handling in _walk); AR-16 gen_cli_docs.py first-marker not verified closed

### 5c - possible owner decision (27)

Grouped by file (an item touching several files appears under each).

- `README.md`
  - BL-E002-032: Default archive name .tar.gz vs uncompressed outer tar - storage_target.py:48 still yields .tar.gz for uncompressed outer tar; naming choice unrecorded
- `_bmad-output/implementation-artifacts/state/archived/epic-003/sprint-01/E003-S01-001.yaml`
  - BL-E003-026: E003: estimates not redone after the reorg and scope growth — sprint-01 rollup s - estimates never redone: E003-S01-001 still standard/7.5h; archived sprint rollup unchanged; owner decision whether to re-estimate historical data
- `_bmad-output/implementation-artifacts/state/archived/epic-003/sprint-01/sprint.yaml`
  - BL-E003-026: E003: estimates not redone after the reorg and scope growth — sprint-01 rollup s - estimates never redone: E003-S01-001 still standard/7.5h; archived sprint rollup unchanged; owner decision whether to re-estimate historical data
- `docs/adr/0025-api-composes-through-the-cli-drivers.md`
  - BL-E006-024: Revisit ADR-0025 option b after first release - owner-deferred post-v1 tracking item; ADR-0025 revisit-when
- `docs/api.md`
  - BL-E006-019: structlog run_id/dry_run keys overwrite host's - still bound un-namespaced (context.py:220); documented at docs/api.md:121-123; namespacing would change the logging.md schema
  - BL-E008-028: RunContext still public - RunContext in api __all__ (api/__init__.py:88); docs/api.md:558 says do not construct
- `docs/restore.md`
  - BL-E003-048: Safety Snapshot database part charged 0 bytes - restore_runner.py:548-560 db scope claims 0 bytes (documented limit)
  - BL-E003-087: Unencrypted snapshots/configs no retention - no retention/prune for snapshots or saved configs in src or docs (grep); RT-S02-008 'accepted by design' but no owner ADR
  - BL-E003-089: Hostile Archive can park Site behind journal - redteam-report RT-S02-010 says deliberate; no ADR; hostile archive can block via journal
  - BL-E003-113: Re-survey cannot detect writes to non-empty Site - closure-report epic-003 line 10: judgement call, no maintenance-mode design; docs/restore.md:416 documents only
  - BL-E003-116: Free-space check omits DB server disk - restore_runner.py _check_space doesn't cover DB server; docs/restore.md:446-472 silent on it
- `pyproject.toml`
  - BL-E006-022: TestPyPI sources; claim names on PyPI - [tool.uv.sources] still testpypi: pyproject.toml:131-139; release-time action
- `src/l3io/wp/backup/adapters/config_writer.py`
  - BL-E002-091: config_writer._write_over duplicates library atomic write - config_writer.py:343 _write_over still duplicates library atomic write; upstream request not made
- `src/l3io/wp/backup/adapters/crypto.py`
  - BL-E004-055: RT-7: database cli age backend passes whole env and unfiltered PATH - Upstream database cli age backend env/PATH; no upstream fix or contract test in tree
- `src/l3io/wp/backup/adapters/lock.py`
  - BL-E002-083: Site Lock key misses case-insensitive/bind-mount spellings - key still sha256(resolved path) per AD-19; spec gap needs architect decision
- `src/l3io/wp/backup/adapters/preflight.py`
  - BL-E005-010: Probe locates binary by name; later run re-resolves PATH (TOCTOU) - wp now resolved once via locate_tool (wpcli.py:~53,975) but age still re-resolved by database; wpcli.py docstring calls residual 'stated, not closed'; see BL-E004-055
- `src/l3io/wp/backup/adapters/redaction.py`
  - BL-E002-118: Passphrase minimum 4 is a redaction artefact - passphrase.py:115 only MIN_SECRET_LENGTH check; no policy/warning
- `src/l3io/wp/backup/adapters/restore_runner.py`
  - BL-E003-048: Safety Snapshot database part charged 0 bytes - restore_runner.py:548-560 db scope claims 0 bytes (documented limit)
  - BL-E003-116: Free-space check omits DB server disk - restore_runner.py _check_space doesn't cover DB server; docs/restore.md:446-472 silent on it
- `src/l3io/wp/backup/adapters/workspace.py`
  - BL-E003-086: Plaintext residue in TMPDIR after SIGKILL - workspace.py:50 mkdtemp in TMPDIR; no stale sweep or doc
- `src/l3io/wp/backup/adapters/wpcli.py`
  - BL-E005-010: Probe locates binary by name; later run re-resolves PATH (TOCTOU) - wp now resolved once via locate_tool (wpcli.py:~53,975) but age still re-resolved by database; wpcli.py docstring calls residual 'stated, not closed'; see BL-E004-055
  - BL-E007-015: Manifest config_path=../wp-config.php creates file in Site Root parent - Designed FR-29 behaviour per redteam-report RT-8 (fix 'none required'); no owner decision/ADR closing it found
- `src/l3io/wp/backup/api/__init__.py`
  - BL-E008-028: RunContext still public - RunContext in api __all__ (api/__init__.py:88); docs/api.md:558 says do not construct
- `src/l3io/wp/backup/api/operations.py`
  - BL-E006-025: Public open_run() and context= post-v1 - owner-deferred post-v1 tracking item with BL-E006-024
- `src/l3io/wp/backup/cli/backup.py`
  - BL-E002-112: Backup plan interpreter in cli/backup.py, AD-4 boundary - ADR-0025 settles drivers in cli/ and FR-24 contract bars cli->api (pyproject.toml:389), but AD-4 gate still exempts all cli/ (ci.yml:105)
  - BL-E006-013: Stale os.environ docstrings / environ param - docstrings now accurate (cli/verify.py:169-171, drive_verify takes environ); drive_backup/restore still read os.environ (backup.py:620, restore.py:1253); environ param is the optional part
- `src/l3io/wp/backup/cli/context.py`
  - BL-E006-014: Joined dry-run ctx with dry_run=False - join_or_open still checks one direction only (context.py:332); public context= removed, returns with BL-E006-025
  - BL-E006-019: structlog run_id/dry_run keys overwrite host's - still bound un-namespaced (context.py:220); documented at docs/api.md:121-123; namespacing would change the logging.md schema
- `src/l3io/wp/backup/cli/parser.py`
  - BL-E008-013: --list-default-excludes still needs --wp-dir - parser.py:222,278 --wp-dir required=True; owner decision in E008 closure-report
- `src/l3io/wp/backup/cli/passphrase.py`
  - BL-E002-118: Passphrase minimum 4 is a redaction artefact - passphrase.py:115 only MIN_SECRET_LENGTH check; no policy/warning
- `src/l3io/wp/backup/cli/restore.py`
  - BL-E003-033: snapshot_location refuses HOME-based default with ~/wp-config.php - core/restore.py:639-657 snapshot_location unchanged; no ~/public_html layout test
  - BL-E003-087: Unencrypted snapshots/configs no retention - no retention/prune for snapshots or saved configs in src or docs (grep); RT-S02-008 'accepted by design' but no owner ADR
  - BL-E006-013: Stale os.environ docstrings / environ param - docstrings now accurate (cli/verify.py:169-171, drive_verify takes environ); drive_backup/restore still read os.environ (backup.py:620, restore.py:1253); environ param is the optional part
- `src/l3io/wp/backup/core/config_location.py`
  - BL-E007-015: Manifest config_path=../wp-config.php creates file in Site Root parent - Designed FR-29 behaviour per redteam-report RT-8 (fix 'none required'); no owner decision/ADR closing it found
- `src/l3io/wp/backup/core/exclusions.py`
  - BL-E004-063: AR-10: wp-config.php protection case-sensitive - core/exclusions.py:48 PROTECTED_PATHS has no Linux-only note; docs/exclusions.md:63 documents case-sensitivity only
- `src/l3io/wp/backup/core/restore.py`
  - BL-E003-033: snapshot_location refuses HOME-based default with ~/wp-config.php - core/restore.py:639-657 snapshot_location unchanged; no ~/public_html layout test
  - BL-E003-089: Hostile Archive can park Site behind journal - redteam-report RT-S02-010 says deliberate; no ADR; hostile archive can block via journal
- `src/l3io/wp/backup/domain/archive_format.py`
  - BL-E008-001: Stream-parse the Manifest (owner: later) - owner-deferred tracking item (E004 D2 c); MANIFEST_MAX_BYTES archive_format.py:123
- `src/l3io/wp/backup/domain/storage_target.py`
  - BL-E002-032: Default archive name .tar.gz vs uncompressed outer tar - storage_target.py:48 still yields .tar.gz for uncompressed outer tar; naming choice unrecorded
- `tests/contracts/test_no_direct_crypto.py`
  - BL-E004-067: AD-5: no-direct-crypto guard forbids a hand-kept list of libraries - Guard still hand-kept FORBIDDEN list; docstring discloses coverage; no owner decision to accept
- `tests/contracts/test_no_dry_run_outside_roots.py`
  - BL-E002-112: Backup plan interpreter in cli/backup.py, AD-4 boundary - ADR-0025 settles drivers in cli/ and FR-24 contract bars cli->api (pyproject.toml:389), but AD-4 gate still exempts all cli/ (ci.yml:105)
- `tests/fixtures/archives/v1/minimal.tar.gz`
  - BL-E002-032: Default archive name .tar.gz vs uncompressed outer tar - storage_target.py:48 still yields .tar.gz for uncompressed outer tar; naming choice unrecorded
- `tests/integration/test_kill_mid_restore.py`
  - BL-E003-086: Plaintext residue in TMPDIR after SIGKILL - workspace.py:50 mkdtemp in TMPDIR; no stale sweep or doc
