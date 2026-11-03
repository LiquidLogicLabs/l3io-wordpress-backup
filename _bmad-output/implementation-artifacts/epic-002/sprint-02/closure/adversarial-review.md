---
review: sprint-closure adversarial + clean-release
epic: E002
sprint: 02
range: cda0b46..dadafe1
paths: src tools pyproject.toml .github
lenses: adversarial (bmad-review), clean-release checklist
date: 2026-11-03
---

# E002 sprint 02 — adversarial closure review

The scope is `git diff cda0b46..dadafe1 -- src tools pyproject.toml .github`, 37 files and +4476/-214 lines. The review looked for defects that cross story boundaries. It does not repeat findings from `review-E002-S02-{007,008,009,011,012}.md`. Findings those reviews already raised and that the code has since fixed were checked and left out: S02-007 R1 `--exclude-dir`, R4 unknown-version fail-open and R7 Dry Run joining a real Run; S02-008 F1 `previous_*` keys; S02-009 F1, F3 and F5.

## Summary

| Severity | Count |
|---|---|
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 4 |
| LOW | 10 |

| Scope | Count | IDs |
|---|---|---|
| cross-story | 8 | AD-1, AD-2, AD-3, AD-4, AD-5, AD-7, AD-8, AD-9 |
| integration | 1 | AD-6 |
| guard-scope | 2 | AD-10, AD-12 |
| single-story | 1 | AD-11 |
| clean-release | 2 | CR-1, CR-2 |

Clean-release checklist results:
- **TODO/FIXME/XXX/HACK markers:** none in the added lines.
- **Commented-out code:** none.
- **Debug artifacts:** none. There is no `print(`, `breakpoint()` or `pdb` in the added lines.
- **Secrets or credentials:** none. No literal password, token, key or PEM was found. The `S105` suppressions sit on constant *names*.
- **Dead code:** see CR-1 and CR-2.
- The file-level `noqa` in `cli/adapter_sets.py` is already S02-012 F5.

---

## Adversarial findings

### AD-1 — MEDIUM — cross-story (S02-008 SIGTERM × tracer guard / writer) — `src/l3io/wp/backup/adapters/noop.py:185`, `src/l3io/wp/backup/adapters/tar.py:219`, `src/l3io/wp/backup/domain/errors.py:378`
- **Problem:** `RunTerminated` subclasses `BackupError(Exception)`. `cli/__init__.py`'s handler raises it from whatever frame is running when SIGTERM arrives. The guarded tracer's `GuardedTracer.call` catches `Exception`, so a SIGTERM delivered during any span start or end is swallowed: the tracer is disabled and the Run keeps going. `tar._decide` also catches `Exception`, which turns a SIGTERM arriving during an exclusion decision into an `ArchiveWriteError`. Reproduced: `GuardedTracer(NoopTracer()).call(lambda: raise RunTerminated)` returns `_FAILED` and logs `tracer_failed error=RunTerminated`.
- **Guard:** derive `RunTerminated` from `BaseException`, as `KeyboardInterrupt` is, and map it explicitly in `main()`. Or re-raise `RunTerminated` first in every broad `except Exception`. Add a test that raises `RunTerminated` from inside a tracer call and from inside `ExclusionPolicy.decide`.
- **Consequence:** FR-28 is not met at random points. `systemctl stop` is ignored until the supervisor sends SIGKILL, so the workspace is leaked and the operator gets no `FAILED` report.

### AD-2 — MEDIUM — cross-story (S02-012 no-op twins × S02-009 real adapters) — `src/l3io/wp/backup/adapters/noop.py:490`, `:536`, `:549`
- **Problem:** `NoopDatabase` repeats the real adapter's AD-29 administrator refusal so that "a Dry Run refuses where the real Run would". The other twins do not repeat the refusals their real adapters make before any effect:
  - `NoopSiteFiles.clean` skips `LocalSiteFiles._refuse_unsafe` (`site_files.py:115`), which refuses `/`, `$HOME` and a non-WordPress directory.
  - `NoopConfigWriter.replace_file` accepts a `keep` value of `None`, which the real adapter refuses at `config_writer.py:296`.
  - `NoopConfigWriter.set_constants` accepts an absent `wp-config.php`, which the real adapter refuses at `config_writer.py:208`.
