# Red team report — E006 (epic-level, not sprint-level)

Scope: the epic diff `git diff b321422^1 83d73a7 -- src tests docs pyproject.toml` (stories S01-001..004), the four story files, ADR-0011/0015/0016/0018/0019/0024 and the architecture spine ADs named in the brief.
Read-only: the only file written is this report. The skill's sanctum was NOT initialised (it would write under `_bmad/memory/`, outside the permitted output path); no research cache was loaded and no live web research was done. Findings below rest on code reading and on runs with sentinel values (`uv run python -I`, scripts in `/tmp/rt6/`, no real credential).

Contract lines (verbatim from the brief):
- You have no inbox. No reply will arrive. If you need a decision you cannot make, write it to disk and end with `BLOCKED: <one-line reason>`. Never wait.
- Never end a turn on a question. Decide it yourself and write down what you assumed, or end with `BLOCKED:`.
- Never poll. Every poll is a full turn.
- Once you have written your final line you are done. Do not arm a wait or schedule a wake-up.
- Read what the task names; widen only with a reason. Stay under 100 turns; near the cap, write what you have and end.
- Your final line must be exactly one of `DONE — [brief metrics]`, `BLOCKED: [one-line reason]`, `FAILED: [one-line reason]`.

## Widening (and why)
- `cli/restore.py`, `cli/backup.py`, `cli/verify.py`, `cli/context.py`, `cli/exit.py`, `cli/passphrase.py`, `cli/credentials.py`, `cli/logging.py`, `adapters/redaction.py`, `adapters/noop.py`, `adapters/phase.py`, `adapters/wpcli.py`: the CLI-vs-API guard comparison and the log/exception data flows run through them.
- The installed dependency `l3io-wordpress-database` 0.3.2 (`_observe.py`): its spans nest under this package's phase spans and are exported to the same host provider.
- `pyproject.toml` source pins (supply-chain lens).

## Trust boundaries and entry points
Host application (trusted caller, but its logging/telemetry sinks and error trackers are weaker trust zones) -> `api.backup/verify/restore` -> `public_boundary` -> `drive_*` (shared with CLI) -> DB, wp-cli, filesystem, archive (untrusted input for verify/restore). Egress channels for secrets: structlog records, exception messages/chains/tracebacks, OTel span status/events, files.

## Guard-parity result (CLI flag vs API)
Compared every CLI option with the API surface; guards live in `drive_*`, so they hold on both roots: FR-7 non-empty guard, Site Lock, `--reset-existing-db-user` consent (`_require_reset_consent`), `--archive-database` identity confirmation, `--require-encrypted`, Safety Snapshot (explicit `SnapshotPolicy`, no default), `ConfigPolicy` (no default), Dry Run (no-op adapter set; a Dry Run cannot join a real Run), `admin_tls` (type-checked in `RestoreRequest.__post_init__`). Differences, none a bypass: the API takes `(mode, overrides)` directly instead of the AD-25 flag-combination table; `OverrideSet` may waive one refusal without the other; `ALL_CLEAN` is one enum value. No guard bypass found (OBSERVATION RT-9).

---

## Findings

