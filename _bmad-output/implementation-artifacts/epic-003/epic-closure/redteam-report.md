# Red team report: Epic E003 (epic level), `wpbackup2 restore`

Scope: the whole restore path across sprints 01 and 02, at `fea5a21`. Method: read the code named below and run two one-line checks against it. Nothing was modified, committed or executed against a real Site or database.

## Scope and widening

Seeds read: `adapters/{restore_runner,staging,journal,tar,site_files,workspace,store,lock,config_writer,database}.py`, `cli/restore.py`, `core/restore.py`, `docs/restore.md`, the two sprint red-team reports, and the BL-E003-* titles in `state/issues.yaml`.

Widened to, with the reason:

- `domain/archive_format.py`: the extraction roles and member-path rules decide what `EXTRACT` may write.
- `domain/errors.py` and `cli/__init__.py`: the signal handling path.
- The installed `l3io.wp.database` `tls.py`: to confirm what "local host" means for the TLS policy.

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 2 |
| LOW | 3 |
| OBSERVATION | 4 |

No new CRITICAL or HIGH was found. The extraction filter, the TLS policy, journal contents, cutover SQL and Archive binding held up against the attacks listed under "Checked and found sound".

The two MEDIUM findings are a missing quiesce of the live Site and a missing per-role member-path constraint. EP-RT-002 has a latent HIGH consequence once Epic 7 turns on config lay-down.

## Findings

### EP-RT-001 (MEDIUM): nothing quiesces the live Site, so writes made after the Safety Snapshot are destroyed with no copy

- **Where:** `adapters/restore_runner.py:322-329` (phase loop), `core/restore.py:726-752` (`recheck_target`), `adapters/staging.py:465-501` (`_loser`).
- **Reasoning, verified:**
  - Nothing in `src/` puts the Site into maintenance or stops writers. A grep for `maintenance` over `src/` returns nothing, and `docs/restore.md` and ADRs 0016 and 0020-0023 never say the Site must be offline.
  - `SNAPSHOT` runs first. `_DESTROYING` (`restore_runner.py:~150`) is every phase after `SNAPSHOT`, so the one `_recheck` runs after the snapshot and immediately before `CLEAN`.
  - `recheck_target` compares only whether a part went from empty to non-empty (`snapshot_scope` against `_covers`). It never compares content, so a Site that is non-empty at both times passes.
  - Everything the web server or database users write between the snapshot and the end of `LOAD_DUMP` is therefore in neither the snapshot nor the Archive. That window covers the whole of `CLEAN`, `EXTRACT`, the Dump load and the post-load check.
  - The Mode that drops the live database (`_loser`, `drop_database` on `live.name`) removes the one place that data survived. For files, `CLEAN` deletes whatever is there at that moment.
- **Reproduction:**
  1. Run `restore --cleanfirst --drop-database` (ALL_CLEAN) on a Site that is serving traffic.
  2. A comment, order or upload is written after `snapshot_written` is logged.
  3. `CLEAN` and `_loser` remove it. `docs/restore.md` still presents the Safety Snapshot as the undo for the run.
- **Also:** the concurrent writer is the web process, which on shared hosts often owns subdirectories of the Site Root. After `CLEAN` the empty Site Root can be written by that process while `EXTRACT` runs. The extraction filter's realpath check (`data_filter`) stops a planted symlink at filter time, but `tarfile` extraction is path-based, so a narrow check-then-write window remains. This is why the finding is MEDIUM rather than LOW.
- **Fix direction:**
  - Document that the Site must be quiesced.
  - Or add a `.maintenance` write and a refusal when the Site Root or database changed since the snapshot (compare `tree_bytes` and the entry count).
  - This is a design gap, not a conflict with an ADR.

### EP-RT-002 (MEDIUM): `config` and `dump` role members are not limited to their one canonical path

