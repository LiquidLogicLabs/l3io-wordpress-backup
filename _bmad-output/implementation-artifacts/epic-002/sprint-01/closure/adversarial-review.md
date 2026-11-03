# Sprint closure review: E002 sprint 01, adversarial lens

- **Content:** `git diff 3e9f80a..d3e0b2b -- src tools .github` (code diff, 41 files, staged at `/tmp/e002s01-sprint.diff`).
- **Lens:** adversarial (bmad-review). Each finding has a scope tag: `clean-release` or `adversarial`.
- **Dedup baseline:** `closure/review-E002-S01-{001..006,010}.md` and `state/issues.yaml`. This review leaves out items that are already resolved (for example the S01-003 F-01 symlink/duplicate bypass, now closed in `_ExtractFilter`, and F-02 RecursionError/ValueError, now in `archive_format.py:359-369`). It also leaves out items that are open in the backlog: F-05 publish gate STRUCTURAL_ONLY, F-06 OSError mapping, F-07 read_member duplicates, F-08 workspace rmtree, KeyboardInterrupt exit 130, the literal-exit-code guard, the ExplicitCredentialSpec repr, `issues.yaml:709` inner-tar/index comparison, the store rename race, and the `.tar.gz` naming.
- **ADRs** in `docs/adr/` are treated as final. ADR-0024 accepts the plaintext payload/Dump in the owner-only workspace, so this review does not raise it.
- **Checked, no finding:**
  - The diff has no committed credentials, commented-out code, `TODO`/`FIXME` markers, `breakpoint`/`pdb` calls or stray `print` calls. The only `print` calls are in `cli/output.py`, and they are intended.
  - The passphrase environment variable does not reach `mysqldump`: database's `transfer.py` gives the child process `_client.child_env()`, a minimal environment.
  - `.github/workflows/ci.yml` adds only the `gen_cli_docs.py --check` step.

## Findings

### AR-01 · adversarial · MEDIUM · `src/l3io/wp/backup/adapters/tar.py:947-958`
- **Trigger:** `exclude` is compared lexically (`PurePosixPath(relative) in self.exclude`) and by `Path.resolve()` equality. On a case-insensitive filesystem (macOS APFS default, SMB/CIFS mounts, ext4 `casefold` directories), a crafted member `site-root/WP-CONFIG.PHP` that is also listed in the crafted index has these properties:
  - It is not lexically excluded.
  - `resolve()` does not canonicalise case for an existing file, so it does not equal `root/wp-config.php`.
  - It passes `data_filter`.

  `extractall` then opens the kept `wp-config.php` and overwrites it. This happens even though the restore did not ask for `--overwrite-wp-config` (ADR-0023). The same lexical blind spot lets `self.links` miss a symlink `L` when members are named `l/...`.
- **Fix:** Decide exclusion by file identity, not by spelling:
  - Before extraction, `lstat` every existing excluded entry and keep its `(st_dev, st_ino)`.
  - In the filter, refuse a member whose target exists and whose `(st_dev, st_ino)` matches.
  - Also compare `os.path.normcase(relative).casefold()` against the casefolded exclude set and the casefolded `links` set.
  - Add a contract test that uses a casefold directory, or that monkeypatches `resolve`/`stat` to simulate one.
- **Consequence:** On those filesystems a keyless crafted Archive overwrites the target's DB credentials and salts. This is the outcome that S01-003 F-01 was closed to prevent.
- **Verdict:** PLAUSIBLE. This was reasoned from the code. It was not reproduced on a case-insensitive filesystem.

### AR-02 · adversarial · MEDIUM · `src/l3io/wp/backup/adapters/tar.py:944-946`, `:1002`
- **Trigger:** The filter holds a v1 member to its indexed **kind** (`kinds={m.path: m.kind}`) but never to its indexed **size**. Verification never opens the payload (ADR-0024). A crafted unencrypted payload, or an encrypted one built with a passphrase the attacker chose, can therefore pass `verify` as VERIFIED with a small index and then expand to an arbitrary size during `extractall`. A gzip bomb fills the restore destination before any refusal, and `ExtractResult.bytes` reports the result after the fact.
- **Fix:** Carry `sizes={m.path: m.size}` next to `kinds`. Refuse a FILE member when `member.size != sizes[name]`, before `data_filter`. Optionally also refuse once `self.bytes` exceeds `sum(sizes.values())`. This is the cheap first step of `issues.yaml:709` (inner tar vs index); link the two.
- **Consequence:** A disk-exhaustion denial of service on the site host during restore, caused by an Archive that verification said was VERIFIED.

