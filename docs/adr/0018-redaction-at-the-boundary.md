# ADR-0018: Redaction is applied where text leaves the program, not inside domain errors

- **Status:** Accepted
- **Date:** 2026-10-27
- **Deciders:** Shawn Anderson
- **Epic:** E002 (stories E002-S01-006 redaction, E002-S01-004 logging chain, E002-S01-005 exit rendering); E006 (`api/`, tracing story 6.4) consumes it
- **Resolves:** arch gate finding AG-36 (2026-10-27)
- **Departs from spec:** n/a
- **Principle(s) in tension:** AD-1 (the pure layers never face outward), AD-7 (no credential in any log, `repr` or exception raised to a caller), AD-14 (structured errors, no credential in a message)

## Context

E002-S01-006 said "`BackupError.__str__` passes its message through the Run's `redact`
when one is active". `BackupError` lives in `domain/errors.py`. The redaction
implementation (`SecretRegistry`, `redact`) is in `adapters/redaction.py`, and the active
Run is a `ContextVar` in `cli/context.py`. AD-1 and the import-linter layer contracts
forbid `domain/` from importing either. A developer following the story would break the
layers or add an unreviewed hook.

AD-7's invariant is not in question. No credential may appear in any log record, span
attribute, `repr`, argument vector or exception raised to a caller. The question is where
the backstop runs.

## Options considered

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. Redact inside `BackupError.__str__`, importing the registry | Every `str(exc)` is redacted wherever it is called | `domain/` imports `adapters/` and `cli/`; `lint-imports` fails | Violates AD-1 |
| B. A `domain/`-level `ContextVar[Callable[[str], str]]` hook, identity by default, set by `open_run` | Layers intact; `str(exc)` redacted while a Run is active | A second redaction seam (`domain/` holding a redaction hook while S01-006's contract test forbids redaction code outside `adapters/redaction.py`); a global hook in the pure core; exceptions inspected after the Run ends are unredacted anyway | Bends AD-1's purity and S01-006's single-implementation rule |
| C. **Redact at the boundary: the composition roots and the logging processor chain they configure** | One implementation, applied where text actually leaves the program; `domain/` stays pure | `str(exc)` or `repr(exc)` called directly by a library caller is not passed through redaction | AD-1 intact; AD-7 held by its primary control plus a boundary backstop |

## Decision

**Option C.**

- **`domain/` errors carry structured fields and a message built without secrets** (AD-14).
  `BackupError.__str__` formats that message and does no redaction. Adapters re-raising a
  dependency's error build their message from structured fields (source label, host,
  database name) and never interpolate a credential or the original exception's text.
  `raise ... from` keeps the chain.
- **The single redaction implementation is E002-S01-006's, in `adapters/redaction.py`.**
  The boundary applies it:
  - **rendered exceptions:** `cli/` (`cli/exit.py`'s rendering of the error and any
    traceback to stderr) passes the rendered text through the Run's `redact` before
    writing it. The root renders while it still holds the Run's `SecretRegistry`;
  - **log records:** `redact_event`, installed by `cli/logging.py::configure_logging` as
    the last processor before the renderer, for structlog-native and stdlib records alike.
    It already walks `exc_info` and exception text in events;
  - **span attributes:** the tracing adapter that `api/` binds (Epic 6, story 6.4) calls
    the same `redact` on attribute values before export.
- No module in `domain/` or `core/` contains redaction code. The existing S01-006 contract
  test, which fails if any module other than `adapters/redaction.py` contains `"***"` or
  defines a function whose name contains `redact`, enforces this.

AD-7's invariant is preserved. Its primary control is unchanged: nothing builds a message,
log field or argument from a credential. Redaction is the backstop, and it now sits where
text leaves the program.

### What this does not cover

- **A library caller formatting an exception itself.** An `api/` caller that catches a
  `BackupError` and calls `str()`, `repr()` or `traceback.format_exception()` on it gets
  the unredacted text. The guarantee there is the primary control (messages built from
  structured fields), not redaction. The same applies to a chained dependency exception
  (`__cause__`). Its text is credential-free by `l3io-wp-database`'s contract (ADR-0015),
  and the test below asserts it rather than trusting it.
