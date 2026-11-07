# Red team report: epic E007 (move a restored WordPress site to a new URL)

Scope: epic level. Read-only analysis; this file is the only thing written. Throwaway proofs are in `/tmp/e007-rt/` (`p1.py`, `p2.py`), outside the repo.

Method notes: the sanctum bootstrap and live platform research steps of the skill were not run. The contract is read-only and the surface is local (WP-CLI, tarfile, procfs), not a cloud platform. Findings were verified in code at HEAD `f1cf076`. Where a claim depends on WP-CLI internals (no `wp` binary is installed on this host), it is marked PLAUSIBLE.

## Widened to, and why

| Area | Why |
|---|---|
| `adapters/tar.py` (`_ExtractFilter`, `extract_archive`) | Who sets the mode and name of a laid-down `wp-config.php`. |
| `adapters/preflight.py` (`_locate`, `_version_output`) | Binary resolution, probe cwd and env, timeout parity with `run_child`. |
| `cli/parser.py`, `cli/passphrase.py`, `docs/restore.md` | Credential channels (argv, env) and what the docs promise about them. |
| `adapters/journal.py` (`journal_directory`), `adapters/workspace.py` | State directory used for the `wp` cwd. No finding. |
| `core/relocation.py` `replacement_pairs`, `adapters/wpcli.py` `_Session.stored` | The only untrusted-to-argv data flow (stored `siteurl`/`home`). |

Inputs not examined: `l3io.wp.config` internals (external library; how its atomic replace treats a symlinked config is unknown), and WP-CLI source.

Owner decisions respected and not reported: O1(a), O3(a), O4(a), ADR-0015, ADR-0018, ADR-0023, `docs/closeout-gate.yaml`. Already-stated residuals (PATH lookup TOCTOU between the two `locate_tool` calls, the parent-directory lay-down TOCTOU, the restored PHP running inside WP-CLI) are not repeated, except where this report shows the stated mitigation does not deliver what the prose claims.

## Findings

### RT-1 (HIGH): restored PHP can read the parent's environment, so "minimal child env" does not protect secrets from the code it was built to isolate them from

- File: `src/l3io/wp/backup/adapters/wpcli.py:185-197` (`wp_cli_environment`), `:250-264` (`run_child`). Overstating prose: `docs/restore.md` "The environment it gets", lines 285-290.
- Attack: a crafted Archive carries a `wp-config.php`, mu-plugin or `wp-content/db.php` that runs `file_get_contents('/proc/'.posix_getppid().'/environ')`. WP-CLI executes it as the same OS user as the restore (O4(a)). `/proc/<ppid>/environ` is the parent's exec-time environment and is readable by the same uid. The parent holds `L3IO_WP_BACKUP_PASSPHRASE` when passed by env (`cli/passphrase.py:7` already notes this exposure), the admin DB credentials from `os.environ` (`cli/restore.py:1226`, `:1241`), and any cloud credentials in the operator's shell. The code can send them to an attacker. `/proc/<ppid>/cmdline` is world-readable and carries `--admin-password` and `--new-db-password`; that exposure is accepted under AD-22 and is not reported here.
- Proof: `/tmp/e007-rt/p2.py` spawns a child with exactly the `wp_cli_environment` keys. The child read the parent's `/proc/<ppid>/environ` and found `L3IO_WP_BACKUP_PASSPHRASE=` in it. With `prctl(PR_SET_DUMPABLE, 0)` set in the parent first, the same read failed with `PermissionError`. The host has `kernel.yama.ptrace_scope=1`, which does not stop this read.
- Reachability: any Archive the operator restores with `--new-site-url` or `--new-site-home-url`. The Archive author controls the input. Unencrypted Archives are explicitly unauthenticated (`archive_unauthenticated` warning).
- Why it matters: BL-E002-117 and the doc state that the passphrase and `L3IO_WP_BACKUP_*` are "not passed on". True of inheritance, false of exposure. The doc's coverage statement is stronger than the control (global rule 3).
- Fix (partial but cheap):
  - Call `prctl(PR_SET_DUMPABLE, 0)` in the parent for the duration of the `wp` child (ctypes, Linux only; no-op on root). Do it right before `subprocess.run`.
  - Reword `docs/restore.md` to say the env is minimal but same-user code can still read the parent's `/proc` entries; `cmdline` stays readable.
  - Bigger option: run `wp` under a separate uid or a sandbox (bwrap or systemd-run). That is an owner decision beyond O3(a).