- **Guard:** move the clean-target heuristic into `SiteFilesQuery` (for example `check_clean(site_root)`). It only reads, so the Dry Run would run it for real. Put the `keep`/absent-file checks in a pure function that both twins call, as `require_site_relative` already is. Add one parity test per refusal that runs the real adapter and the twin.
- **Consequence:** a `--whatIf --cleanfirst` against a mistyped `--wp-dir ~` prints a clean plan and exits `OK`. The real Run then exits `REFUSED`. The rehearsal fails at its one job.

### AD-3 — MEDIUM — cross-story / design (S02-008 Site Lock × S02-009 destructive restore ops) — `src/l3io/wp/backup/adapters/lock.py:64`, `src/l3io/wp/backup/cli/context.py:339`
- **Problem:** the lock directory is `platformdirs.user_state_path(...)`, which is **per OS user**. Two users therefore lock different files for the same Site and both proceed, for example a root cron backup and an operator restore. The lock is also keyed only on the Site Root path. Two Sites that share one database, or a restore whose target database is another Site's, are not serialized. S02-009 now supplies `drop_database`, `revoke_grants`, `load_dump` and `clean` for exactly those targets. S02-008 F4 covered path spellings only, not users or databases.
- **Guard:** offer a system-wide lock location (for example `/run/lock/l3io-wp-backup`, mode `1777` with `0600` lock files), at least for root and for restore. Add a second lock keyed on `db_host:db_port/db_name` before any database command. State the per-user limit in AD-19 until then.
- **Consequence:** once Epic 3 wires restore, a backup can read a database that a concurrent restore under another account is dropping or loading. The result is a corrupt archive or a lost database, and the "one Run per Site" guarantee looks like it held.

### AD-4 — MEDIUM — cross-story (S02-011 CLI surface × S02-007 backup driver) — `src/l3io/wp/backup/cli/backup.py:650`, `src/l3io/wp/backup/cli/__init__.py:102`, `src/l3io/wp/backup/cli/backup.py:624-634`
- **Problem:** `main()` parses and validates `backup --db-credentials-aws-*` through `wordpress_spec`, but `run_backup` hard-codes `wordpress_spec(aws_secret_id=None, aws_region=None)`. `--exclude`, `--no-default-excludes` and `--encrypt-with-passphrase` are never read either: `_cipher` always returns `None`. Only the `NOT_YET_IMPLEMENTED` entries keep these flags from being silently ignored. `test_not_yet_implemented.py` checks that listed entries exit `USAGE`. Nothing checks that a flag **removed** from the table is read by its driver.
- **Guard:** add a contract test. For every subcommand flag not in `NOT_YET_IMPLEMENTED`, a planted non-default value must change the request the driver builds (`BackupRequest`), or the flag must be in an explicit allow-list of informational flags.
- **Consequence:** a binding story (E008-S01-001) that deletes the NYI entries without touching `run_backup` would pass CI. It would then ship `--encrypt-with-passphrase` writing a **plaintext** archive, and `--db-credentials-aws-*` falling back to `wp-config.php`, with no error.

### AD-5 — LOW — cross-story (S02-007 Dry Run × S02-008 lock) — `src/l3io/wp/backup/cli/parser.py:155`, `src/l3io/wp/backup/adapters/lock.py:231-250`
- **Problem:** the `--whatIf` help says the Dry Run will "change nothing". In fact it:
  - creates `<state>/l3io-wp-backup/locks`, mode `0700`;
  - `chmod`s an existing lax directory;
  - creates a lock file;
  - writes and fsyncs a holder record;
  - is refused with `LOCKED` (exit 9) while a real Run holds the Site.
- **Guard:** reword the help and the FR-19 text, for example "changes no Site, database or Storage Target; takes the Site Lock". Mention exit 9 for a Dry Run.
- **Consequence:** an operator running `--whatIf` as a harmless probe during a cron window gets exit 9. The probe also leaves files in their home directory, which the help says it would not.

