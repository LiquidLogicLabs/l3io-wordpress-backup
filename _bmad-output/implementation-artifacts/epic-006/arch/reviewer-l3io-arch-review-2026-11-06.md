# Arch gate review — E006 "Embed it in Python automation"

- **Reviewer:** l3io-arch-review, Mode B (gate review)
- **Date:** 2026-11-06
- **Scope:** `epic-006/sprint-01/stories/E006-S01-00{1,2,3,4}.md`. Story line numbers below are the lines **before** this gate's edits.
- **Checked against:**
  - the built code at `e0902df` (E002 and E003 closed). `HEAD` has since moved to `6f3c5c4`, which only adds E004-S01-001's exclusion files; no file cited here changed;
  - `git log 3e9f80a..HEAD`;
  - `ARCHITECTURE-SPINE.md` (AD-4, AD-8, AD-10, AD-13, AD-14, AD-16, AD-22, AD-24, AD-26, AD-27, Consistency Conventions);
  - PRD §5.7 (FR-22, FR-23, FR-24) and §8;
  - ADR-0011, ADR-0015 (amendment), ADR-0016, ADR-0018, ADR-0022, ADR-0024, including the 2026-11-06 amendments;
  - `docs/closeout-gate.yaml` (no item is owned by an E006 story);
  - `epics.md` and `plan-2026-10-27-v2.yaml`;
  - the E004, E005, E007 and E008 stories;
  - the owner's global rules.
- **Method:**
  - Every interface, file and test the stories name was looked up in `src/` and `tests/`.
  - The exception hierarchy was walked by importing every module, which found 68 `BackupError` classes. Their `__init__` parameters were read, and their `raise` and call sites were found by `ast` (scripts in `/tmp`, run with `uv run python`; nothing in the repository changed).
- **Edits:** the four story files, marked `*(E006 arch gate, 2026-11-06, <id>)*`, plus this report. No AC block or frontmatter was changed. `check-pointers` passes for each story and `check-links` passes.

## Executive summary

The four stories were written before E002 and E003 were built, and their interfaces are stale against the built code in the same way the E004 and E005 gates found.

- **E006-S01-001 (drive from Python).**
  - Its `restore` cannot construct the built `RestoreRequest`: there is no `config_policy`, `admin_tls`, `archive_database`, `relocation` or `reset_existing_db_user`.
  - Its `verify` has no `--authenticate`.
  - It returns where the CLI raises: on a failed verification, and on an unrestored database.
  - It names the wrong encryption errors.
  - It skips the plan's `extra_tools`.
  - It writes a second storage-target grammar.
  - Its parity test is red on arrival, because of `doctor`.
- **E006-S01-002 (typed failures).**
  - Two of its three mechanical gates are red on arrival against built classes it may not edit: seven classes have no `raise X` site, and many concrete classes carry a message only.
  - Its log and span claims put the boundary inside the Run. The Run actually closes first.
- **E006-S01-003 (public surface).** Its hand-kept list of `cli/` re-exports conflicts with the two stories beside it.
- **E006-S01-004 (spans).**
  - The adapter as specified fails the AD-4 `dry_run` gate and does not match the built `Tracer` port.
  - It duplicates the built `GuardedTracer`.
  - Its phase-span test is red for an encrypted backup.
  - Its "no cost" and redaction proofs check nothing on the API path.

Everything the built code, an ADR, the PRD or a recorded decision settles is edited into the stories: 36 of 39 findings. Four questions need the owner: O-1 to O-4.

**Totals:** BLOCKER 3, MAJOR 19, MINOR 17; fixed 36; owner decisions 4; cross-epic edits 5.

**Most important action:** the owner decides O-1, because E006-S01-002's FR-23 field criterion has no buildable path otherwise, and O-3, because E006 and E007 collide on `api/errors.py` in the same phase.

## Findings

Settled by: **code** = the built code; **decision** = an ADR, the PRD or a recorded decision; **owner** = needs the owner.

### E006-S01-001 — drive backup and restore from Python

