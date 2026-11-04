# E003 architecture gate — pre-build re-check against the built E002 code (l3io-arch-review, Mode B)

Date: 2026-11-03. Baseline: gate pass `c4ad808` (`arch-gate-review.md`). Scope: the 6 E003 stories as of HEAD, the `c4ad808..HEAD` story diff (+28/−22), and the E002 modules those stories cite.

Path keys: `S01-001` = `_bmad-output/implementation-artifacts/epic-003/sprint-01/stories/E003-S01-001.md` (the same for the other five stories). Code paths are relative to `src/l3io/wp/backup/` unless they start with `tests/`, `pyproject.toml` or `.venv/`.

## Executive summary

The plan/executor split, the injected `SNAPSHOT`/`LOAD_DUMP` slots and the domain vocabulary all fit the built code. Sprint 01 needs nothing from sprint 02. Three things cannot be built as written:

- the decrypt-failure errors name E004 classes that do not exist, with an exit code that contradicts the built code and the owner's 2026-11-03 decision;
- S01-002's flag-read contract test fails on day one against `cli/__init__.py`;
- BL-E002-114's TLS clause has no seam in `l3io-wp-database` or `adapters/database.py`.

Seven MAJORs are contract gaps a dev agent would most likely get wrong:

- `RestorePorts` is never declared;
- `PREFLIGHT` assumes a preflight port that the `AdapterSet` lacks;
- the unpatched-tarfile refusal is not hoisted before `CLEAN`;
- BL-E002-116 needs an edit to E002's domain file that is not granted, and v0 behaviour is undefined;
- the refusal flag cannot be rendered from `core/`;
- no story calls the Dry Run `check_*` queries;
- BL-E002-114's "match the Manifest" rule does not stop a forged plaintext Archive.

Counts: **3 BLOCKER, 7 MAJOR, 14 MINOR.**

## Findings

BLOCKER: [(a)+(c) contract mismatch / cross-epic leak · Core §3 Design by Contract] S01-001:70, S01-001:119 and S02-006:106 have `VERIFY`'s decrypt raise "E004-S01-003's two errors", `ArchiveDecryptionError` → `CREDENTIALS` (4) and `EncryptedMemberCorruptError` (8). Neither class exists, and E004 runs in parallel with E003. The built cipher `adapters/crypto.py:86-88` raises `PassphraseRejectedError` and `PayloadDamagedError` (`domain/errors.py:433-461`); both are `VERIFICATION_FAILED` (8) "(Shawn, 2026-11-03)", and `cli/verify.py:25` relies on the same. As written, the story imports classes that do not exist and asserts exit 4, which the owner decision rules out. Remediation: cite `PassphraseRejectedError`/`PayloadDamagedError`, both exit 8, in all three places. S01-001:167's fake `PayloadCipher` raises those classes.