### AR-03 · adversarial · MEDIUM · `src/l3io/wp/backup/domain/errors.py:380`, `src/l3io/wp/backup/adapters/tar.py:991`, `src/l3io/wp/backup/domain/exit_codes.py:63`
- **Trigger:** `ExtractionRefusedError` subclasses `RefusedError`, so it exits `3 REFUSED`, which `DESCRIPTIONS` defines as "a check found a reason not to act; nothing was done". But `extract_archive` documents "Members written before a refusal stay written": a refusal at member N leaves N-1 members on disk.
- **Fix:** Pick one of these:
  - Re-parent `ExtractionRefusedError` to `BackupError` with `FAILED`, or to `VERIFICATION_FAILED`, since the payload contradicts its index.
  - Guarantee that extraction only ever targets a staging directory that is discarded on refusal, and state that in the docstring and the exit table.
- **Consequence:** Automation that treats exit 3 as "safe: nothing touched" leaves a partially written site, or partially written staging, unattended.

### AR-04 · adversarial · MEDIUM · `src/l3io/wp/backup/domain/errors.py:361` (`ArchiveReadError`), `:306` (`MalformedManifestError`); `src/l3io/wp/backup/domain/exit_codes.py:21-22`
- **Trigger:** The exit table says corrupt ciphertext maps to `VERIFICATION_FAILED`. However:
  - Payload corruption (gzip CRC, a bad inner tar header, a truncated body) is found only by `extract_archive`/`decrypt_payload`/`read_member`, because verification is keyless. All three raise `ArchiveReadError`, which inherits `FAILED` (1).
  - `MalformedManifestError` also exits 1 when it escapes `verify_probe`.
- **Fix:** Split integrity from I/O. Raise a `VERIFICATION_FAILED`-class error, for example `PayloadCorruptError(ArchiveReadError)` with `exit_code = VERIFICATION_FAILED`, for `tarfile.TarError`, `zlib.error`, `gzip.BadGzipFile` and `EOFError` inside the payload. Keep `ArchiveReadError(FAILED)` for `OSError`.
- **Consequence:** A damaged Archive exits 8 in `verify` but exits 1 at restore. Scripts that branch on 8 ("bad archive, try the previous one") miss the case that matters.

### AR-05 · adversarial · MEDIUM · `src/l3io/wp/backup/domain/errors.py:97`, `:226`; `src/l3io/wp/backup/cli/passphrase.py:67,96,119`
- **Trigger:** The same condition, "no usable passphrase", gets two different exit codes:
  - `PassphraseUnavailableError` subclasses `UsageError` and exits `2 USAGE` ("the command line was invalid") for a missing passphrase, a closed prompt, or a passphrase that is too short.
  - `PassphraseRequiredError` exits `3 REFUSED`.

  The exit table (`exit_codes.py:21`) says a missing or wrong key is `CREDENTIALS` (4). No ADR decides USAGE.
- **Fix:** Re-parent `PassphraseUnavailableError` to `CredentialsError` (4), or record the mapping in an ADR. Then align `PassphraseRequiredError`, or explain why it differs.
- **Consequence:** A cron backup that has lost its secret reports a malformed command line, which sends the operator to the wrong place. Monitoring that keys on 4 for secret-rotation failures stays silent.

### AR-06 · adversarial · MEDIUM · `src/l3io/wp/backup/cli/passphrase.py:89-96`
- **Trigger:**
  - The only passphrase floor is `MIN_SECRET_LENGTH` (4), a redaction threshold, yet the error calls it "too short to protect".
  - A prompted passphrase is read once, with no confirmation entry. For a backup, a typo produces an Archive that nobody can decrypt.
  - Verification is keyless, so that Archive still verifies VERIFIED and is published.