- **Where:** `domain/archive_format.py:681-692` (`is_safe_member_path`), `:703-727` (`member_relative`); consumers `adapters/restore_runner.py:266-270, 735-748, 790-800` and `adapters/staging.py:545-553`.
- **Reasoning, verified:**
  - The builder writes exactly `config/wp-config.php` and `dump/database.sql` (`tar.py:555-556`). Verification and classification accept any safe relative path under either role, in any number.
  - Executed: `is_safe_member_path("config/.ssh/authorized_keys", CONFIG)` is `True`, and so is `is_safe_member_path("dump/x/y.sql", DUMP)`.
  - `extract_archive(roles={CONFIG})` therefore extracts every `config/*` member.
  - Three consumers, three outcomes:
    1. `_extract` with `lay_down_config` and `lays_down_by_role` extracts all `config` members into `config_file.parent`. That is the Site Root, or its parent (`~`) when the config sits one level up (FR-29). A hostile Archive then writes `config/.bashrc` or `config/.ssh/authorized_keys` under `~`. The filter confines the write to the destination, but the destination is the operator's home.
    2. `restore_runner._archive_config` (`:792-800`) extracts them into the workspace, then reads `directory / "wp-config.php"`. `config_member` returns the first `config`-role file, which can be `config/other.php`. The preflight (`:446-447`) checks only that a member exists and its size. `FileNotFoundError` then fails `REWRITE_CONFIG` after `CLEAN` and `EXTRACT`, with `--overwrite-wp-config`.
    3. `load_staged` extracts every `dump/*` member into the Run workspace root. A member named like the decrypted payload, or `archive/<copy>`, truncates or replaces what the Run is still reading. Only the Archive author gains anything here, and it is self-denial.
- **Reachability today:** consequence 1 needs `RestoreRelocation.lay_down_config=True`. The only constructor sets `False` (`core/restore.py:428`) and the only other source is `RESTORE_EXTENSION`, which is `None` in this build. So it is **latent until Epic 7** and becomes a write-anywhere-under-the-config-directory primitive then. Consequences 2 and 3 are reachable now: a forged unencrypted Archive is waived by the 2026-11-06 ADR-0016 decision, so the attacker is whoever can write the file.
- **Fix direction:**
  - Add a manifest invariant in `_member_index` / `_member`: the `config` role is exactly `config/wp-config.php`, the `dump` role exactly `dump/database.sql`, at most one each, and no symlink kind outside `site-root`.
  - For v0, `_v0_role` already pins the Dump.
  - Add a preflight refusal in `_check_config` when `config_member(...).path` is not the canonical name.

### EP-RT-003 (LOW): SIGTERM is absorbed or re-labelled by `except Exception` handlers inside the cutover

- **Where:** `domain/errors.py:411` (`RunTerminated(BackupError)`, so it is an `Exception`); `cli/__init__.py:58-75`; `adapters/staging.py:416` (`_cutover`), `:380` (`_read_back`), `:310` (`_drop_staging`); `adapters/restore_runner.py:611` (`_snapshot`).
- **Reasoning, verified:**
  - FR-28's handler raises `RunTerminated` in the main thread at an arbitrary bytecode.
  - `_cutover` wraps `command.set_constants` in `except Exception`. If the signal lands inside the write, `isinstance(exc, ConfigWriteFailedError)` is false and `_read_back` runs.
  - When the key had already landed, the code logs `cutover_write_error_landed` and **continues**. The termination is swallowed and the Run goes on to `_loser`, which drops the live database.
  - `_snapshot` converts the signal into `SnapshotFailedError`, which gets a different exit code and wording.
  - `_drop_staging` swallows it, though its caller re-raises the original error.
- **Reproduction:** `kill -TERM` the process during `set_constants` of `DB_NAME`. The window is narrow, but the effect is that the operator's stop is ignored right before an irreversible drop.
- **Fix direction:** re-raise `RunTerminated` first in those handlers, or catch `Exception` only after `except RunTerminated: raise`.

### EP-RT-004 (LOW): SIGHUP (an SSH disconnect) kills the Run with no cleanup

- **Where:** `cli/__init__.py:58-75` installs a handler for SIGTERM only. A grep for `signal.` over `src/` finds nothing else.
- **Reasoning, verified:**
  - SIGHUP's default action ends the process at once. No `finally` runs, so the workspace is not removed.
  - RT-S02-007 describes residue after an *uncatchable* kill. SIGHUP is catchable, and an SSH drop is the most likely way a long restore is interrupted.
  - For an encrypted Archive the residue is the decrypted payload and the Dump (plaintext) in `$TMPDIR/l3io-wp-backup-*` (mode `0700`, files `0600`).
  - SIGPIPE is ignored by Python, so a closed stderr raises `BrokenPipeError` from the `print(...)` calls (`cli/restore.py:1306, 1422`). That surfaces as a generic failure.
