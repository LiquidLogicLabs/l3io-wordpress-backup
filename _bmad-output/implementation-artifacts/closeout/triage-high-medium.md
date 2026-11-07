# Close-out triage: open High and Medium backlog items (2026-11-07)

Scope: every `backlog:` entry in `state/issues.yaml` with severity High, Critical or Medium at HEAD `cc60f50`
(37 items: 4 High, 33 Medium, no Critical). Low items are triaged separately (`triage-low.md`) and were not touched.
Nothing is committed. Session `b74a242a-6a71-45d8-8ece-7b06b152e53c`.

## Fixed here, still open in the backlog until committed

`pm-status resolve-issue --resolution fixed` accepts only a story key or a commit SHA as `--ref`, and these fixes are
uncommitted. These 8 items stay open until the fix is committed. Then resolve each one with:

```
uv run -q _bmad/scripts/pm-status.py resolve-issue --state-root _bmad-output/implementation-artifacts/state \
  --key <KEY> --resolution fixed --ref <commit SHA> --note "<the Evidence / fix cell below>" \
  --session-id b74a242a-6a71-45d8-8ece-7b06b152e53c
```

Keys: BL-E002-062, BL-E002-067, BL-E002-092, BL-E002-093, BL-E002-100, BL-E002-101, BL-E002-102, BL-E002-107.

## Triage table

Outcomes:
- **fixed**: fixed in this triage, uncommitted.
- **already-fixed**: resolved `fixed` against the cited commit.
- **decided**: resolved `wontfix`.
- **duplicate**: resolved `duplicate`.
- **owner**: left open; see the owner decisions below.