### AD-6 — LOW — integration (S02-008 lock × backup driver) — `src/l3io/wp/backup/adapters/lock.py:231-250`
- **Problem:** the lock directory defaults to the running user's home state directory. Every Run fails `NOT_READY` (`SiteLockLocationError`) in two cases:
  - **Home at or below the Site Root.** If the home directory is the Site Root or lies inside it, the lock directory is refused for being inside the Site Root. An example is www-data, whose `HOME` is `/var/www`, running `--wp-dir /var/www`, or a WordPress installed directly in `$HOME`.
  - **Home not writable.** If the home directory cannot be written, which is typical for www-data, the lock directory cannot be created.

  `lock_dir` is constructor-only, there is no CLI option, and the `XDG_STATE_HOME` escape that platformdirs honours is documented nowhere.
- **Guard:** document `XDG_STATE_HOME` in the README and in the `SiteLockLocationError` message. Alternatively, fall back to a system lock location (see AD-3).
- **Consequence:** backups that run under the web-server account fail before doing anything, and the error gives no workaround.

### AD-7 — LOW — cross-story (S02-007 preflight × Safety Snapshot entry point) — `src/l3io/wp/backup/cli/backup.py:509`
- **Problem:** `backup(..., preflight=None)` builds `DumpReadinessPreflight` with `credential_source=site.credential_source`. That is always the wp-config label (`adapters/database.py:266`). `drive_backup` passes the real `credentials.source` instead. The Safety Snapshot calls `backup()` directly (E003-S02-003), so under explicit or AWS credentials a failed `check_dump` names `wp-config.php` as the failing source.
- **Guard:** take the label from the bound `database_query`, for example a `credential_source` property on `WpDatabase`. Or make `preflight` and the label required keywords of `backup()`.
- **Consequence:** the restore operator is told to fix `wp-config.php` when the AWS secret is at fault. This is the same defect class as S02-009 F1, on a different path.

### AD-8 — LOW — cross-story (S02-009 query half × S02-007 Dry Run/preflight) — `src/l3io/wp/backup/adapters/database.py:640-650`
- **Problem:** `check_load` is a **query**, and a Dry Run runs it for real. It maps library failures through `_mapped(..., LoadFailedError)`, so a readiness-check failure surfaces as "database load failed: …". It also calls `_require_admin()` even though it checks as the WordPress account and never uses the administrator.
- **Guard:** map query failures to a readiness or provisioning error, or to `DatabaseRefusedError`. Drop `_require_admin` from `check_load`, or document why the administrator is needed there.
- **Consequence:** a Dry Run, or preflight, reports a load failure that never happened. A Run with WordPress credentials but no administrator cannot even ask whether a load would run.

### AD-9 — LOW — cross-story (S02-012 no-op module × S02-009 real adapter) — `src/l3io/wp/backup/adapters/noop.py:45`, `src/l3io/wp/backup/adapters/database.py:105`, `src/l3io/wp/backup/adapters/phase.py` (`_tracer`)
- **Problem:** `adapters/noop.py`, which holds `NoopTracer` and every Dry Run twin, now imports the real `adapters/database.py`, and through it `config_writer`, to get `ADMIN_FLAGS`/`ADMIN_SOURCE`. `phase.py` needed a function-level import to break the resulting cycle. `ADMIN_FLAGS` repeats the parser's flag spellings as a hand-kept literal, and it names only `--admin-username`/`--admin-password`. It omits `--admin-credentials-aws-*` and the documented `L3IO_WP_BACKUP_ADMIN_PASSWORD`.
- **Guard:** move `ADMIN_SOURCE` and the list of admin routes to `domain/`. Derive the routes from `parser.py`'s admin options, or keep them with the parser and inject them. Have the error name every route.
- **Consequence:** the Dry Run module is coupled to the real adapter, and import order stays fragile. `AdminCredentialsRequiredError` sends an operator using AWS secrets to the wrong flags.

