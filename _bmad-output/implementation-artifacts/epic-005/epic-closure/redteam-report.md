# Red team report: epic E005 (EPIC-LEVEL analysis, not sprint-level)

Scope: epic E005 (Sprint 01: S01-001 credential sources, S01-002 binary preflight), diff `8dbdb82..608ccc6`.
Method: l3io-sec-redteam five lenses (external attacker, malicious insider, chaos engineer, abusive legitimate user, design) with AI-poisoning cross-cut, run by hand. The skill's sanctum init was not run: it writes outside the report path and this task is read-only. Platform research was limited to known behaviour of `subprocess.run`, `shutil.which` and WP-CLI config loading; no live web research was done. `shutil.which` behaviour was reproduced locally (Python 3.11.11). The other claims are reasoned from the code and not executed.

Totals: CRITICAL 0, HIGH 0, MEDIUM 4, LOW 5, OBSERVATION 4.

## Scope widening (and why)
Beyond the seed set I read:
- sibling `l3io-wordpress-database/src/l3io/wp/database/credentials.py` (the `Environment`, `AwsSecretsManager` and `CredentialChain` classes that E005 only composes; their error and exception behaviour decides leakage);
- `adapters/redaction.py` and `cli/logging.py` (the sink that must scrub);
- `cli/preflight.py` and a grep of all `src` for `subprocess`, `BinaryPreflight`, `diagnose` (to establish reachability).

## Reachability summary (applies to all findings)
- `BinaryPreflight` and `diagnose()` are defined but **not constructed or called anywhere in `src/`**; only tests reach them. The CLI's actual preflight is `cli/preflight.py::DumpReadinessPreflight`, which only does `shutil.which` with no probe. Findings P-* are therefore latent in this release and become live when a later story wires the adapter (relocation / age).
- No `subprocess` call exists in `src/` outside `adapters/preflight.py`. Nothing in this release runs `wp`, `age`, `tar` or `mysqldump` through this package.
- The AWS provider is library-only (`AwsSecretCredentialSpec`); the `--db-credentials-aws-*` flags are not bound. The environment provider is reachable now: the backup chain is spec, then `L3IO_WP_BACKUP_DB_*`, then wp-config. The restore admin chain uses `L3IO_WP_BACKUP_ADMIN_*`.

## Findings

### M-1 MEDIUM: probe resolves a relative `which()` result against a different directory, so a workspace-planted binary can run (lenses 1, 2, 5)
`src/l3io/wp/backup/adapters/preflight.py:259-290` (`_status`, `__init__` default `which=shutil.which`) and `:172-196` (`_version_output`, `cwd=cwd`).
Reasoning and reproduction: `shutil.which` honours empty and `.` PATH entries and returns a relative path. I confirmed that `PATH=":/usr/bin"` in a directory holding an executable `wp` returns `wp`, and `PATH=".:/usr/bin"` returns `./wp`. The adapter then runs `subprocess.run([path, ...], cwd=<Run workspace>)`. A relative argv[0] containing a slash is resolved after the chdir, i.e. against the workspace rather than the directory `which` searched. Any file named `wp`, `age` or `age-plugin-batchpass` in the Run workspace is therefore executed. If the Run workspace ever holds extracted Archive content (untrusted, ADR-0013 posture), a hostile archive containing an executable `wp` gets code execution at probe time. It needs a poisoned PATH (empty trailing `:` entries are a common misconfiguration) and a workspace containing archive-controlled files. Low likelihood, high impact.
Reachability: latent (adapter unwired).
Fix: resolve `which` to an absolute path (`os.path.abspath` against the process cwd, or reject a non-absolute result). Prefer building the search path from `PATH` entries that are absolute directories only. Pass `cwd` as an empty private temp dir, not the workspace. Add a test with `PATH=".:..."` and a planted file.

