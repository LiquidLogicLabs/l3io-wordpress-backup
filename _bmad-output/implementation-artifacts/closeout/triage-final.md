# Final LOW backlog triage (close-out, last batch)

Scope: every open `severity: Low` backlog entry except titles containing "post-v1" or "owner-deferred", BL-E006-024, BL-E006-025, BL-E008-001, BL-E006-022 and BL-E004-055: **72 items**. Session `b74a242a-6a71-45d8-8ece-7b06b152e53c`, on top of `c9a75ae`. Nothing committed.

Outcomes: **fixed 21** (listed in `fixed-5b.txt`, left open for the orchestrator to commit and resolve), **already fixed 2**, **duplicate/obsolete 5** (both resolved here), **owner decisions 44** (left open; section 3).

Also fixed (product point, no backlog key): `safety_snapshot_encrypted` is now logged at WARNING (`cli/restore.py`), so `--log-format json --log-level WARNING` keeps the "needs this Run's passphrase" notice; the `emit_warning` comment and docstring (`cli/output.py`) now say the caller's record must be WARNING or above because under JSON it is the notice's only trace. Tests: `tests/unit/cli/test_restore_root.py::test_the_encrypted_snapshot_notice_survives_json_at_warning_level` (default and WARNING levels) and a mechanical guard `tests/unit/api/test_on_warning.py::test_every_notice_is_logged_at_warning_or_above` (every `RunWarning(code=...)` module logs that code at warning/error/critical; planted INFO case).

## 1. Fixed (orchestrator commits and resolves)