| # | Sev | Principle | Story:line | Code:line | Finding | Fix | Settled by |
|---|---|---|---|---|---|---|---|
| A1 | BLOCKER | Core §3 DbC; FR-22 parity; AD-5 | S01-001:92-106 | `cli/restore.py:759-802`, `:1652-1679` | `restore()` cannot construct the built `RestoreRequest`. `config_policy` has no default (AD-5, ADR-0023). `admin_tls` must be an `AdminTls`, or `__post_init__` raises `TypeError`. `archive_database`, `relocation` and `reset_existing_db_user` are required fields. So `--overwrite-wp-config`, `--archive-database`, `--admin-tls(-ca)` and `--reset-existing-db-user` are unreachable from the API. E007-S01-001 L58 expects the API to pass the relocation value. `opted_out_by` would name the CLI flag. | The signature gains `config_policy` (no default), `admin_tls` (`None` becomes `AdminTls(None, None)`), `archive_database`, `reset_existing_db_user` and `relocation` (`None` becomes `RelocationFlags.none()`). `opted_out_by` names the library argument. Tests compare the request with the one `run_restore` builds. | code |
| A2 | MAJOR | FR-22 parity; ADR-0016 and ADR-0024 amendments (2026-11-06) | S01-001:86-90, :111 | `cli/verify.py:72-81`, `:122-166`, `:169-191`; `cli/parser.py:475` | "`verify` takes no key, because verification is keyless" predates `verify --authenticate` and the decision of 2026-11-03. The built `VerifyRequest.authenticate` and `drive_verify(passphrase=, environ=, prompt=)` exist. | `verify(authenticate=False, passphrase=None, prompt=None, context=None)`. `environ` is `os.environ` only when authenticating, as `run_verify` does it. | code, decision |
| A3 | MAJOR | PRD FR-23 ("Failures reach a library caller as typed exceptions"); one sequence | S01-001:84-106, :221 | `cli/restore.py:420-442`, `:1689`; `cli/verify.py:133-141`, `:186-187` | The CLI root turns two driver results into failures after the driver returns: a failed verification raises `VerificationFailedError`, and an unrestored database raises `DatabaseNotRestoredError`. The story's API returns both silently. | The API calls the imported `require_database_restored` and raises `VerificationFailedError(result)`, never a copy. Spy tests check both. | decision, code |
| A4 | MAJOR | Core §3; contradicts built code | S01-001:134, :208 | `cli/passphrase.py:65-71`, `:106-125`; `cli/restore.py:942-946`; `cli/verify.py:105-112`; `adapters/crypto.py:85-88`; `domain/errors.py:466-494` | Wrong errors. `backup(encrypt=True)` with no passphrase gives `PassphraseUnavailableError` (USAGE), not `PassphraseRequiredError`. A prompt EOF raises; it does not "resolve none". A wrong passphrase gives `PassphraseRejectedError` (8), not `ArchiveDecryptionError`. The restore prompt label is `RESTORE_PROMPT`. | Corrected under Error handling and the cipher bullets. | code |
| A5 | MAJOR | AD-13; owner decision 2026-10-28 | S01-001:124, :134 | `cli/backup.py:222-240`; `cli/restore.py:787`; E008-S01-001 L64-69, L78 | No `extra_tools`. "Each root passes `extra_tools=cipher.required_tools()`" (*Resolved 2026-10-28, Shawn*, E008-S01-001 L78). The API is a root, so without it the CLI passphrase backend's `age` would never be preflighted for an API Run. | The API passes `extra_tools` for backup and restore. It documents the interim exit-7 behaviour before `RootPreflight`, and tests use the binder backend (A17). | decision |
| A6 | MAJOR | Core §2 reuse (one grammar) | S01-001:114 | `cli/backup.py:494-513` | The story parses strings with `parse_storage_target` directly. The CLI's one grammar is `storage_target_from_cli`, which refuses a host, query, fragment or NUL and makes the path absolute. Using `parse_storage_target` gives the library a looser second grammar. | Use `storage_target_from_cli(os.fspath(value))`, imported. | code |
| A7 | MAJOR | Stale cross-epic contract | S01-001:116, :209 | `adapters/database.py:519-522`; E005-S01-001 L49, L119 | "`AwsSecretCredentialSpec` raises `UsageError` naming Epic 5 until E008 binds." E005-S01-001 removes `_AWS_NOT_BOUND` in phase 2, not E008. A test asserting the old error would go red. | `api/` adds no check. The test spies on the spec reaching `cli/credentials` unchanged. | code |
| A8 | MAJOR | Global rule 3 (prose that overstates); AD-26 | S01-001:231 | `cli/backup.py:609-614`; `cli/restore.py:1220-1232` | "The library reads `os.environ` at one point only … nothing deeper reads the environment" is false. The drivers pass `os.environ` to the credential builders, and E005 adds the credential variables. | Claim withdrawn; the variables read are stated; `docs/api.md` lists them. | code |
| A9 | MAJOR | AD-10 one schema; FR-22 parity | S01-001:132, :220 | E004-S01-001 L81-82; `adapters/exclusions.py` | Every root wraps an `ExclusionSet` in `LoggedExclusionPolicy`, which emits the exclusion records. The story's API does not, so an API backup's records would differ from the CLI's, against its own Observability criterion. `--list-default-excludes` has no library form. | Wrap a caller's set. Re-export `ExclusionSet` and `enumerate_defaults`. | decision (E004-S01-001, gated) |
| A10 | MAJOR | Global rule 4; buildable in its own loop | S01-001:258-259 | `cli/parser.py:109`, `:136-197` | The parity walk is red on arrival. `build_parser()` registers `doctor`, which is `NOT_YET_IMPLEMENTED` until E008. | Scope = registered subcommands minus the bare-subcommand keys of `NOT_YET_IMPLEMENTED`, both read from `cli/parser.py`, with planted cases. Who adds `api.doctor` later is **O-4**. | code (scope); owner (follow-on) |
| A11 | MINOR | AD-8 | S01-001:181 vs :210; :268 | `adapters/database.py:393-413`; `cli/backup.py:602` | The story contradicts itself: the extra is checked "after the lock" (L181) and "before the lock" (L210). Built: after, and for **every** backup and restore, because the descriptor itself is parsed by database. | Corrected; the base-install test also asserts a `files` backup. | code |
| A12 | MINOR | Core §6 current state | S01-001:123, :206 | `cli/restore.py:1133-1143`, `:483-525`, `:1343` | The restore step order is stale. FR-7 refuses at step 6, as `NonEmptyTargetRefusal`, whose message names CLI flags. | Built order cited; the refusal's fields are named as the library contract. | code |
| A13 | MINOR | Global rule 3; pytest only | S01-001:272 | `.github/workflows/ci.yml:65-66` | CI runs `mypy --strict src`, so `tests/typing/api_consumer.py` is never checked unless `ci.yml`, a shared file, is edited. | A pytest test runs `mypy.api.run` over the script, with a planted case. | code |
| A14 | MINOR | BL-E003-050 | S01-001:98 | `cli/restore.py:788`; `adapters/restore_runner.py:347-352` | `opted_out_by` defaults to `--no-safety-snapshot`, so a library `SKIP` would log the CLI flag. | Name the argument (orchestrator decision). | code |
| A15 | MINOR | FR-24; global rule 4 | S01-001:170 | `domain/restore.py`; `adapters/database.py` (`AdminTls`); `cli/restore.py:653`; `ports/exclusion.py`; `core/exclusions.py` | The re-export list misses the types the corrected signatures name. | Add them. A check derives the rule from the signatures' annotations (orchestrator decision under rule 4). | code |
| A16 | MINOR | Plan ordering | state `E006-S01-001.yaml` `depends_on` | — | It uses E004-S01-003's factory and `required_tools()`, and E005-S01-001's credential steps, but depends on neither. | Story sequencing note added. **State edit listed below** (not made). | code |
| A17 | MINOR | Buildable in its own loop | S01-001:208, :273 | E008-S01-001 L69 | Before E008's `RootPreflight`, an encrypted Run under the CLI backend refuses at `PREFLIGHT` (exit 7). | Tests use the binder backend. | decision |
| A18 | MINOR | Edge case | S01-001:139 | `cli/context.py:331-337` | `dry_run=True` joining a real Run raises `UsageError`. This is not stated. | Added. | code |

