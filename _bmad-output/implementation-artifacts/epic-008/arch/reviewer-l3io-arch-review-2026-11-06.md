# Arch gate review — E008 "Wire the parallel epics into the commands"

- **Reviewer:** l3io-arch-review, Mode B (gate review)
- **Date:** 2026-11-06
- **Scope:** `epic-008/sprint-01/stories/E008-S01-001.md` (line numbers below are the story as it stood before this gate's edits)
- **Checked against:** `ARCHITECTURE-SPINE.md` (AD-4, AD-8, AD-9, AD-13, AD-22, AD-26, Stack), PRD FR-14/FR-27,
  ADR-0008, ADR-0015, ADR-0016, ADR-0019, ADR-0022, ADR-0024 (with the 2026-11-06 amendments), `docs/closeout-gate.yaml`,
  `epics.md` (Epic 8, ownership notes), `plan-2026-10-27-v2.yaml` (E008 is phase 4, after E007), the built code at `HEAD`
  (`e0902df`), and the current text of E004-S01-001/-003, E005-S01-001/-002, E006-S01-001 and E007-S01-001..004.
  Uncommitted E004 work in the tree (`adapters/exclusions.py`, `core/exclusions.py`) was ignored; built = committed.
- **Edits made:** the story only, marked *(E008 arch gate, 2026-11-06)*. No source, test, ADR or other epic's story was touched.

## Executive summary

The story's credential and cipher halves now match the built code and E004/E005's promises. The preflight
and flag halves do not. One flag the mapping assigns to E008 (`restore --db-credentials-aws-*`) has no path
into the restore driver. The built guard would also let it go green while the flag does nothing.
`RootPreflight` is described as an `AdapterSet` field that does not exist. Its platform refusal can
never run for a files-only Run. `doctor` binds the wrong preflight. `--list-default-excludes` has no binder.
The "completion check" the story relies on cannot fail.

**Most important action:** make the restore `--db-credentials-aws-*` binding explicit (B1). Then derive the
completion check from the mapping, so a left-over E008 entry or a silently accepted flag fails CI (M5).

Totals: **1 BLOCKER, 8 MAJOR, 11 MINOR. 19 fixed in the story; 1 MINOR (m10) and 2 owner decisions (D1, D2) open.**

## `NOT_YET_IMPLEMENTED`: one binder per entry, derived from the mapping

Derived from `cli/parser.py:90-111` (19 entries). Each entry's binder is read from the comment beside the
entry's reason constant (`cli/parser.py:72-82`), then checked against each story's current text.

| Reason constant (entries) | Binder in the code comment | Story text | Result |
|---|---|---|---|
| `RELOCATION_UNAVAILABLE` (7: `restore --wp-config`, `--new-site-url`, `--new-site-home-url`, `--new-db-host`, `--new-db-name`, `--new-db-user`, `--new-db-password`) | "Epic 7 (E007-S01-001, E007-S01-002)" (`:72`) | E007-S01-001:114 removes "the accepted-and-explained exit" for the `--new-*` flags. No E007 story names `--wp-config` or lists `cli/parser.py` | **Two candidate binders, none per entry.** Cross-epic X3 |
| `_AWS_UNAVAILABLE` (6: `restore --admin-credentials-aws-{secret-id,region}`, `restore --db-credentials-aws-{secret-id,region}`, `backup --db-credentials-aws-{secret-id,region}`) | E008-S01-001 (`:75`) | E008:32, :59; E005-S01-001:34 agrees | One binder. Restore `--db-…` cannot be bound as written (B1) |
| `_ENCRYPTION_UNAVAILABLE` (1: `backup --encrypt-with-passphrase`) | E008-S01-001 | E008:71-75; E004-S01-003:42 agrees; E004-S01-003:189 says "naming this story" | One binder. Cross-epic X1 (wording) |
| `_EXCLUDES_UNAVAILABLE` (3: `backup --exclude`, `--no-default-excludes`, `--list-default-excludes`) | E008-S01-001 | E008:75 binds the first two only; E004-S01-001:90 assigns all three to E008 | **`--list-default-excludes` unbound (M4)** |
| `_DOCTOR_UNAVAILABLE` (1: `doctor`) | E008-S01-001 | E008:70; E005-S01-002:33 agrees | One binder |
| `_REMOVED` (1: `restore --only-appointed-in-asg`) | "Never bound" (`:82`) | AD-22's forced exception | Permanent; correct |

E008 binds **11** entries. After E008 and E007, exactly one entry may remain: `restore --only-appointed-in-asg`.

## Findings

Settled by: **built** = the built code settles it; **doc** = a spine/PRD/ADR line or another story's
current text settles it; **orch** = an orchestrator decision under a named standing rule, which the
owner may overrule; **owner** = Shawn must decide.

| # | Sev | Principle | Location | Finding | Remediation | Settled by | Status |
|---|---|---|---|---|---|---|---|
| B1 | BLOCKER | AD-22 (accepted and explained, never ignored); FR-14; global rule 4 | E008:49, :59; `cli/restore.py:760-787` (`RestoreRequest` has no WordPress spec), `:1221-1222` (`wordpress_source(None, …)`), `:1619-1690` (`run_restore` never reads `args.db_credentials_aws_*`); `cli/__init__.py:97-98`; `tests/contracts/test_not_yet_implemented.py:228, :245-263` | The mapping says E008 binds `restore --db-credentials-aws-*`, but nothing carries the flags into the restore driver. The only read is `check_restore_arguments`'s validation in `cli/__init__.py`. The built silent-acceptance guard counts that validation as "read" (`_ROOT_MODULES = ("restore.py", "__init__.py")`). So deleting the two entries passes CI, while the flag is accepted and ignored. That is exactly what AD-22 forbids. The story's grant on `cli/restore.py` is one construction line (:49). | `RestoreRequest` gains `credentials: CredentialSpec \| None = None`. `run_restore` passes `wordpress_spec(aws_secret_id=args.db_credentials_aws_secret_id, aws_region=args.db_credentials_aws_region)`. `drive_restore` passes `request.credentials` in place of `None` at L1221. Widen the `cli/restore.py` grant to exactly these lines plus the preflight construction. The guard's "read" scope becomes the subcommand's own root module, without `cli/__init__.py` (see M5). | built + doc (FR-14; parser mapping); grant: orch (scoping rule) | Fixed |
| M1 | MAJOR | Core §3 DbC; ADR-0016 | E008:61, :64, :128; `ports/adapter_set.py:37-66`; `cli/backup.py:543, :649`; `cli/restore.py:1019-1025`; `cli/verify.py` | The story contradicts itself and the built code. :61 says `real_adapters`/`dry_run_adapters` bind `RootPreflight` "as `AdapterSet.preflight`". :64 says the built `AdapterSet` has no such field. Also :64 says "every root … `cli/verify.py` … gets it through `real_adapters`", but verify has no preflight at all. :128 tests "`real_adapters` and `dry_run_adapters` bind the same `RootPreflight`", which cannot be built. The built code constructs `DumpReadinessPreflight` at three sites, not two: the `backup()` default (:543, used by the Safety Snapshot), `drive_backup` (:649) and `restore._ports` (:1019). | Construct `RootPreflight` through one factory in `cli/preflight.py`, used at all three sites. The library `api/` reaches it through the drivers. Verify binds none. Add an AST guard: `DumpReadinessPreflight(` and `BinaryPreflight(` are constructed only in `cli/preflight.py`, walked over `src/l3io/wp/backup/`, with a planted violation. | built | Fixed |
| M2 | MAJOR | Core §3 DbC | E008:61 vs E005-S01-002:114-116 | `BinaryPreflight(environ=os.environ)` is not E005's constructor. E005 promises `BinaryPreflight(*, cwd: Path, which=…, probe=…)`, with `cwd` = the Run workspace, and no `environ`. | `BinaryPreflight(cwd=run.workspace)`. | doc (E005-S01-002) | Fixed |
| M3 | MAJOR | FR-27; Core §3 | E008:70, :91; E005-S01-002:170, :175 | `doctor` passes `diagnose(preflight=RootPreflight(...))`. `RootPreflight` raises `PreflightFailedError(tool="platform")` on Windows, so `diagnose` could never report `platform_supported: false`. Its dump route also needs a `DatabaseLocation`, which `doctor` without `--wp-dir` does not have. E005-S01-002:170 says `doctor` binds `BinaryPreflight(cwd=<that Run's workspace>)`. The `--wp-dir` path (how the `Readiness` is obtained) is unstated. A `BackupError` from it would exit through the root rather than be reported (contrary to :91). | `diagnose(preflight=BinaryPreflight(cwd=run.workspace), …)`. With `--wp-dir`: locate `wp-config.php`, build the Site Descriptor and the WordPress source through `cli/credentials.wordpress_source`, bind `real_adapters` in the Run, and call `database_query.check_dump(site.database_location())`. A `BackupError` from that probe is recorded as `Readiness(ready=False, blockers=(<exit-code name>,))`, so the result is `NOT_READY`, never a failure exit. | doc (E005-S01-002:170; E008:91 "doctor reports") | Fixed |
| M4 | MAJOR | AD-22; global rule 4 | E008:75-76; `cli/parser.py:105`; E004-S01-001:90 | `backup --list-default-excludes` is in E008's set (by the mapping and by E004-S01-001:90), but the story never binds it. Removing every E008 entry, as :76 requires, would leave it silently accepted. | `run_backup`: with `--list-default-excludes`, print `enumerate_defaults()` one per line, return `OK` before `drive_backup` (no Run), and ignore the other backup flags. `--wp-dir` stays required by argparse (owner decision D1). | doc (E004-S01-001:90) | Fixed (D1 open) |
| M5 | MAJOR | Global rules 3 and 4 | E008:76, :122; `tests/contracts/test_not_yet_implemented.py:113-124, :216-263`; `cli/parser.py:72-82` | The "completion check" cannot fail. The built test parametrises over whatever entries remain, so a forgotten E008 entry is still a passing case. Its silent-acceptance half covers `restore` only, so the backup flags and `doctor` that E008 binds are not checked at all. The binder exists only in a code comment. | (a) Delete the four reason constants `_AWS_UNAVAILABLE`, `_ENCRYPTION_UNAVAILABLE`, `_EXCLUDES_UNAVAILABLE` and `_DOCTOR_UNAVAILABLE` with their entries. A left-over entry then cannot import. (b) Extend the silent-acceptance guard to every subcommand, derived from `_subparsers()`. Each subcommand maps to its root module(s): `backup`→`cli/backup.py`, `restore`→`cli/restore.py`, `verify`→`cli/verify.py`, `doctor`→`cli/doctor.py`. `cli/__init__.py`'s validation is not a "read" (B1). A planted unread option on each subcommand. (c) Assert that the mapping is exactly `{"restore --only-appointed-in-asg"}` once E007 has bound relocation. | built | Fixed |
| M6 | MAJOR | Core §2 reuse; scope | E008:60; `adapters/database.py:120` (`ADMIN_FLAGS`), `:796`; `adapters/noop.py:352`; E005-S01-001:183 | E005-S01-001 hands `AdminCredentialsRequiredError`'s message to E008. The message is built from `adapters/database.py::ADMIN_FLAGS`, which no E008 grant covers. | Grant the one constant. `ADMIN_FLAGS` becomes `("--admin-username", "--admin-password", "--admin-credentials-aws-secret-id", ADMIN_USER_VAR, ADMIN_PASSWORD_VAR)`, with the variables imported from `adapters/credential_providers`. The name is kept (`tests/unit/adapters/test_staging_report.py:20` imports it). | built; grant: orch (scoping rule) | Fixed |
| M7 | MAJOR | Story template; global rule 4 | whole story | No `## Files in scope`. | Added, checked against the tree at `e0902df` and the E004/E005 files-in-scope lists. | built | Fixed |
| M8 | MAJOR | Spine Stack row ("Windows … refused at preflight"); AD-13 | E008:61, :90; `cli/preflight.py:102-103`; `core/backup.py:97-104` | The platform refusal is placed inside `RootPreflight.check`. `require_tools` returns before calling `check` when the plan has no requirements, and an unencrypted files-only backup has none. So "files-only Runs included" (:90) never happens. | `require_tools` drops its early return and always calls `check` (the built `test_no_requirement_asks_nothing` still holds, since `check([])` asks nothing). `RootPreflight.check` raises the platform error first. Test: `sys.platform` patched to `win32` gives a files-only backup exit 7 with nothing written. | built + doc (Stack) | Fixed |
| m1 | MINOR | AD-8; ADR-0008:32 | E008:89 | "`MissingExtraError(extra="aws")` … raised when the chain is built". ADR-0008 and E005-S01-001:201 say it is raised at resolve time, inside the Run. | Corrected. | doc | Fixed |
| m2 | MINOR | Core §2 | E008:60; `cli/parser.py:71, :429` | Retiring `ADMIN_PASSWORD_VARIABLE` also touches the `--admin-password` help text (:429). The `cli/parser.py` → `adapters/credential_providers` import is safe for a base install only because E005 defers database imports (E005-S01-001:136). | Named both. | built | Fixed |
| m3 | MINOR | Core §1 SoC | E008:61 | `RootPreflight` is placed in `cli/context.py` (the Run Context). The built preflight composition is in `cli/preflight.py`. | `cli/preflight.py`. | orch (Core §1) | Fixed |
| m4 | MINOR | Global rule 4; Core §4 | E008:115 | Two registrations are flawed. "`auto`-without-binder" yields the same requirements as `cli` (E004-S01-003:144), and it depends on `find_spec` on the generating host, so the README would differ by machine. A `src/` callable that enumerates `build_restore_plan` over `RestoreMode` needs a synthetic `ArchiveView`/`TargetState`/`SiteDescriptor` in production code. The restore plan's own requirement is only `DUMP_REQUIREMENT` (`core/restore.py:1486`), which the fixed MySQL row documents. | Register `passphrase_backend_requirements(environ={"L3IO_AGE_PASSPHRASE_BACKEND": "cli"}, …)` only. Move the `RestoreMode` enumeration into the tripwire test, which asserts that every mode's own requirements are documented. | orch (global rule 4) | Fixed |
| m5 | MINOR | Built convention | E008:70; `cli/backup.py:674`; `cli/__init__.py:115` | `run_doctor(args) -> int` breaks the built root shape `run_x(args, *, secrets) -> ExitCode`. The dispatch in `cli/__init__.py` refuses any action outside backup/restore/verify. | `run_doctor(args, *, secrets) -> ExitCode`, plus a `doctor` dispatch arm. | built | Fixed |
| m6 | MINOR | Core §4 | E008:125, :128 | `test_preflight_order.py` injects its own recording preflight into `backup()`, so "reuse with `RootPreflight` bound" has no meaning as written. | A case where the recorded preflight is a `RootPreflight` over recording delegates. | built | Fixed |
| m7 | MINOR | Testability of the ACs | E008:25-27 | The AC lines name `verify`, which takes no credentials and runs no preflight. They also say every binary is checked "by `BinaryPreflight`", but `database-dump` goes to `DumpReadinessPreflight`. | A reading note under the AC block. The AC lines are untouched. | built | Fixed |
| m8 | MINOR | Core §6 | E008:130 | "the delegation lives only in `cli/credentials.py`". After E005 the delegation is in `adapters/database.py`. | Corrected. | doc (E005-S01-001) | Fixed |
| m9 | MINOR | Core §4 | E008:75 | Changing `default_exclusion_policy()` changes the default of every `BackupRequest` built without `exclusion`, including existing tests (`tests/unit/cli/test_secret_registry_order.py:102, :128`). The flag-to-set mapping is stated only in E004-S01-001:90. | Stated the mapping and the full-suite re-run. | doc | Fixed |
| m10 | MINOR | Spine Stack row | E008:90; `cli/verify.py` | `verify` runs no preflight, so a Windows `verify` is not refused. The Stack row says "refused at preflight", and verify has none. | Owner decision D2. | **owner** | Open |
| m11 | MINOR | Global rule 3 | E008:106 | `credential_sentinel` takes only `{"passphrase": True}` (`tests/conftest.py:219`). Provider variants must plant their variables in-test, not through a conftest edit (E005 re-check M-5). | Stated. | built | Fixed |

## Per-principle walkthrough

| Principle | Result |
|---|---|
| Core §1 Separation of concerns | m3. Otherwise PASS: the cipher and exclusions stay per-call arguments (ADR-0016), and no database type reaches `cli/` (ADR-0015 amendment) |
| Core §2 Reuse | M6, m2. The built `admin_spec`/`wordpress_spec`/`require_passphrase` are reused: PASS |
| Core §3 Design by contract | M1, M2, M3 |
| Core §4 Testability | M5, m4, m6, m9 |
| Core §5 Brevity | PASS: the binding is a few dozen lines once M1 makes it one factory |
| Core §6 Comments state the current state | m8. Several historical paragraphs remain, each marked as superseded |
| Core §7 Dependencies | PASS: no new dependency |
| Core §8 GA over preview | PASS |
| Core §9 Unified logging | PASS: the records are cited from E005 (`credential_source_selected`, `preflight_checked`). The built `preflight_passed` stays |
| Core §10 Documentation | PASS: the README tool table and the CLI usage block are regenerated (`tools/gen_cli_docs.py`, E005's `tools/gen_tool_docs.py`) |
| Python overlay (uv, mypy strict, ruff, pytest only) | PASS: every test is pytest; no runner or harness |
| Global rule 1: never hand-roll | PASS: providers from database, `packaging` from E005, `json` for rendering |
| Global rule 3: enforce mechanically | M5, m11 |
| Global rule 4: a guard's scope from the source of truth | B1, M4, M5, m4 |
| AD-4 Dry Run | PASS: `RootPreflight` runs for real in a Dry Run (ADR-0019), since it is built in the drivers whatever the adapter set |
| AD-8 / ADR-0008 | m1 |
| AD-9 | PASS: the order comes from `domain/credentials.py` through E005 |
| AD-13 | M8, m7 |
| AD-22 | B1, M4, M5 |
| AD-26 / ADR-0024 | PASS: `require_passphrase` on backup and `resolve_passphrase` on restore, both as built; `PassphraseCipher` only through `build_payload_cipher` |
| ADR-0016 / ADR-0022 amendments (2026-11-06) | PASS: neither touches the binding |
| `docs/closeout-gate.yaml` | PASS: no item names an E008 story |

## Gate

The BLOCKER and every MAJOR are fixed in the story. The built code, a spine/PRD/ADR line or another
story's current text settles each of them. Two grants are orchestrator decisions under the owner's
standing scoping rule (B1's `cli/restore.py` lines, M6's `ADMIN_FLAGS`), and the owner may overrule
them. Two MINORs (D1, D2) wait for the owner. Neither blocks the build: the story states a default for each.

## Decisions needed — Shawn's

1. **D1 — `backup --list-default-excludes` and `--wp-dir`.** argparse requires `--wp-dir` on `backup`
   (`cli/parser.py:236`). That holds even when the operator only wants the list. Options:
   (a) keep it required, so the operator types `backup --wp-dir . --list-default-excludes`, with no parser change;
   (b) make `--wp-dir` optional in argparse and require it in `run_backup` unless listing.
   **Recommend (a):** it changes nothing in the frozen surface, and the list is rarely needed.
   The story builds (a) until told otherwise.
2. **D2 — Windows and `verify`.** The Stack row says Windows is "refused at preflight". `verify` has no
   preflight, so it is not refused. Options: (a) leave `verify` allowed, because it is read-only and writes
   nothing an owner-only mode protects; (b) refuse it with the same `unsupported_platform()` check at the
   top of `drive_verify`. **Recommend (b):** "unsupported in v1.x" should mean one behaviour on every
   command, and `verify --authenticate` decrypts into a Run workspace that AD-12 requires to be owner-only.
   The story builds (a), which needs no `cli/verify.py` edit, until told otherwise.

## Edits other epics' stories would need (not made)

| # | Story | Edit |
|---|---|---|
| X1 | E004-S01-003:189 | "It sits in `NOT_YET_IMPLEMENTED`, naming this story" should say it names E008-S01-001. The code comment (`cli/parser.py:76`) already does. |
| X2 | E006-S01-001:134 | `build_payload_cipher(passphrase=..., required=..., secrets=registry)` is stale. The factory takes no `required` (E004-S01-003:125-126). |
| X3 | E006-S01-001:209 | "An `AwsSecretCredentialSpec` before E008-S01-001 binds Epic 5's provider raises `UsageError`" is stale. After E005-S01-001's edit to `adapters/database.py`, the library resolves AWS without E008. Only the CLI flags wait for E008. |
| X4 | E006-S01-001 (backup/restore API) | The API never passes `extra_tools=cipher.required_tools()`, so a library-encrypted backup under the `cli` backend does not preflight `age` ≥ 1.3. This is the decided route (ADR-0022:15-16; E008 `run_backup`; E004-S01-003 `run_restore`). |
| X5 | E007-S01-001, E007-S01-002 | Each of the seven `RELOCATION_UNAVAILABLE` entries needs exactly one binder. Name the entries each story removes from `cli/parser.py::NOT_YET_IMPLEMENTED` (suggested: the six `--new-*` in E007-S01-001, `restore --wp-config` in E007-S01-002, which consumes `RelocationFlags.wp_config`). Add `cli/parser.py` to their file lists, and correct the comment at `cli/parser.py:72` to the per-entry binder. E008's guard (M5 c) assumes the relocation entries are gone when it builds (phase 4 follows phase 3). |

## Recommended ADRs

None. D1 and D2 are CLI behaviour choices, small enough for a line in the story and the release notes.

DONE — Blocker: 1, Major: 8, Minor: 11