### RT-1 — HIGH — Redaction is defeated for the one record the API boundary writes: `unexpected_exception` is logged after the Run's registry is closed (VERIFIED)
- Where: `src/l3io/wp/backup/api/errors.py:579-586` (`_log.error("unexpected_exception", ..., exc_info=exc)`); registry lifetime `adapters/redaction.py:open_registry` / `cli/context.py:open_run` (registry is live only inside the Run).
- Reasoning: `redact_open_runs` (the only redaction step in `configure_logging`'s chain) redacts with the registries live *at emit time*. For `context=None` the Run, and so its registry, is gone before the boundary logs. The CLI handles exactly this: `cli/exit.py:report_failure` re-opens the registry (`with open_registry(secrets)`) and withholds non-`BackupError` messages in its traceback (`_WITHHELD`). The API does neither.
- Reproduction (`/tmp/rt6/h1b.py`): `configure_logging("INFO","json")`; `backup(site, scope=FILES, encrypt=True, passphrase="SENTINEL-PASSPHRASE-9f3a")` with an unexpected `RuntimeError("... SENTINEL-PASSPHRASE-9f3a")` raised inside the Run. Result: the `unexpected_exception` JSON record on stderr contains the sentinel in `exception` (`RuntimeError: dependency echoed secret SENTINEL-PASSPHRASE-9f3a`). The same run's span status IS redacted, so the two sinks disagree.
- Aggravation (VERIFIED, default structlog config + `rich` installed): the traceback is rendered with locals and the `backup` frame's `passphrase = 'SENTINEL-PASSPHRASE-9f3a'` is printed to stdout (`/tmp/rt6/h3.py`). `rich` is not a runtime dependency here (dev tool only), so this needs a host with rich/`dict_tracebacks(show_locals=True)` (the latter UNVERIFIED).
- Reach: any `Exception` that is not an exported `BackupError` escaping a Run the call opened. Secret exposure additionally needs a dependency whose message echoes a registered secret (or locals rendering for the passphrase argument).
- Fix: in `public_boundary`, wrap the `_log.error` call in `open_registry(registry)` (needs the call's registry passed to the wrapper, or stamp it on the exception beside `RUN_ID_ATTR`), and log `exc_info` only as class + frames with non-`BackupError` messages withheld, i.e. reuse `cli.exit.render_failure`/`_frames`. Never pass `exc_info=exc` to a host-configured chain. Add a contract test that plants a registered sentinel in the escaping exception and asserts it is absent from captured logs.

### RT-2 — MEDIUM — Span status carries the full text of any exception, including dependency text; the CLI's withhold policy is not applied (VERIFIED)
- Where: `src/l3io/wp/backup/adapters/tracing.py:679-680` (`_fail`: `Status(ERROR, secrets.redact(str(exc)))`), used by `run_span` (line 768) and `phase_span` (line 787).
- Reasoning: only secrets in the registry are removed. The module's own docs and `cli/exit.py` state a dependency's text is untrusted. In the CLI the tracer is no-op, so this path is new with E006 and sends text to an external telemetry backend that is outside the host's log-retention controls.
- Reproduction (`/tmp/rt6/h2.py`, in-memory SDK exporter): `RuntimeError("s3 error https://x?token=UNREGISTERED-TOKEN-77aa")` from the verify phase. Both `l3io.wp.backup.verify` and `l3io.wp.backup.run` spans export status description `'s3 error https://x?token=UNREGISTERED-TOKEN-77aa'`. The token was never registered (AWS keys, signed URLs, tokens in subprocess stderr are all unregistered classes).
- Fix: status description only for `BackupError` (built from typed fields), else `type(exc).__name__` (`error.type` is already set). Extend `tests/contracts/test_tracing_redaction.py` with an unregistered-sentinel case.

### RT-3 — MEDIUM — Dependency database spans record exception events and `{error}` status text, unredacted, into the same provider; the module doc claims otherwise (VERIFIED at the dependency; end-to-end UNVERIFIED, needs a live DB)
- Where: installed `l3io/wp/database/_observe.py` lines 32 (`start_as_current_span` with default `record_exception=True`), 89-90 (`span.record_exception(error)`, `Status(ERROR, f"{type(error).__name__}: {error}")`); contradicted by `adapters/tracing.py` module doc ("No exception event is recorded ... so no stack trace or chained dependency text reaches a span") and `ports/tracing`.
- Reproduction (`/tmp/rt6/h5.py`): `operation("dump", conn)` raising `RuntimeError("driver said SENTINEL-DBPASS-31cc")` with the sentinel registered in a `SecretRegistry`: the exported span has status `RuntimeError: driver said SENTINEL-DBPASS-31cc` and an `exception` event with `exception.message` and `exception.stacktrace`. No registry is consulted. These spans are children of the E006 phase spans, so an SDK-configured host receives them.
- Reach: any database failure inside `backup`/`restore`/`verify --authenticate`-adjacent paths when a host runs an OTel SDK.
- Fix: (a) owner of `l3io-wordpress-database` stops recording message/stacktrace (or takes a redactor), tracked as a dependency issue; (b) until then, correct the claim in `tracing.py`/ADR-0011/`docs/api.md` to scope it to this package's own spans, and document that a host must scrub `exception.*` events and span statuses of `l3io.wp.database.*`; (c) add a contract test that fails when a dependency span carries an event under the E006 tracer.

### RT-4 — LOW — Typed errors do not survive pickling; the real failure is replaced by a `TypeError` in worker pools (VERIFIED)
- Where: `domain/errors.py` and `api/errors.py` classes with required keyword-only/positional `__init__` args and a single-message `super().__init__` (e.g. `UnexpectedError.__init__(cause_type, *, api_function)`, `PassphraseUnavailableError(variable, *, reason)`, `SiteLockedError(site_root, lock_path)`).
- Reproduction (`/tmp/rt6/h4.py`): `pickle.loads(pickle.dumps(err))` raises `TypeError: ... missing ... required keyword-only argument` for all three. A host using `ProcessPoolExecutor`/Celery gets `BrokenProcessPool`/`TypeError` instead of the error, losing `exit_code`, `snapshot_path` (the Safety Snapshot location after a failed restore) and the refusal type, so refusal handling that keys on `RefusedError` fails open to a generic handler. `__cause__` is also dropped by pickle (acceptable).
- Fix: define `__reduce__` on `BackupError` that rebuilds from stored fields, or make all constructors accept the single message; add a contract test round-tripping every exported class.

### RT-5 — LOW — `PR_SET_DUMPABLE` is toggled process-wide around each wp-cli child; concurrent library calls race (UNVERIFIED, from reading `adapters/wpcli.py:268-310`)
- Reasoning: the flag is per-process, not per-thread. Two threads: A reads 1, sets 0; B reads 0, sets 0; A restores 1 while B's child is still running, so B's child can read `/proc/<ppid>/environ` (the passphrase/admin password): the protection the code exists to give is silently dropped for B. The flag also stays 0 for up to `CHILD_TIMEOUT_S` (4 h) for the whole host (no core dumps, no same-user ptrace/py-spy). Neither was a concern for a one-shot CLI; both are process-global side effects for a library. It is not covered by `tests/unit/api/test_process_state.py`.
- Fix: serialise the toggle with a module lock plus a reference count (restore only when the last child exits), and document the host-visible effect in `docs/api.md` "Before you call it".

### RT-6 — LOW — `backup(target=None)` writes the Archive (DB dump with password hashes) into the host's current directory (VERIFIED by reading `operations.py:140`, `cli/backup.py:607`)
- Reasoning: for the CLI the cwd is an operator decision; for a long-lived service the cwd is incidental (`/`, an app dir, a web root). The file is mode 0600 (`adapters/tar.py:141`), which limits exposure to the same OS user, but a web server running as that user serves it.
- Fix: make `target` required in the API (consistent with the AD-5 stance on `scope`), or resolve the default against an explicit directory argument.

### RT-7 — OBSERVATION — Secrets are plain `str` parameters, so they sit in the locals of every frame of the exception chain
- `backup`, `verify`, `restore` receive `passphrase: str` and `ExplicitCredentialSpec(password=...)`; `UnexpectedError.__cause__` and every exported `BackupError` keep `__traceback__`, so an error tracker that captures locals (Sentry `include_local_variables`, rich, `dict_tracebacks`) records them. `repr` is guarded everywhere checked (`ResolvedPassphrase`, `ExplicitCredentialSpec`, `RelocationFlags.new_db_password` `repr=False`, `RunContext`, `SecretRegistry`, `_RunSpan`), but `dataclasses.asdict()`/`astuple()`/pickle of those values expose the secret by design. Record in `docs/api.md`; consider an opaque `Secret` type for v1.0.

### RT-8 — OBSERVATION — The redaction registry set is global and cross-Run
`adapters/redaction._OPEN` applies every live Run's secrets to every record (stated in the module). Effect: concurrent calls in one host redact each other's registered values; a 4-character secret rewrites unrelated text in other Runs' records. No leak; a diagnosability cost only. The `ACTIVE_TRACER` and structlog `contextvars` bindings are reset in `ExitStack` callbacks and did not leak between calls in the paths read; threads that do not inherit the context (worker threads) get an empty registry in `join_phase`.

### RT-9 — OBSERVATION — Guard parity holds, but the API weakens two CLI-side validations by construction
`restore` accepts `(mode, overrides)` without AD-25's flag-combination table, so `ALL_CLEAN` (clears the Site Root and drops the losing database) is one enum argument, and one FR-7 refusal can be waived alone. FR-7's guard, snapshot, lock and consent checks still run in `drive_restore`, so nothing is bypassed. A host that forwards user input into `RestoreMode(...)`/`Override(...)` is the new risk surface; worth one sentence in `docs/restore.md`/`docs/api.md`.

### RT-10 — OBSERVATION — Supply chain: the family dependencies resolve from TestPyPI in `[tool.uv.sources]`; published metadata names them by range only
`pyproject.toml` pins `l3io-wordpress-database`, `-config`, `l3io-age-passphrase` to `testpypi` (stated as development-only, removed by `uv build --no-sources`). A third-party consumer with a plain `pip install` resolves those names from PyPI, so the names must be claimed on PyPI before the first release or the library invites dependency confusion. Pre-existing and recorded in the pyproject comments; flagged because E006 is the epic that makes third-party installation real.

---

## Lens coverage
External attacker (untrusted archive/path inputs, log/span egress): RT-1..3. Malicious insider / host-side secrets in telemetry: RT-1..3, RT-7. Chaos engineer (threads, pools, process state, cwd): RT-4..6, RT-8. Abusive legitimate user (guard bypass via the library): RT-9, no bypass found. Design/architecture: RT-1/RT-3 (redaction reaches only what the package itself emits). AI-poisoning cross-cut: nothing in E006 consumes model or tool output; not applicable.

Checked and found sound: `public_boundary` message building (no dependency text in `UnexpectedError`); `UnexpectedError.__init__` fields; passphrase resolution chain and registration order; `SecretRegistry` pickle refusal; `RunContext`/`ResolvedPassphrase` reprs; Dry Run cannot join a real Run; archive staged 0600 with `O_EXCL|O_NOFOLLOW`; verify bounds manifest and extended header sizes; no `os.environ` mutation, chdir, umask, signal handler, atexit, warnings filter or tracer-provider setting in `src/` outside the CLI.

Severity count: CRITICAL 0, HIGH 1, MEDIUM 2, LOW 3, OBSERVATION 4
