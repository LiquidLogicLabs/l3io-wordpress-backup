---
sprint: E003-S01
review: adversarial lens (bmad-review) + clean-release also_consider
diff: git diff a42ec0d..10193bf -- src docs tests tools
date: 2026-11-03
adversarial: 14
clean-release: 4
critical: 0
high: 3
medium: 9
low: 6
---

# Sprint E003-S01 — adversarial and clean-release review

Read-only. Source files were not modified. Items already raised and resolved in
`review-E003-S01-001.md` and `review-E003-S01-002.md` (double VERIFY record, in-root lay-down,
`config_exists` re-derivation, mode copies, refusal text, etc.) are not repeated. The line numbers
refer to the tree at `10193bf`. No credential value was read or printed.

Threat model: destructive restore over a non-empty target, path traversal in extraction, credential
leakage, symlink handling, partial-failure states.

## Adversarial findings

### ADV-1 (HIGH) — The FR-7 guard fails open when the database survey is absent or reports zero tables

- **Location:** `src/l3io/wp/backup/core/restore.py:429-431` (`_survey_empty`), used by `check_target` at `:520-528` and `snapshot_scope` at `:456`
- **Problem:** `_survey_empty` returns True for `db is None`, `not database_exists` and `table_count == 0`.
  - `None` is what the root passes when no survey ran or the survey could not be made. The type does not tell "no database" from "not looked at".
  - `table_count == 0` ignores views, routines, triggers and events. It also ignores a survey that counted 0 because of a privilege error.
- **Failure scenario:** `--mode db` with `database-clean` and a live database that holds only views, or a survey error mapped to `None`. `check_target` passes without `--force`, and `snapshot_scope` returns `None`, so no SNAPSHOT is planned. `LOAD_DUMP` then replaces and drops the previous database with no refusal and no safety copy.
- **Fix:** Make the unknown state its own value (`DatabaseTargetState | Unsurveyed`) and refuse it, or require `database_exists` to carry an explicit object count. Test the guard with `database=None` in a database Mode.

### ADV-2 (HIGH) — A v0 Archive's duplicate members bypass the space and expansion check, and extraction has no cap

- **Location:** `src/l3io/wp/backup/adapters/tar.py:719` (`scan.digests.setdefault`) with `:783-785` (`member_digests`); `src/l3io/wp/backup/adapters/restore_runner.py:371-376` (`_check_space`); `tar.py:1120-1124` (`cap = None` for v0)
- **Problem:** For a v0 Archive (no Manifest, `STRUCTURAL_ONLY`, not authenticated), PREFLIGHT sizes the restore from `member_digests`, which keeps the first occurrence of each name.
  - A tar can hold `wp-root/x` twice: first 1 byte, then 50 GB.
  - The preflight sees 1 byte. The extraction filter has no indexed size and no cap, so it writes the second member.
  - `CLEAN` has already run by then.
- **Failure scenario:** A hostile or corrupt v0 Archive passes BL-E002-116 (ratio and free space) and fills the Site Root filesystem after `files-clean` emptied it. The Site is gone and the extraction is half written.
- **Fix:** Refuse a duplicate member name in a v0 view at classification or probe time. Or keep a running total and `max(size)` per name in `_stream_outer`, and give `_ExtractFilter` a cap from the preflight total for v0.

### ADV-3 (HIGH) — The path-spelling compare in `_clean` can delete the target's `wp-config.php`

- **Location:** `src/l3io/wp/backup/adapters/restore_runner.py:448-449` (`_clean`) versus `:335` (`_check_config`) and `:320-322` (`config_existed`)
- **Problem:** `keep` is decided by `Path(config_path).parent == site_root`, a lexical compare. A symlinked Site Root, a non-canonical `--wp-dir`, or a relative path makes the compare False while the file really is in the root.
  - `keep` is then empty, so `CLEAN` removes `wp-config.php`.
  - `_extract` still excludes the tree's copy, because `config_existed` was True at PREFLIGHT.
  - Nothing restores the file.
- **Failure scenario:** `/var/www/site` is a symlink to `/srv/site`, and the config is given as `/srv/site/wp-config.php`. `files-clean` with the default KEEP removes the target's config, excludes the Archive's copy, and leaves a Site with no config and the target's DB credentials gone.
- **Fix:**
  - Canonicalise both paths in the root and assert it in the executor.
  - Or have `CLEAN` verify that `config_existed` implies `wp-config.php` survives (`keep` derived from the same fact).
  - Or in `_extract`, set `keeps` only when the file really exists after `CLEAN`.

### ADV-4 (MEDIUM) — Nothing re-checks the target between planning and the first destructive step