- Test impact: `run_child` is the injected `ChildRunner` seam. Unit tests that record the call are unaffected. `tests/integration/test_search_replace.py` runs the real `run_child` and would exercise a `prctl` change. A new test with a PHP stub reading `/proc/<ppid>/environ` is needed to prove the fix.

### RT-2 (MEDIUM): no timeout and unbounded output capture on the `wp` child, while the Site Lock and an open journal are held

- File: `src/l3io/wp/backup/adapters/wpcli.py:250-264` (`run_child`: no `timeout=`, `capture_output=True`). Contrast `adapters/preflight.py:187-197`, whose probe has `_PROBE_TIMEOUT_S`.
- Attack: restored `wp-config.php` or an mu-plugin does `sleep(PHP_INT_MAX)` or loops. The restore hangs forever in `SEARCH_REPLACE`. The Site Lock stays held, the journal stays open, the site has the new DB and config but old content URLs. Or it prints gigabytes to stdout or stderr, which Python buffers entirely in memory (`capture_output`, `text=True`) until OOM. A kill leaves an interrupted Run that the next restore refuses until `--resolve-interrupted-run`.
- Reachability: the Archive author. A legitimate large `search-replace` can take long, so a fixed small timeout is wrong; this needs a generous, documented ceiling.
- Fix: `timeout=` on `subprocess.run` (configurable, generous; map `TimeoutExpired` to `WpCliFailedError` with `step` and an "owed" hint). For output, redirect stdout and stderr to files in the 0700 `wp-cli-*` directory and read a bounded tail and a bounded stdout (`option list` JSON and the preview table are small; a cap such as 8 MiB is generous).
- Test impact: touches `run_child` and `_Session.call`. Unit tests inject a `ChildRunner` and are unaffected. The real-runner integration test (`test_search_replace.py`) does exercise it.

### RT-3 (MEDIUM): a malicious Archive can shadow the laid-down parent `wp-config.php` with its own `<root>/wp-config.php`, and the relocation is applied to a config WordPress never reads

- File: `src/l3io/wp/backup/adapters/restore_runner.py:762-776` (`keeps = run.config_existed or laid_in_root`, `exclude=_KEPT_CONFIG if keeps else frozenset()`). Placement logic: `core/config_location.py:112-180`; `adapters/wpcli.py:811-833`.
- Attack: Archive Manifest `config_path` = `../wp-config.php`, a `config`-role member (attacker content A), and a Site Root tree member `wp-config.php` (attacker content B). Target has no config. `place_config` returns PARENT/lay-down, so A is extracted to `<root>/../wp-config.php`. The tree extraction (`SITE_ROOT` role) excludes `wp-config.php` only when `config_existed` or the config was laid in the root. Neither holds, so B is written to `<root>/wp-config.php`. WordPress prefers the root file (`existing_location` order). `REWRITE_CONFIG` and the cutover then write `WP_SITEURL`, `WP_HOME` and the relocated `DB_*` (including `--new-db-password`) into A, which is dead. `wp search-replace` bootstraps from B, whose `DB_HOST`/`DB_NAME` the attacker chose.
- Reachability: Archive author. No verification step rejects a tree `wp-config.php` next to a PARENT Manifest. The resolve-time `loaded` check in `_config_file` only guards a stated `--wp-config`.
- Impact: the operator's relocation and credential flags are silently not applied to the live config; the real WordPress runs on attacker-chosen DB settings. The attacker already controls restored code, so this is an integrity and silent-misconfiguration issue, not a new privilege.
- Fix: when `lay_down_config` places the config outside the Site Root, always pass `exclude={wp-config.php}` for the `SITE_ROOT` extraction (a PARENT placement means the root must not hold one), or refuse with `ConfigLocationConflictError` in `place_config`/PREFLIGHT when the view has a root-level `wp-config.php` member. The refusal is preferable (fails before touching anything).
- Test impact: `tests/integration/test_config_outside_root.py` and `test_config_parent_resolve.py` exercise this path. A new Archive fixture with both members is needed.

