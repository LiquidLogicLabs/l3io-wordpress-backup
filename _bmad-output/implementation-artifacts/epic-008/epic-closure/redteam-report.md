# RED TEAM FINDINGS REPORT

Scope: EPIC E008 (composition root, whole-product surface) of l3io-wordpress-backup
Date: 2026-11-07
Analyst: Red Team Agent (l3io-sec-redteam), orchestrator invocation

## Severity summary

| Severity | Count |
|----------|-------|
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 1 |
| LOW | 5 |
| OBSERVATION | 3 |

Paths below are relative to `src/l3io/wp/backup/`.

## Scope and method

Seed: commit 14029e8, story E008-S01-001, ADRs 0016/0018/0019/0023/0024/0025, the prior review.
Widened (reason in brackets): `adapters/tar.py` extraction filter and `adapters/restore_runner.py`
[archive extraction and file modes are in the focus list]; `adapters/config_writer.py`
[DB password written to wp-config.php]; `adapters/lock.py`, `adapters/journal.py`,
`adapters/store.py`, `adapters/site_files.py` [Site Lock, permissions, Clean]; and the installed
`l3io/wp/database` package (`_passphrase.py`, `_client.py`, `transfer.py`) in `.venv`
[the passphrase backend and the dump load are where secrets leave this package].
Static analysis only; no code was run and no credential value was read or printed.

Checked and found sound (so the next reader does not repeat it): `Secret` (no repr/str/hash/pickle
leak; constant-time eq); `SecretRegistry` (escaped forms, span merging, exception walking);
`resolve_passphrase` chain and the 12-char encryption floor; wp-cli argv (list, never a shell; a leading
`-` refused for both new and stored URLs); wp-cli environment (4 keys only) and the dumpable guard;
`PATH` relative-entry refusal for `age`; tar extraction (data_filter, hard links refused, duplicate
names, member below a symlink, header size vs index, running cap, extended-header bound, patched-tarfile
capability check, `MAX_EXPANSION_RATIO` and free-space check before CLEAN, v0 via header sizes); the
verified private copy of the archive (TOCTOU); lock/journal opened `O_NOFOLLOW`, files `0600`;
published archive and Safety Snapshot `0600` and checked; archive-supplied wp-config reaching the admin
only after `--archive-database` confirmation; dump loaded as the WordPress account, not the administrator.

## Findings

### RT-01 | MEDIUM | Restored Site code runs with the operator's full privileges during relocation
Lens: DAR / EXT (hostile or substituted archive)

- Where: `adapters/wpcli.py:202-214` (`wp_cli_environment` passes `HOME`), `adapters/wpcli.py:333-352`
  (`run_child`), `adapters/restore_runner.py` (EXTRACT precedes the relocation `wp` calls);
  `cli/restore.py:1341-1360` (unencrypted Archive proceeds with a warning).
- Evidence: `docs/restore.md:287-289` states that `wp-config.php`, `wp-content/mu-plugins/*` (loaded
  even with `--skip-plugins`) and drop-ins such as `wp-content/db.php` execute in the `wp` child. The
  child is the same uid as the CLI, with a real `HOME`, no filesystem confinement, and (with
  `--allow-root`) possibly uid 0. The `PR_SET_DUMPABLE` guard protects `/proc/<ppid>/environ` and
  ptrace only; it does not stop the code reading files by path.
- Attack path: an attacker who can write the Storage Target (or who swaps it) places an unencrypted
  Archive (accepted by default with a warning; an unencrypted Archive has no authenticity, RT-002
  waiver) or one encrypted with a passphrase they know. It carries `wp-content/mu-plugins/x.php`. Any
  restore with `--new-site-url` or `--new-site-home-url` runs it as the operator, which can read
  `~/.aws/credentials` (the AWS Secrets Manager principal), the Infisical token under
  `$XDG_RUNTIME_DIR`, `~/.ssh`, other Sites' `wp-config.php`, the Safety Snapshot and the journal, and
  can exfiltrate them.
- Impact: credential theft and code execution as the operator from a single relocating restore.
  Accepted in docs as a trust assumption, but the compensating controls (warning only) are weak for the
  default unencrypted case, and nothing limits the blast radius even for encrypted Archives.