### E006-S01-002 — catch typed failures

| # | Sev | Principle | Story:line | Code:line | Finding | Fix | Settled by |
|---|---|---|---|---|---|---|---|
| B1 | MAJOR | Global rule 4 (a guard red on arrival) | S01-002:151-155 | `adapters/lock.py:154-155`; `domain/preflight.py:117`; `core/restore.py:1090`; `adapters/database.py:260-265`; `cli/restore.py:508`, `:1343` | The raise-site walk, counting only names after `raise`, misses seven exported classes that are raised: `SiteLockedError`, `PreflightFailedError`, `IncompleteRunError`, `DatabaseAuthPluginRefusedError`, `DatabaseTlsHandshakeError`, `DatabaseTlsVerificationError` and `NonEmptyTargetRefusal`. Each is constructed, then raised through a variable or a helper. | A construction `X(...)` counts as a raise site. The planted case stays. | code |
| B2 | BLOCKER | FR-23; buildable in its own loop | S01-002:26 (AC), :120, :156 | `domain/errors.py:97-107`, `:205-210`, `:298-308`, `:440-446`, `:466-494`; 28 bare `raise UsageError(msg)`/`raise RefusedError(msg)` sites (e.g. `cli/backup.py:361, :373`; `cli/restore.py:993`; `adapters/journal.py:425, :431`) | The story assumes Epic 2's classes all carry fields, and they do not. Message-only concrete classes: `UsageError` and `RefusedError` (raised directly); `PassphraseRejectedError` and `PayloadDamagedError` (no argument; E004 adds one optional keyword to the latter); `DumpFailedError`, `LoadFailedError` and `MalformedManifestError` (`reason` only). Their owners are closed, and this story may not edit them. The AC cannot be met in E006's loop. | **O-1.** Meanwhile the field test is built with a named exemption mapping checked for staleness, and the exemptions are listed. | **owner** |
| B3 | MAJOR | AD-10; Core §3 | S01-002:94, :128 | `cli/context.py:270-304`; `cli/exit.py:113` | `unexpected_exception` is claimed to come "before `run_finished`" and to carry the Run's `run_id`. That is impossible: the boundary wraps the entry point, and the Run opened inside the driver has already closed and unbound its id. The id travels on the exception as `RUN_ID_ATTR`. | After `run_finished`, with `run_id=getattr(exc, RUN_ID_ATTR, None)`. | code |
| B4 | MAJOR | Core §3; one statement per fact | S01-002:130; S01-004:87 | `cli/context.py:275-290` | "`error.type` is the exported class's name", and "after E006-S01-002 always a package type". Both are false. `open_run` writes `run_finished.error` and calls `set_outcome(error_type=type(error).__name__)` inside the Run, from the raw exception. | Both stories now say the log and span name the original type, a name and never a value. `docs/api.md` says so. | code |
| B5 | MAJOR | Parallel-phase ownership | S01-002:64-70 | E007-S01-001 L131-138; E007-S01-002 L111-117; E007-S01-003 L136-138; `epics.md` Epic List | E007, in the same phase, adds `BackupError` subclasses in its own modules. The bidirectional export test then fails in whichever of E006/E007 lands second, and `api/*` is E006's alone, so no story may add the lines. | **O-3.** The story states the collision and exports what exists when it builds. | **owner** |
| B6 | MINOR | Global rule 4 | S01-002:161 | — | "Every callable in `api.__all__` is wrapped" includes classes, `configure_logging` and `add_trace_ids`, so the test is red. | Scope = functions defined in `api/operations.py`, non-empty. | code |
| B7 | MINOR | Core §6 | S01-002:98-106 | `domain/errors.py:287`, `:358`, `:596` | Stale field lists: `RunContextActiveError(active_run_id)`, and the extra fields of `SiteLockedError` and `ConfigWriteFailedError`. | Corrected. | code |
| B8 | MINOR | Shared test file | S01-002:162 | `tests/conftest.py:211-235`; E004-S01-003 edits `conftest.py` | The fixture plants only the password, plus optionally the passphrase. | Plant the admin and passphrase positions in-test; `conftest.py` is not edited (as E005 M-5). | code |