### RT-4 (MEDIUM): a laid-down `wp-config.php` takes its mode from the Archive; `--new-db-password` can end up in a world-readable file, and the response is only a log line

- File: `src/l3io/wp/backup/adapters/restore_runner.py:750-761` (config-role extract), `:787-806` (`_log_laid_down`: WARNING only); `adapters/tar.py:1110-1210` (`data_filter` keeps read bits).
- Attack: the config-role member's tar header says mode 0666 or 0644. Proof (`/tmp/e007-rt/p1.py`): `tarfile.extractall(..., filter="data")` with a 0666 member and umask 022 yields 0644. `config_laid_down` logs, `config_world_readable` warns, and nothing tightens it. The cutover then writes the operator-supplied `--new-db-password` (and the archive's own `DB_PASSWORD`) into that file via `set_constants`, which keeps the mode. Any local user can read it.
- Reachability: Archive author plus a multi-user host. Legitimate backups from this tool stage config at 0600 (`_STAGED_MODE`, `tar.py:141`), so only crafted or foreign Archives hit this.
- Fix: after the lay-down extract, `os.chmod(config_file, 0o600)` before REWRITE_CONFIG (or force it in the extract filter for `MemberRole.CONFIG`: `filtered.mode = 0o600`). Keep the warning only for the in-tree path.
- Test impact: `_log_laid_down` and the lay-down integration tests (`test_config_outside_root.py`) touch it; assert mode 0600.

### RT-5 (MEDIUM): the stored `siteurl`/`home` read back from the (untrusted) database is used as an argv positional with no validation

- File: `src/l3io/wp/backup/adapters/wpcli.py:381-404` (`_Session.stored` accepts any non-empty string), `:218-234` (`search_replace_argv`), `core/relocation.py:338-362`. Only the operator's new URLs go through `_url_rule_broken` (`core/relocation.py:141-159`), which rejects a leading `-`.
- Attack: the dump sets `siteurl` to `--require=/path/x.php` or `--export=/path/out.sql`. It becomes `old` in `wp search-replace <old> <new> ...`. Proof (`/tmp/e007-rt/p1.py`): `_Session.stored` returns it and `search_replace_argv` emits `['wp', ..., 'search-replace', '--export=/tmp/e007-rt/pwned.sql', 'https://new.example', ...]`. WP-CLI parses `--x=y` anywhere in argv as a global or assoc argument, and I do not believe it honours a `--` terminator (PLAUSIBLE, not tested here), so `--` is not a sufficient fix. Control characters (ANSI, newline) in the stored value also reach logs and the operator terminal, because `old` is logged in `search_replace_home_moved`, `search_replace_completed` and the debug argv, and printed in the report.
- Reachability: Archive author, in the dump. Delta over what the attacker already has (PHP execution in WP-CLI) is small, so this is not rated higher. It still breaks the stated invariant that argv injection through URLs is closed, and it lets the attacker redirect the operator-privileged `wp` to write to a chosen path through a path that is not PHP.
- Fix: validate `stored` values with the same `_url_rule_broken` string rules (allow a trailing `/`, since stored values legitimately may have one; reject leading `-`, whitespace, control and format characters, and require an http(s) scheme). On failure raise `StoredUrlMissingError`-style refusal before any replacement (`read_urls` precedes all writes). Strip or `repr()` the value in log fields.
- Test impact: `core/relocation.py` and `_Session.stored` are exercised by `tests/integration/test_search_replace.py` (real stored values `http://...`); a valid-URL path is unchanged. Add a negative case.

### RT-6 (LOW): the `wp` child gets the unfiltered `PATH`, while the checked `wp` was found over absolute entries only

