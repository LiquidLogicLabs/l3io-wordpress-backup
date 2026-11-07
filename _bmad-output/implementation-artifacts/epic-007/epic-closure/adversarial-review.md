# E007 epic closure: adversarial and clean-release review

Scope: the src, docs and CI of 91f8222, 2b3de85, c4059b6 and f1cf076, read against the current
`adapters/wpcli.py`, `core/relocation.py`, `core/config_location.py`, `adapters/restore_runner.py`,
`adapters/config_writer.py`, `adapters/preflight.py`, `cli/restore.py`. Tests were read only to judge
adequacy. The per-story reviews were not repeated. Owner decisions O1(a), O3(a), O4(a), ADR-0015
(with its 2026-11-06 amendment), ADR-0023 and `docs/closeout-gate.yaml` were treated as final.

`wp`/`php` are not installed on this host, so nothing below that depends on WP-CLI's own behaviour was
run. Each finding says which part is read from code and which part is WP-CLI behaviour taken from its
documentation.

## Findings

### AR-1 — adversarial — HIGH — The `wp` version probe in PREFLIGHT reads `wp-cli.yml` from the ancestors of `$TMPDIR`; the docs say no WP-CLI config is read

- Where: `src/l3io/wp/backup/adapters/preflight.py:22-24` (docstring), `:135-141` (`KNOWN_TOOLS["wp"]`),
  `src/l3io/wp/backup/cli/context.py:251` (workspace), `docs/restore.md:278-291`.
- Verified in code. The probe runs `wp cli version` with `cwd` set to the Run workspace. That is
  `tempfile.mkdtemp(dir=workspace_parent)`, so it sits under `$TMPDIR`, usually `/tmp`. Only
  `WP_CLI_CONFIG_PATH=/dev/null` is set, which covers the global config. There is no `find_wp_cli_config`
  check on the probe path. The same check does guard the real `wp` child (`wpcli.py:332`), whose
  directory is deliberately moved out of `$TMPDIR` for this reason.
- The preflight docstring says "never a Site Root, so no `wp-cli.yml` above it is read". `docs/restore.md`
  says `wp` "never" runs from `$TMPDIR`, and that the same holds "for the version probes". Both are false
  for ancestors of the workspace.
- Reasoning on WP-CLI behaviour, not run here: WP-CLI searches the working directory and every parent
  for `wp-cli.yml`/`wp-cli.local.yml` (the premise `wpcli.py` itself relies on), and its `require:` key
  loads PHP files.
- Scenario: a local unprivileged user creates `/tmp/wp-cli.yml` containing `require: /tmp/x.php`. An
  operator runs `restore --new-site-url ...` as root with the default `$TMPDIR`. Story 7.4 now puts `wp`
  into every URL-relocating restore's PREFLIGHT, so the probe runs `wp cli version` in
  `/tmp/l3io-XXXX`, finds `/tmp/wp-cli.yml`, and loads `x.php` as root before any check.
  `wp_cli_running_as_root` (O4) does not cover this path: the probe happens first, on a different code
  path.
- Epic seam: story 3 hardened the `wp` child and story 4 added the probe to the restore path. Each
  claims the other's property. The S01-004 review called the probe "E005's and unchanged"; it is
  unchanged, but this epic is what makes it reachable from `restore`.
- Fix: before the probe runs, apply `find_wp_cli_config` to the probe `cwd` and to its `.resolve()`, as
  `_Session.call` does, and refuse with `WpCliConfigFoundError`. Or run the probe from the same state
  directory the child uses. Add a contract test that plants a `wp-cli.yml` above the workspace and
  asserts the probe is not run. Correct the docstring and `docs/restore.md` in the same change.

### AR-2 — adversarial — MEDIUM — The `wp-cli.yml` ancestor refusal happens at SEARCH_REPLACE, after the site has been wiped, extracted and loaded

- Where: `src/l3io/wp/backup/adapters/wpcli.py:332-334` (only call site of `find_wp_cli_config`),
  `:290-305` (`_wp_cli_directory`), `:377-410`.
- Verified in code. `WpCliConfigFoundError` and the state-directory preparation error
  (`WpCliFailedError("prepare")`) are both raised inside `relocate_content`, i.e. in phase
  SEARCH_REPLACE, the last phase. `find_wp_cli_config` walks every ancestor of
  `~/.local/state/.../snapshots/<sha>/wp-cli-XXXX`. A `wp-cli.yml` in `$HOME`, `/home` or `/` is the
  likeliest trigger, and it is a state of the host that is knowable at PREFLIGHT.
