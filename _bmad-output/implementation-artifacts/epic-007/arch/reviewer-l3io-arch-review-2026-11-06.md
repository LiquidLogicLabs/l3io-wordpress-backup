# Arch gate review — E007 "Move a site to a new URL"

- **Reviewer:** l3io-arch-review, Mode B (gate review)
- **Date:** 2026-11-06
- **Scope:** `epic-007/sprint-01/stories/E007-S01-001.md` … `E007-S01-004.md`
- **Checked against:** `ARCHITECTURE-SPINE.md` (AD-1, AD-3, AD-4, AD-7, AD-11, AD-12, AD-13, AD-17 with its
  2026-10-20 and 2026-10-27 amendments, AD-18, AD-22), PRD §5.3 (FR-11, FR-12, FR-13, FR-29), ADR-0005,
  ADR-0015 (amendment), ADR-0016/ADR-0022/ADR-0024 (2026-11-06 amendments), ADR-0021, ADR-0023,
  `docs/closeout-gate.yaml`, `epics.md` (Epic List and ownership notes), `plan-2026-10-27-v2.yaml`, the built
  code at `HEAD` (`e0902df`), the E005, E006 and E008 stories, `state/issues.yaml` (BL-E002-117,
  BL-E003-056, BL-E003-057), the E002 epic red-team report (RT-4), and the owner's global rules.
- **WP-CLI behaviour** was checked against the WP-CLI handbook's config reference
  (`make.wordpress.org/cli/handbook/references/config/`, fetched 2026-11-06), not from memory.
- Line numbers below are the story lines **before** this gate's edits.

## Executive summary

The four stories were elaborated on 2026-10-27, before E002 and E003 were built. Their "resolved" OPEN
items were marked as resolved, but the bodies were never rewritten. They still design the seam E003 has
since built: a `Relocation` type, `config_rewrite_keys`, `relocate`, `rewrite_undo`, new plan rows, and
new value types. E003 built all of these, under other names.

What E007 actually delivers is narrower. It writes `adapters/wpcli.py::restore_extension(flags, *, secrets)`,
whose `resolve(...)` returns the built `core/restore.py::RestoreRelocation`. It also adds the pure helpers
behind it in `core/`, the WP-CLI adapter behind the built `SearchReplaceCommand` port, and the binding
edits (the static import line and the `NOT_YET_IMPLEMENTED` entries). E007-S01-001 also contradicts AD-17's
2026-10-27 amendment, and the built `RestoreRelocation.__post_init__` would reject it. Under that
amendment, `REWRITE_CONFIG` may not write `DB_HOST`, `DB_USER` or `DB_PASSWORD`.

E007-S01-003 makes two security claims that WP-CLI does not support:
- **A safe working directory.** WP-CLI looks for `wp-cli.yml` in the working directory *and every parent*,
  so a Run workspace under `/tmp` lets any local user plant a `require:`.
- **Plugin code is kept out.** `--skip-plugins` still loads mu-plugins.

The close-out item BL-E002-117 is specified only for wp-cli. Two things are open: whether the passphrase
must also be removed from `os.environ`, and which epic may edit `cli/` to do it.

Neither story 7.3 nor story 7.4 can run its relocating end-to-end case inside E007's loop. The built
restore root binds `DumpReadinessPreflight`, which reports `version=None` for `wp`. So `wp` always fails
its minimum until E008-S01-001 binds E005's `BinaryPreflight` in phase 4.

Totals: **3 BLOCKER, 16 MAJOR, 16 MINOR; 4 owner decisions; 3 cross-epic edits.** 33 of the 35 findings
are settled by the built code, an ADR, the spine or the PRD, and are edited into the stories. A8 and B4
need the owner, and so does part of C4 (O3); O4 is a product choice raised by C1.

**Most important action:** rebuild E007-S01-001's interface around the built `RestoreExtension` seam, with
AD-17's cutover keys (A1, A3). Then make the owner decision on the closed-module grant (O1, O2).

## Findings

Settled by: **built** = the built code settles it; **decision** = an ADR, the spine, the PRD or
`epics.md` settles it; **owner** = Shawn must decide.

### E007-S01-001 — point a restored site at a new host and database