### M-2 MEDIUM: probing `wp` loads project config and PHP from cwd ancestors and HOME (lenses 1, 4, 5)
`preflight.py:131-133` (`probe_env=WP_CLI_CONFIG_PATH=/dev/null`), `:169-173` (`HOME` inherited), `:276-282`.
Reasoning (from WP-CLI's documented config loading, not executed here): `WP_CLI_CONFIG_PATH` redirects only the *global* config. WP-CLI also reads the project config `wp-cli.yml` / `wp-cli.local.yml` from cwd and every ancestor. Its `require:` key makes WP-CLI include arbitrary PHP files before running the command, including for `cli version`. WP-CLI also loads installed packages from `$WP_CLI_PACKAGES_DIR`, default `~/.wp-cli/packages`, so inheriting HOME is a second code-load path. The commentary at `preflight.py:22-30` claims the probe is isolated ("so no global ... reaches the child"); that is only true for the global file. If the workspace sits beneath a Site Root or any directory with a `wp-cli.yml` (the code itself notes "the current directory could be a Site Root"), the version probe executes site-controlled PHP, at the privilege of the backup user (often root: `--allow-root` is appended).
Reachability: latent.
Fix: probe `wp` with `cwd` = a fresh empty dir with no config in any ancestor (e.g. a mkdtemp under `/tmp` or the Run dir's private subdir), set `HOME` to that empty dir, set `WP_CLI_PACKAGES_DIR` and `WP_CLI_CONFIG_PATH` to /dev/null-like empties, and pass `--no-... ` options where available (`--skip-plugins --skip-themes` are irrelevant for `cli version`). Correct the docstring to say what is and is not isolated.

### M-3 MEDIUM: `subprocess.run(..., timeout=)` can hang past the timeout when the tool forks (lenses 3, 4)
`preflight.py:183-195`.
Reasoning: on timeout `subprocess.run` kills only the direct child and then calls `communicate()` again. If a grandchild (a bash wrapper around `wp`, `age` wrapper, or a hostile binary) inherited the stdout/stderr pipes, that second `communicate()` blocks until the grandchild exits. The 10 s bound is then not a bound and `--check`/preflight can hang forever (denial of service of the very gate that is meant to fail fast). Not reproduced here; this is the documented CPython behaviour (bpo-37424 family).
Reachability: latent.
Fix: `Popen(..., start_new_session=True)`, `communicate(timeout)`, on timeout `os.killpg` then drain with a short secondary timeout. Add a test using a script that backgrounds `sleep`.

### M-4 MEDIUM: secrets of 3 characters or fewer are never redacted, and registration happens only after a successful resolve (lenses 3, 4, 5)
`adapters/redaction.py:60,96-103` (`MIN_SECRET_LENGTH = 4`, WARNING and return); `adapters/database.py:~607` (`self._secrets.register(resolved.password, ...)`).
Reasoning: a password from the environment or AWS shorter than 4 chars is accepted by the credential chain (database accepts any string, `""` included) and used for the connection, then appears verbatim wherever the database library or a child's error echoes it, with no scrubbing, only a WARNING. The design is a deliberate trade-off (redacting "ab" shreds logs), but nothing refuses or flags the credential at the point of use. Dev and test databases commonly have weak passwords. The registration-after-resolve ordering is sound for exceptions raised inside `resolve()` (nothing secret exists yet), but any secret that reaches output through a different path before the first `resolve()` of the Run (e.g. an env var echoed by an unrelated error text) is not known to the registry.
Reachability: live (env provider).
Fix: treat a too-short password as a `CredentialsError` or a hard WARNING surfaced to the CLI; or register short secrets only in structured-field redaction (exact-match on whole values), not substring replacement. Document the limit in ADR-0018.

### L-1 LOW: `AwsSecretsManager` failures are chained (`from exc`) with botocore exceptions and local frames holding the SecretString
`l3io-wordpress-database/.../credentials.py:~150-175` (consumed unchanged by `backup/adapters/credential_providers.py:_explicit_step`); the backup wrapper at `database.py:~597` re-raises `DatabaseError` as is, with `__cause__` intact.
Reasoning: for a malformed secret, `CredentialsUnobtainableError ... from JSONDecodeError`; the `JSONDecodeError` object carries `.doc` = the entire SecretString (the password, unregistered, since resolve failed). Today `str()` and the traceback render do not print `.doc`, and `format_exc_info` renders no locals, so no leak occurs in this release. It breaks the moment someone adds `dict_tracebacks` with `show_locals`, Sentry/OTel exception capture, `exc.doc` logging, or a debugger/crash-dump hook. The comment at `database.py:~599` ("raised outside the handler, so not even `__context__` holds the untrusted exception") shows the authors know the pattern, but it is applied only to non-database exceptions. Plain-text (non-JSON) secrets are the common user mistake and that is exactly the case that carries the password in `.doc`/`payload`.
Reachability: library only; no leak by default.
Fix: in the wrapper, convert `CredentialsUnobtainableError` to a fresh `CredentialsError` without `__cause__` (`raise ... from None` after capturing `type(cause).__name__`); or upstream, `from None` on the parse failures. Add a test that asserts `exc.__cause__ is None` and no attribute chain reaches the payload.

### L-2 LOW: `SecretBinary` secrets and other KeyErrors are reported as "could not read secret"
database `credentials.py` (`["SecretString"]` inside the AWS-talking `try`). A binary-type secret raises `KeyError`, folded into the generic "could not read secret ... from AWS Secrets Manager". Misleading but not a security flaw; it makes operators loosen IAM or paste credentials into env vars instead. Fix: distinguish and say "secret has no SecretString".

### L-3 LOW: version parsing takes the first dotted-number token from merged stdout+stderr
`preflight.py:97,199-206,191` (`_VERSION_TOKEN`, `_parse_version`, `output = stdout + stderr`).
Reasoning: PHP notices before the WP-CLI banner (`PHP Deprecated: ... /usr/share/php8.3/...`, or `PHP 8.3.6`) can yield `8.3` for `wp`, which clears a floor like `>=2.5`. A tool that prints a spoofed version string also passes (inherent). A hostile or merely noisy tool can thus make an old/broken binary look new, defeating the AD-13 "fail before the first effect" purpose. Impact: wrong refusal decision, not code execution.
Fix: parse stdout only, and anchor per tool (`^WP-CLI (\d+\.\d+\.\d+)`, age `v?(\d+\.\d+\.\d+)`); treat no match as `version_unknown`. Put the pattern in `ToolInfo`.

### L-4 LOW: untrusted tool output is placed in refusal and log text unsanitised, and output size is unbounded
`preflight.py:196-205` (first output line into `error`, truncated to 200 chars by `_truncate`), `:183` (`capture_output=True`, no cap), `domain/preflight.py:107-108` (`unusable (...)` in the refusal).
Reasoning: control characters and ANSI escapes in the first line reach the terminal and JSON logs (terminal-escape and log-injection). A binary that writes gigabytes within 10 s inflates memory (`capture_output` buffers all of it). Redaction is applied by the logging processor but not to the refusal message itself printed by the CLI (verify at the CLI sink).
Fix: strip non-printables (`str.isprintable` filter) before truncation; read via `Popen` with a capped read; ensure the refusal path runs through `SecretRegistry.redact`.

### L-5 LOW: `unset_partner` reads the live `os.environ` instead of the `environ` the chain was built from
`database.py:~513` calls `unset_partner(os.environ, "wordpress")`; review F-5 (known). With the restore-side `environ={}` (F-1 fix) the chain has no environment step, so this can mislabel an unrelated error with a variable name. Names only, never values, so no leak. Fix: pass the mapping used to build the chain.

### O-1 OBSERVATION: the environment secret is inherited by every future child process
`L3IO_WP_BACKUP_DB_PASSWORD` and `L3IO_WP_BACKUP_ADMIN_PASSWORD` live in `os.environ` for the life of the process (also readable by same-uid `/proc/<pid>/environ`). The probe correctly builds a minimal env (`_probe_environment`, good), but when later stories launch `wp` (relocation), `age`, or `mysql*`, nothing enforces the same minimal-env rule: `wp` and its plugins/`require:` files would receive the admin password. Add a contract test that every `subprocess` call in `src/` passes an explicit `env` built from an allow-list (rule 3/4: derive scope from the source, i.e. an AST scan for `subprocess.*` / `Popen` calls without `env=`). Today only one subprocess call exists and it is compliant.

### O-2 OBSERVATION: precedence lets an ambient env var silently change which account a run uses
For backup, `L3IO_WP_BACKUP_DB_*` outranks wp-config. In a multi-site loop or shared CI shell, a leftover variable from one site makes every backup run as that account. It is logged (`credential_source_selected`, source label only, no value) but not warned. Consider a WARNING when the environment step wins over a present wp-config definition. Half-set pairs fail loudly and do not fall through to wp-config (correct and tested).

### O-3 OBSERVATION: positive controls verified
- `ResolvedCredentials.__repr__`, `Explicit.__repr__`, `_RegisteringSource.__repr__`, `BinaryPreflight.__repr__` and `SecretRegistry.__reduce__` all avoid emitting values.
- Non-database exceptions from a source are reduced to the class name and raised outside the handler (`database.py:~597-604`).
- The `L3IO_WP_BACKUP_*` prefix avoids stray `DB_USER`/`DB_PASSWORD` capture; empty-string values count as set and are not silently skipped.
- Review F-1 (restore provisioning the wrong account) is fixed: `restore.py` now passes `environ={}` for the WordPress account.
- M1 from the S01-002 review (invalid UTF-8) is fixed: `encoding="utf-8", errors="replace"`.
- The probe passes no shell, uses an allow-listed tool set (`KNOWN_TOOLS`, unknown name raises `ValueError` before any execution) and a minimal env.
- AWS: no static key handling in this package (default boto3 chain), `secret_id` and region only in labels; the secret value is registered immediately on resolve.

### O-4 OBSERVATION: logs include the absolute tool path and AWS secret id/ARN
`_log_checked` logs `path`; the AWS source label includes `secret_id` (may embed the account id). Not secrets, but log aggregation may be broader than the secret store's audience. Keep at DEBUG for success; already so.

## Not covered
No live exploitation of M-2/M-3 (they depend on the WP-CLI and CPython behaviour cited, not executed here). No review of `l3io-wordpress-database` beyond the credential module. The tests (`test_credential_providers.py`, `test_preflight.py`) were not run. IAM scoping, KMS and secret rotation for the AWS secret are out of the code's control and unaddressed in the diff.