| Key | Sev | Outcome | Evidence or fix | Test |
|---|---|---|---|---|
| BL-E002-097 | High | decided | Owner decision RT-S02-004 (Shawn 2026-11-06, option A): the account stays `USER@'%'`. An existing account is not reset without `--reset-existing-db-user`. Recorded in `docs/restore.md:131-136`, the `cli/restore.py` `ExistingDatabaseUserError` docstring, and `adapters/database.py:147` | `tests/unit/cli/test_restore_root.py::test_an_existing_account_named_by_the_archive_is_refused_before_any_effect` |
| BL-E002-098 | High | owner | Still real. `tar.extractall` runs into the live Site Root (`adapters/tar.py`, `site_files.py`). There is no privilege drop and no private-directory staging, and no ADR covers it | none |
| BL-E002-101 | High | fixed | `cli/parser.py` `_add_backup`: `--whatIf/--what-if` is accepted after `backup` (SUPPRESS default, as on restore; AD-22 allows additions). `backup --help` lists it. `--list-default-excludes` refuses it like any other flag (`cli/backup.py` `_refuse_flags_with_list` treats a SUPPRESS default as False). README regenerated | `test_backup_dry_run_effects.py::test_what_if_after_backup_is_a_dry_run`, `::test_backup_help_lists_what_if`; `test_backup_flags.py::test_list_default_excludes_refuses_every_other_backup_flag[--what-if]` |
| BL-E002-102 | High | fixed | `cli/backup.py` `DRY_RUN_TRAILER`: the last line of a successful backup Dry Run says nothing was written and that write-time failures (full disk, unreadable file) go undetected. Restore already had `completion_line`. The README Usage section documents rehearsing a backup | `test_backup_dry_run_effects.py` (trailer asserted); `tests/integration/test_dry_run_backup.py` (updated; DB-gated) |
| BL-E002-061 | Medium | already-fixed | `b424f7c` (hardened in `0034ae6`): `_BoundedTarInfo` refuses an extended header over 1 MiB before reading it (`adapters/tar.py:155-193`) | `tests/adapters/test_tar_extended_header_bound.py::test_verify_refuses_an_extended_header_bomb_with_bounded_memory` |
| BL-E002-062 | Medium | fixed | `adapters/tar.py` `_add_file` opens with `O_NONBLOCK`, so a FIFO swapped in no longer blocks the open. The `fstat` check refuses it, and blocking mode is restored before reading | `tests/adapters/test_tar_writer.py::test_a_file_swapped_for_a_fifo_after_collection_fails_without_hanging` (hangs, then fails, without the fix) |
| BL-E002-063 | Medium | owner | Still real, and wider than reported: operator-held credentials (`--db-credentials-aws-secret-id`, `L3IO_WP_BACKUP_DB_*`) go to `DB_HOST` from the Site's `wp-config.php` (`domain/site.py:58-60`) | none |
| BL-E002-064 | Medium | owner | Part fixed: the self-inclusion consequence is closed by BL-E002-100. A target inside the Site Root is still not refused, so the Archive (dump, `wp-config.php`) sits under the docroot | `test_archive_not_collected.py` (the size part) |
| BL-E002-065 | Medium | owner | Part fixed: SIGTERM/SIGINT clean the workspace (`5b44ca5`, `cli/__init__.py`). After SIGKILL, OOM or power loss there is still no sweep of stale `l3io-wp-backup-*` workspaces or `.partial` files | `tests/integration/test_site_lock_signals.py::test_sigterm_releases_the_lock_and_exits_failed` (the part that is fixed) |
| BL-E002-067 | Medium | fixed | `adapters/tar.py` `_ExtractFilter` refuses a member whose target has the same `(st_dev, st_ino)` as an excluded entry (`_existing_identity`). `WP-CONFIG.PHP` on a case-insensitive filesystem therefore cannot overwrite the kept `wp-config.php`. A case variant that is a different file is still extracted | `tests/adapters/test_site_files.py::test_a_case_variant_of_the_kept_config_is_refused_where_it_is_the_same_file` (fails without the fix), `::test_a_case_variant_that_is_another_file_is_extracted` |
| BL-E002-068 | Medium | already-fixed | `ffe6319` (BL-E002-116): the per-member indexed size and running-total cap in `_ExtractFilter`, plus the size and free-space preflight before CLEAN | `tests/unit/adapters/test_tar.py::test_a_member_larger_than_its_index_entry_is_refused`, `::test_the_running_total_stops_at_the_indexed_sum` |
| BL-E002-069 | Medium | owner | AR-04 mostly fixed (`PayloadDamagedError` is 8; VERIFY runs before EXTRACT). AR-03 (`ExtractionRefusedError` exits 3 "nothing was done" after a partial write) and AR-05 (a missing passphrase exits 2 or 3, while the exit table says 4) are still real | `tests/adapters/test_crypto.py::test_a_damaged_stream_is_damaged_not_rejected` (AR-04) |
| BL-E002-070 | Medium | already-fixed | `14029e8`: 12-character floor and double prompt (`cli/passphrase.py` `require_passphrase`; ADR-0024 amendment) | `tests/unit/cli/test_passphrase_chain.py::test_a_prompted_passphrase_is_asked_twice`, `::test_a_mismatched_confirmation_is_refused` |
| BL-E002-072 | Medium | owner | Still real: `view.payload is None` branches in `core/verify.py:154`, `adapters/tar.py` (`_inner`, `extract_archive`, `decrypt_payload`) and `adapters/restore_runner.py` (`_check_space`). The guard matches `format_version` only | none |
| BL-E002-074 | Medium | owner | UX-01-4 fixed by `02b9752` (restore works). UX-01-3 is still real: a missing `--wp-dir` and a missing `--archive` exit 1 (`InvalidSiteDescriptorError`, `ArchiveNotFoundError`). This is an exit-code decision | `tests/contracts/test_not_yet_implemented.py::test_no_bound_feature_is_marked_unavailable` (UX-01-4) |
| BL-E002-092 | Medium | fixed | `RunTerminated` is re-raised ahead of every broad `except` in `adapters/noop.py` (`GuardedTracer.call`), `adapters/tar.py` (`_decide`), `adapters/staging.py` (`_cutover`, `_drop_staging`, `_read_back`) and `adapters/restore_runner.py` (`_snapshot`). The three handlers that map every failure (`cli/__init__.py` main, `cli/exit.py`, `api/errors.py` boundary) carry a marker comment | `tests/contracts/test_run_terminated_propagates.py`: an AST guard over every package module, with planted cases; the tracer-guard and `_decide` behaviour tests fail without the fix |
| BL-E002-093 | Medium | fixed | Shared refusal functions: `adapters/site_files.py` `refuse_unsafe_clean_target` (called by `NoopSiteFiles.clean`) and `adapters/config_writer.py` `refuse_unkeepable` (called by `NoopConfigWriter.replace_file`). An absent `wp-config.php` is deliberately not checked in a Dry Run, because EXTRACT wrote nothing (stated in the docstring) | `tests/unit/adapters/test_noop_restore.py::test_the_site_files_twin_refuses_an_unsafe_clean_target_like_the_real_one[3]`, `::test_the_config_twin_refuses_an_unkeepable_constant_like_the_real_one` (fail without the fix) |
| BL-E002-094 | Medium | decided | Spine AD-19 fixes the lock's identity (the canonical Site Root path) and location (a per-user state directory). The per-user scope is in `docs/restore.md`, and this triage added it to `SiteLockedError`'s message | `tests/unit/cli/test_lock_joining.py`, `test_lock_order.py` (existing; full suite green with the new message) |
| BL-E002-095 | Medium | already-fixed | `14029e8`: every parser option is either `NOT_YET_IMPLEMENTED` or read as `args.<dest>` by its root module | `tests/contracts/test_not_yet_implemented.py::test_no_option_is_silently_accepted`, `::test_a_planted_unread_option_is_caught` |
| BL-E002-099 | Medium | already-fixed | `ebff354`: one key per `set_constants`, journaled before and after each write; recovery derives written and pending keys from the journal. The SIGTERM swallow in `_cutover` is fixed under BL-E002-092 | `tests/unit/core/test_restore_journal.py::test_written_and_pending_keys_inside_the_cutover`; `tests/integration/test_kill_mid_restore.py` |
| BL-E002-100 | Medium | fixed | `cli/backup.py` `_SkipStaged` also leaves out earlier Archives (`wpbackup2-*.tar.gz`, from `domain/storage_target.py` `DEFAULT_ARCHIVE_NAME_GLOB`) and killed Runs' staged files (`.wpbackup2-*.tar.gz.*.partial`), whatever the flags say. They are recorded in the Manifest's excluded list. `docs/exclusions.md` updated | `tests/unit/cli/test_archive_not_collected.py::test_a_second_default_backup_from_the_site_root_does_not_hold_the_first` (the red team's reproduction; fails without the fix), `::test_the_default_name_and_its_staged_file_match_the_skipped_globs` |
| BL-E002-103 | Medium | owner | UX-3 already fixed (`fea5a21`). Fixed here: UX-4 (plan lines "check dump readiness", "no exclusion patterns"), UX-6 (`SiteLockedError` gives the next step and the per-user scope), UX-7 (README: rehearsing a backup), UX-8 (metavars `USER`, `PASSWORD`, `ID`, `REGION`, `URL`, `HOST`, `NAME`), UX-9 (capitalisation; a description for the restore options group), UX-10 (`--drop-database` help names its valid combinations), UX-11 path part (the missing Site Root error shows the absolute path). Still open: UX-5 (on a non-TTY a failure is JSON only) and UX-11's exit code (1, not 2 or 3) | `test_backup_dry_run_effects.py` (plan wording); `tests/contracts/test_cli_docs.py` (README) |
| BL-E002-104 | Medium | owner | Phase-name half fixed under BL-E002-107. `check_load` still calls `_require_admin()` but checks as the WordPress account (`adapters/database.py:956-967`), and the two story specs disagree (S02-012 against S02-009:51) | none |
| BL-E002-106 | Medium | owner | Spec proposal for SD-01-2. The drift is neither fixed nor ratified (see BL-E002-072) | none |
| BL-E002-107 | Medium | fixed | `adapters/phase.py` `join_open_phase`: a Dry Run no-op joins the innermost open phase whatever its name and emits no record of its own, so the executor alone decides plan structure (AD-17) | `test_noop_restore.py::test_a_twin_called_inside_any_phase_joins_it_and_emits_none_of_its_own` (every twin x every `RestorePhase`), `::test_a_dry_run_cutover_is_recorded_inside_load_dump` (fail without the fix) |
| BL-E002-108 | Medium | duplicate | Of BL-E002-086 (resolved, `d23c9bd`; pinned in `docs/closeout-gate.yaml`) | `tests/contracts/test_cli_surface.py::test_python_dash_m_wpbackup2_reaches_this_parser` |
| BL-E002-109 | Medium | owner | Still real: backup emits `DUMP` and the like, restore emits `load-dump` (`adapters/restore_runner.py:332`), and `docs/logging.md:53` lists only the backup set. The journal stores the kebab values on disk | none |
| BL-E004-057 | Medium | already-fixed | `9f56434`: `publish.yml` step "every optional extra installs from PyPI alone" (`--no-sources --all-extras`) fails a PyPI release until the family is on PyPI (owner decision, `pyproject.toml:75-81`; AD-15). A tag never publishes to PyPI | the workflow step itself |
| BL-E004-058 | Medium | already-fixed | `3f29b0d`: `cryptography` dropped from the dependencies and `uv.lock`; nothing in `src/` imports it | import-linter forbidden list |
| BL-E005-013 | Medium | already-fixed | `14029e8`: `cli/preflight.py` `RootPreflight` in every run root, which refuses Windows first | `tests/contracts/test_preflight_construction.py::test_epic_5_preflight_has_production_callers`; `tests/unit/cli/test_root_preflight.py` |
| BL-E006-009 | Medium | already-fixed | `14029e8`: `domain/secret.py::Secret` (owner decision, ADR-0018 amendment 2026-11-07). This triage rewrote the stale `docs/api.md` paragraph that still said "plain `str`" | `tests/contracts/test_secret_reveal.py` |
| BL-E006-010 | Medium | owner | Still real, in `l3io-wordpress-database` (`_observe.py:32`, `:89-90`: `record_exception` and the status text). The ADR-0011 amendment states that this package cannot change it and that this item carries the request | none |
| BL-E008-003 | Medium | owner | E008 closure decision 1 (Manifest cap timing) | `test_manifest_cap.py` (adapter only) |
| BL-E008-010 | Medium | owner | Spec proposal for the same decision 1 (`spec-proposals/AD-2.md`) | none |
| BL-E008-011 | Medium | owner | E008 closure decision 2 (read-side memory at the 256 MiB cap) | none |
| BL-E008-012 | Medium | owner | E008 closure decision 3 (`on_warning` coverage) | none |
| BL-E008-014 | Medium | owner | E008 closure decision 5 (restored Site code unconfined in the relocation `wp` child) | none |

Also listed for the owner (Low, not resolved here): BL-E008-013 (`--list-default-excludes` needs `--wp-dir`).

## Integration-tested code touched

- `adapters/tar.py`: the backup writer's `_add_file` and the extraction filter. `tests/integration` backup and restore
  runs exercise both.
- `adapters/staging.py`: `_cutover`, `_drop_staging`, `_read_back`. `tests/integration/test_kill_mid_restore.py` and the
  restore CLI tests exercise them.
- `adapters/restore_runner.py` (`_snapshot`), `adapters/site_files.py`, `adapters/config_writer.py`: the real restore path.
- `adapters/noop.py`, `adapters/phase.py`: `tests/integration/test_dry_run_restore.py`, `test_dry_run_backup.py`.
- `cli/backup.py` (exclusion, trailer, `--whatIf`), `cli/parser.py`: `tests/integration/test_dry_run_backup.py` was
  updated for the trailer.
- `adapters/database.py`, `adapters/lock.py`, `cli/restore.py`, `cli/doctor.py`: only the missing-Site-Root message now
  shows the absolute path.

The DB-gated integration tests did not run here (137 skipped, `DB_HOST` unset), so `make ci` must run them.

## Owner decisions (items left open)

1. **Root restore: extraction symlink race (BL-E002-098, High).** `extractall` into a Site Root another user can write
   leaves a window between the data filter's check and its open. A site-user foothold can turn that window into a root
   file write. Options:
   - (a) extract into a private `0700` directory on the Site Root's filesystem, then move entries into place with
     `os.rename(..., src_dir_fd=, dst_dir_fd=)` along an `O_NOFOLLOW` directory-fd walk;
   - (b) when euid is 0, run CLEAN and EXTRACT as the Site Root's owner;
   - (c) refuse a root restore into a tree writable by another uid unless a flag allows it;
   - (d) accept it and document it next to O4(a).

   **Recommendation: (a)**, recorded in a new ADR beside the CVE-2024-12718 capability check. It needs no privilege
   handling, and it closes the window for every caller.
2. **Operator credentials sent to the Site's `DB_HOST` (BL-E002-063).** Options:
   - (a) refuse when the credentials come from an operator source (AWS secret or environment) and `DB_HOST` is not
     local, unless a flag confirms the host;
   - (b) take the host from the AWS secret when it carries one;
   - (c) warn only;
   - (d) accept: the operator chose the Site.

   **Recommendation: (b), falling back to (a).** This mirrors the restore side's `--archive-database` confirmation
   (BL-E003-069).
3. **Backup target inside the Site Root (BL-E002-064).** Self-inclusion is fixed (BL-E002-100). What remains is that
   the Archive sits in the docroot. Options:
   - (a) refuse it (USAGE) unless a flag allows it. This changes the v0.2.21 default `cd docroot && wpbackup2 backup`,
     so under AD-22 it needs a named release-note entry;
   - (b) WARNING naming the path;
   - (c) keep as is.

   **Recommendation: (b).** It keeps existing cron jobs working, and the Archive is mode `0600`.
4. **Plaintext left after an uncatchable kill (BL-E002-065).** Options:
   - (a) at startup, sweep the invoking user's own stale `l3io-wp-backup-*` workspaces and `.wpbackup2-*.partial`
     files older than a threshold (24 h), and log a count;
   - (b) also route SIGHUP and SIGQUIT through the SIGTERM handler;
   - (c) document only.

   **Recommendation: (a) and (b).** The age threshold and the uid check keep the sweep away from live Runs.
5. **Exit-code corrections (BL-E002-069 AR-03/AR-05, BL-E002-074 UX-01-3, BL-E002-103 UX-11).**
   - AR-03: a member-level `ExtractionRefusedError` exits 3 ("nothing was done") after CLEAN and a partial write.
     Options: 8 VERIFICATION_FAILED (the payload contradicts its index), 1 FAILED, or keep 3. Keep 3 for the
     no-data-filter case either way, since that refusal comes before anything is written.
   - AR-05: a missing passphrase exits 2 (`PassphraseUnavailableError`) or 3 (`PassphraseRequiredError`). The table's
     own text in `domain/exit_codes.py` says 4 CREDENTIALS.
   - UX-01-3 and UX-11: a nonexistent `--wp-dir` or `--archive` exits 1. Options: 2 USAGE or 3 REFUSED.

   **Recommendation:** AR-03 to 8 for member refusals; AR-05 to 4; UX-01-3 and UX-11 to 2. Each change is a named
   release-note entry (AD-22).
6. **AD-23 and version proxies (BL-E002-072, spec proposal BL-E002-106).** Five sites branch on `view.payload is
   None`. Options:
   - (a) amend AD-23 so that the presence of `ArchiveView.payload` is a normalized layout fact ("the members live
     inside a payload member") that consumers may branch on. Then widen `test_version_branching.py` to forbid real
     version proxies outside `archive_format`, for example `MANIFEST_MEMBER in index.names`;
   - (b) refactor: move the v0/v1 rule-set choice into `domain/archive_format` and give the adapters a layout enum.

   **Recommendation: (a).** The remaining branches choose where the bytes are, which an optional field expresses
   honestly. Option (b) only renames the branch.
7. **Failure output on a non-TTY (BL-E002-103 UX-5).** With JSON logs, a failure is reported only as a JSON record.
   Options:
   - (a) always also print the plain `wpbackup2: error:` line on stderr;
   - (b) keep JSON-only stderr and document it in `--help` and the README.

   **Recommendation: (b).** stderr stays strict JSON lines for cron and journald consumers.
8. **`check_load` runs as which account (BL-E002-104).** It requires the administrator but checks as the WordPress
   account, and E002-S02-012 and E002-S02-009:51 disagree. Options:
   - (a) drop `_require_admin` from `check_load`, and map its failures to a readiness error instead of
     `LoadFailedError`;
   - (b) keep the requirement and record why in AD-29.

   **Recommendation: (a).** The load itself runs as the WordPress account.
9. **Phase field spelling (BL-E002-109).** Backup emits `DUMP` and restore emits `load-dump`. Options:
   - (a) emit `RestorePhase.name` (upper case) in the log field, leave the journal unchanged, and list both flows in
     `docs/logging.md`;
   - (b) keep kebab case, list both sets, and amend AD-17.

   **Recommendation: (a).** The spine writes AD-17's phases in upper case, and one spelling keeps the documented
   `jq` query working. Do it before the first release.
10. **Unredacted database spans (BL-E006-010).** Options:
    - (a) fix `l3io-wordpress-database` (`_observe.py`: `record_exception=False`, class-only status), release it,
      raise the floor here and run `uv lock`;
    - (b) accept the ADR-0011 host-scrub guidance as final and resolve this item `wontfix`.

    **Recommendation: (a)**, before the family moves to PyPI.
11. **Manifest cap timing (BL-E008-003; spec proposal BL-E008-010).** The cap is refused at `WRITE_ARCHIVE`, and a
    Dry Run reports success. Options:
    - (a) amend the acceptance criteria and ADR-0019 to say so;
    - (b) add a read-only measuring query before `DUMP`, run in Dry Runs too.

    **Recommendation: (a) for v1, (b) after v1** (`epic-008/epic-closure/spec-proposals/AD-2.md`).
12. **Read-side memory at the 256 MiB cap (BL-E008-011).** Parsing takes about 4.3x the Manifest's size before
    authentication. Options:
    - (a) accept it and document the figure;
    - (b) a streaming parse;
    - (c) lower the read cap.

    **Recommendation: (a) now, (b) before larger Sites are supported.**
13. **`on_warning` coverage (BL-E008-012).** Options:
    - (a) document that `passphrase_below_encryption_floor` and `password_on_command_line` stay log-only;
    - (b) emit both through `emit_warning`.

    **Recommendation: (b).**
14. **`--list-default-excludes` needs `--wp-dir` (BL-E008-013, Low).** Options:
    - (a) keep it as is;
    - (b) make `--wp-dir` optional when the flag is given, with a post-parse check.

    **Recommendation: (b).**
15. **Restored Site code unconfined in the relocation `wp` child (BL-E008-014).** Options:
    - (a) accept it, keeping the documentation and the warning for unencrypted Archives;
    - (b) require an authenticated Archive for relocation unless a flag allows otherwise;
    - (c) optional confinement (bubblewrap or similar).

    **Recommendation: (a) for v1, (c) as an opt-in after.**