| # | Sev | Principle | Location | Finding | Remediation | Settled by |
|---|---|---|---|---|---|---|
| A1 | BLOCKER | AD-17 (amended 2026-10-27); AD-18.3 | E007-S01-001:36, :93, :104, :137, :145, :166, :189, :206 | `REWRITE_CONFIG` is said to write `DB_HOST`, `DB_USER` and `DB_PASSWORD`, and `config_rewrite_keys` yields them. The spine's 2026-10-27 amendment moves all four `DB_*` keys into `LOAD_DUMP`'s cutover. The built `RestoreRelocation.__post_init__` raises `ValueError` for any `CUTOVER_KEYS` member in `rewrite_values` (`core/restore.py:154`, `:407-414`). The OPEN at :117 says "resolved by decision 1 … E007 adopts", but the body was never changed. | `rewrite_values` carries only `WP_SITEURL`/`WP_HOME`. `DB_HOST`/`DB_USER` go in `cutover_host`/`cutover_user`, `DB_PASSWORD` in `cutover_password=True`, and `DB_NAME` in `load_database`. The built `planned_cutover_keys` (`core/restore.py:815-828`) derives the cutover's key set. Tests assert that no `DB_*` key is ever in `rewrite_values`. | decision (AD-17) |
| A2 | MAJOR | FR-11 ("no option accepted and discarded"); AD-17 | E007-S01-001:133-137; E007-S01-004:37, :105, :150 | The story says "`--new-db-host` and the credentials alone are allowed in a files mode … written by `REWRITE_CONFIG`". After A1, the `DB_*` keys are written only at the cutover. A files mode has no `LOAD_DUMP` (`core/restore.py:1295-1296`), so those flags would be accepted and discarded. | Every `--new-*` flag except `--wp-config` is refused with `RelocationNeedsDatabaseRestoreError` (`USAGE`) in a mode that does not restore the database. 7.4's "config-only relocation" cases use a database mode. | decision (AD-17 + this story's AC line 25) |
| A3 | BLOCKER | Core §2 reuse; Core §3 DbC | E007-S01-001:65-106, :110-119 | The interface designs a seam that E003 has already built: `Relocation`/`none()`/`is_empty` (built: `cli/restore.py:652-692` `RelocationFlags`; `core/restore.py:385-436` `RestoreRelocation`), `load_target_name` (built `load_names`, `:801-812`), `relocate(...)` (built `RestoreRelocation.load_location` and the staging hook, `adapters/staging.py:192-199`), `rewrite_undo` (built `RestoreRelocation.undo`, required by `_require_relocation_text`, `:1374-1391`), the plan rows (built `_kept`/`_line`, `:1278-1371`) and the executor arms (built `adapters/restore_runner.py:820-862`). The deliverable the built code expects is `adapters/wpcli.py::restore_extension`, matching `RestoreExtensionFactory` (`cli/restore.py:695-724`). | Rewrite the interface: `restore_extension(flags, *, secrets) -> RestoreExtension`. `resolve(...)` validates, then fills every `RestoreRelocation` field (`actions` and `undo` included); `required_tools()`, `wordpress_spec()` and `search_replace()` fill the rest. `core/relocation.py` keeps only the pure validation and derivation that `resolve` calls. | built |
| A4 | MAJOR | ADR-0015 amendment (no database type in `cli/`/`api/`); AD-7 | E007-S01-001:88, :104, :174 | "The composition root's `l3io.wp.database.Explicit(db_user, password)`". The built root takes `ext.wordpress_spec()` as a backup-owned `CredentialSpec`, builds the source through `wordpress_source` (which registers it), and resolves the password at the cutover through `password_at_call_time` (`cli/restore.py:1224-1254`; `adapters/database.py:459-470`). `cli/restore.py:1173-1176` and `:1637-1638` already register `--new-db-password`. | `wordpress_spec()` returns `ExplicitCredentialSpec(new_db_user, new_db_password)` (`domain/credentials.py:57-64`), or `None` without the pair. This story constructs no database credential class. | decision (ADR-0015 amendment) + built |
| A5 | MAJOR | AD-1 (import-linter "layers run inward only") | E007-S01-001:65-68 (no type for the flags) | `RestoreExtensionFactory` takes `flags: RelocationFlags`, a `cli/` type (`cli/restore.py:652`, `:718-720`). `adapters/wpcli.py` may not import `cli/`: the `layers` contract in `pyproject.toml` `[tool.importlinter]` includes type-checking imports, because `exclude_type_checking_imports` is not set. | `core/relocation.py` declares a read-only `Protocol`, `RelocationRequest`, with the seven attributes `RelocationFlags` has. `restore_extension` is annotated with it. `RelocationFlags` satisfies it structurally, and a mypy-checked test assigns one to the other. | built |
| A6 | MAJOR | AD-22; FR-11; Core §10 | E007-S01-001:46, :65-68 | "No `--new-*` flag exits `USAGE` as not yet implemented", but the file list omits where those exits live. They are `cli/parser.py::NOT_YET_IMPLEMENTED` (`:92-98`, whose comment at `:73` names "Epic 7 (E007-S01-001, E007-S01-002)" as the binder, and `:88`, "the binding story removes its entries"); the generated `README.md` usage (`tools/gen_cli_docs.py`); the grant line `cli/restore.py:724`; and `tests/unit/cli/test_restore_extension.py:180-183`, which asserts the `= None` line and must change in the same commit. | List all four in `## Files in scope`. 7.1 removes the six `--new-*` entries and 7.2 removes `restore --wp-config`. `RELOCATION_UNAVAILABLE` stays, because `cli/restore.py:1153-1158` still uses it when the factory is `None` (tests monkeypatch it). | built |
| A7 | MAJOR | AD-22 (`--force` overrides FR-7); Core §2 | E007-S01-001:138, :204 | `RelocationTargetNotEmptyError`, "before any destructive phase, `--force` does not waive it". The built `check_target` already refuses a non-empty load database (`core/restore.py:678-717`, `TargetState.load_target`, surveyed at `cli/restore.py:1070-1071`) with `NonEmptyTargetError` → `REFUSED`, unless `--force` waives it. Under `--force`, database's `load` refuses at `LOAD_DUMP` (`DatabaseRefusedError`, `adapters/staging.py:513-515`). `resolve` runs before the survey, so E007 cannot refuse earlier without editing E003's modules. | Drop the new error and reuse the built refusal. State the `--force` behaviour as built: a refusal at `LOAD_DUMP`, journaled, with the Safety Snapshot taken. | built + decision (AD-22) |
| A8 | MAJOR | `epics.md` ownership (Epic 7 owns `adapters/wpcli`, relocation logic in `core/`, one line in `cli/restore.py`); ADR-0023 step 2 | E007-S01-001:106 | BL-E003-057 needs edits to `ports/config.py`, `adapters/config_writer.py` and `adapters/noop.py` (Epic 2), and to `adapters/restore_runner.py:777-817` (Epic 3). E007 is granted none of them (`epics.md:133`, `:160-167`). ADR-0023 step 2 also prescribes "read … `read_member` into the workspace", and step 4 calls `replace_file(config_file, source, …)` with a path. | **Owner decision O1.** | **owner** |
| A9 | MINOR | Core §7; Core §6 | E007-S01-001:220 | "`l3io-wordpress-database` at `47384ba`". The lock has 0.3.2, and `database_location_from_db_host` (`adapters/database.py:435-456`) is the adapter-layer splitter. | Cite 0.3.2 and the built wrapper. | built |
| A10 | MINOR | Collision with E006 | E007-S01-001:58 | "Epic 6's `api/` … passes the same `Relocation` value to `drive_restore`". No E006 story has a relocation parameter (grep of `epic-006/` is empty). | Correct the text, and list the gap as cross-epic X3. | built |
| A11 | MINOR | ADR-0022 amendment (2026-11-06) | E007-S01-001:171-180 | With `--new-db-user`, the Dump loads as the relocated account (`adapters/staging.py:199`, `load_user`), which keeps whatever grants it has. The security section does not say so. | Add one line citing the amendment. | decision |
| A12 | MINOR | Global rule 3 (state a guard's blind spots) | E007-S01-001:106 | The BL-E003-057 AST check covers `adapters/restore_runner.py` only. `cli/restore.py::_archive_config`/`_write_private` (`:948-973`, `:850-860`) still writes the Archive's config into the workspace in a Dry Run. ADR-0016's 2026-10-27 amendment prescribes that write, but the story does not say it is outside the guard. | State it in the guard's docstring. | decision (ADR-0016) |

### E007-S01-002 — config outside the Site Root

| # | Sev | Principle | Location | Finding | Remediation | Settled by |
|---|---|---|---|---|---|---|
| B1 | MAJOR | Core §2 reuse; AD-6 single owner | E007-S01-002:73 | Declares `ConfigLocation(StrEnum)` in `core/config_location.py`. It already exists, with the same members, in `domain/archive_format.py:153-157`, and `adapters/database.py`, `adapters/tar.py` and `cli/backup.py:431` already use it. | Import it, and declare nothing. | built |
| B2 | MAJOR | Core §2; Core §3; AD-1 | E007-S01-002:74, :76-82, :99 | `ConfigFacts` is gathered "in the restore composition root", and `place_config` re-derives rules 1–2. Built: `drive_restore` has already run `locate_wp_config` (wp-load's rule, `adapters/database.py:301-345`) at step 3 (`cli/restore.py:1199-1215`). The extension gets only `resolve(site_root, site, view, files, mode)` (`:704-712`), and its output is `RestoreRelocation.config_file`/`lay_down_config` (`core/restore.py:401-402`), which the runner and cutover read (`adapters/restore_runner.py:248-250`, `:729-769`; `adapters/staging.py:407`). | `resolve` gathers the facts through `files.file_exists` (`ports/site_files.py:46`). When the target has a config, it returns `config_file=None, lay_down_config=False`, so the located file stands. `place_config` is the pure rule for the no-config case only. A test asserts that `place_config` and `locate_wp_config` agree over the fact table. | built |
| B3 | MAJOR | FR-29 (second consequence); AD-22 | E007-S01-002:56, :98 | The non-goal "an operator-stated config path … not delivered here" is stale. The flag is built (`cli/parser.py:350-353`), it reaches `_locate` (`cli/restore.py:1199`), and `parser.py:92` refuses it as unimplemented, naming E007 as binder. A stated path must exist (`adapters/database.py:321-329`). One outside both search locations only warns (`:330-332`), and WordPress, so WP-CLI too, cannot bootstrap from it. | 7.2 removes `restore --wp-config` from `NOT_YET_IMPLEMENTED`. A stated path is used as located. A URL relocation (`SEARCH_REPLACE`) whose config is at neither search location is refused in `resolve` (`USAGE`), before any effect. | built + decision (FR-13) |
| B4 | MAJOR | AD-18.1 (recoverable); ADR-0021 | E007-S01-002:82, :99 | For a `PARENT` lay-down, the journal header records `wp_config_path=str(target_config)` (`cli/restore.py:1367`), which is `<root>/wp-config.php` when the target had none (`:1215`). The runner and the cutover write `relocation.config_file` (the parent). On `--resolve-interrupted-run`, `complete_cutover` writes `header.wp_config_path` (`adapters/staging.py:778-798`): the wrong file. The survey's `config_exists` (`cli/restore.py:1066`) also looks at `target_config`. The fix is in `cli/restore.py`, beyond E007's one-line grant. | **Owner decision O2.** | **owner** |
| B5 | MAJOR | Core §4 (a test that can pass) | E007-S01-002:57, :92-97, :159 | The backup half is built. `adapters/tar.py:556` writes the `config`-role member, `cli/backup.py:431` records `config_path`, and `tests/adapters/test_locate_wp_config.py` exists. Case 1's `xfail(strict=True)` would XPASS and fail the suite. | Drop the `xfail`, and mark the Epic 2 dependency as met. | built |
| B6 | MINOR | Core §2 | E007-S01-002:113-115 | `ConfigFileMissingError` duplicates built refusals: `NoConfigForCutoverError` (database modes, `core/restore.py:245`, `:1451-1454`), `ArchiveHasNoConfigError` (`:234`) and `TargetConfigMissingError` (`adapters/restore_runner.py:166`). | Reuse them. A new error only where no built one applies (`UnsupportedConfigLocationError`, `ConfigLocationConflictError`). | built |
| B7 | MINOR | Buildable in E007's loop; AD-1 | E007-S01-002:119 | The BL-E003-056 alternative, "inside `check_target`'s scope and `snapshot_scope` covers it", needs `core/restore.py` (E003) edits. `_extract` lays down by role with `extract` (`adapters/restore_runner.py:737-746`), which writes over an existing file. | Take the refusal branch, inside `resolve`. `lay_down_config=True` only when no file exists at the placement and the Mode restores files. A file at the placement is `use-existing` (rules 1–2), or `REFUSED` when it is not the located config. | built |

### E007-S01-003 — content URLs (and BL-E002-117)

| # | Sev | Principle | Location | Finding | Remediation | Settled by |
|---|---|---|---|---|---|---|
| C1 | BLOCKER | AD-11; AD-7; global rule 3 (no overclaiming) | E007-S01-003:100, :109, :164-167; E007-S01-004:80-82, :122 | The story claims "running from the workspace means WP-CLI never reads a `wp-cli.yml`" and "`--skip-plugins --skip-themes` keep the site's plugin and theme code from executing". Both are false. WP-CLI reads `wp-cli.local.yml`/`wp-cli.yml` "inside the current working directory (or upwards)" (handbook), and `--skip-plugins` notes "mu-plugins are still loaded". The Run workspace is `mkdtemp` under `$TMPDIR` (`adapters/workspace.py:47`), normally `/tmp`, where any local user can create `/tmp/wp-cli.yml` with a `require:`. That code then runs as the operator, or as root under `--allow-root`. Drop-ins (`wp-content/db.php`) and `wp-config.php` also run. | WP-CLI's working directory is a fresh `0700` `tempfile.mkdtemp(dir=adapters/journal.py::journal_directory(site_root))` (per-user state, AD-19/ADR-0021), removed in a `finally`. Before every `wp` call the adapter refuses (`WpCliConfigFoundError`, `REFUSED`) when any ancestor of that directory holds `wp-cli.yml` or `wp-cli.local.yml`. The child environment points `WP_CLI_CONFIG_PATH` at an empty `0600` file in that directory. The security section states what still runs: `wp-config.php`, mu-plugins and drop-ins. | decision (AD-11, AD-7) + WP-CLI docs; *orchestrator decision for the directory choice* |
| C2 | MAJOR | Core §3 DbC | E007-S01-003:84, :99, :116-120 | `relocate_content(site_root, relocation: Relocation)` and new `StoredUrls`/`Replacement`/`SearchReplaceReport` in `core/relocation.py`. The built port takes `urls: UrlRelocation` (`ports/search_replace.py:86-89`), and the four types already exist in `domain/search_replace.py:27-59`. The port's docstring says "Epic 7 projects onto it". | Implement the built port, and import the domain types. `RestoreRelocation.search_replace` is `UrlRelocation(site_url, home_url)`. `replacement_pairs` takes `UrlRelocation`. | built |
| C3 | MAJOR | Built seam; AD-13 | E007-S01-003:95-98; E007-S01-004:76 | `WpCliSearchReplace(wp, *, workspace, …)` with "`wp` is the path that preflight resolved". `ext.search_replace()` takes no arguments and is called at step 3 (`cli/restore.py:1243`), before `PREFLIGHT`. The factory receives only `flags, secrets` (`:718-720`), so no Run workspace and no preflight path can reach the adapter. `require_tools` discards the statuses (`cli/preflight.py:98-108`). | The factory resolves `shutil.which("wp")` once and constructs the adapter with that absolute path. The working directory is C1's. `relocate_content` re-probes nothing. The residual is stated: preflight looks `wp` up again by name, on the same `PATH`, in the same process. | built |
| C4 | MAJOR | Close-out gate BL-E002-117; global rules 3 and 4 | E007-S01-003:161 | The gate item is specified for wp-cli only. (i) "Removed from the environment the Run passes on" is ambiguous. Removing the passphrase from `os.environ` needs `cli/` edits that E007 does not own (`cli/passphrase.py:74-101`, the roots at `cli/backup.py:612`, `:688`, `cli/verify.py:183`, `cli/restore.py:1645-1651`). It would also mutate a library host's environment (E006-S01-001:231). (ii) The test derives names from `cli/credentials.py`, which reads only `ADMIN_PASSWORD_VARIABLE` (`:69-70`). E005's variables live in `adapters/credential_providers`. (iii) No guard covers "every child process this package starts". | (a) wp-cli gets `env=` built from an allow-list: `PATH`, `HOME`, `LC_ALL=C`, plus C1's `WP_CLI_CONFIG_PATH`. (b) `tests/contracts/test_child_env_explicit.py` walks every module under `src/l3io/wp/backup` and fails on any `subprocess.*`/`os.exec*`/`os.system`/`os.spawn*`/`asyncio.create_subprocess_*` call without an explicit `env=`. It plants a violation, and it states its blind spots (an aliased import, `**kwargs`). (c) A sentinel test sets the passphrase and every variable in the environment to sentinel values and asserts the child's `env` keys are exactly the allow-list. Whether `os.environ` must also be scrubbed is **owner decision O3**. | decision (gate text, RT-4) + **owner** (O3) |
| C5 | MAJOR | Buildable in E007's loop; `epics.md` Epic 8 | E007-S01-003:194-211; E007-S01-004:144-151; E007-S01-001:194-204 | Through `drive_restore`, any URL relocation exits `NOT_READY` until E008. The root binds `DumpReadinessPreflight` (`cli/restore.py:1011-1029`), whose status for any non-dump tool has `version=None` (`cli/preflight.py:69-76`). `preflight_failure` fails every stated minimum on an unknown version (`domain/preflight.py:108-109`). E005's `BinaryPreflight` is bound by E008-S01-001 (`RootPreflight`), in phase 4. | E007's integration tests call `execute_restore` with `RestorePorts` composed in the test from E005's `BinaryPreflight` (landed in phase 2) through `functools.partial(require_tools, …)`. Recording and unit tests use a `Preflight` double. The CLI end-to-end relocating cases move to E008-S01-001 (cross-epic X1). | decision (`epics.md` Epic 8) + built |
| C6 | MINOR | Core §6; parallel-file ownership | E007-S01-003:215-219 | "`ci.yml` … is changed to fetch a pinned release phar and verify its checksum". That is already done (`.github/workflows/ci.yml:219-233`, `WP_CLI_VERSION: "2.12.0"`, sha512), and `tests/contracts/test_workflow_invariants.py:239` checks the pin's shape. | E007 does not edit `ci.yml`. `tests/contracts/test_wp_cli_pin.py` reads `WP_CLI_VERSION` from that step and asserts it equals `WP_CLI_MINIMUM`. | built |
| C7 | MINOR | Core §6 | E007-S01-003:116-124 | The "Resolved" and "partly resolved" OPEN items are history. The Dry Run behaviour is built (`adapters/noop.py:567-594`: pairs with `UNRESOLVED_URL`, `None` counts), and `RESERVED_SOURCE_KEYS` is reserved (`domain/archive_format.py:119`). | Cite the built behaviour. The Manifest-recorded stored URLs remain a follow-up, outside E007. | built |
| C8 | MINOR | AD-17 | E007-S01-003:141 | "Never in a Mode without `LOAD_DUMP`": the built `_kept` already enforces it (`core/restore.py:1297-1298`). | Cite it. 7.1's refusal (A2) makes the flags `USAGE` rather than an omitted phase. | built |

### E007-S01-004 — WP-CLI missing

| # | Sev | Principle | Location | Finding | Remediation | Settled by |
|---|---|---|---|---|---|---|
| D1 | MAJOR | Core §3 (one contract) | E007-S01-004:106 | "Two tools missing … every failed requirement is reported in one message". The built `require_tools` raises the first failure (`cli/preflight.py:98-108`). | Reword: the first unmet requirement, in plan order, is named. | built |
| D2 | MINOR | Core §2; DbC | E007-S01-004:68, :85, :96-99 | The install URL is inside `needed_for`, and the messages are written by hand. `ToolRequirement.install_hint` exists (`domain/preflight.py:43`), and `PreflightFailedError` renders `"{tool} is needed for {needed_for}; found: {found}\ninstall: {hint}"` (`domain/errors.py:419-437`). The unknown-version text is `UNKNOWN_VERSION` = "unusable (version unknown)" (`domain/preflight.py`). | Put the URL in `install_hint`, and assert the messages against the built rendering. | built |
| D3 | MINOR | Core §2 (one route) | E007-S01-004:68-74 | `ToolRequirement` "must be placed where `core/` may name it": it is in `domain/preflight.py:37`. Requirements can reach the plan two ways, `ext.required_tools()` (`cli/restore.py:1340`) and `RestoreRelocation.required_tools` (`core/restore.py:1493`). | Use `required_tools()` only, the route E005's tripwire walks (E005-S01-002 G5), and keep `RestoreRelocation.required_tools=()`. | built |
| D4 | MINOR | AD-13 | E007-S01-004:76 | "One `wp` path … the binary that was checked is the binary that runs" is not reachable as written (C3). | Restate it per C3, with the residual named. | built |
| D5 | MINOR | FR-11 | E007-S01-004:37, :105, :150 | The config-only relocation cases assume a files mode is allowed (A2). | Run them in `database` mode. | decision |

### Cross-story

| # | Sev | Principle | Location | Finding | Remediation | Settled by |
|---|---|---|---|---|---|---|
| E1 | MINOR | `epics.md` ownership; E002-S01-001 provision | all four `**Files.**` lists | `domain/errors.py` belongs to Epic 2. The E003 precedent ("subclasses only") is cited, but no story has a `## Files in scope` section checked against real paths. | Add `## Files in scope` to each story. | built |
| E2 | MINOR | Core §6 | E007-S01-001:110-119; E007-S01-002:92-99; E007-S01-004:80-86 | The OPEN items are resolved history, and the bodies were never folded in. | Fold them in through the corrections block. Keep the OPEN text as the record. | built |
| E3 | MINOR | E005 collision | E007-S01-004:80-82; E005-S01-002:116, :210 | E005's `wp cli version` probe runs with the Run workspace (`/tmp`) as its working directory. Whether `wp cli version` loads a discovered `wp-cli.yml`'s `require:` was not verified. If it does, C1 applies to the probe too. | Cross-epic X2. | built |

## Per-principle walkthrough

| Principle | Result |
|---|---|
| Core §1 Separation of concerns | PASS once A3/A5 apply: pure derivation in `core/relocation.py` and `core/config_location.py`; effects in `adapters/wpcli.py` |
| Core §2 Reuse | A3, A7, B1, B2, B6, C2, D2, D3 |
| Core §3 Design by contract | A1, A3, C2, C3, D1 |
| Core §4 Testability | B5, C5. The guards' scope is derived (`dataclasses.fields`, `RestoreMode`, a package walk, the parser's actions) |
| Core §5 Brevity | PASS. The extension is a few hundred lines once A3 drops what E003 built |
| Core §6 Current state | A9, C6, C7, E2 |
| Core §7 Dependencies | PASS. No new dependency; WP-CLI is ADR-0005's |
| Core §9 Logging | PASS. Records carry keys and labels, never values |
| Core §10 Documentation | A6 (README regenerated) |
| Global rule 1: never hand-roll | PASS. Serialized PHP goes through `wp search-replace` only (ADR-0005); no PHP unserializer, no SQL `REPLACE`, no `phpserialize`; `json`/`csv` from the stdlib parse WP-CLI's output; PHP itself is the test oracle |
| Global rule 3: enforce mechanically | C1 (an overclaim), C4 (no guard for "every child"), A12 (blind spot not stated) |
| Global rule 4: guard scope from source | C4 (a package walk, not a list), A6 (the `--new-*` test derives from the parser). Otherwise PASS |
| Pytest only | PASS |
| `docs/closeout-gate.yaml` | BL-E002-117 is owned by E007-S01-003. C4 specifies it; O3 is open |
| ADR-0016/0022/0024 amendments | A11 (ADR-0022), O4 (ADR-0016: unauthenticated PHP run as root). ADR-0024: the passphrase never reaches wp-cli (C4) |
| ADR-0023 | A8/O1 (step 2/4 wording); B3 (a `PARENT` config is replaced at its located path, as the ADR says) |
| E006 collision | A10 (X3). No shared file: E006 owns `api/*` and `adapters/tracing` |
| E008 collision | C5 (X1). E008 removes other `NOT_YET_IMPLEMENTED` entries in phase 4, after E007, in the same file; this is a serial edit, not a parallel one |

## Gate

The BLOCKERs A1, A3 and C1, and every MAJOR, must be resolved or recorded in an ADR before E007 builds.
Every finding except A8 (O1), B4 (O2) and half of C4 (O3) is settled, and is edited into the stories,
marked *(E007 arch gate, 2026-11-06)*. O4 is a product choice the stories leave at the current text
(a WARNING) until the owner rules. MINORs are fixed in the stories rather than deferred, because each was
a one-line correction.

## Decisions needed — Shawn's

**O1 (A8) — BL-E003-057's edits to closed Epic 2 and Epic 3 modules.**
- **The question.** May E007-S01-001 edit `ports/config.py`, `adapters/config_writer.py`,
  `adapters/noop.py` and `adapters/restore_runner.py`, and may ADR-0023 steps 2 and 4 be amended,
  so the Archive's tree-member config goes through `ConfigCommand` with no intermediate workspace file?
- **The options.**
  - **(a)** Grant those four files to E007-S01-001 in an `epics.md` ownership note, and amend ADR-0023:
    `replace_file(config_file, body: bytes, keep=…)`, with its no-op twin.
  - **(b)** Move BL-E003-057 to a new follow-up story in a later phase.
  - **(c)** Accept it as documented. The Dry Run writes a `0600` credential-bearing file into its own
    `0700` workspace, and that file is removed with the workspace.
- **Recommendation: (a).** Epics 2 and 3 are closed. E006, the only phase-3 sibling, owns `api/*` and
  `adapters/tracing` and touches none of these files, so no parallel edit can collide. E007 is the epic
  already changing `REWRITE_CONFIG`'s inputs. Until the owner rules, the story keeps the AC with its
  grant marked "pending O1".

**O2 (B4) — the journal header's config path for a `PARENT` lay-down.**
- **The question.** May E007-S01-002 edit `cli/restore.py`? Two expressions would change:
  `JournalHeader(wp_config_path=…)` at `:1367` and the survey's `target_config` at `:1066`. Both would
  read `relocation.config_file or target_config`. E007's grant is the single import line.
- **The options.**
  - **(a)** Widen the grant by exactly those two expressions.
  - **(b)** Move the fix to a follow-up story.
  - **(c)** Refuse a `PARENT` lay-down until it is fixed. That defers FR-29's first consequence for
    fresh targets.
- **Recommendation: (a).** The defect is E003's, but only E007 can trigger it. Two expressions, with a
  kill-and-resolve test.

**O3 (C4) — the scope of BL-E002-117.**
- **The question.** Does the close-out condition also require removing `L3IO_WP_BACKUP_PASSPHRASE`
  from `os.environ` after resolution, or is an explicit minimal `env=` on every child this package
  starts enough, enforced by the new AST guard?
- **The options.**
  - **(a)** Minimal `env=` plus the guard. This closes RT-4's attack: site PHP, through wp-cli, reading
    the variable. Two kinds of child still inherit `os.environ`: `mysqldump`, and the `age` CLI
    fallback. Both are started by `l3io-wordpress-database` and run no site code.
  - **(b)** Also pop the variable in the CLI roots, after `resolve_passphrase`. That is a `cli/` edit
    to `run_backup`, `run_restore` and `run_verify`. E007 owns none of those files. E008-S01-001 edits
    those roots in phase 4. This option needs either a grant to E007 or a split of the gate item, with
    the pop half owned by E008-S01-001.
  - **(c)** Pop it inside `resolve_passphrase`. Rejected: it mutates a library host's environment, and
    E006-S01-001:231 reads `os.environ` through it.
- **Recommendation: (a).** Record the residual on BL-E002-117 when it is resolved. Choose (b) only if
  the owner wants `mysqldump` and `age` covered as well. The story is written for (a) and names (b)
  as pending.

**O4 (C1, ADR-0016 amendment) — WP-CLI as root on an unauthenticated Archive.**
- **The question.** With `--allow-root`, the restored Site's `wp-config.php` (when the target had none),
  its mu-plugins and its drop-ins run as root. Under the 2026-11-06 amendment, an unencrypted Archive
  can be forged by anyone who can write it.
- **The options.**
  - **(a)** WARNING only (the current text).
  - **(b)** Refuse `SEARCH_REPLACE` as root unless the Archive is authenticated (encrypted), telling
    the operator to run as the Site's owner.
  - **(c)** Always refuse as root.
- **Recommendation: (b).** It matches the amendment's trust model, in which encryption is how an
  operator gets authenticity, and it costs one refusal in `resolve`. The story keeps (a) until the
  owner rules.

## Cross-epic edits (listed, not made)

- **X1 — E008-S01-001.** Take the CLI end-to-end relocation cases (`wpbackup2 restore --new-site-url …`
  with a real, absent, old or broken `wp`, and the Dry Run). They need `RootPreflight` to bind
  `BinaryPreflight` (C5). E008's own `NOT_YET_IMPLEMENTED` edit must leave the relocation entries, which
  E007 removes first.
- **X2 — E005-S01-002.** (i) The `wp cli version` probe's working directory: use C1's rule, or show that
  `wp cli version` loads no discovered `require:` (E3). (ii) Expose the minimal child environment as a
  named constant, for example `adapters/preflight.py::CHILD_ENV_KEYS`, so E007's adapter and the AST
  guard use one allow-list. Until then E007 declares `WP_CLI_ENV_KEYS` with the same three keys, and a
  test asserts they are equal. (iii) There is a residual for E004 and E005, both in phase 2.
  `test_child_env_explicit.py` (C4) walks all of `src/`. Any subprocess call either epic lands without
  `env=` would turn E007's guard red on a file E007 does not own. E005-S01-002 already specifies `env=`
  for its probes. E004-S01-003 should confirm that it starts no child itself (the `age` CLI is started by
  `l3io-wordpress-database`).
- **X3 — E006-S01-001.** `api.restore` has no relocation parameter. Either add one that builds the same
  flags value `run_restore` builds, or record that relocation is CLI-only in v1.0. E007-S01-001:58's
  claim is withdrawn.

DONE — BLOCKER 3, MAJOR 16, fixed 33, owner decisions 4, cross-epic edits 3
