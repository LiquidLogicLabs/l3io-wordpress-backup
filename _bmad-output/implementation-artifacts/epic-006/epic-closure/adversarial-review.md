# E006 adversarial review (lens: adversarial; also_consider: clean-release)

Content: `git diff b321422^1 83d73a7 -- src tests docs pyproject.toml` (staged at /tmp/e006.diff; 74 files, +11456/-193). HEAD 83d73a7.
Method: read api/operations.py, api/errors.py, adapters/tracing.py, cli/{context,restore,backup,verify,passphrase,credentials,logging,__init__}.py, adapters/{redaction,wpcli,database}.py, domain/errors.py; compared each CLI flag/guard with the API entry points; ran sentinel snippets with `uv run python -I` (no real credentials; sentinels only). Nothing in the repo was modified.
Scope tags: `adversarial` or `clean-release`. "Verified" = reproduced by running a snippet; "read" = established by reading code, not run.

Guards compared (restore): refusals/FR-7 guard, `--what-if`, `--archive-database`, `--require-encrypted`, `--reset-existing-db-user`, `--no-safety-snapshot`, lock, journal, snapshot all live inside `drive_restore`/`drive_backup`/`drive_verify`, which the API calls; none is bypassed. The one CLI-only rule (the AD-25 flag table, `check_restore_arguments`) is replaced by typed arguments (see AR-8). No finding of a CLI safety check skipped by the API.

---

## AR-1 — HIGH — adversarial — Structured errors cannot be unpickled; a process pool breaks and the real error is lost
- Where: src/l3io/wp/backup/domain/errors.py (every class with required/keyword-only `__init__` fields), src/l3io/wp/backup/api/errors.py:217-233 (`UnexpectedError`).
- Reachable: yes, from the public API. Status: VERIFIED.
- Reasoning: `BaseException.__reduce__` rebuilds with `cls(*self.args)`, and `args` is the single formatted message. Every class whose constructor takes more than the message (O-1(b) added `database=`, `arguments=`, `exit_status=`, ... as required keywords) fails on unpickle.
- Reproduction: `pickle.loads(pickle.dumps(api.DumpFailedError("r", database="d", exit_status=2)))` -> `TypeError: DumpFailedError.__init__() missing 1 required keyword-only argument: 'database'`. Same for `UsageError`, `WpCliFailedError`, `DatabaseUnreachableError`, `UnexpectedError`. Of 75 exported exception classes constructed generically, 49 failed to round-trip (some of that is my generic constructor, but the four hand-built ones above fail for the reason stated). `ProcessPoolExecutor(1).submit(fn_raising_DumpFailedError).result()` -> `BrokenProcessPool: A process in the process pool was terminated abruptly`, the package error is not delivered.
- Consequence: a third party running `backup`/`restore` in `ProcessPoolExecutor`, `multiprocessing`, Celery, Dask etc. loses every failure and may see the pool marked broken. Also copy/`copy.copy` of an exception.
- Fix: give `BackupError` a `__reduce__` that returns `(_rebuild, (cls, args, kwargs, __dict__))` or have each class store its constructor kwargs and restore via `__dict__` (e.g. `__reduce__` -> `(cls.__new__-based helper, (cls,), self.__dict__)` plus `Exception.__init__(self, message)` call). Add a contract test that round-trips every class in `api.errors.__all__` (the walker in test_api_exception_exports.py already enumerates them).

