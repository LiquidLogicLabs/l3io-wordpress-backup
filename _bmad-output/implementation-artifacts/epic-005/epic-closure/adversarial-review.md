# E005 epic closure: adversarial + clean-release review

Range 8dbdb82..608ccc6 (read-only; E004 working-tree files untouched). `skill:bmad-review` was not invoked; done by hand with the same lenses.

## Fix status of the per-story findings (checked in code at HEAD)

| Finding | Status |
|---|---|
| S01-001 F-1 (restore provisions env account) | Fixed: `cli/restore.py:1225-1226` passes `environ={}`. |
| S01-001 F-2 (`exc.extra` in install hint) | Fixed: `_backup_extra` in `adapters/database.py`. |
| S01-001 F-3 (info log on every resolve) | NOT fixed: see A-4. |
| S01-001 F-4 (message names a flag that does not exist) | NOT fixed: see A-5. |
| S01-001 F-5 (adapter reads `os.environ`) | NOT fixed: see A-6. |
| S01-002 M1, M2, M3, L2, L3, L4 | Fixed (code present in `preflight.py:190-191, 495-504, 474-492, 452-471`). L1 deferred as BL-E005-003. |

## Findings

### A-1 [adversarial] MEDIUM: relative result from `which` is executed against a different cwd
`src/l3io/wp/backup/adapters/preflight.py:274, 280-282` (`shutil.which` then `subprocess.run(cwd=workspace)`).
`shutil.which` returns a relative path when PATH has a relative or empty entry (`PATH=:/usr/bin`, `PATH=.:...`). The probe then runs with `cwd=<Run workspace>`, so the relative path resolves in the workspace, not in the cwd where `which` found it. The probed file differs from the one located, and `ToolStatus.path` records a path that was never probed. The workspace is a directory the Run writes into (a restore extracts untrusted archive content near it), so a planted `age`/`wp` executable there runs during preflight, before any effect or consent check. Reproduce: `PATH=.:$PATH`, planted `./age` in cwd, `BinaryPreflight(cwd=tmp).check([age])`; the child runs `tmp/age` if present.
Fix: make the result absolute against the process cwd before use (`os.path.abspath(path)`), and ignore or refuse non-absolute PATH entries. Add a test with a relative PATH entry.

### A-2 [adversarial] LOW: preflight does not pin the checked binary (TOCTOU)
`preflight.py:274` records `path` but nothing later uses it; commands later run by bare name and resolve PATH again. A binary swapped, or a PATH changed, between preflight and use passes the check but runs something else. It is only a defence-in-depth gap while no consumer exists.
Fix: when E008/E004 bind this, hand `ToolStatus.path` to the command adapters instead of re-resolving by name, or state that preflight is advisory.

### A-3 [clean-release] MEDIUM: the README claims more than this build enforces
`README.md` "External tools": "A Run checks every external tool its plan needs before its first effect." `BinaryPreflight` and `diagnose` have no caller in `src/` (grep: no import outside `preflight.py`). The roots still bind `cli/backup.py:543,649` and `cli/restore.py:1021` to `DumpReadinessPreflight`, which only does `which`, no version probe. The wiring is E008-S01-001's by a recorded decision (story line 30), but the user-facing text is not qualified. A plan with a minimum for a non-dump tool fails as "unusable (version unknown)", not as a version check. The table itself says "no plan in this build" for all three tools, which contradicts the sentence above it.
Fix: qualify the README sentence ("from <release>" or "database client tools only today") until E008-S01-001 lands, or state it in the generator's output. Also `cli/preflight.py:10` ("until Epic 5 supplies the generic adapter") is stale, since the adapter now exists.

### A-4 [adversarial] LOW: `credential_source_selected` is logged at INFO on every resolve (unfixed S01-001 F-3)
`adapters/database.py:610`. `check_*`, `ensure`, `load` and `dump` each re-resolve, so a restore logs the line many times. For an AWS source the label may carry the secret id, and each resolve is also a Secrets Manager API call (cost and rate limit). No value leaks.
Fix: log once per wrapper (a `_logged` flag) or at DEBUG.

