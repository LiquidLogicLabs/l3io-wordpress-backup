# RED TEAM FINDINGS REPORT

Scope: Epic E003, Sprint 01 (E003-S01-001 restore plan and executor; E003-S01-002 FR-7 non-empty-target guard)
Commit range: a42ec0d..10193bf
Date: 2026-11-03
Analyst: Red Team Agent

## SEVERITY SUMMARY

| CRITICAL | HIGH | MEDIUM | LOW | OBSERVATION |
|---|---|---|---|---|
| 0 | 0 | 3 | 4 | 4 |

## Scope and widening

Seed: the diff for src, docs, tests, tools, plus the two story files.

Widened to, and why:
- `adapters/site_files.py` (`clean`, `extract`): the destructive primitive that `_clean` calls. Needed to judge the guard's real reach.
- `adapters/config_writer.py` (`save_copy`, `replace_file`): untrusted wp-config handling.
- `adapters/workspace.py`: where the decrypted payload and the archive's wp-config copy live.
- `tar.py` `_inner`, `read_member`, `decrypt_payload`: how many times the archive is re-read after VERIFY.
- `domain/restore.py` (`OverrideSet`), `core/restore.py` (`check_target`, `build_restore_plan`): guard logic.
- `adapters/database.py`, plus a grep for who calls `execute_restore` and `check_target`: DB credential exposure, and whether this sprint's code is reachable.

Method: static reading only. No code was run or modified. No credential values were read or printed.

Reachability baseline: `execute_restore` and `build_restore_plan` have no caller under `src/` outside their own modules. The CLI root (`cli/restore.py`) and the database slots (`take_snapshot`, `check_database`, `load_database`) are E003-S02 work. Today the code is reachable only as a library call and from tests. Every MEDIUM below becomes reachable from the command line when E003-S02-006 wires the root, and each fix is cheapest now.

Not assessable this sprint: DB credentials in process args, env or logs for `load_database` and `check_database`. They are injected slots and no implementation exists yet. `adapters/database.py` has no `MYSQL_PWD`, `--password` or `-p` argv construction, so the existing adapter does not use the mysql CLI argv. Re-run this lens on E003-S02. The sprint's own credential test (`test_restore_execute_is_credential_clean`) covers only the fake-slot path.

## FINDINGS

| Severity | ID | Title | Lens | Reachable in this sprint's code? |
|---|---|---|---|---|
| MEDIUM | RT-001 | Plain archive is re-read after VERIFY with no re-check of content (TOCTOU) | EXT/CHA | Yes, via `execute_restore` as a library call |
| MEDIUM | RT-002 | Restore has no authenticity check for an unencrypted archive | EXT/DAR | Yes, via `execute_restore` as a library call |
| MEDIUM | RT-003 | FR-7 guard and Safety Snapshot scope rest on a plan-time survey; the executor never re-surveys | CHA/ABU | Yes, via `execute_restore` as a library call |
| LOW | RT-004 | Inode and member-count exhaustion is not bounded | ABU | Yes |
| LOW | RT-005 | `exclude` and `keep` comparison is byte-exact and case-sensitive | EXT | Only on a case-insensitive target FS |
| LOW | RT-006 | `CLEAN` safety is a heuristic; a Site Root that is a symlink or holds a planted marker passes | ABU/INS | Yes |
| LOW | RT-007 | Archive wp-config is parsed and written whole; only four `DB_*` keys are protected | EXT | Yes (`--overwrite-wp-config`) |
| OBS | RT-008 | Tar extraction hardening verified | EXT | n/a |
| OBS | RT-009 | Archive-config copy and decrypted payload handling verified | INS | n/a |
| OBS | RT-010 | FR-7 specificity verified; it protects only if `plan.mode` is the mode executed | DAR | n/a |
| OBS | RT-011 | `execute_restore` takes no run lock | CHA | n/a |