## AR-2 — MEDIUM — adversarial — `public_boundary` logs the original exception after the Run's registry is closed: traceback and message are unredacted
- Where: src/l3io/wp/backup/api/errors.py:270-276 (with cli/context.py `open_registry` scope and adapters/redaction.py `redact_open_runs`).
- Reachable: yes. Status: VERIFIED (mechanism); the trigger needs a non-package exception whose text carries a registered secret.
- Reasoning: `redact_open_runs` only redacts with registries that are currently open, and `open_run` closes its registry when the Run ends (docs say "for exactly the Run's lifetime"). The boundary runs after the Run, so `_log.error("unexpected_exception", exc_info=exc)` goes through the host's chain (even the package's own `configure_logging` chain) with an empty `_OPEN`. The docstring says "Nothing here redacts (ADR-0018): no message is built from a dependency's text", but that is exactly what `exc_info=exc` logs (full `str(exc)` + traceback of a dependency exception).
- Reproduction: patch `operations.drive_backup` to register sentinel `SENTINEL-PASS-xyz-9981` in the passed registry, leave `open_registry`, then `raise RuntimeError(f"... password={S}")`; `configure_logging("INFO","json")`, capture handler stream: `S in log output` -> True; `S in str(UnexpectedError)` False; `S in str(e.__cause__)` True.
- Consequence: any defect-class exception (a dependency's `OperationalError`, `CalledProcessError` with argv, etc.) that interpolates a passphrase/DB password reaches the host's logs at ERROR, contradicting ADR-0018's "backstop" for the library path. The CLI avoids this because `report_failure` wraps the log in `open_registry(secrets)`.
- Fix: in `public_boundary`, redact before logging; the registry is not available there, so either (a) keep the registry on the exception like `RUN_ID_ATTR` and wrap the log call in `open_registry(registry)`, or (b) do not log `exc_info`/message at all (log `cause_type` + traceback frames only, or only `type` and `exc.__traceback__` locations).

## AR-3 — MEDIUM — adversarial — `PR_SET_DUMPABLE` toggled process-wide around the `wp` child: race between threads and an undocumented host-process side effect
- Where: src/l3io/wp/backup/adapters/wpcli.py (`_unreadable_by_the_child`, `_set_dumpable`, `run`), docs/restore.md (only place that mentions it); not in docs/api.md; tests/unit/api/test_process_state.py does not observe it.
- Reachable: yes (restore with relocation via `api.restore`). Race status: VERIFIED. Side effect: read.
- Reasoning: dumpable is per-process, not per-call. Two threads (restores of different Sites hold different locks) each save `previous` and restore it. Thread A and B overlap: B reads `previous=0` (set by A), A exits first and sets dumpable back to 1 while B's `wp` child (running restored-site PHP, up to 4 h) is still running. Protection (the reason for the code: hide `/proc/<ppid>/environ` with the passphrase and admin credentials) is silently lost for B.
- Reproduction: two threads entering `_unreadable_by_the_child()` with an event handshake: reading `PR_GET_DUMPABLE` inside B after A exited returned `1`.
- Also: for up to 4 h per `wp` call a library host's whole process is non-dumpable (core dumps, `py-spy`/gdb attach, `/proc` ownership for every thread), and a host that started with `suid_dumpable` value 2 gets 1 restored. api.md's "process state" section and the process-state test list logging/SIGTERM only.
- Fix: guard with a module lock plus a depth counter (set on 0->1, restore on 1->0), or drop in-process prctl and instead pass the secrets to the child via a mechanism that doesn't expose them (the child already gets a minimal env; the exposure is the parent's `/proc/<ppid>/environ`, which prctl cannot protect on all kernels). Document it in docs/api.md and add it to test_process_state.

## AR-4 — MEDIUM — adversarial — Secrets are held in frames of the traceback on every escaping exception (host tools that capture locals leak them)
- Where: api/operations.py (`backup`: `passphrase`, `resolved`, `built`), wrapper `kwargs` in api/errors.py:261, adapters/crypto.py `PassphraseCipher._passphrase`; ExplicitCredentialSpec/RelocationFlags in driver frames.
- Reachable: yes. Status: VERIFIED.
- Reproduction: `api.backup("/nonexistent", scope=..., encrypt=True, passphrase=S)` raises `InvalidSiteDescriptorError`; walking `e.__traceback__` shows frame locals `wrapper.kwargs`, `backup.passphrase`, `backup.cipher._passphrase`, `backup.built._passphrase` contain S.
- Consequence: Sentry (`include_local_variables` default on), `structlog.processors.dict_tracebacks(show_locals=True)`, rich tracebacks, pytest `--showlocals` print the passphrase. ADR-0018 states this is not covered, but the API docs do not warn the caller; `repr` guards (AD-7) do not help because the raw `str` is in the frame.
- Fix: minimal: `del`/clear the argument and `resolved` as soon as the cipher exists is not enough (kwargs holds it); document the limitation in docs/api.md "Secrets" and recommend a `prompt` callable; optionally have the cipher keep the passphrase as bytes in a `SecretStr`-like wrapper whose `__repr__` is `***` so local-variable printers show the wrapper.