### AD-10 — LOW — guard-scope (S02-011) — `tools/check_mode_defaults.py:53`
- **Problem:** `GUARDED_TYPES` is a hand-kept list of five names. The same sprint added other closed restore choices that it does not include: `Override`, `RestorePhase`, the `RestorePart` literal, and the `frozenset[MemberRole]` `roles`. Global rule 4 requires the scope to be derived from the source of truth.
- **Guard:** derive the set from `domain/restore.py` by AST: every `Enum` subclass, every `Literal` alias, and `OverrideSet`. Plant a defaulted `RestorePhase` parameter to attack the scope.
- **Consequence:** a defaulted destructive choice added in Epic 3 passes the AD-5 gate.

### AD-11 — LOW — single-story (S02-009 review-F1 fix) — `src/l3io/wp/backup/adapters/database.py:697`
- **Problem:** the F1 fix resolves the WordPress source once to get the right label, and `wpdb.ensure` then resolves it again. An AWS Secrets Manager source is fetched twice. If the secret rotates between the two fetches, the first value is checked and the second value is the one set.
- **Guard:** resolve once, then pass a fixed, already-resolved source to `wpdb.ensure`, wrapped so the registry still sees it.
- **Consequence:** an extra remote call on every ensure, and a narrow race in which the account's password differs from the one that was checked.

### AD-12 — LOW — guard-scope (S02-007 CI gate) — `.github/workflows/ci.yml:103-104`
- **Problem:** the rewritten AD-4 step pipes `grep -r … | grep -vE …`. When the scanned path is missing (a package rename), the first grep exits 2. The second grep, which has no input, exits 1, so the `if` is false and the step passes. Reproduced under both `bash -e` and `bash -eo pipefail`: the output is `GATE_PASSED`.
- **Guard:** run `test -d src/l3io/wp/backup || exit 1` first. Or capture the first grep's status and fail when it is 2. `tests/contracts/test_no_dry_run_outside_roots.py` is the stronger guard, so the CI step could be deleted instead.
- **Consequence:** the gate passes while scanning nothing.

---

## Clean-release findings

### CR-1 — LOW — clean-release (dead code, S02-012) — `src/l3io/wp/backup/domain/search_replace.py:36`
- **Problem:** `StoredUrls` is exported in `__all__`, but nothing in `src/` or `tests/` references it.
- **Guard:** delete it until Epic 7 needs it, or have Epic 7's story add it.
- **Consequence:** an unused public type ships in the domain API, and no test pins its shape.

### CR-2 — LOW — clean-release (src-unreferenced scaffolding, S02-011/S02-012) — `src/l3io/wp/backup/cli/restore.py` (`snapshot_policy_from_cli`, `config_policy_from_cli`), `src/l3io/wp/backup/domain/flag_map.py` (`authorizing_combination`), `src/l3io/wp/backup/domain/restore.py` (`is_subsequence`, `RESTORE_PHASE_ORDER`), `src/l3io/wp/backup/cli/adapter_sets.py` (`UnboundSearchReplace`, `UnboundRunJournal` are bound but only ever raise)
- **Problem:** these are public functions with no caller in `src/`, only tests. They are staged for Epic 3 and Epic 7. That is deliberate, but the sprint ships them without a marker the backlog harvest can see.
- **Guard:** add a `# E003-S02-006 binds this` (or Epic 7) marker so `/l3io-doctor` harvest records the hand-off. Or list them in the sprint closure's carry-over.
- **Consequence:** if the downstream story chooses a different path, the code stays unused and drifts.

---

## Assumptions

- "Cross-story" means the defect needs code from two or more of S02-007/008/009/011/012, or from one of them plus code that existed before the sprint.
- Severity weighs present impact against latent impact. Restore stays `NOT_YET_IMPLEMENTED`, so AD-3 and AD-4 are rated MEDIUM, not HIGH. Raise them to HIGH when E003-S02-006 binds restore or E008-S01-001 binds the backup flags.
- Tests were read only to check coverage claims (AD-4, AD-10, AD-12). Nothing was modified or committed.