### E006-S01-003 — know what is public

| # | Sev | Principle | Story:line | Code:line | Finding | Fix | Settled by |
|---|---|---|---|---|---|---|---|
| C1 | MAJOR | Global rule 4; consistency between sibling stories | S01-003:93 | `cli/restore.py:344, :483, :527, :574, :653` | Only four `cli/`-defined names are allowed in `api.__all__`. S01-002 must export four `cli/restore.py` exception classes, and S01-001 exports `RelocationFlags`, so the test is red, and the list is hand-kept besides. | Exceptions are governed by S01-002's walk. Any other `cli/`-defined public name must carry a fixed note in `docs/api.md`, derived from `__module__` (orchestrator decision under rule 4). | code |
| C2 | MINOR | Core §6 | S01-003:69, :72 | seven `__init__.py`; `docs/logging.md`, `docs/restore.md` | Stale: all seven package `__init__` files declare `__all__`, and `docs/` already holds two reference pages. | Corrected; `docs/api.md` links `docs/logging.md`. | code |
| C3 | MINOR | Global rule 4 | S01-003:84-86 | `pyproject.toml` `[tool.importlinter]` | The layers list holds `"…cli : …api"` as one entry. Read whole, `api` is never classified. | Split on `:` and assert `cli` and `api` are both found. | code |
| C4 | MINOR | Plan ordering | state `E006-S01-003.yaml` | — | The bidirectional docs check covers S01-002's and S01-004's public names, but S01-003 depends only on S01-001. | Sequencing note; **state edit listed**. | code |
| C5 | MINOR | FR-24 documentation | S01-001:47, :170 | `cli/context.py:104-148` | `RunContext` and `context=` are public, but nothing public returns a `RunContext`. | `docs/api.md` preface states it, along with the environment variables, the logging limits and the run-report naming. | decision (S01-001 Non-goals) |