## AR-5 — MEDIUM — adversarial (doc/behaviour mismatch also tagged clean-release) — `drive_restore` prints to the host's stderr from library calls
- Where: src/l3io/wp/backup/cli/restore.py:1307-1311 (`archive_unauthenticated` banner), :1434-1445 (`safety snapshot is NOT encrypted` / `is encrypted with ...`). Contradicted by: cli/restore.py `drive_restore` docstring ("never prints"), api/operations.py:9 ("It never prints"), tests/unit/api/test_process_state.py scope.
- Reachable: yes, `api.restore(...)` of a plain archive, or any real restore that takes a snapshot. Status: read (code path unconditional on `request.dry_run` for the banner; snapshot lines only when `performed`).
- Consequence: a library host (service, GUI) gets unsolicited stderr text it cannot suppress; the snapshot path is also printed (information that should be in `RestoreOutcome.snapshot_path`, which already carries it). Violates AD-10 for the library root.
- Fix: replace the `print` calls with `_log` records (already emitted) and surface the facts through `RestoreOutcome`; let `run_restore` (CLI root) print them from the outcome. Add `capsys` assertions to test_process_state.

## AR-6 — LOW — clean-release — Stale/incorrect claims about `os.environ`
- Where: cli/verify.py:169 ("nothing below a root reads `os.environ`"), cli/passphrase.py:6 ("nothing deeper reads the environment"), vs cli/backup.py:612 (`environ=os.environ` in `drive_backup`), cli/restore.py:1225, :1240 (`drive_restore`). api/operations.py's docstring and docs/api.md's environment table do disclose the driver reads; the two driver docstrings are the stale ones.
- Consequence: a library caller wanting an isolated environment (tests, multi-tenant host) cannot supply one for backup/restore credentials; readers of the docstrings are misled. Status: read (grep).
- Fix: correct the docstrings; consider an `environ: Mapping[str, str] | None` parameter on `drive_backup`/`drive_restore` so the library can pass an explicit one.

## AR-7 — LOW — adversarial — A joined Dry-Run context with `dry_run=False` silently becomes a rehearsal
- Where: cli/context.py `join_or_open` (checks only `dry_run and not ctx.dry_run`); api/operations.py passes `context` straight through.
- Reasoning: reverse direction is refused (good), but `restore(..., dry_run=False, context=<dry-run ctx>)` uses the joined Run's no-op adapter set while the caller asked for a real restore; the outcome then reports `dry_run=False` semantics on a no-op database report (`require_database_restored` then raises `DatabaseNotRestoredError` for restore, a misleading error; backup returns a result whose `performed` is False). Status: read. Note `RunContext` has no public constructor (api.md: "v0 offers no public way to open one"), so reachability is limited to internal callers today.
- Fix: refuse `not dry_run and ctx.dry_run` with `UsageError(arguments=("dry_run","context"))`.

## AR-8 — LOW — adversarial — `UsageError.arguments` carries CLI flag spellings on library paths
- Where: cli/restore.py (`arguments=("--overwrite-wp-config",)`, `("--archive-database",)`, `request.relocation.given`), adapters/database.py (`("--admin-tls",)`, `("--admin-tls-ca",)`), vs api/operations.py:162 which uses parameter names (`("passphrase","encrypt")`); relocation messages cite `--wp-config`.
- Consequence: the O-1(b) structured field is inconsistent: a programmatic caller matching on `exc.arguments` gets flag names for some errors, parameter names for others, so it cannot map an error to the argument it passed (`config_policy`, `archive_database`, `admin_tls`). Status: read.
- Fix: fix one vocabulary (parameter names, with the CLI root translating via `_cli_flag`), and add it to test_boundary.

## AR-9 — LOW — adversarial — Passphrase handling edge cases in the library
- Where: cli/passphrase.py:80-85 (`if explicit:`), api/operations.py:155-164, :218-229.
- (a) `backup(encrypt=True, passphrase="")` ignores the empty string and falls through to `L3IO_WP_BACKUP_PASSPHRASE`, then `prompt`: a caller whose variable was unset silently picks up an ambient secret from the host environment. (b) `verify(authenticate=False, passphrase="x")` ignores the passphrase while `backup` refuses the same misuse "rather than ignored" (docs rationale: an Archive is never written plain by omission; for verify the cost is a keyless pass the caller thought was authenticated). Status: read.
- Fix: treat `explicit == ""` as `PassphraseUnavailableError`/`UsageError`; refuse `passphrase` without `authenticate=True` in `verify`.