### RT-001 MEDIUM - Archive re-read after VERIFY; content not re-checked
- Attack path: `_verify` streams the archive once and passes. `_extract` and `_archive_config` then re-open the same path, the plain-payload branch of `tar._inner`. ADR-0024 says verification never opens the inner payload. `ArchiveChangedError` compares only the `ArchiveView`, which comes from the Manifest. An attacker with write access to the archive's directory (shared NFS or SMB, a world-writable local store, or a compromised sync agent) can swap the file between VERIFY and EXTRACT. The swapped file keeps the Manifest and the member names, kinds and sizes. The `_ExtractFilter` index and size caps hold, but nothing checks the file bytes. Replacing `wp-config.php` or a plugin's PHP with attacker code gives code execution on the restored site. The SNAPSHOT and CLEAN phases that run between VERIFY and EXTRACT widen the window.
- Impact: a verified archive is not the archive that is extracted. The attacker needs write access to the archive location during the window.
- Not affected: encrypted archives. They are decrypted once into the 0700 workspace (`decrypt_payload`, O_EXCL, O_NOFOLLOW, 0600) and read from there.
- Recommendation: before the first phase after VERIFY, either copy the plain payload into the Run workspace and extract from the copy, or hash members during extraction against the Manifest checksums. At minimum, fstat the archive fd (inode, size, mtime) at VERIFY and compare it before each re-read. Add a test that swaps the archive after VERIFY.

### RT-002 MEDIUM - No authenticity check on restore of an unencrypted archive
- Attack path: `verify_archive_view` is keyless. Its own docstring says `authenticated` is always False and that the checksums live in the unauthenticated Manifest. An attacker who can put an archive in a Storage Target, or who tampers with one there, can craft a self-consistent v1 archive, or a v0 one, which reports only `STRUCTURAL_ONLY` and still restores. With `files-clean` or `all-clean` and `--force`, the operator then restores attacker PHP, a hostile `wp-config.php` or a hostile SQL Dump. The Dump loads in a later sprint. `STRUCTURAL_ONLY` is reported but does not gate anything.
- Impact: code execution on the site and database takeover, if the archive source is less trusted than the operator.
- Recommendation: add a restore-time authentication option (the `verify --authenticate` path already exists) and make it the default when a key or passphrase is configured. Make `STRUCTURAL_ONLY` and unauthenticated archives require an explicit flag, and name that in the Dry Run output. Record the trust assumption in `docs/restore.md`; it does not mention one.

### RT-003 MEDIUM - Guard and snapshot scope decided at plan time
- Attack path: `check_target` and `snapshot_scope` consume a `TargetState` surveyed before the run. `execute_restore` never re-surveys. VERIFY, decrypt, free-space checks and the SNAPSHOT itself take time. If the Site Root or the database is empty at survey and gains content before CLEAN or LOAD_DUMP (a concurrent deploy, a cron job, another operator), the run proceeds without `--force`, and `snapshot_scope` may be `None`. `CLEAN` then deletes content that no snapshot covers. This is the exact failure FR-7 exists to prevent.
- Impact: unauthorized, unrecoverable destruction in a race. It needs a concurrent writer.
- Recommendation: re-run the emptiness query inside the executor immediately before the first destructive phase, and compare it with `plan.snapshot_scope` and the waivers. `_verify` already does this for the archive view; do it for the target. Take the E003-S02 run lock for the whole Run (see RT-011).

### RT-004 LOW - Inode and member-count exhaustion
- The space and expansion checks count bytes only. A Manifest-consistent archive with millions of zero-byte files, directories or symlinks passes `check_expansion` (expanded ~0) and exhausts inodes on the target after `CLEAN`.
- Recommendation: cap member count and check free inodes in PREFLIGHT.

### RT-005 LOW - Exclude and keep paths compared byte-exact
- `_ExtractFilter` compares `PurePosixPath(relative)` to `exclude`, and `_clean` compares `entry.name` to `kept`. On a case-insensitive or Unicode-normalizing filesystem, a member named `WP-Config.php` writes onto the kept config. Linux ext4 and xfs are not affected.
- Recommendation: compare with `os.path.samefile` or casefolded NFC names when the target FS is not case-sensitive. Otherwise document it.

