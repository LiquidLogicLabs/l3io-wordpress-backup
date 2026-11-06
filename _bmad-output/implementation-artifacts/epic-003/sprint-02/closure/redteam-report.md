# Red team report: Epic E003, Sprint 02 (`wpbackup2 restore`)

Scope: `git diff 4d85bd3..ae7a10d` (src and docs), the stories in `epic-003/sprint-02/stories/`, and ADRs 0015, 0016, 0020 to 0024.
Method: read the code, plus three small tests (one in a throwaway MariaDB 11.4 container with no network, since removed; two inline reads of installed libraries). No source edited, nothing committed, no credential value printed.
Owner decision honoured, not reopened: ADR-0016 amendment of 2026-11-06 says an unauthenticated Archive restores with a warning. Findings below do not argue against that. They are about what an untrusted Archive can reach beyond "its own content becomes the site".
Sanctum note: `_bmad/memory/l3io-sec-redteam/` was absent and was not initialised, because the task forbids writing outside the report. The reference files were not loaded.

## Widened scope (and why)

- `l3io.wp.database` 0.3.1 in the uv cache (`transfer.py`, `_client.py`, `provision.py`, `tls.py`). The restore delegates the dump load, account provisioning and TLS policy to it, so the real attack surface sits there.
- `pymysql/connections.py` (installed copy). I needed it to see how the client answers a server that asks for a particular auth plugin.
- `adapters/tar.py` `extract_archive` and `_ExtractFilter`, `domain/archive_format.py` `member_path` and `member_relative`. Not changed this sprint, but restore is the first caller that extracts into a live Site Root after `CLEAN`.
- `cli/parser.py`, `cli/credentials.py` and `tests/contracts/test_no_secret_valued_flags.py`, to trace the administrator-password channel.

## Summary

| ID | Severity | Title |
|----|----------|-------|
| RT-S02-001 | CRITICAL | Archive dump runs shell commands as the operator (mysql client commands not disabled) |
| RT-S02-002 | HIGH | The documented safe channel for `--admin-password` does not exist, so the password goes on argv |
| RT-S02-003 | HIGH | `--archive-database` "confirmation" is satisfied by copy-paste, so a forged Archive can aim the administrator login at an attacker's server |
| RT-S02-004 | MEDIUM | `ensure` resets the password of any existing account the Archive config names, and the account is `@'%'` |
| RT-S02-005 | MEDIUM | The dump load is not scoped to the load database (cross-database statements, `USE`) |
| RT-S02-006 | LOW | `--snapshot-dir` web-served guard is skipped when the snapshot is skipped, yet `saved_config` is still written there |
| RT-S02-007 | LOW | Plaintext residue (decrypted payload, Dump, Archive copy) in `$TMPDIR` after SIGKILL or power loss |
| RT-S02-008 | LOW | Safety Snapshots and `saved_config` accumulate unencrypted with no retention, next to the journal |
| RT-S02-009 | LOW | Skipped-role members are decompressed without a size bound (CPU/time DoS on an unauthenticated Archive) |
| RT-S02-010 | LOW | A hostile Archive can park a Site behind a blocking journal (availability, by design) |

Counts: CRITICAL 1, HIGH 2, MEDIUM 2, LOW 5.

## Findings

### RT-S02-001 (CRITICAL): the Archive's dump can run shell commands as the operator