## AR-10 — LOW — adversarial — Plain-`str` secrets survive `pickle`/`dataclasses.asdict` on public value types
- Where: domain/credentials.py `ExplicitCredentialSpec` (repr is guarded, pickle and `asdict` are not), cli/restore.py `RelocationFlags.new_db_password`, adapters/crypto.py `PassphraseCipher`. `SecretRegistry` blocks pickling (`__reduce__`), these do not. Status: VERIFIED (`S.encode() in pickle.dumps(spec)` True; `repr` False; `asdict` contains it).
- Consequence: the AD-7 repr guarantee is the only guard; a host that logs `dataclasses.asdict(spec)` or ships a spec to a worker puts the password in a log or on a wire. The caller already holds the value, so impact is limited.
- Fix: `__reduce__` raising `TypeError` as `SecretRegistry` does, or a `SecretStr` wrapper.

## AR-11 — LOW — adversarial — Library misuse becomes `UnexpectedError` ("a defect")
- Where: api/errors.py:264-277; cli/restore.py `RestoreRequest.__post_init__` (ValueError/TypeError), `BackupRequest` construction; domain `resolve_flags` `KeyError`.
- Reasoning: passing `scope="all"` (str), a `mode` of the wrong type, or an `OverrideSet` the AD-25 table would never produce, produces a non-package exception, logged at ERROR with traceback and labelled a defect of this package. There is no input validation at the API edge, so a caller bug is reported as a library defect. Also typed combinations the CLI table rejects (`FlagCombinationRejectedError`) are not checked for the typed (mode, overrides) pair. Status: read.
- Fix: validate argument types/enum membership in `operations.py` and raise `UsageError(arguments=<parameter names>)`.

## AR-12 — LOW — adversarial — `RunContext` is public but cannot be obtained; the `context` parameter is untestable surface
- Where: api/__init__.py `RunContext` in `__all__`, `context=` on all three entry points; docs/api.md:58 states there is no public way to open one.
- Consequence: a published, versioned (from v1.0) parameter with joining semantics, `RunContextActiveError`, and the AR-7 hazard that no caller can use; any future `open_run` export constrains these semantics. Status: read.
- Fix: either remove `context` from the public signatures until a public opener exists, or export the opener in the same epic.

## AR-13 — LOW — adversarial — structlog contextvars: `run_id`/`dry_run` keys overwritten for the host during a call
- Where: cli/context.py:220-221 (`bind_contextvars(run_id=..., dry_run=...)`, reset via tokens).
- Reasoning: a host that binds its own `run_id` (common name) sees it replaced for every log record emitted by other code in that context during the call, and restored afterwards; `add_trace_ids`/`correlation_id` guidance in tracing docs does not mention the collision. Status: read.
- Fix: namespace the keys (`l3io_run_id`) or document.

---

## Clean-release checklist (diff scope: src, tests, docs, pyproject.toml)
- Secrets/credentials in changed files: none found. Grep of added lines for private keys, AKIA/ghp_/xox tokens, IPs, hostnames, home paths found only synthetic test sentinels (`sentinel-search-0123456789abcdef`, `planted-secret-value`, `not-a-real-secret`). Verified by grep.
- Debug artifacts / breakpoints / `pprint` / stray `print` in src: none new except the two intentional stderr prints in AR-5 (flagged there).
- TODO/FIXME/XXX/HACK in added lines: none.
- Commented-out code: none found (grep of added comment lines for statement-shaped text).
- `# type: ignore`/`noqa` additions: all in tests or justified with a reason on the line (`noqa: PTH101/116/113`, `S603`, `F403`); no new `type: ignore` in src.
- Dead code: `ruff check` on api/, adapters/tracing.py and core/config_location.py passes; `vulture` is not installed so a dead-name sweep was not run (not covered). `EXECUTABLE_NAME`, `INSTRUMENTATION_NAME` and `phase_span_name` are used only inside tracing.py and its `__all__` except via tests (acceptable).
- Stale comments: AR-6.
- Untracked, outside the diff: `.codex/` is untracked and not ignored in the working tree (not part of the epic diff); worth a `.gitignore` entry or deletion before release. Not verified what it contains.

---

Severity count: CRITICAL 0, HIGH 1, MEDIUM 4, LOW 8 (adversarial 12 + clean-release 1; AR-5 and AR-6 carry the clean-release tag)

DONE — 13 findings (1 HIGH, 4 MEDIUM, 8 LOW); 5 verified by running snippets (AR-1, AR-2, AR-3 race, AR-4, AR-10), rest by reading; no repo files modified