### RT-006 LOW - `CLEAN` guard is acknowledged as a heuristic
- `_refuse_unsafe` accepts any directory containing `wp-config.php` or `wp-includes/`. A mistyped `--wp-dir` pointing at a parent that holds a WordPress install, or at a home-adjacent directory with such a marker, passes (the home directory and `/` themselves are caught). `site_root.absolute()` plus `resolve()` is evaluated once, and `iterdir` runs afterwards (a minor TOCTOU). The FR-7 `--force` requirement is the real control.
- Recommendation: accept as is, or require both markers. Record the decision.

### RT-007 LOW - Archive wp-config applied whole under `--overwrite-wp-config`
- Only `DB_HOST`, `DB_USER`, `DB_PASSWORD` and `DB_NAME` are carried from the target. Every other constant, and any PHP code in the file, comes from the archive: salts, `WP_HOME`, `WP_DEBUG_LOG`, `DISALLOW_FILE_EDIT`, includes and so on. This is intended, but it is a second unauthenticated code path (see RT-002). `replace_file` does check the temp file's mode and owner, and fsyncs and reads back the file. The 1 MiB read cap is good.
- Recommendation: document it. Consider warning when the archive config contains non-constant PHP statements.

### Observations
- RT-008: tar extraction is well hardened. Hard links are refused. Names must be in the verified index, and duplicates are refused. Header kinds must equal the indexed kinds for v1. Header size may not exceed the indexed size, and a running cap is enforced. Members below an archive-written symlink are refused. Out-of-root and absolute symlinks are skipped, not written. `require_patched_tarfile()` checks by capability (`os.path.ALLOW_MISSING`) before CLEAN, which closes the CVE-2025-4517 class. Special files fail `data_filter`. The directory fix-up second pass is handled. Mode and ownership bits come from `data_filter`.
- RT-009: the archive-config copy uses O_NOFOLLOW and 0600 in a 0700 `mkdtemp` workspace that is removed in `finally`. The decrypted payload uses O_EXCL, O_NOFOLLOW and 0600, and is deleted on failure. The saved original config is 0600 via `mkstemp`. Logs name paths, never values. The new credential-hygiene test asserts that the sentinel password is absent from plan, inputs, ports, report, journal and planned actions.
- RT-010: `check_target` takes the mode and waivers separately. `OverrideSet` cannot name a mode, and `files` and `database` are checked separately, each against its own waiver. `test_no_flag_reads.py` derives its scope from `COVERED_FLAGS` and plants violations. The executor checks `header.restore_mode is plan.mode`. The guard covers only the plan it ran for: nothing ties the `plan` and `inputs` passed to `execute_restore` to the `target` that `check_target` saw. E003-S02-006 should build the plan, inputs and executor from one object.
- RT-011: no lock is acquired in the executor (`adapters/lock.py` exists). A concurrent backup or restore of the same site is not excluded. The root should hold the lock across survey through finish.

## EXECUTIVE SUMMARY

Reviewed the restore executor, plan and FR-7 guard (about 1,600 added lines of source, plus tests and docs), widened to the clean, extract, config-writer and workspace adapters. Stack: Python, stdlib tarfile and structlog, with an external wp-config parser. Overall posture: MEDIUM. No finding rates HIGH or CRITICAL because the code has no production entry point yet, and the in-process hardening (tar filter, index pinning, size caps, patched-tarfile gate, private workspace, 0600 files, no secrets in logs) is strong. The most dangerous finding is RT-001/RT-002 taken together: restore trusts an archive it verified only for consistency, and re-reads it afterwards, so a party with write access to the archive location can substitute PHP that the tool then installs. The most important recommendation is to bind the restore to one immutable copy of the verified payload (copy into the workspace, or hash during extraction), and to re-survey the target before the first destructive phase, before E003-S02-006 exposes `restore` on the command line.