- **Fix:**
  - Give `require_passphrase` a `confirm: bool`. When the backup root's chain is wired in E002-S02-011, it should prompt twice and refuse a mismatch.
  - Separate a strength floor, or at least a WARNING below about 12 characters, from the redaction minimum, and reword the error.
- **Consequence:** A silently unrecoverable backup is found only at restore time. A 4-character passphrase is accepted as protection.
- **Note:** The chain is not called yet, because `_cipher` returns `None`. Fix this before S02-011 wires it.

### AR-07 · adversarial · LOW · `src/l3io/wp/backup/adapters/redaction.py:155-167`
- **Trigger:** `_walk` redacts `str`, exceptions, mappings and sequences only. `bytes`, `Path` and arbitrary objects are returned untouched, and the renderers then `repr()` them: `JSONRenderer` through its fallback, `ConsoleRenderer` directly. So a `bytes` field (for example a captured tool stderr) or an object whose `repr` embeds a secret reaches stderr unredacted. The redaction step runs before the renderer, so the stringification that happens later is never examined.
- **Fix:** In `_walk`, decode `bytes`/`bytearray` with `errors="surrogateescape"` and redact the result. For any other non-primitive type (not `int`, `float`, `bool` or `None`), redact `repr(value)`. Add a unit case that plants a secret in a `bytes` field and in a custom `__repr__`.
- **Consequence:** The backstop has a type-shaped hole. This module's docstring lists its limits but does not list this one.

### AR-08 · adversarial · LOW · `src/l3io/wp/backup/cli/__init__.py:43-52`; `src/l3io/wp/backup/cli/backup.py:449`
- **Trigger:** The root creates the registry at line 43, but it becomes live for `redact_open_runs` only inside `open_run`. Anything registered and then logged before the Run opens is not redacted by the pipeline. That covers `_cipher(...)` at `backup.py:449`, which will resolve the passphrase there once S02-011 wires it, and l3io.wp.config's stdlib records during `drive_backup`'s pre-Run `resolve(strict=True)`. Only `report_failure` re-opens the registry, and only for the failure record.
- **Fix:** Wrap the dispatch in `main` in `with open_registry(secrets):`, so the registry is live from creation to exit. `open_run` re-entering the same registry is harmless, because `open_registry` removes by identity.
- **Consequence:** A secret logged between resolution and `run_started` bypasses the backstop, contrary to passphrase.py's claim that "it is redactable before `run_started`".

### AR-09 · adversarial · LOW · `src/l3io/wp/backup/adapters/database.py:226`, `:517`
- **Trigger:** `InvalidSiteDescriptorError(str(exc))` interpolates the text of database's `InvalidArgumentsError`/`InvalidIdentifierError`. That text is built from `DB_HOST`/`DB_NAME` in a `wp-config.php` that the site's (lower-privileged) owner controls. `render_failure` prints a `BackupError` message verbatim (`exit.py:101`), so control characters or ANSI escape sequences in `DB_NAME` reach the operator's terminal. This also contradicts the S01-006 F-03 rule that a dependency's text is never interpolated (AD-7).
- **Fix:** Build the message from fixed text plus the constant's name. For example: `"DB_NAME is not a valid identifier"`, with `paths=(str(config_file),)`, and the value `repr()`-escaped if it is shown at all.
- **Consequence:** Terminal and log injection from site-controlled config, and an inconsistent application of the untrusted-text rule.

### AR-10 · adversarial · LOW · `src/l3io/wp/backup/adapters/store.py:113-122`
- **Trigger:** `_rename` calls `os.replace` but never `fsync`s the parent directory. The staged file is fsynced (`sync()`), but the rename is not durable. After a power loss or a hypervisor crash, the directory entry can be gone even though `archive_published` was logged and the process exited 0. ADR-0023 specifies the temp-fsync-replace sequence for config writes, but not for the Archive.
- **Fix:** After `replace`, `fd = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY); os.fsync(fd); os.close(fd)`, mapping `OSError` to `ArchiveWriteError(final)`.
- **Consequence:** A backup is reported as done but is not on disk after a crash. The usual next step, a destructive restore of another site, would have relied on it.