- File: `src/l3io/wp/backup/adapters/wpcli.py:192-194` (`"PATH": os.environ.get("PATH", os.defpath)`), versus `adapters/preflight.py:240-249` (`_locate` drops empty and relative entries).
- Attack: `wp` is `#!/usr/bin/env php`-style and resolves `php` through the child's raw `PATH`. An empty, `.` or relative entry resolves against the child's cwd. That cwd is the fresh empty 0700 `wp-cli-*` directory, so no planted binary can win today. The two lookup rules still disagree, which is the same class of drift the E007-S01-004 residual note describes. If the cwd ever changes (for example to the Site Root), the gap becomes a hijack.
- Fix: pass `os.pathsep.join(_absolute_entries(PATH))` to the child. One line.
- Test impact: `wp_cli_environment` has unit tests asserting the exact env; they would need the filtered value.

### RT-7 (LOW): child stderr tail is placed in the exception message verbatim

- File: `src/l3io/wp/backup/adapters/wpcli.py:352-355`, `domain/errors.py:729-735`.
- Attack: restored PHP writes ANSI/OSC escape sequences or fake "Success:" lines to stderr and exits non-zero. The last 20 lines, redacted only for registered secrets, are printed on the operator's terminal as the error. Terminal escape injection (title, clipboard OSC 52 on some terminals) and message spoofing. Needs a non-zero exit, which the attacker controls.
- Fix: neutralise control characters (`str.isprintable`-based, keep `\n`/`\t`) in the tail before it is stored; cap line length.
- Test impact: only error-rendering unit tests.

### RT-8 (LOW): the Archive's Manifest can cause a file to be created in the parent of the Site Root

- File: `core/config_location.py:112-180`, `adapters/wpcli.py:811-833`.
- Attack: `config_path: "../wp-config.php"` selects PARENT. If `<root>/..` has no `wp-config.php` and no `wp-settings.php`, a file with Archive-chosen content is created outside the Site Root, outside the Site Lock's coverage and outside the Safety Snapshot. The name is fixed (an enum), and existing files are never overwritten (`lexists` check), so it is bounded. This is the designed FR-29 behaviour, noted here because it is Archive-triggered and not confined to the Site Root; combined with RT-4 it is world-readable.
- Fix: none required beyond RT-4. Optionally, require an explicit `--wp-config` or a flag to lay down outside the root when the target had no config and the Archive requests PARENT.
- Test impact: lay-down integration tests.

## Observations

- **OBS-1 (disagree with O4(a), root warns only).** As root, the restored Archive's PHP runs as root with `--allow-root`. RT-1 shows that code can read the parent's whole environment and, as root, anything on the host. A warning is easy to miss in unattended runs (cron, orchestrators). I would make root refuse unless an explicit `--allow-root-wp-cli` style flag is given. Decision stands as final; recorded only as disagreement.
- **OBS-2.** `--new-db-password` still travels on argv (accepted under AD-22; help says so). An env or file-descriptor alternative for restore, or a refusal when stdin is a terminal, would remove the only credential on the process list that this epic owns.
- **OBS-3.** `docs/restore.md` line 285-292 and the module docstring state env isolation without the same-user `/proc` caveat; see RT-1. Rule 3 applies: the prose claims more than the check covers.
- **OBS-4.** `find_wp_cli_config` ancestor scan covers the state directory chain, so a `wp-cli.yml` in `$HOME` refuses every relocating restore for that user (exit 3). Safe behaviour, but an operator DoS by a pre-planted file. No change suggested.
- **OBS-5.** Checked and found sound: no shell anywhere on the `wp` path (list argv, `shlex.join` only for journal text); `WP_SITEURL`/`WP_HOME` input rejects quotes, backslash, whitespace and control characters before they reach the config writer; `replace_file` writes the temp file after `fchown`/`fchmod` and `os.replace`s it, so no credential-bearing file is ever more permissive than the original; the `wp` cwd and `WP_CLI_CONFIG_PATH` file are fresh 0700/0600; preflight (`required_tools`) runs before any effect (`_CHECK_PHASES`); the Manifest `config_path` selects an enum member and is never joined to a path.

## Summary

CRITICAL: 0, HIGH: 1, MEDIUM: 4, LOW: 3, OBSERVATION: 5