### E006-S01-004 — spans

| # | Sev | Principle | Story:line | Code:line | Finding | Fix | Settled by |
|---|---|---|---|---|---|---|---|
| D1 | BLOCKER | AD-4 gate; Core §3 (port contract) | S01-004:79, :82 | `ports/tracing.py:38-80`; `cli/context.py:225-234`; `.github/workflows/ci.yml:101-105`; `tests/contracts/test_no_dry_run_outside_roots.py`; E002-S01-004 L152 | The adapter specifies `run_span(..., dry_run: bool, ...)` and sets a `dry_run` attribute. The built port takes `attributes: SpanAttributes`, because the AD-4 grep forbids the word `dry_run` outside `cli/` and `api/`, and `adapters/tracing.py` would fail it. | Implement the built port, set `attributes` verbatim, and keep the word out of the module. | code |
| D2 | MAJOR | Spine AD-24 vs built | S01-004:80-82, :150-151 | `cli/context.py:230`; spine AD-24; `epic-002/epic-closure/arch-drift-review.md` L52 | The built root keys are `l3io.operation`/`l3io.dry_run`, the spine names `dry_run`, and the story names `wpbackup2.operation`/`dry_run`. The E002 closure deferred recording the rename to this adapter. A public span schema needs one spelling. | **O-2.** The adapter is made spelling-agnostic: verbatim keys, and a test that reads them from `open_run`'s mapping. | **owner** |
| D3 | MAJOR | Core §2 reuse; AD-10 one record | S01-004:138 | `adapters/noop.py:27-31`, `:157-300`; `cli/context.py:212-213` | The story's `tracing_error` wrapper duplicates the built `GuardedTracer`, which already logs `tracer_failed` once and disables tracing. One failure would get two records. | No wrapper in the adapter; the test drives `GuardedTracer`. | code |
| D4 | MAJOR | AD-10, AD-24 | S01-004:97 | `cli/logging.py:59-75`, `:92-103` | "`add_trace_ids` sits in `configure_logging`'s chain". The slot is the `trace_processors` keyword, nothing fills it, and the CLI passes `()`. | A host passes `trace_processors=(add_trace_ids,)`. No wrapper, documented and tested (orchestrator decision under AD-10). | code |
| D6 | MAJOR | Global rule 4 (red on arrival) | S01-004:176 | `adapters/tar.py:448-455`, `:565-569`; `adapters/restore_runner.py:328` | "Children's `wpbackup2.phase` equal the steps, one each" is red for an encrypted backup (`ENCRYPT` is a child of `WRITE_ARCHIVE`) and for Snapshot phases. | Compare the spans with the Run's own `phase_completed` multiset, check descent from the root, and check the root's children against the restore plan, with a planted case. | code |
| D7 | MAJOR | Global rule 4 (false green); AC 2 and 4 | S01-004:190, :192 | `api` passes `command_args=None` (S01-001:124) | The zero-call `redact_argv` spy on the API path proves nothing, since nothing passes argv. No test shows `process.command_args` redacted at all. | Adapter-level tests under non-recording and recording spans, with a planted raw-argv adapter. | code |
| D8 | MINOR | File scope; plan ordering | S01-004:64-67; state `E006-S01-004.yaml` | S01-001:124 | S01-004 must edit `api/operations.py` to pass its tracer, but neither lists the file nor depends on S01-001. | File added; sequencing note; **state edit listed**. | code |
| D9 | MINOR | Consistency Conventions (adapter naming) | S01-004:66 | — | The tracer class is unnamed. | `OpenTelemetryTracer` (orchestrator decision under the naming convention). | decision |