- **Location:** `src/l3io/wp/backup/adapters/restore_runner.py:247-285` (`execute_restore`); `docs/restore.md` ("Every check below runs before the first destructive step")
- **Problem:** `TargetState` is read once, before `build_restore_plan`. PREFLIGHT and VERIFY re-check the Archive, the tools and the space, but not the target's emptiness.
  - `site_root_empty` and the database table counts are never read again.
  - SNAPSHOT is omitted when the target was empty at planning time.
- **Failure scenario:** The target is empty at planning. A deploy job or a second restore populates it before `CLEAN` or `LOAD_DUMP`. There is no refusal and no snapshot, and FR-7 is bypassed.
- **Fix:** Re-run `check_target` and `snapshot_scope` from a fresh `TargetState` after VERIFY. Refuse if the plan's SNAPSHOT decision changed. Soften the docs sentence if this is not done.

### ADV-5 (MEDIUM) — A destructive phase starts without checking that the snapshot was made

- **Location:** `src/l3io/wp/backup/adapters/restore_runner.py:436-444` (`_snapshot`), `:447-453` (`_clean`)
- **Problem:** `_snapshot` stores the `SnapshotResult` and moves on. It does not check `performed`, `integrity`, or `bytes > 0` for a scope that was non-empty.
- **Failure scenario:** A root or adapter returns `performed=False` (a bug, or a Dry-Run binding wired into a real Run). `CLEAN` deletes the Site with no safety copy and the report says the snapshot ran.
- **Fix:** After `take_snapshot`, if the plan took a snapshot and not in a Dry Run, raise unless `result.performed` and `result.integrity is VERIFIED`. Test it with a fake that returns `performed=False`.

### ADV-6 (MEDIUM) — The snapshot directory has no free-space check, so CLEAN can precede an out-of-space EXTRACT

- **Location:** `src/l3io/wp/backup/adapters/restore_runner.py:360-400` (`_check_space`); `src/l3io/wp/backup/core/restore.py:581-611` (`space_need`)
- **Problem:** BL-E002-116 covers the target and the workspace only. The Safety Snapshot writes up to the whole Site Root and the Dump to `snapshot_dir`, and nothing sizes that destination.
- **Failure scenario:** `snapshot_dir` shares a filesystem with the Site Root or the workspace. The snapshot consumes the free space the preflight counted. Either the snapshot fails partway, or it succeeds and `EXTRACT` hits ENOSPC after `CLEAN`. The Site is half restored and the journal is not orderly.
- **Fix:** Add a third need (`snapshot_need`, from the current Site Root size) checked against `free_bytes(snapshot_dir)`. Sum the needs that share a device (`os.stat().st_dev`).

### ADV-7 (MEDIUM) — The config size limit is enforced late, after CLEAN, EXTRACT and the saved copy

- **Location:** `src/l3io/wp/backup/adapters/restore_runner.py:104` (`_CONFIG_MAX_BYTES`), `:505-511` (`_archive_config`), `:532-535` (`_rewrite_config`)
- **Problem:** `read_member(max_bytes=1 MiB)` refuses an oversized config only inside `REWRITE_CONFIG`. The manifest size is known in PREFLIGHT, but `_check_space` only adds the config copy size to the workspace need. It does not compare it with the limit.
- **Failure scenario:** `--overwrite-wp-config` and an Archive whose config is over 1 MiB. `CLEAN` and `EXTRACT` finish, `save_copy` runs, then `MemberReadRefusedError`. The Site is left unfinished, with the original config saved but not replaced.
- **Fix:** In `_check_config`, refuse when `_config_member(view).size > _CONFIG_MAX_BYTES`. Also probe the read in PREFLIGHT for a v0 Archive.

### ADV-8 (MEDIUM) — The existence probe and the rewrite target disagree when `relocation.config_file` is set