| Key | Change | Integration-tested code? |
|---|---|---|
| BL-E002-034 | manifest_from_json on_unknown is a required keyword (domain ignore_unknown names the opt-out); classify carries ArchiveView.unknown_values; adapters/tar.verify_archive_manifest logs WARNING manifest_value_unknown per value; tests in test_archive_format.py and test_tar_probe.py | yes: every verify/restore VERIFY (adapters/tar.py) |
| BL-E002-041 | adapters/workspace.run_workspace: a workspace that survives removal after a block that exited normally raises the new WorkspaceNotRemovedError (exit 1, path field, message names only the prefix; exported from api/errors, docs/api.md, docs/logging.md); a failing Run keeps its own error and the WARNING; tests in tests/adapters/test_workspace.py | yes: every Run (adapters/workspace.py) |
| BL-E002-048 | adapters/tar.verify_or_raise (the publish gate) also requires Integrity.VERIFIED; a v0-shaped STRUCTURAL_ONLY pass is refused as UNRECOGNISED_FORMAT; test in tests/adapters/test_tar_probe.py (gzip and plain v0) | yes: every backup publish (adapters/tar.py) |
| BL-E002-049 | adapters/tar.py: decrypt_payload tags its read side and write side, so an Archive read OSError is ArchiveReadError, a destination write (or its close flush) ArchiveWriteError, and the cipher's own errors propagate unchanged (no raw OSError escapes); extract_archive maps a payload= that cannot be opened to ArchiveReadError(payload) instead of ArchiveWriteError(destination); residual: an EIO mid-extraction still names the destination; tests in test_tar_probe.py | yes: encrypted restore/verify (adapters/tar.py) |
| BL-E002-050 | adapters/tar.TarArchiveProbe.read_member reads the stream to its end and refuses (MemberReadRefusedError "repeated in the archive") a name that occurs twice, so it can never return a copy extraction would not use; tests in test_tar_probe.py | yes: restore config read (adapters/tar.py) |
| BL-E002-055 | VerificationFailedError gains an `archive` field and names it in its message ("archive verification failed for '<path>': ..."); cli/verify, api.verify, the publish gate and the restore's VERIFY pass it; docs/api.md; tests in tests/integration/test_verify_cli.py (no database needed) and test_restore_runner.py | yes: verify CLI, restore VERIFY |
| BL-E003-068 | RestoreInputs gains archive_name (the operator's --archive) and archive_label; restore_runner's ArchiveHasNoConfigError, ArchiveChangedError, VerificationFailedError and the v0 ArchiveNotFoundError name it instead of the workspace copy deleted with the Run; cli/restore passes str(request.archive.path); test in test_restore_runner.py | yes: restore runner |
| BL-E002-056 | A Run or phase ended by an interrupt that is not an Exception (KeyboardInterrupt) logs run_finished/phase_completed with exit_code (and exit_reason) null and sets no span exit code, instead of claiming 1/FAILED while the process exits 130 (cli/context.open_run, adapters/phase.phase); docs/logging.md; tests in test_run_context.py and test_phase_join.py | yes: every Run record (cli/context.py, adapters/phase.py) |
| BL-E002-058 | cli/parser.CliParser overrides error(): argparse's own usage errors end with "[exit 2 USAGE: the command line was invalid]" like every other failure; tests/integration/test_exit_codes_end_to_end.py no longer exempts USAGE; tests in test_render_failure.py | CLI parsing only (exit-code end-to-end test) |
| BL-E003-101 | CliParser.commands/for_command: a check after parsing (check_restore_arguments, backup's --wp-dir, not-yet-implemented flags) reports with the subcommand's usage, not the top-level one (the missing --archive case was already the restore usage since --archive became required, 48324e4); test in test_render_failure.py | no (usage errors) |
| BL-E002-081 | cli/__init__._terminate sets every terminating signal to SIG_IGN before raising RunTerminated, so a second SIGTERM/SIGHUP/SIGQUIT cannot interrupt the cleanup (workspace removal) the first started; _sigterm_raises restores the previous handlers; test in test_closeout_residue.py | no (signal handler) |
| BL-E002-088 | ports/site_files.require_top_level: clean refuses (ValueError, before anything is removed) a keep path that is not one top-level entry, in LocalSiteFiles and NoopSiteFiles alike, instead of keeping its whole ancestor; tests in tests/adapters/test_site_files.py and tests/unit/adapters/test_noop_restore.py | yes: restore CLEAN (adapters/site_files.py) |
| BL-E002-090 | tests/contracts/test_credential_sources_built_once.py: the l3io.wp.config WpConfigFile exemption is withdrawn for a name the module also binds any other way (assignment, another import, def/class, loop/with target), so WpConfigFile = wpdb.WpConfigFile is an offence; planted rebinding and re-import cases | no (test guard only) |
| BL-E003-013 | RestoreRequest gains mode_flags (the flags as typed, via render_flags over the AD-25 combination); run_restore fills it and drive_restore logs it as `flags` on restore_mode_resolved (None for a library call, which passes the Mode itself); test in test_restore_root.py | yes: restore root |
| BL-E005-006 | adapters/database._RegisteringSource.resolve: a CredentialsUnobtainableError whose cause is not a database error (an AWS secret that is not JSON: JSONDecodeError.doc is the SecretString) is re-raised with the same type and text but no __cause__/__context__ and no inner frames; test in tests/unit/adapters/test_credential_providers.py | yes: credential sources (adapters/database.py) |
| BL-E005-007 | adapters/database.require_wp_config_credentials takes the chain's environ (keyword, required) and names a half pair's unset variable from it; drive_backup passes its environ; the adapter no longer imports os; AST test that adapters/database.py never reads os.environ, with a plant | yes: backup credential check |
| BL-E005-008 | adapters/database._RegisteringSource logs credential_source_selected only when the resolved source differs from the one it last logged, so a Run that resolves one source several times (check, dump, ensure) logs its choice once; test in test_credential_providers.py | yes: credential sources |
| BL-E008-008 | adapters/lock.py imports fcntl inside _lock_exclusive/_unlock instead of at module import, so a platform without fcntl reaches the preflight platform refusal; test loads lock.py with fcntl unimportable (tests/adapters/test_site_lock.py) | yes: Site Lock (adapters/lock.py) |
| BL-E004-056 | The sweep of stale l3io-wp-backup-* workspaces landed with owner decision 4 (63a4c71); docs/api.md TMPDIR row now documents the SIGKILL residue, the 24-hour sweep (stale_residue_removed) and TMPDIR on tmpfs (doc only, no test) | no (docs) |
| BL-E008-018 | adapters/preflight.run_bounded waits for the leader with os.waitid(WEXITED|WNOWAIT|WNOHANG) polling (_wait_unreaped), kills the group while the leader is still a zombie (its pid cannot be reused), and only then reaps it with process.wait(); test in tests/unit/adapters/test_preflight.py | yes: every child probe (adapters/preflight.run_bounded) |
| BL-E003-111 | tests/contracts/test_no_database_import_in_cli.py: an AST contract over every cli/ module (derived from the tree) that refuses any run-time import of l3io.wp.database (module, function-level, from l3io.wp import database); TYPE_CHECKING-only annotation imports allowed and stated; planted spellings; staging.py's own import was already gone (BL-E003-098, 48324e4). import-linter cannot forbid a subpackage of the external l3io namespace, so no contract was added there | no (test guard only) |

## 2. Resolved here

| Key | Resolution | Ref | Evidence |
|---|---|---|---|
| BL-E003-115 | fixed (already) | 63a4c71 | SIGHUP and SIGQUIT are handled like SIGTERM (`cli/__init__.py` `TERMINATING_SIGNALS`, owner decision 4); `tests/unit/cli/test_closeout_residue.py` parametrizes all three |
| BL-E003-104 | fixed (already) | 63a4c71 | `emit_warning` prints nothing under `--log-format json` (owner decision 10); the `archive_unauthenticated` WARNING record carries STRUCTURAL_ONLY; `test_restore_root.py` parses every stderr line of a JSON restore as JSON |
| BL-E004-061 | obsolete | 63a4c71 | ADR-0016 amendment 2026-11-07: a passphrase with a plain Archive needs `--allow-unencrypted`, and then encrypts the Safety Snapshot, so the cipher backend is genuinely needed |
| BL-E003-018 | obsolete | 63a4c71 | No caller joins a restore into an outer Run: `run_restore` and `api.restore` both pass `ctx=None` (context= removed, owner decision 24); returns with owner-deferred BL-E006-025 |
| BL-E008-025 | obsolete | 48324e4 | The password file is read in `run_restore` before `drive_restore` takes the Site Lock; owner/mode check settled as advice by BL-E008-016; a regular-file check would refuse `--new-db-password-file <(...)` |
| BL-E003-114 | duplicate | BL-E002-092 | Fixed in 0cc6c12: every `except Exception` in staging/restore_runner re-raises `RunTerminated`; `tests/contracts/test_run_terminated_propagates.py` |
| BL-E008-026 | duplicate | BL-E004-055 | Same defect; owner decision 6 bundles it into the database release BL-E004-055 waits on |

## 3. Owner decisions (left open)

Each: question, options, recommendation. "Wontfix" recommendations are items with no correctness or UX value at v1 (outcome 5); none was closed here.

### 3a. Bundles: most sub-findings are still open

An independent audit of the six bundles (59 sub-findings) found 24 closed, 33 open, 2 docs-only. **Question:** close each bundle by splitting its open sub-findings into single items, or close it wontfix? **Recommendation:** split; the open ones worth doing are listed, the rest wontfix.

- **BL-E002-066** (L1-L5): L1 closed (BL-E002-040). Open: L2 `SecretRegistry._walk` leaves bytes/objects unredacted (= AR-07); L3 Ctrl-C prints a traceback (main does not catch `KeyboardInterrupt`; now `run_finished` claims no code, BL-E002-056); L4 hard-linked files stored whole; L5 a symlinked `wp-config.php` is refused late. Recommend: fix L2 and L3 (small); wontfix L4/L5 (documented skip/refusal behaviour).
- **BL-E002-071** (AR-07..16): 6 closed. Open: AR-07 (= L2), AR-08 registry not live before the Run, AR-10 no parent-directory fsync after publish, AR-13 `report_failure` swallows its own failure silently, AR-16 `gen_cli_docs` replaces the first marker pair only. Recommend: fix AR-10 and AR-16; wontfix AR-08/AR-13.
- **BL-E002-075** (UX-01-11..17): all six open (sub-command help has no exit-code epilog/`--log-level` metavar; a <512-byte non-tar reads `TRUNCATED`; verify failure has no gloss; console prints a failure twice; exit suffix wording; `passphrase=` hint shown to CLI users). Recommend: fix UX-01-17 and UX-01-11; wontfix the rest as wording preference.
- **BL-E002-096** (AD-5..12, CR-1/2): AD-8, CR-1, CR-2 closed. Open: AD-5 help says "change nothing" though the Site Lock is taken; AD-6 no `XDG_STATE_HOME` hint; AD-7 snapshot preflight label; AD-9 noop imports database's `ADMIN_FLAGS`; AD-10 `check_mode_defaults` hand list (= SD-02-8); AD-11 secret resolved twice; AD-12 the CI grep step has no `test -d`. Recommend: fix AD-5, AD-10, AD-12 (rule 4: derive the scope); wontfix the rest.
- **BL-E002-105** (SD-02-3..12): SD-02-11 closed in code. Open: SD-02-3 (docs), -4 atomic-write copies, -5 substring match, -6 (= AD-9), -7 `administrator_bound` not on the port, -8 (= AD-10), -9 layer placement, -10 CHANGELOG entry for the `--force`/`--cleanfirst` change, -12 events missing from docs/logging.md. Recommend: fix -8, -10, -12; wontfix the refactors (-4, -6, -9) for v1.
- **BL-E003-040** (E003-S01 closure): 3 closed, SD-01-3 = BL-E003-109. Open include ADV-13 `finish_run` can mask the original error, RT-004 no inode check, RT-005 CLEAN `keep` is case-sensitive, UX-8 no "re-run" hint, plus docs/cosmetic items. Recommend: fix ADV-13 and RT-005; wontfix the rest.

### 3b. Architecture refactors (no behaviour change)

**Question:** do these structural refactors at close-out, or defer post-v1? **Recommendation:** defer post-v1 (owner-deferred), or wontfix; each is churn in the two largest, most integration-tested modules with no user-visible effect, right before release.

- BL-E003-109 split `core/restore.py` (1543 lines) and `cli/restore.py` (1880).
- BL-E007-019 split `adapters/wpcli.py` (~1000 lines).
- BL-E007-018 move `_config_file` placement logic from the adapter into `core/config_location.py`.
- BL-E007-020 `RunFacts` shared mutable state between resolve and the replacer.
- BL-E003-099 `RestoreRequest` carries the adapters-layer `AdminTls`; global `RESTORE_EXTENSION`.
- BL-E004-016 one `ExclusionSource` type (str / Literal / StrEnum today).
- BL-E004-036 `ArchiveView.config_path` means a member path for v0 and a location for v1.
- BL-E002-024 `Manifest.payload` required, so a placeholder payload is invented before it exists.
- BL-E007-005 export one `wp-config.php` name constant.
- BL-E003-100 `adapters/staging.py` reads `time.monotonic_ns` for a duration instead of the Clock port (the port has no monotonic reading).
- BL-E006-015 `UsageError.arguments` mixes CLI flag spellings and parameter names on library paths (needs a vocabulary decision: recommend parameter names on library paths).

### 3c. Dead code and tidy-ups (recommend wontfix)

- BL-E003-094 / BL-E007-011: the `factory is None` branch, `_NoExtension` and `RELOCATION_UNAVAILABLE` are unreachable in the product but are the fail-closed seam `tests/unit/cli/test_restore_extension.py` exercises; `new_db_password` registered twice is harmless (idempotent); `require_database_restored(outcome, outcome)` works. No correctness or UX value.
- BL-E004-065: two `pragma: no cover` `manifest_text is None` branches; they are defensive, not wrong.

### 3d. Tests to add (recommend: do post-v1 with the CI rig, or wontfix)

**Question:** each needs the database/WP-CLI rig or a new restore-level harness case; worth the close-out time? **Recommendation:** BL-E007-002 stays deferred on real WP-CLI output captured in CI (as its title says); the others wontfix for v1, since the behaviour is covered at unit level.

- BL-E003-021 restore-level test of skipped symlinks (unit and tar-level coverage exist: `test_tar.py`, `test_restore_symlink_notice.py`).
- BL-E003-032 credential-hygiene test with a secret on the database/cutover side.
- BL-E004-049 right-passphrase `drive_restore` with a Dump-loading mode.
- BL-E006-007 concurrent Runs never share trace/span ids.
- BL-E007-002 replay real WP-CLI 2.12.0 output; execute_restore-level case.
- BL-E007-003 rig test of a SITE_ROOT v1 Archive laid down by role.
- BL-E004-060 AST contract pairing `extract_archive`/`decrypt_payload` with `read_inner_manifest` at every call site (today only the restore runner and verify call them).
- BL-E003-064 contract test that only the restore root passes `admin_tls`.
- BL-E004-064 replace `pass@word1` in `tests/data/wp-config.php` with a unique sentinel: it is baked into the committed reproducible v0 fixtures, so the fixtures must be regenerated and their reproducibility test re-pinned.

### 3e. Behaviour questions

- **BL-E002-120** `verify --authenticate` writes the decrypted payload to the 0700 workspace only to discard it. Options: (a) stream decrypt into the inner-tar reader through a pipe/thread; (b) accept. Recommend (b) wontfix: the workspace is private, removed in `finally`, swept after 24 h, and documented (docs/api.md TMPDIR, tmpfs advice).
- **BL-E004-059** restore decrypts once to read `wp-config.php` and again in VERIFY (ADR-0024 says so). Options: (a) reuse the first decryption for VERIFY; (b) accept. Recommend (b): age authenticates every chunk, so the config read is already authenticated; the cost is one extra decryption.
- **BL-E003-041** load-database creation (`EnsureOutcome.created_database`) is not journaled, so undo after a crash cannot tell created from pre-existing for an operator-named load DB. Options: (a) add a journal step `load_database_created`; (b) accept, undo text says "if it exists". Recommend (a) post-v1 (a journal format addition).
- **BL-E003-088** members of a skipped role are decompressed without a size bound during extraction. Options: (a) apply the indexed-size cap to skipped members too; (b) accept (the expansion-ratio refusal still bounds the whole Archive). Recommend (a), small, but it touches the AD-11 extraction function; schedule with the next tar change.
- **BL-E004-045** `path_excluded` count differs from the Manifest's excluded entries when the target is inside the Site Root (`_SkipStaged` is unlogged). Owner decision 3 now refuses that layout without `--allow-target-in-site-root`. Recommend: log it as `path_excluded` (small) or wontfix as the opted-in case.
- **BL-E004-024** FR-26 inventory: licence and exit-plan entries for pyrage / l3io-age-passphrase. Recommend: add the two rows to the dependency inventory doc (docs only).
- **BL-E004-025** E004-S01-003 still says "database.sql intact"; per-member error/event names (`member_decryption_failed`) predate the one-payload layout. Recommend: dated note in the story; keep the event names (renaming breaks log consumers).
- **BL-E006-018** wrong-typed `scope`/`mode` to the library surfaces as `UnexpectedError`. Options: (a) validate enum/types at the API edge and raise `UsageError`; (b) document that type errors are defects. Recommend (a).
- **BL-E007-013** the `wp` child receives the unfiltered `PATH` although `wp` was located over absolute entries only. Options: (a) pass the filtered PATH; (b) accept (PATH lookup window already accepted for v1, BL-E005-010). Recommend (a), one line, but WP-CLI runs `php` from PATH, so it needs the integration rig to confirm.
- **BL-E008-015** `Secret` is unhashable and refuses pickling/copy by design (no secret leaves via pickle), which makes `ExplicitCredentialSpec`/`RelocationFlags` unhashable. Recommend wontfix (deliberate).
- **BL-E008-020** `doctor` blockers carry only an exit-code name. Recommend: add the error message (small UX gain).
- **BL-E008-021** restore flag combinations such as `--allow-unencrypted` with an encrypted Archive are accepted silently. Recommend: log an INFO "flag had no effect" (no refusal).
- **BL-E008-022** an `on_warning` sink that raises aborts a restore after the Safety Snapshot. Options: (a) catch, log ERROR `on_warning_failed`, continue; (b) keep "propagates" (documented in `cli/output.py`, docs/api.md). Recommend (a) for notices after the snapshot.
- **BL-E008-023** a config laid down from the tree keeps the Archive's mode before it receives the relocated DB password. Recommend: chmod 0600 at lay-down (small, restore path, integration rig).
- **BL-E008-027** an existing per-user state directory is used without an owner/mode check. Options: (a) refuse one not owned by the user or group/world-writable; (b) accept (it is under the user's home). Recommend (a).

## 4. Gates (final tree, all green)

- ruff check, ruff format --check, `mypy --strict src`, lint-imports (4 kept), `check_purity`, `check_mode_defaults`, `gen_cli_docs --check`, `gen_tool_docs --check`, `uv lock --check`, spec-align `check-links` (25 ADRs): pass.
- Full pytest: 5201 passed, 137 skipped (database-backed integration tests skip without `DB_HOST`).
- Full pytest with `os.geteuid()` simulated as 0 (throwaway `sitecustomize.py` outside the repo on `PYTHONPATH`): 5193 passed, 145 skipped.
- 3.14 isolated, all extras: 5201 passed, 137 skipped.
- `tests/contracts/test_closeout_gate.py`: 7 passed.
- `pyproject.toml`/`uv.lock` unchanged (an import-linter contract for BL-E003-111 was tried and dropped: import-linter cannot forbid a subpackage of the external `l3io` namespace; a pytest AST contract carries it). CLI help unchanged, so the README needs no regeneration.
- Integration tests edited without a local database run: `test_backup_end_to_end.py`, `test_backup_exclusions.py`, `test_backup_scope.py`, `test_encrypted_backup.py` (each only adds `on_unknown=ignore_unknown` to `manifest_from_json`); CI's database job is their first real run.