(D5 was merged into B4.)

## Owner decisions needed

**O-1 (B2). FR-23's "structured fields" against message-only built classes.**
- **Question:** how does E006-S01-002 meet the criterion when concrete classes in closed E002/E003 modules carry a message only?
- **Options:**
  - **(a)** Grant E006-S01-002 the edits to `domain/errors.py` and the 28 bare `UsageError`/`RefusedError` raise sites. It would add specific subclasses with fields, for example a `UsageError` subclass carrying `argument` and `reason`.
  - **(b)** Define the rule as "every concrete class carries at least one named attribute, and `reason` counts". `UsageError`, `RefusedError`, `PassphraseRejectedError` and `PayloadDamagedError` are exempt by name in the test, with the exemption checked for staleness. The bare raises go to the backlog against a v1.0 owner.
  - **(c)** Leave the test red. This is not buildable.
- **Recommendation:** (b) for v0, plus a backlog item for the 28 bare raises.
  - The two authentication errors deliberately say nothing more. ADR-0024's amendment adds only an optional field name to `PayloadDamagedError`.
  - (a) reopens two closed epics and touches files E004 and E005 edit in phase 2.
  - Either way, record it in an ADR-0014 or FR-23 note.

**O-2 (D2). The span attribute names.**
- **Question:** the root span's operation and Dry Run keys. The built `l3io.operation`/`l3io.dry_run` (`cli/context.py:230`), the spine's `dry_run` (AD-24), or `wpbackup2.*`?
- **Options:**
  - **(a)** Keep the built keys, and amend AD-24 to name them.
  - **(b)** Rename them in `cli/context.py` to the spine's spelling, which needs a one-line grant into a closed E002 module.
- **Recommendation:** (a), with `wpbackup2.*` kept for the attributes the adapter sets itself (`wpbackup2.run_id`, `wpbackup2.phase`).
  - An unprefixed `dry_run` is likely to collide in a host's trace backend.
  - The built keys already exist, and their tests pass (`tests/unit/cli/test_tracing_hooks.py:131`).
  - The spine line needs the amendment either way.

**O-3 (B5). Exception exports from a parallel or later epic.**
- **Question:** E007, running beside E006 in phase 3, and any later epic add `BackupError` subclasses. `api/errors.py` must export them, but `api/*` is E006's alone.
- **Options:**
  - **(a)** Record in `epics.md`'s ownership notes that an epic adding a `BackupError` subclass adds its import and `__all__` entry to `api/errors.py`, and only that.
  - **(b)** E008, the binding epic, adds E007's exports. E007 would land red until then.
  - **(c)** Build `api/errors.py`'s `__all__` dynamically, which mypy cannot see.
- **Recommendation:** (a). It is the smallest grant, the export test enforces it mechanically, and it matches the 2026-10-27 single-line grant to E007 for `cli/restore.py`.

**O-4 (A10). `doctor` in the Python API.**
- **Question:** when E008-S01-001 binds `wpbackup2 doctor`, FR-22 ("every operation available on the CLI") requires an API equivalent, and E006's parity test will demand one. Who adds it?
- **Options:**
  - **(a)** Grant E008-S01-001 one entry, `api.doctor(...) -> Diagnosis`, in `api/operations.py`, calling the same driver function its `run_doctor` uses.
  - **(b)** Rule `doctor` a CLI-only diagnostic, and record an FR-22 exception plus a named parity exemption.
- **Recommendation:** (a). E005's `diagnose` already returns a value, so the API form is a few lines, and the parity test enforces it the moment `doctor` leaves `NOT_YET_IMPLEMENTED`.

## Cross-epic edits needed (not made)