- Consequence: CLEAN, EXTRACT, REWRITE_CONFIG and LOAD_DUMP (with the cutover) have already run. The
  site is left with the new DB credentials, restored files and the old URLs. It is journaled incomplete
  with the "owed" text, which tells the operator to run from a directory with no `wp-cli.yml` "in it or
  above it" — that condition cannot be satisfied if the offender is in `$HOME`. Story 4 is titled "fail
  before touching anything", and this is a checkable precondition left to the last phase. A PHP
  notice from archive code on stdout (`json.loads` failure in `stored`, `wpcli.py:381-396`) fails
  late in the same way.
- Fix: run the ancestor check, and the state-directory prepare step, from `required_tools`/PREFLIGHT for
  a URL relocation. For example, give the extension a `preflight_checks` hook that creates and removes
  the directory and runs `find_wp_cli_config` on it, so a refusal is exit 3 with nothing changed. Keep
  the check in `call` as the TOCTOU backstop.

### AR-3 — adversarial — MEDIUM — No timeout on any `wp` child; a hang holds the Site Lock with the site half-restored

- Where: `src/l3io/wp/backup/adapters/wpcli.py:250-264` (`run_child`), `_Session.call` `:327-355`.
- Verified in code: `subprocess.run(...)` has no `timeout=`, and `grep timeout wpcli.py` finds none. The
  preflight probe has a 10 s timeout; the search-replace calls, which bootstrap the restored site's
  `wp-config.php`, mu-plugins and `db.php`, have none. A drop-in that loops, a `DB_HOST` that
  black-holes (the cutover points it at an operator-supplied host), or a very large `--precise`
  replace blocks forever. Ctrl-C raises `KeyboardInterrupt` after the
  rewrite and load have been done. The SEARCH_REPLACE phase is the one most likely to run long, because
  `--precise` unserializes every row.
- Fix: a generous `timeout=` on `read_urls`/`version`/`preview` (for example 300 s) and a configurable
  one on `replace`/`recount`. Map `subprocess.TimeoutExpired` to `WpCliFailedError(step, None,
  "timed out")` and keep the existing "may have been made" semantics for the after-write steps. The
  `ChildRunner` Protocol already isolates the change.

### AR-4 — adversarial — MEDIUM — URLs read back from the restored (untrusted) database are used unvalidated as argv, in terminal output and in the journal text

- Where: `src/l3io/wp/backup/adapters/wpcli.py:381-396` (`stored`), `:138-160` (`search_replace_argv`),
  `src/l3io/wp/backup/cli/restore.py` `search_replace_line` (text mode) and `search_replace_fields`.
- Verified in code: `stored()` returns any non-empty string from the options table; the `-`/scheme/
  whitespace/control-character rules in `core/relocation.py:141-160` are applied only to the operator's
  `--new-*` values. `replacement_pairs` puts `stored.siteurl`/`stored.home` into `pair.old`, which is a
  positional argv element of `wp search-replace OLD NEW ...`.
- Reasoning on WP-CLI behaviour, not run here: WP-CLI parses `--name=value` tokens anywhere in the
  argument list as options, so an Archive whose `siteurl` is `--require=/path` or `--exec=...` has
  its value taken as a global option.
- Impact: the Archive already supplies PHP that `wp` executes (`wp-config.php`, mu-plugins), so this is
  not a new capability. It does bypass the explicit design decision to refuse `wp-cli.yml` `require:`:
  the same effect is reachable through a database row. Separately, `search_replace_line` prints `p.old`
  to the terminal unescaped, so an Archive can plant ESC sequences or newlines in a result line, and
  the same text goes into the journal's undo/owed text.
- Fix: validate `stored.siteurl`/`stored.home` with `_url_rule_broken` in `stored()`. A violation is
  `StoredUrlMissingError` or a new refusal naming the option, before any replacement. Strip or escape
  control characters when printing pairs in `search_replace_line`.

### AR-5 — adversarial — MEDIUM — A laid-down `wp-config.php` keeps the Archive's mode; world-readability is only logged, after the fact, and the new DB password is then written into it