- **Fix direction:** treat SIGHUP like SIGTERM in `_sigterm_raises`, with the EP-RT-003 caveat.

### EP-RT-005 (LOW): the free-space check does not cover the database server

- **Where:** `adapters/restore_runner.py:464-545` (`_check_space`).
- **Reasoning, verified:** the claims cover the Site Root, the workspace and the snapshot directory. The Dump is loaded into a *new* staging database on the database server before the cutover (`staging.py:547-560`). No size or free-space check exists for that server. A large Dump fills the server's datadir, which can stop *other* databases there. This is a distinct failure from the existing BL-E003 entry about the snapshot's database part being charged 0 bytes.
- **Fix direction:** warn, or compare the indexed `dump` size to the server's datadir space if the library offers it. At minimum, document it.

## Observations

- **EP-RT-O1:** `shutil.rmtree` in `site_files.py:122` descends into mount points (no `st_dev` check). A bind-mounted or NFS `uploads` directory under the Site Root is emptied at its source. The Safety Snapshot reads it too, so this is consistent, but it is not stated in `docs/restore.md`.
- **EP-RT-O2:** the TLS policy treats `localhost`, `127.0.0.1` and `::1` as plaintext whatever the port (`l3io/wp/database/tls.py:93`). `DB_HOST=localhost:3307` that is an SSH tunnel or published container port sends the administrator password in the clear across that hop. This follows ADR-0020's policy and is not a defect. It is worth one sentence in the TLS section of `docs/restore.md`.
- **EP-RT-O3:** in merge Modes (`--force` without `--cleanfirst`) an existing in-root symlink such as `wp-content/uploads -> /mnt/uploads` makes `data_filter` refuse every member below it (`OutsideDestinationError`). The run fails mid-`EXTRACT` after `SNAPSHOT`. It fails closed, but it is an operational trap and the refusal text does not name the cause.
- **EP-RT-O4:** for an encrypted Archive the Manifest lives outside the authenticated payload. An attacker with no key can edit `members`, `config_path`, `dump_present` and `backup_scope`. Every effect found is fail-closed (a size, kind or name mismatch refuses), but `dump_present: false` on an ALL Mode would plan files-only phases. This should be recorded next to the ADR-0024 payload-only decision.

## Checked and found sound

- **Extraction.** `_ExtractFilter` refuses hard links, duplicate names, members below an Archive-written symlink, kind mismatches, headers larger than the indexed size, and a running total above the indexed sum. Symlinks that resolve outside are skipped, and writes onto an excluded path through a second name are refused. I found no traversal, link or bomb route that stays within the filter. Python 3.11 is gated by `require_patched_tarfile` before `CLEAN`.
- **Archive TOCTOU.** `_bind_archive` copies the Archive into the `0700` workspace, and `_verify` re-checks the view, so the post-`VERIFY` swap in RT-001 is closed.
- **Journal.** The header excludes `DB_PASSWORD` (`domain/journal.py`). Files are written `0600` via temp, `fsync` and `link`/`replace`, with `O_NOFOLLOW`. `begin_run` is exclusive, and a corrupt record refuses rather than reading as absent.
- **Credentials.** No SQL and no identifier quoting live in this repo's adapters. `_validate_name` runs before `ensure_database`. The cutover value is dropped after its one write. The admin password is not placed on any subprocess; none is spawned.
- **TLS.** Every connection goes through `WpDatabase._connection`. A below-policy override is `USAGE` before any connection, and the Archive-named host needs the typed `--archive-database` confirmation.
- **Resume.** `complete_cutover` surveys the load database before writing, and writes only keys whose target is in the journal.

## Not examined

- The `l3io.wp.database` library internals (`load`, `ensure`, `revoke`, quoting) beyond its TLS default.
- Behaviour on a real MySQL or MariaDB server, and Windows semantics.
- The Epic 7 extension seam beyond confirming it is `None` here.
- Whether `wp_config` writes through a symlinked `wp-config.php` replace the link.