1. **E007-S01-001, -002, -003:** each story that adds a `BackupError` subclass adds its export to `api/errors.py` and its `docs/api.md` heading, once O-3 is decided.
2. **E008-S01-001:** when it removes `doctor` from `NOT_YET_IMPLEMENTED`, it adds `api.doctor` (O-4 (a)), or the recorded exemption (O-4 (b)).
3. **E008-S01-001:**
   - Add API-path cases to `test_root_preflight.py`. The API root also passes `extra_tools` and gets `RootPreflight` through `drive_backup`/`drive_restore`, and the AC says no root assembles them differently.
   - State whether the changed `default_exclusion_policy()` returns the default set wrapped in `LoggedExclusionPolicy`. A library backup with `exclusion` omitted takes that default, and E006-S01-001 wraps only a caller's set.
4. **E004-S01-003 L137-145:** the superseded `build_payload_cipher(*, passphrase, required, secrets) -> AgePayloadCipher | None` paragraph sits right after the corrected one (L125-135) and contradicts it. Mark it superseded. E006-S01-001 cites L125.
5. **`epics.md` Epic List:** Epic 6 "Depends on Epic 2". It also consumes Epic 3 (`drive_restore`), Epic 4 (`build_payload_cipher`, `ExclusionSet`, `LoggedExclusionPolicy`) and Epic 5 (the credential steps). The phase order makes this safe; the table should say it.

**State edits in E006's own tree (not made; only story files were in scope):**
- `E006-S01-001.yaml`: add `depends_on` E004-S01-003 and E005-S01-001;
- `E006-S01-003.yaml`: add E006-S01-002 and E006-S01-004;
- `E006-S01-004.yaml`: add E006-S01-001.

## Checked and consistent (no finding)

- `cli : api` is one non-independent layer, and no `cli/` module imports `api/`.
- `drive_backup`, `drive_verify` and `drive_restore` take `ctx`, `tracer`, `secrets` and `command_args` as S01-001 L124 says. `open_run` adopts `secrets`.
- The root span opens before the lock (`cli/context.py:225` before `:249`), joining opens none, and `phase()` reaches the tracer through `ACTIVE_TRACER`.
- `RunContextActiveError` comes from `open_run`.
- `default_exclusion_policy()` is the one default.
- `py.typed` is asserted in the wheel. `built_distributions` and `test_the_check_would_catch_the_violation` exist.
- `test_repr_clean.py`, `test_single_redaction.py` and `test_logging_configured_once.py` exist with the shapes the stories reuse.
- The `otel` extra and `opentelemetry-api>=1.40,<2` are as stated, with no grpcio.
- No proposed test file name collides with an existing one under pytest's default import mode.
- `docs/closeout-gate.yaml` has no item owned by an E006 story.

## Per-principle walkthrough

| Principle | Result |
|---|---|
| Core §1 Separation of concerns | PASS. `api/` composes the drivers; no second sequence (A3 keeps the roots' rules imported) |
| Core §2 Reuse | A6, A9, D3 |
| Core §3 Design by contract | A1, A2, A4, B3, B4, D1 |
| Core §4 Testability / own loop | A10, A13, A17, B1, B2, B6, D6, D7 |
| Core §6 Current state | A12, B7, C2 |
| Core §7/§8 Dependencies | PASS. No dependency added; `opentelemetry-api` and `packaging` already present |
| Core §9 Logging | A9, B3, D3, D4 |
| Core §10 Documentation | C5; `docs/api.md` preface extended |
| Global rule 1 (never hand-roll) | A6 (second grammar), D3 (second guard); otherwise PASS. The SDK's in-memory exporter, `mypy.api` and `packaging.requirements` are used |
| Global rule 3 (enforce mechanically) | A8, A13 |
| Global rule 4 (scope from the source of truth) | A10, A15, B1, B6, C1, C3, D6, D7 |
| Parallel and later epics | B5 (E007), A10 (E008), cross-epic list |
| ADR-0016/0022/0024 amendments (2026-11-06) | A2 (authenticate), A12 (stderr warnings). Otherwise PASS |

## Gate

The BLOCKERs A1 and D1 are fixed in the stories. B2 is open on O-1. Every MAJOR is fixed except B5 (O-3) and D2 (O-2). E006 may build once O-1 and O-3 are decided. O-2 and O-4 can follow: the adapter is agnostic to O-2, and O-4 bites only when E008 binds `doctor`.

DONE — BLOCKER 3, MAJOR 19, fixed 36, owner decisions 4, cross-epic edits 5