### A-5 [adversarial] LOW: the error names a flag that does not exist (unfixed S01-001 F-4)
`database.py:595-600`, `_DB_REGION_FLAG`/`_ADMIN_REGION_FLAG` at about line 134. The CLI flags are "accepted but not implemented" (README), and library callers have `AwsSecretCredentialSpec.region`. The message is correct for the library only.
Fix: name the spec field, or the flag only once it works.

### A-6 [adversarial] LOW: an adapter reads `os.environ` to word an error, and `environ` is not the mapping that resolves (unfixed S01-001 F-5)
`database.py:518` (`unset_partner(os.environ, "wordpress")`) and `credential_providers.py:21-22,95-105`. The environment step is chosen from the passed `environ`, but database's `Environment` reads `os.environ` at resolve time. Any caller that passes a mapping other than `os.environ` (a test, the library) gets a step chosen on one mapping and valued from another. `restore.py:1226` uses `{}` to mean "no environment step", which works only because absence skips the step; it is not a general contract. A mapping with the variable set, passed from a non-`os.environ` source, is silently resolved from the process environment.
Fix: wrap the passed mapping in a small `CredentialSource` (or assert `environ is os.environ`), and thread the mapping into `CredentialsError` wording.

### A-7 [adversarial] LOW: probe output reaches logs and `ToolStatus.error` unredacted
`preflight.py:203-205, 325-326`. The first output line of a failing tool (for `wp`, a PHP warning that may include a filesystem path or config fragment) goes into `error`, is logged at ERROR, and `Diagnosis.to_dict` emits it. The story requires it to pass through the Run's `SecretRegistry`, but the adapter has no registry and the "root's rendering" is E008's. The probe env is clean (PATH/HOME/LC_ALL only), so a credential reaching it is unlikely; the residual risk is a tool echoing a value.
Fix: record in E008-S01-001's acceptance criteria that `error` is redacted at both the log and `to_dict` surfaces, with a sentinel test.

### A-8 [clean-release] LOW: dead branch and a stale pointer
- `preflight.py:291-298`: the `except subprocess.TimeoutExpired` branch is unreachable with the default probe (`_version_output` converts it). It only serves injected probes, and the message duplicates line 196. Drop it or have the probe contract say it may raise it.
- `DOCUMENTED_REQUIREMENT_SOURCES = ()` (`preflight.py:148-150`) is an intentional empty extension point, tripwire-tested; acceptable.
- `cli/preflight.py:10` stale docstring (see A-3).

## Checked, no finding
- No credential value reaches argv, env, logs or exceptions in the changed code: the probe env is `PATH`, `HOME`, `LC_ALL`, plus `WP_CLI_CONFIG_PATH=/dev/null` for `wp`; `_RegisteringSource` maps unknown exceptions to the class name unchained; `credential_providers` logs only labels. Passwords are registered immediately after `resolve()`.
- No shell, no `shell=True`, a 10 s timeout, a name allow-list (`ValueError` for an unknown name before anything runs).
- AD-4: no `dry_run` conditional in `preflight.py`, `credential_providers.py` or `database.py` (grep). The restore root's Dry Run selection is by adapter set. AD-1 and AD-8: `l3io.wp.database` and `boto3` are imported inside functions or under `TYPE_CHECKING`. ADR-0015: the dump/load client tools are not named in `src/`; `packaging` is a maintained library, not hand-rolled (AD-7 respected; the dependency has a recorded rationale in `pyproject.toml`).
- No TODO/FIXME/debug prints in changed source. The `print` in `tools/gen_tool_docs.py:154` is the CLI's output. No commented-out code. `# noqa: S105` lines suppress variable *names* only.
- Tests use unique sentinels (`credential_sentinel` fixture in `test_credential_providers.py`).

## Counts
CRITICAL 0, HIGH 0, MEDIUM 2 (A-1, A-3), LOW 6 (A-2, A-4, A-5, A-6, A-7, A-8).