- **A host that configures logging itself** without E002-S01-004's `configure_logging`,
  so `redact_event` is not in its chain. E002-S01-006 already records this limit, and the
  `api/` documentation (Epic 6) states it.
- **Secrets that string matching cannot see:** URL-encoded, split across fields, or
  shorter than the registry's 4-character floor. These are recorded in S01-006.

### How the tests still prove no leak across a full run

E002-S01-006's leak test plants a known password, registered as a real Run would
register it. Across a full backup and a full verify (and, from Epic 3, a full restore,
through the reusable fixture) it asserts the password is absent from:

1. every captured structlog event and stdlib record, at every level;
2. the CLI's stdout and stderr, including the rendered error on failure-injected runs;
3. `str()`, `repr()` and `traceback.format_exception()` of **every exception the drivers
   raise**, taken directly with no redaction applied. This checks the primary control on
   exactly the surface Option C leaves uncovered;
4. the `repr` of every value the walked `__repr__` check covers, every constructed
   argument vector and environment, and the Manifest.

Item 3 is what makes the "not covered" path above tested and not just assumed: if a
domain message ever interpolated a credential, it would fail there, before any boundary
redaction could hide it.

## Consequences

- Positive: `domain/` stays importable without I/O or Run state, and `lint-imports` and
  `tools/check_purity.py` need no allowance.
- Positive: there is one redaction implementation and three call sites (CLI rendering, log
  processor, tracing adapter), all in layers allowed to import `adapters/`.
- Negative / trade-offs accepted: for library callers, redaction of exceptions they
  format themselves rests on the primary control alone. The leak test's item 3 is what
  keeps that honest.
- Follow-ups: E002-S01-006 removes the `BackupError.__str__` redaction sentence and adds
  item 3 to its leak test. E002-S01-005's error rendering applies `redact`. E006 story 6.4
  applies it to span attributes.
- **Revisit when:** a code path must put text from an untrusted or dependency source into a
  `domain/` error message verbatim. That would need redaction before the message is built
  (in the adapter raising it), not a hook in `domain/`.

## Amendment — 2026-11-06: no credential shorter than the floor is accepted (Shawn, option a)

Red team M-4 of the E005 closure (BL-E005-005): the registry deliberately skips values shorter
than its 4-character floor, so a database password of 3 characters or fewer was accepted and
never masked; and a resolved password was registered only after a successful resolve.

**Decided:**
- **Refuse short credentials.** A database password (WordPress or administrator account, from
  any source: `wp-config.php`, `--admin-password`, the environment, AWS Secrets Manager or
  `--new-db-password`) shorter than the registry's floor is refused at the credential boundary,
  before any connection. It is a `CredentialsError` subclass naming the source and the minimum
  length, never the value. The floor is one constant, the registry's, so the two cannot drift.
- **Register before use.** A password is registered with the `SecretRegistry` as soon as it is
  read from its source, before it is validated or used, so a failure after reading cannot print
  it.

The redaction guarantee then holds without an exception: every accepted credential is masked.

**Owner:** E008-S01-001, which binds every credential source and is granted the edits this
needs: `adapters/credential_providers.py`, `adapters/database.py`, `cli/credentials.py`,
`domain/errors.py` (one subclass) and their tests. A test plants a 3-character password through
each source and requires the refusal, with no connection attempted; another reads a password,
fails afterwards, and requires that the error output masks it.

**Implemented (E008-S01-001, 2026-11-07).** `CredentialTooShortError(source, minimum)` is raised
by `adapters/database.py`'s registering wrapper after the resolved password is offered to the
registry, and by `adapters/credential_providers.py` for an explicit spec as it is built; the
relocated account's password is held to the same floor in `cli/credentials.new_db_password`.
The floor is `adapters/redaction.MIN_SECRET_LENGTH`. An empty password (a variable set to `""`,
or `DB_PASSWORD` empty in `wp-config.php`) is refused the same way, still exit 4.

## Amendment — 2026-11-07: the library boundary logs frames, never a dependency's text (E006 closure)

`api/errors.py::public_boundary` logs `unexpected_exception` after the Run, when the Run's registry is
already closed, so the record cannot be redacted (red team RT-1, adversarial AR-2, arch AD-3).