- Where: `src/l3io/wp/backup/adapters/database.py:896` (`wpdb.load(connection, self._credentials, source)`, called with no `options`). The Dump comes from the Archive via `adapters/staging.py:499-506` (`extract`, then `load_dump` at line 506).
- Reasoning: `wpdb.load` feeds the Dump to the `mysql`/`mariadb` client on stdin (`l3io/wp/database/transfer.py:920-970`). The library allows `--binary-mode` for loads (`_client.py:77`, `LOAD_OPTIONS`). This package never passes it. A grep of `src/` and `docs/` for `binary-mode`, `sandbox` and `\!` finds nothing. The only guard the library has is MariaDB's sandbox line, which is a line the dump itself must carry. The attacker simply omits it.
- PoC (run, then cleaned up): MariaDB 11.4 server and client in a container with `--network none`. A dump file whose only line is `\! echo PWNED > /w/pwn.txt`, loaded the way the library does (`--default-character-set=utf8mb4 --database=x`, stdin) created `pwn.txt` containing `PWNED`. The same file with `--binary-mode` gave `ERROR at line 1: Unknown command '\!'.` and no file. A dump starting with the sandbox line gave `Not allowed in the sandbox mode`, which is why the omission is the attack. I did not run the MySQL 8.4 client against a server. The MySQL manual documents the same client-command behaviour and the same `--binary-mode` effect, but that is not tested here.
- Impact: any unencrypted Archive, once restored, runs arbitrary commands as the user running `wpbackup2` (often root or a deploy user, with the database administrator password in its process environment). This is more than the RT-002 acceptance covers. That acceptance is about the Archive's content becoming the web site, which runs as the web user. This crosses to the operator's account. `\.`/`source` also lets the dump read local files into the staging database. A Dry Run is not affected (the load is a no-op).
- Fix: in `load_dump`, call `wpdb.load(..., options=("--binary-mode",))`. Add a contract test that loads a `\!` dump against a real client and asserts no side effect, because that is the only thing that proves the rule. Do not set `allow_unsandboxed`.

### RT-S02-002 (HIGH): the safe channel for the administrator password is not there

- Where: `cli/parser.py:66` (`ADMIN_PASSWORD_VARIABLE`) and `cli/parser.py:362-367` (help text: "prefer L3IO_WP_BACKUP_ADMIN_PASSWORD"). `adapters/database.py:534` and `:557` take `environ` and ignore it (`# noqa: ARG001 - Epic 5 binds the environment step`). `cli/credentials.py:81-109` only passes the argv value through. `rg` for the variable name finds only the definition and one test.
- Reasoning: nothing reads the variable. The only working way to give an administrator password is `--admin-password VALUE` on the command line; the AWS route raises `UsageError` ("not bound"). The help text tells the operator the opposite. `tests/contracts/test_no_secret_valued_flags.py:64` asserts only that the help string mentions the variable, so the guard checks the prose and not the behaviour (the failure your global rule 3 describes).
- Impact: the database administrator password (or `--new-db-password`) is readable by every local user through `ps` or `/proc/<pid>/cmdline` for the length of the run, which includes the whole database load. Redaction protects logs, not the process table.
- Fix: read the variable in `admin_spec` or `drive_restore` (and add a file or stdin option). Add a test that sets the variable and runs `restore` without `--admin-password`. Until then, change the help text so it stops promising it.

### RT-S02-003 (HIGH): the Archive-database "confirmation" can be copy-pasted, so the administrator login can be sent to a server the Archive names

- Where: `cli/restore.py:531-532` (the error says "Pass `--archive-database <user@host:port/name>` to confirm it") and `cli/restore.py:1147-1148` (the check). The administrator survey and TLS happen afterwards, from `site.database_location()` taken from the Archive's `wp-config.php` (`adapters/database.py:321-337`, `_parse`).
- Reasoning: when the target has no `wp-config.php` (the usual disaster-recovery case) the database host, name, user and password come from the Archive. The RT-1 control is meant to stop a forged Archive from directing the administrator at a server the operator did not name. But the refusal prints the Archive's values and gives the exact flag to paste. An operator who retries as instructed passes the check without ever comparing the host to anything they know.
- Impact: a forged Archive with `DB_HOST` set to a server the attacker owns. The default TLS is `verify_identity` against the system store, so an attacker with a valid certificate for their own host passes it. PyMySQL then does whatever authentication the server asks for. In the installed copy (`pymysql/connections.py:1058-1059`) a server that asks for `mysql_clear_password` receives `self.password + b"\0"`, the password in clear. `mysql_old_password` (weak scramble) and the `dialog` prompt are also honoured. Result: the administrator credential, and later the WordPress account password, reach the attacker. The unauthenticated warning (`cli/restore.py:1178-1187`) is printed after this check, so it does not help here. I did not stand up a hostile server; the client behaviour is read from the library source.
- Fix: do not print a paste-ready flag; print only the host and ask the operator to type the identity. Better, accept a host only if it is in an operator-supplied allow-list or equals the host the operator gave with `--admin-*`. Refuse the clear-text and old-password plugins (PyMySQL `auth_plugin_map` or an `allowed_plugins`-style option in `l3io-wordpress-database`). Add a test that a forged host never receives a login.