### AR-11 · adversarial · LOW · `src/l3io/wp/backup/cli/backup.py:392`; `src/l3io/wp/backup/adapters/database.py:364,387`
- **Trigger:** `drive_backup`, the library driver, reads `os.environ` itself and passes it to `wordpress_source`, which ignores it (`noqa: ARG001`). This contradicts `passphrase.py:5-6` ("the CLI passes `os.environ`; nothing deeper reads the environment"). A library caller (Epic 6 `api/`) cannot supply or withhold the environment. When Epic 5 binds the environment step, the process's `DB_PASSWORD` will be used silently for library calls.
- **Fix:** Add `environ: Mapping[str, str]` to `drive_backup` (or `BackupRequest`), default it to an empty mapping for library use, and have `run_backup` pass `os.environ`.
- **Consequence:** The environment is an implicit input to the library, and a credential can be resolved from a source the caller never chose.

### AR-12 · adversarial · LOW · `src/l3io/wp/backup/adapters/tar.py:994`
- **Trigger:** The interpreter gate is `hasattr(tarfile, "data_filter")`. That accepts 3.11.4-3.11.12 and 3.12.0-3.12.10, whose `data_filter` has the 2025 realpath/PATH_MAX bypasses (CVE-2025-4517/4138/4330). The comment at `:888` already names the patched versions. The filter's own `resolve()` checks share the same realpath, so the only independent control is the lexical "below a symlink in this archive" check, which AR-01 shows is spelling-sensitive.
- **Fix:** Refuse (`ExtractionRefusedError`) or WARN when `sys.version_info` is below the patched floor for its minor version. Alternatively, raise `requires-python` or the CI and runtime floor to the patched releases.
- **Consequence:** Defence-in-depth depends on one lexical check on interpreters that are known to be vulnerable.
- **Verdict:** PLAUSIBLE. No exploit was constructed against the current filter.

### AR-13 · adversarial · LOW · `src/l3io/wp/backup/cli/exit.py:120-133`
- **Trigger:** `except Exception: pass` covers more than the stderr write. It also covers `render_failure`: a `DESCRIPTIONS` lookup miss, a `BackupError` whose `__str__` raises, or a structlog processor defect. In any of those cases the process exits with a code and writes nothing to stderr or the log.
- **Fix:** Keep the code decided first. Inside the `except`, attempt a fixed fallback line, `os.write(2, f"wpbackup2: error: {type(exc).__name__} [exit {int(code)}]\n".encode())`, itself suppressed. That way only a truly closed stderr is silent.
- **Consequence:** A silent non-zero exit with no record, which is the hardest failure to triage.

### AR-14 · clean-release · LOW · `src/l3io/wp/backup/cli/credentials.py:32-75`; `src/l3io/wp/backup/adapters/database.py:384-397`; `src/l3io/wp/backup/cli/backup.py:424-434,448`; `src/l3io/wp/backup/adapters/redaction.py:133`
- **Trigger:** The release ships speculative code that this sprint never reaches:
  - `admin_spec`/`admin_source`/`_aws_spec` have no CLI caller.
  - `run_backup` always passes `wordpress_spec(aws_secret_id=None, aws_region=None)`, which is always `None`.
  - `_cipher` is a stub (`del secrets`, returns `None`).
  - `command_args` is never supplied by a root.
  - `redact_argv` has no caller.
  - `environ` parameters carry `noqa: ARG001`.
- **Fix:** Remove the parts that have no caller until the owning epic lands, or keep them with an explicit `# reserved for E00x-…` marker and a test that would fail once they are wired. In particular, `admin_spec` takes a password value whose only plausible future source is an argv flag, which ADR-0003 rules out for MySQL clients. Decide its transport before keeping that API.
- **Consequence:** Dead paths are reviewed as if they were live, and an API shape that invites a password on argv becomes entrenched.

### AR-15 · clean-release · LOW · `src/l3io/wp/backup/cli/parser.py:107`; `src/l3io/wp/backup/cli/__init__.py:40-41`
- **Trigger:** The `restore` subcommand is registered and advertised in `--help`, and so in the generated README block. `main` then refuses it with "is not implemented in this build". It also has no arguments, so `wpbackup2 restore --wp-dir x` fails as a usage error before it reaches that message.
- **Fix:** Do not register `restore` until Epic 3, or give it help text that says "(not available in this build)".
- **Consequence:** A released CLI documents a command that cannot run.