BLOCKER: [(b) sprint order · Core §4 Testability] S01-002:54 and S01-002:106 specify `tests/contracts/test_no_flag_reads.py`. It greps for `args.force`, `args.cleanfirst` and `.cleanfirst` everywhere except `domain/flag_map.py` and `cli/parser.py`. The built `cli/__init__.py:89-91` (E002-S02-011's pre-Run `check_restore_arguments` call) already reads `args.cleanfirst`, `args.drop_database` and `args.force`. The test fails as soon as it is written. S01-002 owns no `cli/` file, so sprint 01 cannot close green. The grep also leaves out the fourth AD-25 dimension, `drop_database` (`domain/flag_map.py:59-61`), so the guard's scope is incomplete. Remediation: allow-list `cli/__init__.py`'s pre-Run check and `cli/restore.py` by name. Add `args.drop_database`/`.drop_database` to the pattern. Plant one violation per pattern.

BLOCKER: [(e) close-out AC not buildable where placed · Core §7 Dependencies] S02-006:136 (BL-E002-114) requires "administrator connections to a non-local host require TLS". Nothing exists to build it on:
- `.venv/.../l3io/wp/database/connection.py:35-49`: `Connection` has only `host`, `database` and `port`, and the installed `l3io-wp-database` contains no `ssl` or `tls` anywhere.
- `adapters/database.py` (E002's; `_connection` called at :508, :543) has no TLS option.

E003-S02-006 cannot meet this clause and so cannot resolve BL-E002-114 with its key. `tests/contracts/test_closeout_gate.py` then blocks project close-out. Remediation (owner decision): either schedule an upstream `l3io-wp-database` release with TLS, plus an edit grant to `adapters/database.py` and an ADR (Core §7), or reword the clause to a control that can be built, such as refusing a non-loopback administrator host on the no-target-config path unless the operator explicitly confirms that host.

MAJOR: [(e) close-out AC does not close the finding · Core §3] S02-006:136 lets the Archive-config `DB_HOST`/`DB_NAME`/`DB_USER` through when they "match an operator-supplied value or the Manifest's recorded descriptor".
- Restore has no `--db-*` flags. The only operator-supplied values are Epic 7's `--new-db-*`, which are `NOT_YET_IMPLEMENTED` (`cli/parser.py:82-85`).
- So until Epic 7 lands, the only acceptable match is `Manifest.source`, which comes from the same forgeable plaintext Archive. That forgeability is RT-1's premise.
- The specified test ("a forged Archive config naming another host is refused") passes only if the forger leaves the Manifest alone.
- `ArchiveView` does not carry `Manifest.source` (`domain/archive_format.py:590-604`), so this also needs an E002 domain edit that is not granted.

Remediation: add a test in which the config and `Manifest.source` are forged together, and require it to refuse. State that a Manifest match alone is insufficient for an unauthenticated Archive, so the operator must confirm or supply the identity. Grant the `ArchiveView` edit explicitly.

MAJOR: [(e) BL-E002-116 placement · Core §3, §4] S01-001:155 says "`ViewMember` keeps the Manifest's `size`", but the edit grant covers only `extract_archive`.
- `ViewMember` has no `size` (`domain/archive_format.py:580-586`), and `classify` (`domain/archive_format.py:774`) drops it. Adding the field is an edit to E002's domain file and every `ViewMember` constructor.
- v0 Archives have no Manifest sizes. The story does not say what `PREFLIGHT` does for v0 (refuse every v0 restore, or skip the check?). `tests/contracts/test_version_branching.py` forbids branching on `format_version` in `adapters/`.
- The "stated limit" on expansion ratio is never stated, nor is where it lives.

Remediation: grant the `ViewMember`/`classify` edit by name. Use `size: int | None`, with `None` for v0. Define v0 behaviour, for example a free-space check against the Archive's size times the limit, plus the running header-size cap during extraction. Branch on `size is None`, never on the version. Name the ratio constant and its module.

MAJOR: [(a) owner decision applied too late in the phase order · Core §3] Owner decision 2026-11-03: extraction refuses on a Python without the tarfile CVE fix. It is implemented inside `extract_archive` (`adapters/tar.py:1076-1079`, the `os.path.ALLOW_MISSING` probe), so it fires at `EXTRACT`, after `SNAPSHOT` and after `CLEAN` has emptied the Site Root in `files-clean`/`all-clean`. `pyproject.toml:9` allows `>=3.11`, which includes unpatched 3.11.0–3.11.12. No E003 story checks this before `CLEAN`; S01-001:155's `PREFLIGHT` covers size only. Remediation: in S01-001, have `PREFLIGHT` call the same capability check, factored out of `adapters/tar.py` as one function both callers use so they cannot diverge, and raise `ExtractionRefusedError` before any command. Test it with `ALLOW_MISSING` monkeypatched away and assert an empty command log.

MAJOR: [(a)+(d) undeclared contract · Core §1, §3] `RestorePorts` is used in four places, but no story declares its module, its complete field list, or how it is built from the bound `AdapterSet`:
- S01-001:68 and S01-001:104 use `ports.preflight` and `ports.search_replace`;
- S01-001:106 adds `restore_database_*` and `database_*`;
- S02-005:66 and S02-005:70 use `ports.site_files`;
- S02-006:87 uses `RestorePorts.cutover_credentials`.

AD-4 Dry Run equality depends on building it from `ctx.adapters`. It cannot live in `core/`: the layers contract at `pyproject.toml:358-367` forbids `core` → `ports`. Remediation: S01-001 declares a frozen `RestorePorts` in `adapters/restore_runner.py` with every field, and one constructor, `restore_ports(adapters: AdapterSet, *, preflight: Preflight, cutover_credentials: ...)`. The other stories cite it.

MAJOR: [(a)+(d) port that does not exist · Core §1] S01-001:68 says `PREFLIGHT` runs "through `ports.preflight`", and S02-006:88 says "Preflight is whatever `real_adapters` binds". Neither matches the code:
- `AdapterSet` has no preflight field (`ports/adapter_set.py:37-58`), and `real_adapters` binds none (`cli/adapter_sets.py:117-178`).
- The backup root uses `cli/preflight.DumpReadinessPreflight` and `cli/preflight.require_tools`. Both are in `cli/`, which `adapters/restore_runner.py` may not import under the layers contract.

Remediation: the restore root constructs the `Preflight` and passes it in `RestorePorts`. The executor turns `ToolStatus` results into `PreflightFailedError` using `domain/preflight.py::preflight_failure` (`domain/preflight.py:94`), or `require_tools` moves below `cli/`. Correct S02-006:88.

MAJOR: [(a)+(d) layering · Core §1] S01-002:55 gives `NonEmptyTargetError` a field `authorizing_flag: str` "looked up from the same E002 table".
- `authorizing_combination` returns a `FlagCombination` keyed by `BackupScope` (`domain/flag_map.py:233-251`).
- Rendering that as CLI text needs `cli/scope.py::CLI_MODE_TO_SCOPE` (`db` ↔ `database`), which `core/` cannot import.
- A rendering done in `core/` would name `--mode database`, which the parser rejects.

Remediation: the error carries the `FlagCombination`, and `cli/restore.py` renders it through `CLI_MODE_TO_SCOPE`. Alternatively, the flag table gains a domain-owned rendering. A test asserts that the rendered text parses back to the same combination.

MAJOR: [(a) Dry Run parity unassigned · AD-4, Core §4] S01-001:72 and S02-004:100 say the queries `check_ensure`, `check_load` and `check_drop_database` "run for real" in a Dry Run.
- S02-005:66-71 gives `load_staged` the sequence creation → load → check → cutover → drop, using commands only. No story assigns the `check_*` calls.
- `NoopDatabase` consults none of them; it only runs `_require_admin` (`adapters/noop.py:350-405`).

As written, a Dry Run will not refuse a missing privilege or an occupied operator-named database where the real Run would. Remediation: S02-005 calls `check_ensure(load_location)`, `check_load(load_location, source=<dump path or None>)` and `check_drop_database(..., confirm=...)` before the matching command, in both Runs, and raises on `Readiness.ready is False`.

MINOR: [(a) stale names] S01-001:77 and S01-001:72 cite `DatabaseQuery.survey` and `DatabaseCommand.ensure`/`load`/`drop_database`. The built names are `survey_target`, `ensure_database` and `load_dump`, and the checks have keyword parameters: `check_load(location, *, source)` and `check_drop_database(location, *, confirm)` (`ports/database.py:41-100`).

MINOR: [(a) signature] S01-001:70 has `VERIFY` call `verify_archive` "on the Archive" and raise on `passed is False`. The built `verify_archive(path: Path)` returns rather than raises and takes a `Path`, not `inputs.archive: StorageTarget` (`adapters/tar.py:832`, `:849`, `:878`). Use `verify_archive_view` and require the verified view to equal `inputs.view`. That also covers RT-8, the Archive being swapped between the step-5 probe and `VERIFY`.

MINOR: [(a) constructor] S01-001:73 raises `ArchiveHasNoDumpError` "naming the Archive's `backup_scope`". The built class takes `(archive: str, backup_scope: str | None)` (`domain/errors.py:517-524`), but `ArchiveView` has no `backup_scope` and `build_restore_plan` takes no archive name. A v1 files-scope Archive would therefore be reported as "a v0 archive".

MINOR: [Core §3 consistency] S01-001:56 puts new error classes in `domain/errors.py`. S01-001:63, S01-001:100 and S02-004:79 put them in the module that raises them. Pick one rule.

MINOR: [Core §2 Reuse] S01-001:58 re-derives the mode → phase predicates and the subsequence assertion. `domain/restore.py` already provides `restores_files`, `restores_database`, `clears_site_root` and `removes_database` (:58-114), and `is_subsequence`/`RESTORE_PHASE_ORDER` (:132-145). Use them so there is only one mode table.

MINOR: [(d) unspecified type] S01-001:58 and S01-001:91 type `RestorePlan.actions: tuple[PlannedAction, ...]` but do not say where `PlannedAction` comes from. The existing `cli/backup.py:173` type cannot be imported from `core/`. Declare a pure one in `core/` or `domain/`.

MINOR: [(a) stale flag surface] S01-002:43 and S01-002:59 say the frozen `--mode`, `--cleanfirst` and `--force` "carry the authorizations". They leave out `--drop-database` (`domain/flag_map.py:9-11`, `cli/parser.py:281`), and the resolved note predates the 2026-10-20 table.

MINOR: [(a) missing inputs] S01-002:55-57 needs `found` (the canonical Site Root, or `host:port/db_name`) and `detail` (entry or table count). `check_target(mode, overrides, target)` receives only `TargetState`, which holds none of these. Add the fields; `SiteFilesQuery.entry_count` exists (`ports/site_files.py:46`).

MINOR: [(a) E002-S01-004 deviation] S02-006:66 and S02-003:68 give the snapshot `backup(...)` call without `config: LocatedConfig`. That parameter is keyword-required (`cli/backup.py:516-528`; recorded in E002-S01-004's completion notes). S02-003:58 also says the call "goes through `join_or_open`", but `backup()` takes a required `ctx` and never opens a Run. Neither story says which `LocatedConfig` to pass for a target that has no config.

MINOR: [(a) signatures] S02-006:59, S02-006:61 and S02-006:68 do not match the built signatures:
- `join_or_open(..., dry_run=request.dry_run)` is omitted (`cli/context.py:308-317`).
- `real_adapters` also requires `wordpress_source=` and `secrets=` (`cli/adapter_sets.py:117-126`).
- `site_descriptor_from_wp_config` returns `(SiteDescriptor, CredentialSource)` (`adapters/database.py:199-207`).
- `site_descriptor_from_archive_config` takes `config_file=` as a keyword (`adapters/database.py:210-212`).

MINOR: [(c) avoidable E004 dependency] S02-006:57 builds the cipher with E004-S01-003's `build_payload_cipher` ("`None` until" E008), so every encrypted Archive is refused until then. E002 already ships `adapters/crypto.PassphraseCipher`, and `cli/verify.py:113` builds it directly. S02-006:71 also calls `cipher.required_tools()`, which `PayloadCipher` does not have (`ports/crypto.py:17-29`). This is the owner's sequencing choice; it is recorded, not contested.

MINOR: [Core §2 Reuse] S01-001:75 and S02-006:15 treat `cli/restore.py` as a new file granted to Epic 3. E002 built it (`cli/restore.py:39-99`: `restore_mode_from_cli`, `snapshot_policy_from_cli`, `config_policy_from_cli`), and `cli/__init__.py:85-91` already runs the pre-Run check. S02-006:57 should extend and reuse it, not resolve the flags a second way.

MINOR: [Core §4 hollow-guard risk] S01-001:157 and S02-006:137 both claim to satisfy `tests/contracts/test_credential_hygiene.py:583-595`. That tripwire fires as soon as S01-001 creates `core/restore.py`, and it checks only that a test name contains "restore". S01-001 has to make it a real `credential_sentinel` case: `execute_restore` over fake config and database ports that hold a registered secret. S02-006 then adds the CLI case.

MINOR: [AD-8 optional import] S02-005:67 has `adapters/staging.py` call `l3io.wp.database.validate_database_name`. State that the import is deferred, as `adapters/crypto.py:6` and `adapters/database.py` do, so a build without the `database` extra still imports the module.

## Answers to the five questions

- **(a) Contract mismatches.** Three BLOCKER/MAJOR items: the decrypt error classes, `RestorePorts`, and the preflight port. The rest are MINOR name and signature drift. They are listed above.
- **(b) Sprint order.** S01-001 and S01-002 need no sprint-02 code. These all exist: `RunJournalCommand`, `NoopRunJournal`, the restore halves, `DatabaseTargetState`, `EnsureOutcome`, `JournalHeader`/`JournalEntry`, `SkippedLink`, `PayloadCipher`, `decrypt_payload` and `extract_archive(..., exclude=, payload=)`. The slots are injected. Sprint 01 can close green once three things are fixed: the S01-002 flag-read test (BLOCKER), and the declarations of `RestorePorts` and preflight (MAJOR).
- **(c) Cross-epic leaks.** One BLOCKER: the E004 error classes (S01-001:70/119, S02-006:106). One MINOR: E004's `build_payload_cipher`. The Epic 7 and Epic 5 seams are optional by construction (`RESTORE_EXTENSION = None`, and preflight through `real_adapters`, though that last part is wrong per the preflight MAJOR).
- **(d) Layering.** The three contracts are in `pyproject.toml:324-367`. Every type `core/restore.py` needs is in `domain/` or stdlib (`pathlib` is not forbidden). The risks are `RestorePorts` placement, `require_tools`/`DumpReadinessPreflight` living in `cli/`, CLI-flag rendering from `core/`, and `PlannedAction`. Each is a finding above.
- **(e) Close-out ACs.** BL-E002-116 (S01-001) can be built once the `ViewMember` edit is granted and v0 behaviour is defined (MAJOR). BL-E002-114 (S02-006) cannot be built as placed (TLS BLOCKER), and its matching rule does not close RT-1 (MAJOR).

## Per-principle walkthrough

- Core §1 Separation of concerns: findings on `RestorePorts`, the preflight port and CLI-flag rendering. Otherwise PASS; the plan in `core/` and the executor in `adapters/` fit the layers contract.
- Core §2 Reuse: three MINORs (the domain predicates, the existing `cli/restore.py`, `PassphraseCipher`).
- Core §3 Design by contract: the decrypt-errors BLOCKER, plus the BL-E002-114, BL-E002-116, tarfile-check, `RestorePorts` and preflight MAJORs, plus the MINOR signature drift.
- Core §4 Testability: the flag-read-test BLOCKER, the Dry Run `check_*` MAJOR and the hygiene-tripwire MINOR. Scope derivation elsewhere (`FLAG_MAP` keys, `dataclasses.fields`) PASS.
- Core §5 Brevity: PASS (story length is earlier gates' accretion and is not a build risk).
- Core §6 Comments: PASS.
- Core §7 Dependencies: the TLS BLOCKER (the dependency lacks the feature).
- Core §8 GA over beta: PASS (no pre-release dependency introduced).
- Core §9 Logging and correlation: PASS (`phase()`/`join_phase` exist, and the snapshot joins the restore's `run_id`).
- Core §10 Documentation: PASS (`docs/restore.md` is owned by S01-001).
- Python overlay: PASS, except that `requires-python >=3.11` admits unpatched interpreters. That is handled by the owner's runtime refusal; see the tarfile MAJOR.

## Verdict

**NOT READY TO BUILD.** E003's architecture holds against the built E002 code, and the sprint order is sound. Each of the three BLOCKERs makes a story unbuildable or a close-out item unresolvable as written:
- the decrypt error classes and exit code;
- S01-002's flag-read test;
- BL-E002-114's TLS clause.

The first two, and all seven MAJORs, are story-text corrections that need no new decision. The TLS clause needs an owner decision: an upstream dependency release with an ADR, or a reworded control. Fix the BLOCKERs and MAJORs, then re-run this check before sprint 01 starts. The MINORs go to the backlog.

## Recommended ADRs

- If the owner keeps TLS in BL-E002-114: an ADR adopting an `l3io-wp-database` release with TLS on `Connection`, plus the `adapters/database.py` edit grant (Core §7).

Sections read: the E003 story diff `c4ad808..HEAD`; all six E003 stories (S01-001 and S01-002 in full; S02-003:55-100; S02-004:50-100; S02-005:50-159; S02-006 in full); code: `domain/{restore,journal,flag_map,archive_format (ViewMember/ArchiveView/classify/Manifest), errors, database, site, preflight, config}.py`, `ports/{site_files,database,journal,adapter_set,config,preflight,crypto,search_replace,archive}.py`, `adapters/{tar (verify/extract/decrypt/read_member), site_files, crypto, phase, noop (NoopDatabase), database (LocatedConfig, descriptor constructors, credential sources)}.py`, `cli/{restore,adapter_sets,context (open_run/join_or_open),backup (backup()/BackupResult/PlannedAction/_cipher),credentials,scope,parser (NOT_YET_IMPLEMENTED, restore flags),passphrase,exit,__init__ (pre-Run check)}.py`, `core/` (listing; `core/backup.py` signatures); `pyproject.toml` import-linter contracts; `tests/contracts/{test_credential_hygiene,test_exception_hierarchy,test_exit_codes_family}.py` (relevant parts); `tests/unit/cli/test_drivers.py` header; `docs/closeout-gate.yaml`; `state/issues.yaml` BL-E002-114..117; `epic-002/epic-closure/redteam-report.md` RT-1/RT-3/RT-8/RT-10/RT-11 rows; E002 completion-note deviations (grep; E002-S01-004, E002-S02-007, E002-S02-008, E002-S02-011, E002-S02-012); the installed `l3io/wp/database/connection.py`. No spec section from `/tmp/wpb-tools/e003-sections.txt` was needed: every finding is a story-to-code comparison.