- **Location:** `src/l3io/wp/backup/adapters/restore_runner.py:320-322` (`config_existed` from `inputs.config_path`) versus `:204-207` (`config_file`), `:340-341`, `:526-541`
- **Problem:** `config_existed` and the `_clean` compare use `inputs.config_path`. `_check_config`, `_extract` and `_rewrite_config` use `config_file` (the relocation's, when set). They are different files in a relocation.
- **Failure scenario:** The relocation moves the config. `TargetConfigMissingError` is judged on the old path, `REWRITE_CONFIG` then runs `save_copy` and `read_constants` on a path that does not exist. The failure is a late `OSError` after `CLEAN` and `EXTRACT`, not a clean refusal. The reverse case excludes or keeps the wrong file in `_extract`.
- **Fix:** Probe `run.config_file` and derive every in-root, KEEP and existence decision from that single property. Add a test with `relocation.config_file != inputs.config_path`.

### ADV-9 (MEDIUM) — A failure in SNAPSHOT (or any pre-destructive phase after begin) leaves the journal open for a state that changed nothing

- **Location:** `src/l3io/wp/backup/core/restore.py:550-564` (`is_orderly_failure`); `src/l3io/wp/backup/adapters/restore_runner.py:264-274`
- **Problem:** An orderly failure needs `staging_dropped` plus `check_failed` or `cutover_readback_failed`. A failed snapshot (disk full, permission) has none of these. The run began at SNAPSHOT, so `finish_run` is skipped.
- **Failure scenario:** `take_snapshot` raises ENOSPC. Nothing was touched, yet the journal stays unfinished and the next restore demands `resolve` on a Site that is intact.
- **Fix:** Treat "no `phase_started` in `_FILE_PHASES`, no cutover step, and no `LOAD_DUMP` started" as orderly. Or begin the journal after SNAPSHOT.

### ADV-10 (MEDIUM) — `--overwrite-wp-config` and lay-down paths write the config outside the FR-7 guard and the snapshot scope

- **Location:** `src/l3io/wp/backup/core/restore.py:442-465` (`snapshot_scope` ignores the config), `:512-538` (`check_target` ignores the config file), `:708-714` (`_kept`); `restore_runner.py:464-473`; `docs/restore.md` ("file Modes only")
- **Problem:**
  - `EXTRACT` with `lay_down_config` is planned in any Mode. In a database Mode, `_extract` writes the config-role member into `config_file.parent`, over an existing config.
  - Neither `check_target` nor the snapshot covers that file. In a database Mode `snapshot_scope` is DATABASE only.
  - The docs say `--overwrite-wp-config` is for file Modes only, but `build_restore_plan` does not enforce it. It checks only the Archive's config and the flag table (not in this diff).
- **Failure scenario:** `--mode db --force` with a relocation that lays down the config. The target's `wp-config.php` is replaced, and `REWRITE_CONFIG` may `save_copy` it but there is no snapshot of it. A user who expected a database-only restore loses a file.
- **Fix:** Refuse OVERWRITE and lay-down in Modes that do not restore files, in `build_restore_plan`. Or include the config in `snapshot_scope`. Add a test for each Mode.

### ADV-11 (MEDIUM) — The Archive's `wp-config.php` copy in the workspace is created without enforcing mode or ownership and is not cleaned by the executor

- **Location:** `src/l3io/wp/backup/adapters/restore_runner.py:512-519` (`_archive_config`)
- **Problem:** `os.open(..., 0o600)` sets the mode only when the file is created. An existing `archive-wp-config.php` keeps its mode and owner. `O_NOFOLLOW` protects the last component only, not a symlinked or shared workspace directory. The copy holds the source site's DB credentials and salts, and no `finally` removes it.
- **Failure scenario:** A shared or reused workspace (a fixed path, or a stale file from a crashed Run) has a world-readable `archive-wp-config.php`. The new Run truncates and fills it, and its mode stays 0644.
- **Fix:** Use `O_EXCL` (unlink first if stale), `os.fchmod(fd, 0o600)` before the write, and delete it after `replace_file` in a `finally`. Assert the workspace is a fresh 0700 directory.

### ADV-12 (LOW) — A dry-run bomb cap and size check act on unverified v1 sizes before VERIFY

- **Location:** `src/l3io/wp/backup/adapters/restore_runner.py:315-326`, `:366-369`
- **Problem:** `_check_space` runs in PREFLIGHT, before the Archive is verified, and trusts `inputs.view`. The `view != inputs.view` check in VERIFY covers the later change, but the space check ran on an unauthenticated manifest. A `VERIFICATION_FAILED` archive can first surface as `InsufficientSpaceError` or `ExpansionRatioError`.
- **Failure scenario:** A corrupt Archive produces a space-error exit code, not `VERIFICATION_FAILED` (8). Wrapper scripts that branch on the exit code mis-route it.
- **Fix:** Document the order, or run the size check after VERIFY and before the first destructive phase (still preflight-safe).

### ADV-13 (LOW) — `finish_run` in the exception handler can mask the original error

- **Location:** `src/l3io/wp/backup/adapters/restore_runner.py:271-274`
- **Problem:** If `finish_run()` raises inside `except BaseException`, its error replaces the restore failure (chained, but it is the one the CLI maps to an exit code). A `KeyboardInterrupt` is also caught and may finish the journal.
- **Failure scenario:** A full journal disk during an orderly failure turns a `CheckFailed` refusal into a journal write error with a different exit code.
- **Fix:** Wrap `finish_run` in `contextlib.suppress`/log, or `try/finally`, and re-raise the original.

### ADV-14 (LOW) — `planned_skipped_links` agrees with extraction only for archive-defined links

- **Location:** `src/l3io/wp/backup/core/restore.py:647-673`
- **Problem:** The Dry Run lists links by a lexical rule. The real extraction uses `realpath`, and a pre-existing symlinked directory on the target changes the answer. The docstring admits this, but the Dry Run claim in `docs/restore.md` ("lists those links") carries no such caveat. A Dry Run can promise a link will be restored that the real Run refuses, or the reverse. This also holds for a link written through a symlinked parent in a non-clean Mode.
- **Failure scenario:** `files` mode (no clean) on a target where `wp-content/uploads` is a symlink to `/mnt/media`. The Dry Run lists no skipped links, and the real Run skips or refuses members under it.
- **Fix:** Add the caveat to `docs/restore.md`, or let the Dry Run resolve the target's real directory structure through a query port.

## Clean-release findings

### CR-1 (MEDIUM) — Scope change outside the sprint: new top-level package `wpbackup2`

- **Location:** `src/wpbackup2/__init__.py:1`, `src/wpbackup2/__main__.py:9`; `pyproject.toml:199-202`
- **Problem:** The `python -m wpbackup2` entry (AD-22) is not part of E003-S01-001/002 (restore plan, executor, target guard). It adds a second top-level distribution package and `# type: ignore[func-returns-value]`.
- **Fix:** Record it as its own story or commit, or note it in the sprint closure. Replace the `type: ignore` with a typed `NoReturn` signature on `run_from_cli`.

### CR-2 (LOW) — `type: ignore` is used heavily in the new test harness

- **Location:** `tests/restore_harness.py` (the `Unused(...)` port fakes); `tests/integration/test_restore_modes.py`, `tests/integration/test_restore_refusal.py` and `tests/integration/test_dry_run_restore.py` (`h.config = writer  # type: ignore[assignment]`, `h.load_database = never  # type: ignore[assignment, method-assign]`)
- **Problem:** Eleven suppressions (8 in restore_harness.py, 2 in test_restore_modes.py, 1 in test_dry_run_restore.py) of assignment type errors, all on harness attribute replacement. The suppressions hide an API that is not typed for substitution.
- **Fix:** Make `config` and `load_database` real constructor parameters of the harness, or use `monkeypatch.setattr`. The `Unused` fakes can satisfy the ports with `typing.cast`.

### CR-3 (LOW) — E402 `noqa` block in the live-database test

- **Location:** `tests/integration/test_restore_refusal.py` (the import block after `pytest.importorskip`, 13 `# noqa: E402`)
- **Problem:** Lint suppression to import after a module-level skip.
- **Fix:** Use `pytest.importorskip` into a fixture, or a `conftest.py` collect-ignore marker. Or exclude the file in per-file ruff config rather than per line.

### CR-4 (LOW) — Docs claim more than the code enforces

- **Location:** `docs/restore.md` ("Before anything is changed", "`wp-config.php`")
- **Problem:** "Every check below runs before the first destructive step, so a refusal leaves the Site ... as they were." The target-emptiness and snapshot decision are not re-checked (ADV-4), the config size limit is late (ADV-7), and a database survey of `None` passes (ADV-1). "`--overwrite-wp-config` (file Modes only)" is not enforced in `core/` (ADV-10).
- **Fix:** Fix the code (preferred, per global rule 3) or narrow the claims, and add a check where the claim can be enforced.

## Clean-release scan results

- **Dead code, commented-out code, debug artifacts, TODO/FIXME/XXX/HACK, `print`, `breakpoint`, `pdb`:** none found in the added lines of `src/`, `docs/`, `tests/`, `tools/`. Verified by a grep over the diff.
- **Secrets and credentials:** none in the changed files. The strings with `password` are the `CredentialSentinel` fixture values (`source-password-0` and `target-password-0` in the test data) and `os.environ["DB_PASSWORD"]` reads. `RestorePorts.__repr__` hides the credential callable. `NonEmptyTargetError` names `host:port/name` only.
- **Unused exports:** no `__all__` entry in the diff lacks a use in `src/` or `tests/`. `RestoreRelocation.cutover_*` and `actions`/`undo` are unused by the executor and by design belong to Epic 7's seam, and `required_tools` is used.

## Not repeated (already resolved or tracked)

The double VERIFY phase record, the `lay_down_config` ordering, the `config_exists` fact, the two copies of the Restore Mode, the untested `planned_skipped_links`, the LOAD_DUMP and PREFLIGHT action-line text, the v0 first-read, the vacuous Dry Run and credential assertions, and the deferred S02-006 ACs.