**Decided:** that record carries the traceback **frames** with the message of every non-package
exception withheld (`cli.exit.render_failure`, the rule the CLI's failure report applies), and never
`exc_info`. The original, message included, is on `__cause__` for the host. Secrets held in the frames'
local variables are not covered by redaction (stated in `docs/api.md`); the wrapper type for secret
arguments is the `Secret` type of the next amendment.

## Amendment — 2026-11-07: an opaque `Secret` type for passwords and passphrases (owner decision, E006 closure decision 4 option a; built by E008-S01-001)

Recorded at E008 closure (arch finding AD-1 of epic-008/epic-closure/arch-drift-review.md): the decision was taken and built but written into no ADR.

**Decided (owner):** passwords and passphrases that cross the public boundary or the CLI/library seams are
held as `domain/secret.py::Secret`, re-exported from `api/`:

- `repr` and `str` give `Secret('***')`; it has no hash and is not equal to its plain `str`; two
  `Secret`s compare in constant time (`hmac.compare_digest`); pickling (and so `copy`) raises `TypeError`.
- `.reveal()` returns the text and is called only where the value leaves the package or where the
  `SecretRegistry` learns it: `adapters/credential_providers.py`, `adapters/crypto.py`,
  `adapters/redaction.py` and `domain/secret.py`. `tests/contracts/test_secret_reveal.py` derives that
  set from the source tree and fails on any other caller.
- It is accepted (`Secret | str`) for `ExplicitCredentialSpec.password`, `RelocationFlags.new_db_password`
  and the `passphrase` parameters of `api.backup/restore/verify`; a plain `str` is wrapped (`as_secret`) where
  the value is read. The CLI sources produce `Secret`s.

**Options not taken:** a pydantic `SecretStr` (rejected by the owner: no new runtime dependency for about
thirty lines; pydantic is not otherwise used); keeping plain `str` and relying on the registry (what this
ADR's frames decision left, and what BL-E006-009 recorded as the gap).

**Cost accepted:** `Secret` is not hashable, so a dataclass holding one is not hashable and cannot be
`copy`-ed or pickled (A-04); `len()` reveals the length. The owner of the type is this package.

**Revisit when:** a host needs to pass credentials across a process boundary (pickling), or the library moves
to the neutral module of BL-E006-024.

## Amendment — 2026-11-08: a short database password is accepted, with a warning (Shawn, owner decision 61)

The 2026-11-06 amendment above is **reversed for database passwords** (owner decision 61, for
unattended use: a site's existing account may have a short password, and refusing it blocked
the backup). A database password (the WordPress account, the administrator or
`--new-db-password`, from any source) shorter than the registry's 4-character floor is
**accepted**, with WARNING `credential_below_redaction_floor` naming its source and the floor,
never the value: it cannot be masked in logs. `adapters/redaction.note_below_redaction_floor` is
the one place that logs it; `adapters/database.py`'s registering wrapper logs it once per source
selected, `adapters/credential_providers.py` when an explicit spec is built, and
`cli/credentials.new_db_password` as the relocated password is read. Every password is still
offered to the registry as soon as it is read. `CredentialTooShortError` is gone (pre-1.0,
CHANGELOG). The redaction guarantee therefore holds for every password of 4 characters or more;
a shorter one is the operator's accepted, logged exception. The 12-character floor for a new
encryption passphrase (ADR-0024 amendment) stays, and the registry's own 4-character floor for a
passphrase is unchanged.

**Extended 2026-11-08 to decryption passphrases (Shawn, owner decision 69).** A passphrase that
decrypts (verify, restore) and is shorter than the 4-character floor is accepted the same way:
WARNING `credential_below_redaction_floor` naming its source, never the value, logged by
`cli/passphrase.resolve_passphrase` through `note_below_redaction_floor`; it was refused with
`PassphraseUnavailableError`. The registry's own floor is unchanged, so such a passphrase is not
masked in logs; the redaction guarantee holds for every passphrase of 4 characters or more. The
12-character floor for a new encryption passphrase stays (ADR-0024 amendment of 2026-11-08).