- Fix (any one reduces it; first is strongest): run the `wp` child confined (bubblewrap/`unshare`,
  read-only bind of everything except the Site Root, no network, empty `HOME`); or refuse relocation of
  an unauthenticated Archive unless `--allow-unencrypted` is given explicitly; or set the child's `HOME`
  to the private `wp-cli-*` directory and state in `docs/restore.md` that it is not a confinement. Add a
  contract test that plants a mu-plugin which tries to read a sentinel file outside the Site Root.

### RT-02 | LOW | A config laid down from the tree keeps the Archive's mode, then receives a DB password
Lens: ABU / EXT

- Where: `adapters/restore_runner.py:744-760` (`_restrict_laid_down` is called only when
  `by_role`); the tree path (`lay_down and not by_role`, `:760-785`, and `_check_config` `:431-440`).
- Evidence: only the config-role branch calls `_restrict_laid_down` ("take every permission from other
  off"). When the Archive has no `config` member (a v0 Archive, or one without the role) `wp-config.php`
  comes out of the tree extraction with `data_filter`'s mode (group/other write stripped, other read
  kept). The cutover then writes the relocated account's password into it.
- Attack path: Archive stores `wp-config.php` as 0644 or 0604; restore with `--new-db-user` and
  `--new-db-password`; any local user reads the new password.
- Impact: local disclosure of the relocated DB account password; limited to hosts with other local users.
- Fix: call `_restrict_laid_down(config_file)` after the tree extraction whenever `lay_down` is true and
  the config file exists (move it out of the `if by_role` block); add a test with a 0644 tree config.

### RT-03 | LOW | `--admin-password` on argv gives no runtime warning (`--new-db-password` does)
Lens: PBR / ABU

- Where: `cli/credentials.py:179` logs `password_on_command_line` for `--new-db-password` only;
  `cli/credentials.py:admin_spec` and `cli/parser.py:419-426` (help text only).
- Evidence: `grep password_on_command_line` has one hit. The value is the database administrator's
  password, a higher-value secret than the relocated account's.
- Impact: the administrator password is visible to every local user in `ps` / `/proc/<pid>/cmdline`
  with no signal at run time; the help text is the only mitigation.
- Fix: log the same WARNING (flag name only) when `args.admin_password` is not None; consider the same
  for any future value flag. Test: the warning is emitted and carries no value.

### RT-04 | LOW | `--new-db-password-file` is read with no type, mode or owner check
Lens: ABU / CHA (adjacent to BL-E008-006, which covers size only)

- Where: `cli/credentials.py:138-149` (`_read_password_file`).
- Evidence: `path.open().readline()`: follows symlinks, accepts a FIFO or a character device, and
  accepts a group- or world-readable file without comment. A FIFO with no writer blocks forever while
  the Site Lock is held (the lock is taken before the password is resolved in `drive_restore`).
- Impact: a mis-permissioned secret file is silently trusted (the common `chmod` mistake); a blocking
  path holds the Site Lock indefinitely (self-inflicted denial of service).
- Fix: `os.open(..., O_RDONLY | O_NONBLOCK | O_NOFOLLOW)` then `fstat`: require a regular file, owned
  by the caller, and warn (or refuse) when `mode & 0o077`; fold with the size bound of BL-E008-006.

### RT-05 | LOW | The `age` CLI backend inherits the whole environment, including this tool's secrets
Lens: PBR (dependency `l3io-wordpress-database`, not this repo)

- Where: `.venv/.../l3io/wp/database/_passphrase.py:155-166` (`drop = {AGE_PASSPHRASE,
  AGE_PASSPHRASE_FILE}`; everything else passed to `age` and `age-plugin-batchpass`). Reached from
  `adapters/crypto.py:PassphraseCipher.encrypt/decrypt`.
- Evidence: the database package builds the child env from `os.environ`; it removes only age's own
  variables. `L3IO_WP_BACKUP_PASSPHRASE`, `L3IO_WP_BACKUP_DB_PASSWORD`, `L3IO_WP_BACKUP_ADMIN_PASSWORD`
  and `L3IO_WP_BACKUP_NEW_DB_PASSWORD` therefore appear in the child's `/proc/<pid>/environ` (same-uid
  readable). Its own `_client.py` child_env does the right thing for the SQL clients, so the gap is
  inconsistent. Only reachable when the `cli` backend is selected (binder absent or forced).