- Where: `src/l3io/wp/backup/adapters/restore_runner.py:746-761` (extract), `:787-806`
  (`_log_laid_down`); `adapters/config_writer.py` (`set_constants` keeps the file's mode).
- Verified in code: the config is extracted with the tar `data` filter (mode from the Archive, group/other
  write cleared, nothing else) and no `chmod` follows. `_log_laid_down` emits WARNING
  `config_world_readable` if `mode & S_IROTH` and does nothing else. The Safety/`_CONFIG_MODE`
  `0600` handling this epic removed from `_archive_config` was for the temp copy only; the laid-down file
  has no equivalent. A typical Site's `wp-config.php` is `0644`.
- Impact: with `--new-db-user/--new-db-password`, the cutover then writes the new password into a file
  that is world-readable on a shared host. With a PARENT lay-down it sits in the Site Root's parent
  (for example `/var/www`), which often has a wider audience than the Site. The window also exists
  between EXTRACT and the cutover for the Archive's original password.
- Fix: after the lay-down, `chmod` the file to at most `0640` (or `0600` with the owner kept) before the
  next step, or extract it with a restrictive `umask`. Keep the WARNING for the case where the operator
  asked to keep an existing, more open file. Add a test with a `0644` member.

### AR-6 — adversarial — LOW — A `--new-site-url` with userinfo (`https://user:pw@host`) passes validation and reaches argv, logs and the JSON report

- Where: `src/l3io/wp/backup/core/relocation.py:154-157` (only an empty authority and a leading or
  trailing `@` are refused); consumers `wpcli.py` `search_replace_argv`, `search_replace_completed` log,
  `search_replace_fields`.
- Verified in code. `SecretRegistry` covers only registered values, and the URL is not registered. The
  value is visible in `/proc/<pid>/cmdline` of the `wp` child and in INFO logs and the machine report.
  WordPress does not support userinfo in `siteurl`, and the cost of refusing it is nil.
- Fix: refuse `@` anywhere in the authority in `_url_rule_broken` ("must not contain credentials").

### AR-7 — clean-release — LOW — Dead branch, stale constant text and a mid-file import with a suppression

- Where: `src/l3io/wp/backup/cli/restore.py:727` and `:1158-1164`; `cli/parser.py:76`.
- Verified in code. `RESTORE_EXTENSION` is now bound by an import at module level, so `factory is None`
  in `drive_restore` cannot occur outside a test that patches it, and the `RELOCATION_UNAVAILABLE`
  message ("not supported yet") is then never shown to a user. The import sits mid-module under
  `# noqa: E402, I001`. The `_NoExtension` fallback is similarly unreachable. `RELOCATION_UNAVAILABLE`
  is still re-exported in `parser.__all__`.
- Fix: move the import to the import block (no cycle should result, since `adapters/` does not import
  `cli/`), and drop the `None` branch and the constant once no test needs them. If a test seam is
  wanted, say so in the comment.

### AR-8 — clean-release — LOW — A 919-line generated patch is committed, and local tool directories are untracked but not ignored

- Where: `_bmad-output/implementation-artifacts/epic-007/sprint-01/closure/diff-E007-S01-004.patch`
  (added by f1cf076; `git show --stat f1cf076`); `git status` shows `.claude/worktrees/` and `.codex`
  untracked.
- It is a derived artifact of the commit it sits in. `.gitignore` has no entry for `.codex` or
  `.claude/worktrees`.
- Fix: delete the patch from the tree (it is reproducible from git) and add the two directories to
  `.gitignore`.

## Checked, no finding

- Child environment: `wp_cli_environment` builds the dict from four literals; no `os.environ` spread.
  The passphrase, `MYSQL_*` and `L3IO_WP_BACKUP_*` do not reach `wp`.
- Credentials: `--new-db-password` is registered before use; `RelocationFlags` and
  `ExplicitCredentialSpec` omit it from `repr`; `wp` receives no password by argv or env; stderr tails
  and unrecognised-output reasons are passed through `SecretRegistry.redact`.
- Config location: the Manifest value only selects a `ConfigLocation` member; the `config` role pins the
  member name to `wp-config.php`; a lay-down refuses any existing path (`lexists`), so an Archive cannot
  overwrite a parent-directory file. The TOCTOU window is documented.
- Serialized data: replacement goes through `--precise`; no Python-side value rewriting exists.
  `safe_order`, `nested` and the recount cover the self-interference cases with at most two pairs.
- Secrets or debug artifacts in changed src/CI: none found (no TODO/FIXME/breakpoint/stray `print`;
  the three CI `print` calls are install smoke checks). WP-CLI is pinned with a sha512.

CRITICAL: 0, HIGH: 1, MEDIUM: 4, LOW: 3