### RT-S02-004 (MEDIUM): `ensure` resets existing accounts named by the Archive, as `@'%'`

- Where: `adapters/database.py:119` (`_ANY_HOST = "%"`) and `:856` (`wpdb.ensure(..., user_host=_ANY_HOST)`); library `provision.py:95-106` (`plan_ensure`: an existing user whose password does not work gets `set_password`, i.e. `ALTER USER user@'%' IDENTIFIED BY <config password>`), then `GRANT ALL ... ON <staging>.*`.
- Reasoning: on the no-config path `DB_USER` and `DB_PASSWORD` come from the Archive. If `DB_USER` names an account that already exists on that server (another application's account), its password is changed to the value in the Archive and it is granted all privileges on the staging database. The only gate is RT-S02-003's confirmation. Separately, accounts this restore creates are `@'%'`, reachable from any host, which is wider than WordPress needs. `check_load`/`survey` report `user_exists`, but nothing refuses on it.
- Impact: lock-out of another application and a known password on its account (limited to the staging database's grants plus whatever that account already had).
- Fix: refuse (or require an explicit flag for) `set_password` on a user that existed before the run when the credentials came from the Archive. Make the account host configurable, defaulting to the connection's own host.

### RT-S02-005 (MEDIUM): the load is not confined to the load database

- Where: `adapters/database.py:896`; `adapters/staging.py:506-507`.
- Reasoning: the dump runs as the WordPress account. The tool's own dumps avoid `USE` and `CREATE DATABASE` (`_client.py:239`), but nothing stops a crafted dump from containing `USE`/`db`.`table` references. During `LOAD_DUMP` that account still holds `ALL` on the live database (the revoke happens after the cutover, `adapters/staging.py:446-451`) and on any other database it was granted. The post-load check (`:328-345`) only looks at the staging database.
- Impact: a hostile Archive can damage the live database or others sharing the account before the cutover, even when the staging database later fails its check. In modes that replace the database the live data is destroyed anyway, so the added harm is the other databases and the pre-cutover timing (the Safety Snapshot was taken before, so recovery exists unless `--no-safety-snapshot`).
- Fix: after RT-S02-001, add a pre-load structural scan the library supports, or load as an account granted on the load database only. Record the residual risk in ADR-0022.

### RT-S02-006 (LOW): the snapshot-location guard is skipped with `--no-safety-snapshot`, but `saved_config` still lands there

- Where: `core/restore.py:1417-1421` (`snapshot_location` is called only `if snapshot is SnapshotPolicy.TAKE`); `cli/restore.py:1229-1233` (`saved_config` is built from `snapshot_dir` whenever `--overwrite-wp-config` is set); `adapters/config_writer.py:230-264` (`save_copy`).
- Reasoning: `--no-safety-snapshot --overwrite-wp-config --snapshot-dir <inside the web root>` skips the check and writes a copy of the target's `wp-config.php` (database password in it) into the directory. The file is `0600`, and its name ends `.php`, so it is more likely executed than served. The damage is limited to a copy sitting where the guard says it must not.
- Fix: run `snapshot_location` whenever `saved_config` is set, not only on `TAKE`.

### RT-S02-007 (LOW): plaintext residue after an uncatchable kill

- Where: `adapters/workspace.py:31-46`, `cli/restore.py:_bind_archive`, `adapters/restore_runner.py:_verify` (`decrypt_payload`), `adapters/staging.py:499-506` (Dump extracted).
- Reasoning: the Run workspace in `$TMPDIR` holds the Archive copy, the decrypted payload of an encrypted Archive, and the plain Dump. SIGTERM and SIGINT are handled (`cli/__init__.py:59-74`) and the workspace is removed. SIGKILL, the OOM killer or power loss leave all of it. The directory is `0700` and the files `0600`, so exposure is to the same user and root, and to anything that copies `/tmp` (backups, a core dump).
- Fix: place the workspace on a path the operator chooses and document it, or sweep stale `l3io-wp-backup-*` directories at the next start. The existing kill test (`tests/integration/test_kill_mid_restore.py`) is the place to assert the residue is cleaned or flagged.

### RT-S02-008 (LOW): unencrypted snapshots with no retention

- Where: `cli/restore.py:1272-1281` (`take_snapshot`, `safety_snapshot_unencrypted` warning), `:832-838` (default directory is the journal directory).
- Reasoning: without a passphrase the Safety Snapshot (full file tree, `wp-config.php`, database dump) is written in clear to `~/.local/state/l3io-wp-backup/snapshots/<sha256>/`. The directory is `0700` and the file `0600` (`adapters/store.py`, `mkstemp`), and the operator is warned. Nothing prunes old snapshots or saved configs, so every restore adds a full copy of the Site's secrets. Accepted by design; recorded so it is not lost.
- Fix: a retention setting or a printed list of snapshots older than N days.

### RT-S02-009 (LOW): decompression cost is unbounded for members that are not extracted

- Where: `adapters/tar.py:_ExtractFilter.__call__` (the role check returns `None` before the header-size check, lines near 1010-1025), `_inner` (streams the whole payload).
- Reasoning: the size cap applies only to members of the roles being extracted. A member in another role is read through and discarded. An unencrypted payload is not hashed (ADR-0024, `Integrity.STRUCTURAL_ONLY`), so its real size need not match the index. A small Archive can make the decompressor run for a long time while holding the Site Lock and, in modes that already ran `CLEAN`, after the Site has been emptied.
- Fix: count the bytes read from the stream and stop at a multiple of the indexed total.

### RT-S02-010 (LOW): a hostile Archive can leave the Site blocked

- Where: `core/restore.py:736-750` (`is_orderly_failure`), `adapters/journal.py:370-374`, `adapters/restore_runner.py:386-392`.
- Reasoning: a failure inside `LOAD_DUMP` that is not a `check_failed` (for example a corrupt dump mid-stream) leaves the journal in `current.json`, and every later restore of that Site refuses until `--resolve-interrupted-run`. This is deliberate, and the operator can resolve it, so it is only an annoyance. Noted because a forged Archive can trigger it on demand.

## Checked and found sound

- Extraction: `member_path` rejects absolute names, `..`, `.`, empty segments, NUL and backslash; every member must be in the index and under a role prefix; hard links are refused; symlinks that point outside are skipped and reported; a member below an Archive-written symlink is refused; the header size cannot exceed the indexed size; the running total is capped; `data_filter` removes setuid/setgid and special files. `require_patched_tarfile` runs in `PREFLIGHT`, before `CLEAN`.
- Archive TOCTOU (RT-001): the Archive is copied into the `0700` workspace (`cli/restore.py:_bind_archive`, `O_EXCL|O_NOFOLLOW`) and `VERIFY` compares the new view with the planned one, so a swap of the original after the copy has no effect.
- Journal: `0700` directory, `0600` file, `O_NOFOLLOW`, write-fsync-replace-fsync, refuses to replace an existing record, unreadable record refuses rather than reading as absent, `DB_PASSWORD` cannot be journaled (`JOURNALED_CONSTANTS`).
- Credentials: the mysql client receives its option file on `/dev/fd/N`, not on argv; passwords are registered with the redaction registry before use; `DB_PASSWORD` is held for one write and cleared (`staging.py:378-389`); error classes carry names and flags, not library text.
- Config rewrite: all constants go through `l3io-wordpress-config` (`WpConfigFile.set`, `WpConfigString.set`), which refuses unrenderable values; owner and mode kept or refused; atomic replace with read-back verification.
- `CLEAN`: `rmtree` does not follow symlinks, the root is never removed, `/` and the home directory are refused, and a directory with neither marker is refused. It is a heuristic guard and says so.
- Signals: SIGTERM raises `RunTerminated` and the lock and workspace are released; a kill leaves the journal blocking, and the next run reads it.
- TLS to the administrator: verified TLS for every host except `localhost`, `127.0.0.1` and `::1`; `--admin-tls` can only raise the policy.

## Not examined

- `legacy/` (the old `wpbackup`).
- Epic 7 relocation (`RESTORE_EXTENSION` is `None` in this build; the relocation flags are rejected as not implemented).
- The behaviour of a real hostile database server (RT-S02-003 rests on the client source).
- The MySQL 8.x client for RT-S02-001 (tested on the MariaDB 11.4 client only).