- Impact: narrow (same-uid readers, trusted `age` binary); matters if `age` or the plugin is replaced.
- Fix: upstream, pass a minimal env (`PATH`, `LC_ALL`, `AGE_PASSPHRASE_FD`) to the `age` child, with a
  test that none of the secret variables reach it. Until then record it in ADR-0018 "What this does not
  cover" and prefer the binder backend in `docs`.

### RT-06 | LOW | Per-user state directory ownership and mode are never verified
Lens: INS / CHA

- Where: `adapters/workspace.py:private_directories` ("existing directories are left as they are");
  `adapters/lock.py:default_lock_dir`, `adapters/journal.py:journal_directory`
  (`platformdirs.user_state_path`, which honours `XDG_STATE_HOME` and `HOME`).
- Evidence: components are created `0700`, but one that already exists is accepted with any owner or
  mode. Files inside are opened `O_NOFOLLOW` and written `0600`, which limits the damage, but the
  directory entries (journal, lock, Safety Snapshot names, `wp-cli-*` temp directory, and the
  Safety Snapshot itself) can be pre-created, replaced or listed by whoever owns or can write the
  directory.
- Attack path: the tool run via `sudo` with the invoking user's `HOME`/`XDG_STATE_HOME` preserved, or
  with `XDG_STATE_HOME` pointed at a shared path: the unprivileged owner of the directory swaps a
  component for a symlink and the privileged run writes a Safety Snapshot (and `mkstemp` files) where
  they chose.
- Impact: limited to the sudo or shared-state scenario; no effect in the plain single-user case.
- Fix: after creating or finding each state directory, `lstat` it and refuse (new `NOT_READY` error)
  unless it is a real directory owned by the effective uid with `mode & 0o077 == 0`; refuse symlinked
  components. Test by pre-creating the directory as a symlink and as 0777.

### RT-07 | OBSERVATION | `process.command_args` is safe only because no CLI caller sets it
Lens: DAR

- Where: `cli/context.py:195-231` and `adapters/tracing.py:189-193` (`redact_argv(command_args)`);
  `cli/restore.py:1200`, `cli/backup.py:603`.
- Evidence: `drive_restore` opens the Run (and the root span) with `command_args` and only afterwards
  registers `--new-db-password` (`cli/restore.py:1207`); the admin password is registered even later.
  Every current caller passes `None`/omits it (`grep command_args=` shows no `sys.argv`), so nothing
  leaks today. A future caller that binds `sys.argv` would send both passwords to the tracing backend
  unredacted, because the registry is empty when the span is created.
- Fix: register `--admin-password`/`--new-db-password` values before `open_run`, or redact flag values
  structurally (`--flag value` and `--flag=value` for the secret flag names) in `run_span`; add a
  contract test that opens a Run with `command_args=["--new-db-password", "x1234567"]` and asserts the
  span attribute.

### RT-08 | OBSERVATION | The SQL Dump is executed as the WordPress account; its trust equals the Archive's
Lens: EXT / DAR

- Where: `adapters/database.py:1039-1055` (`load_dump` loads as the WordPress account);
  `.venv/.../transfer.py` ("never parses SQL").
- Evidence: the Dump is not inspected. This is the right account (not the administrator), so the blast
  radius is that account's grants. If it holds grants on other databases (shared account across Sites),
  an unauthenticated Archive can alter them (`USE other_db; ...`).
- Fix: document in `docs/restore.md` that an unencrypted Archive's Dump runs with the WordPress
  account's full grants, and recommend one account per Site; nothing to change in code.

### RT-09 | OBSERVATION | Known items still open at this commit (not re-reported as findings)
- BL-E008-002 (`/tmp/wp-cli.yml` blocks relocating restores): `find_wp_cli_config` walks every parent of
  the state directory (`adapters/preflight.py:237-247`) and `_Session.call` refuses on a hit
  (`adapters/wpcli.py:435-441`); still reachable. It is a denial of service only, not a trust bypass:
  the check refuses and never reads the file.
- BL-E008-003 (Manifest cap after the dump): `_refuse_oversized_manifest` (`adapters/tar.py:610`) is
  still the enforcement point; not closed.
- BL-E008-004: Safety Snapshot passphrase floor, owner decision (a); `note_short_restore_passphrase`
  warns by length only. Consistent with the decision.
- BL-E008-005..009: not touched by the E008 diff.