### AR-16 · clean-release · LOW · `tools/gen_cli_docs.py:84-91`, `:54-59`
- **Trigger:**
  - `render` replaces only the first BEGIN/END pair for each name. A duplicated block (from a copy-paste or a merge) is left stale, and `--check` still passes.
  - The subcommand walk is one level deep, so nested subparsers would never be documented. That contradicts "found by walking the parser, not listed here".
  - `parser._actions` and `argparse._SubParsersAction` are private API.
- **Fix:** Require exactly one BEGIN and one END for each block, and fail otherwise. Recurse into each sub-parser's `_SubParsersAction`. Keep the private-API `noqa`, but name the Python versions it is pinned against in the docstring's "not covered" section.
- **Consequence:** A CI drift gate that can report green over stale generated docs (global rule 4: the guard's scope).

## Findings (JSON)

```json
[
  {"lens":"adversarial","id":"AR-01","scope":"adversarial","severity":"MEDIUM","location":"src/l3io/wp/backup/adapters/tar.py:947-958","trigger_condition":"exclude and symlink-parent checks are case-sensitive/lexical; on a case-insensitive FS a member WP-CONFIG.PHP overwrites the kept wp-config.php","guard_snippet":"compare excluded entries by (st_dev, st_ino) and casefolded relative path; casefold the links set","potential_consequence":"crafted Archive overwrites target DB credentials/salts without --overwrite-wp-config"},
  {"lens":"adversarial","id":"AR-02","scope":"adversarial","severity":"MEDIUM","location":"src/l3io/wp/backup/adapters/tar.py:944-946,1002","trigger_condition":"filter checks indexed kind, never indexed size; keyless verify never opens payload","guard_snippet":"refuse FILE member when member.size != index size; cap total bytes at sum of index sizes","potential_consequence":"decompression bomb fills restore destination from a VERIFIED Archive"},
  {"lens":"adversarial","id":"AR-03","scope":"adversarial","severity":"MEDIUM","location":"src/l3io/wp/backup/domain/errors.py:380; adapters/tar.py:991; domain/exit_codes.py:63","trigger_condition":"ExtractionRefusedError exits 3 'nothing was done' while members written before the refusal stay written","guard_snippet":"re-parent to FAILED/VERIFICATION_FAILED or guarantee staging-only extraction discarded on refusal","potential_consequence":"automation treats a partially written site as untouched"},
  {"lens":"adversarial","id":"AR-04","scope":"adversarial","severity":"MEDIUM","location":"src/l3io/wp/backup/domain/errors.py:306,361; domain/exit_codes.py:21-22","trigger_condition":"payload corruption found at extract/decrypt raises ArchiveReadError (exit 1), not VERIFICATION_FAILED as the table states","guard_snippet":"PayloadCorruptError(ArchiveReadError) with exit_code VERIFICATION_FAILED for tar/zlib/gzip/EOF errors","potential_consequence":"scripts branching on 8 miss corrupt archives at restore"},
  {"lens":"adversarial","id":"AR-05","scope":"adversarial","severity":"MEDIUM","location":"src/l3io/wp/backup/domain/errors.py:97,226; cli/passphrase.py:67,96,119","trigger_condition":"missing passphrase exits 2 USAGE (and PassphraseRequiredError 3); table says missing key is CREDENTIALS 4","guard_snippet":"re-parent PassphraseUnavailableError to CredentialsError or record the decision in an ADR","potential_consequence":"lost secret in cron reported as bad command line"},
  {"lens":"adversarial","id":"AR-06","scope":"adversarial","severity":"MEDIUM","location":"src/l3io/wp/backup/cli/passphrase.py:89-96","trigger_condition":"4-char redaction minimum is the only passphrase floor; prompt has no confirmation for backup","guard_snippet":"confirm=True double prompt for backup; separate strength floor/warning from MIN_SECRET_LENGTH","potential_consequence":"typo'd passphrase yields a published VERIFIED Archive nobody can decrypt"},
  {"lens":"adversarial","id":"AR-07","scope":"adversarial","severity":"LOW","location":"src/l3io/wp/backup/adapters/redaction.py:155-167","trigger_condition":"_walk passes bytes/objects through; renderer repr()s them after redaction","guard_snippet":"decode bytes and redact; redact repr() of non-primitive values","potential_consequence":"secret in a bytes field or custom repr reaches stderr"},
  {"lens":"adversarial","id":"AR-08","scope":"adversarial","severity":"LOW","location":"src/l3io/wp/backup/cli/__init__.py:43-52; cli/backup.py:449","trigger_condition":"registry is not live for the pipeline until open_run; pre-Run registration/logging is unredacted","guard_snippet":"with open_registry(secrets): around main's dispatch","potential_consequence":"secret logged before run_started bypasses the backstop"},
  {"lens":"adversarial","id":"AR-09","scope":"adversarial","severity":"LOW","location":"src/l3io/wp/backup/adapters/database.py:226,517","trigger_condition":"str(exc) of database errors built from site-controlled DB_HOST/DB_NAME is printed verbatim","guard_snippet":"fixed message naming the constant; repr() any value","potential_consequence":"terminal/log injection; AD-7 untrusted-text rule broken"},
  {"lens":"adversarial","id":"AR-10","scope":"adversarial","severity":"LOW","location":"src/l3io/wp/backup/adapters/store.py:113-122","trigger_condition":"os.replace without fsync of the parent directory","guard_snippet":"open parent O_DIRECTORY and fsync after replace","potential_consequence":"published Archive missing after power loss despite exit 0"},
  {"lens":"adversarial","id":"AR-11","scope":"adversarial","severity":"LOW","location":"src/l3io/wp/backup/cli/backup.py:392; adapters/database.py:364,387","trigger_condition":"library driver reads os.environ itself, contradicting passphrase.py's 'nothing deeper reads the environment'","guard_snippet":"environ parameter on drive_backup/BackupRequest, empty default; run_backup passes os.environ","potential_consequence":"library callers silently resolve credentials from process env once Epic 5 binds it"},
  {"lens":"adversarial","id":"AR-12","scope":"adversarial","severity":"LOW","location":"src/l3io/wp/backup/adapters/tar.py:994","trigger_condition":"hasattr(tarfile,'data_filter') accepts interpreters with CVE-2025-4517/4138/4330 data_filter bypasses","guard_snippet":"refuse or warn below patched 3.11.13/3.12.11; or raise the floor","potential_consequence":"defence-in-depth rests on one lexical check on vulnerable interpreters"},
  {"lens":"adversarial","id":"AR-13","scope":"adversarial","severity":"LOW","location":"src/l3io/wp/backup/cli/exit.py:120-133","trigger_condition":"except Exception: pass also swallows render/log defects","guard_snippet":"fallback os.write(2, fixed line) inside the except, itself suppressed","potential_consequence":"silent non-zero exit with no stderr or log record"},
  {"lens":"adversarial","id":"AR-14","scope":"clean-release","severity":"LOW","location":"src/l3io/wp/backup/cli/credentials.py:32-75; adapters/database.py:384-397; cli/backup.py:424-434,448; adapters/redaction.py:133","trigger_condition":"speculative unreached code: admin_spec/admin_source/_aws_spec, _cipher stub, unused command_args/redact_argv, noqa'd environ params","guard_snippet":"remove until owning epic, or mark reserved with a failing-when-wired test; decide admin password transport","potential_consequence":"dead paths reviewed as live; argv-password API shape entrenched"},
  {"lens":"adversarial","id":"AR-15","scope":"clean-release","severity":"LOW","location":"src/l3io/wp/backup/cli/parser.py:107; cli/__init__.py:40-41","trigger_condition":"restore subcommand advertised in --help/README but refused at runtime","guard_snippet":"do not register until Epic 3, or label it unavailable","potential_consequence":"released CLI documents a command that cannot run"},
  {"lens":"adversarial","id":"AR-16","scope":"clean-release","severity":"LOW","location":"tools/gen_cli_docs.py:84-91,54-59","trigger_condition":"only the first marker pair replaced; one-level subcommand walk; private argparse API","guard_snippet":"require exactly one BEGIN/END per block; recurse into sub-parsers","potential_consequence":"CI drift gate green over stale generated docs"}
]
```

## Totals

Critical 0, High 0, Medium 6, Low 10. Scopes: adversarial 13, clean-release 3.
