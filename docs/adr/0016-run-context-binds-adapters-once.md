# ADR-0016: The Run Context is opened without adapters and binds them exactly once

- **Status:** Accepted
- **Date:** 2026-10-27
- **Deciders:** Shawn Anderson
- **Epic:** E002 (stories E002-S01-004, E002-S02-007, E002-S02-008); E003 (Safety Snapshot, story 3.3) and E006 (`api/` drivers) consume it
- **Resolves:** arch gate finding AG-31 (2026-10-27)
- **Departs from spec:** _bmad-output/planning-artifacts/architecture/architecture-py-wordpress-backup-2026-09-15/ARCHITECTURE-SPINE.md#ad-16--one-run-context-created-by-the-outermost-composition-root
- **Principle(s) in tension:** AD-16 (one Run Context, bound adapter set), AD-19 (the Site Lock before credentials), Core §3 contract coherence and single ownership

## Context

AD-16 lists the Run Context's contents as "Run ID, workspace, logger, Site Lock, clock,
bound adapter set", created exactly once by the outermost composition root. E002-S01-004
wrote that literally: `open_run(*, adapters: AdapterSet, …)` and a frozen
`RunContext.adapters: AdapterSet`.

The adapter set cannot exist at that moment. Its database halves are built from the
WordPress credential source, which comes from `wp-config.php`. E002-S02-008 requires the
Site Lock to be held before `wp-config.php` is read, and the lock is taken inside the Run.
So `drive_backup`'s own step list (open the Run and take the lock, locate `wp-config.php`,
build the descriptor, build the credential source, then `real_adapters(...)` or
`dry_run_adapters(...)`) needs the adapters after `open_run` has already demanded them.

Separately, `AdapterSet` had two owners: E002-S01-004 typed it on the Run Context and
E002-S02-007 declared that it defines `AdapterSet` and its only two constructors.

## Options considered

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. Keep `adapters` as an `open_run` argument | Matches AD-16's wording | Cannot be satisfied: the adapters need credentials, credentials need `wp-config.php`, and that read must follow the lock taken inside the Run | Breaks AD-19's ordering or forces reading `wp-config.php` unlocked |
| B. `open_run` takes an adapter *factory*, called after the lock | One construction call site | The factory needs the descriptor and credential source, so it closes over values the driver computes mid-Run; the ordering is hidden inside a callback | Honours AD-16; obscures the step order that S02-008's lock test asserts |
| C. Take `AdapterSet` off the Run Context and pass it beside it to `backup(...)` | Simple types; no mutable field | A nested entry point (the Safety Snapshot) must be handed a second value and could be handed a different set than its outer Run's | Departs from AD-16's "bound adapter set"; reopens the forked-Run risk AD-16 prevents |
| D. **Open the Run without adapters; the composition root binds them exactly once after the lock and the `wp-config.php` read** | Step order stays explicit in the driver; one adapter set per Run; joiners inherit it | The Run Context has one write-once field and two new programming errors | Keeps AD-16's invariant (one context, joined not re-created, one bound set); changes only *when* the set is bound |

## Decision

**Option D.**

- `open_run(...)` creates the Run Context **without** adapters. Everything else AD-16 lists
  (Run ID, workspace, logger, clock, secret registry, the Site Lock slot) is set at
  creation as before.
- The composition root that **opened** the Run binds the adapter set exactly once, after
  the Site Lock is taken and `wp-config.php` is read:
  `ctx.bind_adapters(adapter_set)`.
- A second `bind_adapters` on the same context is an error. Reading `ctx.adapters` before
  a bind is an error. Both are programming errors, not operator-facing refusals; the
  exception types are E002-S01-004's to name.
- An entry point that **joins** a Run (AD-16: "joins it and creates nothing") also binds
  nothing. It uses the adapter set the outer root bound, including the outer root's real
  or dry-run selection. `join_or_open` tells the driver which case it is in, so the driver
  binds only when it opened the Run; it never tests "is anything bound yet?", which would
  hide a double bind.
- **`AdapterSet` has one owner: E002-S02-007** (ports and adapter wiring). It defines the
  type and its only two constructors, `real_adapters(...)` and `dry_run_adapters(...)`.
  E002-S01-004 references the type on the Run Context and does not define it.

AD-16's rule still holds: a Run Context is created exactly once, by the outermost
composition root, joined and never re-created, and creating one while another is active is
an error. This ADR only moves the binding of the adapter set from creation to a single,
later, checked step.

## Consequences

- Positive: `drive_backup`'s order is linear and testable: open the Run, take the lock,
  read `wp-config.php`, build the descriptor and credential source, bind adapters, run.
  `tests/unit/cli/test_lock_order.py` can assert that `bind_adapters` follows the lock
  acquisition and the `wp-config.php` read.
- Positive: the Safety Snapshot (E003) joins the restore Run and uses its adapter set, so a
  restore Dry Run's nested backup is a Dry Run too, without passing a flag.
- Negative / trade-off accepted: `RunContext` is no longer frozen in every field. All other
  fields stay immutable; `adapters` is a read-only property over a write-once slot set by
  `bind_adapters` (`frozen=True` alone cannot express this, so the slot is a dedicated
  holder, not an `object.__setattr__` workaround scattered through callers).
- Negative: the window between `open_run` and `bind_adapters` (lock acquisition,
  `wp-config.php` read, descriptor parse) cannot use any port in the adapter set. Those
  steps use the Site Lock and the config reader directly, as S02-008 already requires.
- Follow-ups: E002-S01-004 removes `adapters` from `open_run`'s signature and adds
  `bind_adapters`; E002-S02-007 is the sole definition of `AdapterSet`; E002-S02-008's
  lock-order test includes the bind. Unit tests: double bind raises, read before bind
  raises, a joined driver binds nothing.
- The tracer passed to `open_run` (AD-24: the root span opens before the lock) is the same
  instance the adapter set later carries in its `tracer` field; it is not re-created at
  bind.
- **Revisit when:** a port is needed before the lock is taken (for example a remote lock
  backend reached through the adapter set). That would split the set into a pre-lock and a
  post-lock part rather than move the whole bind earlier.

## Concern

**Sprint order.** As planned, E002-S01-004 (sprint 01) types `RunContext.adapters` as
`AdapterSet`, and E002-S02-007 (sprint 02) defines that type. Binding later removes
sprint 01's need to *construct* an adapter set at `open_run`. It does not remove the
annotation's need for the type to exist: mypy and the import of `cli/context.py` both need
it. So the "S01-004 references, does not define" ownership holds only if the AG-30
reorganisation makes S02-007's `AdapterSet` available before S01-004 is built, for example
by moving its definition into a sprint-01 story or by sequencing S02-007's type ahead of
S01-004. This ADR does not choose between those. The choice belongs to the AG-30
resolution.

## Resolution of the Concern — 2026-10-27

The gate resolves it within this decision's intent (one definition of `AdapterSet`): the type
is **defined once, in sprint 1, by E002-S01-004** (`ports/adapter_set.py`), holding the ports
that exist in sprint 1. Later E002 stories (E002-S02-007, E002-S02-008, E002-S02-012) **add
fields** to that same class for the ports they introduce; none defines a second container.
Epic 2 owns the module throughout. This supersedes "owned by E002-S02-007" above.

## Amendment — 2026-10-27: a restore target with no `wp-config.php` (Shawn)

When the restore target has no `wp-config.php`, the Site Descriptor and WordPress source come
from the Archive's `config` member, which ordinarily is extracted only after `VERIFY`, while
`PREFLIGHT` (before `VERIFY`, AD-17) needs the bound adapters. Decided: **read, do not
extract.** The composition root reads the `config` member's bytes through
`ArchiveProbe.read_member` (read-only, bounded, nothing written to the Site), digests them,
writes them to a `0600` file in the Run workspace, builds the descriptor from that file, binds
the adapters once, and runs `PREFLIGHT` and then `VERIFY` in AD-17's order. `VERIFY` must find
the member's stored digest equal to the digest of the bytes that were used; any difference —
including the Archive changing between the two reads — aborts the Run before any destructive
phase.

Rejected: running `VERIFY` before `PREFLIGHT` (amends AD-17 for every restore, and finds a
missing tool only after a long verification); refusing such targets (breaks FR-29 and E003's
criterion). Accepted cost: unverified bytes are used to *plan* before `VERIFY` confirms them.
Nothing destructive runs before `VERIFY` (Safety Snapshot and `CLEAN` follow it), so a tampered
config member can only produce an abort. *(Narrowed by the amendment of 2026-11-06, below: this
holds for an Archive changed by someone who cannot also rewrite its checksums, and for every
encrypted Archive.)*

## Amendment — 2026-11-06: what `VERIFY` proves about an unauthenticated Archive (Shawn)

Red team RT-002 (BL-E003-036, BL-E003-054) showed the sentence above claims too much.
`VERIFY` compares the Archive's contents with the checksums recorded in the same Archive.

- **Encrypted Archive (ADR-0024):** `age` authenticates the payload when it is decrypted, so
  a tampered config member, or any other tampered member, aborts the Run.
- **Unencrypted Archive, and every v0 Archive (STRUCTURAL_ONLY):** the check is consistency,
  not authenticity. Whoever can write the Archive can rewrite a member and its checksum
  together, and that forgery passes `VERIFY` and is restored.

**Decided: restore it, with a warning; do not refuse.** Before any effect, the restore logs
WARNING `archive_unauthenticated` and writes a warning to stderr, which names a
STRUCTURAL_ONLY Archive as such (AD-26: a safety warning is never only a log record; UX-S02-01).
`docs/restore.md`
states the trust model: for an unencrypted Archive, whoever controls where it is stored is
trusted, and encryption is how an operator gets authenticity.

What still holds without authentication:
- The restore reads only its own `0600` copy of the Archive in the `0700` workspace, made
  before the probe, so the Archive cannot be swapped after `VERIFY` (RT-001).
- When the target has no `wp-config.php`, `--archive-database` must confirm the Archive's
  database identity before any connection (BL-E002-114), so a forged config cannot redirect
  the restore to another database.

Rejected:
- **Refusing unauthenticated Archives, or requiring a flag for them.** Encryption is optional,
  so most Archives and every v0 Archive are unencrypted. Refusing would block the tool's main
  job, or put a mandatory flag on most restores.

This matches the decision of 2026-11-03 for `verify`: without a key it reports consistency,
and `verify --authenticate` authenticates.

**Revisit when:** an operator needs a restore that refuses an unauthenticated Archive. That
would be an opt-in flag, for example `--require-authenticated`, added without changing the
default.

## Amendment — 2026-11-07: a passphrase means an encrypted Archive is expected (Shawn, E004 D1 option b)

The 2026-11-06 waiver (an unauthenticated Archive restores with a warning) is narrowed in one
case. When the operator supplies a passphrase explicitly (`--passphrase-file`, the prompt, or
`L3IO_WP_BACKUP_PASSPHRASE`, through the one resolution chain), an unencrypted Archive is
refused before any effect (`ArchiveNotEncryptedError`, exit 2), unless `--allow-unencrypted` is
also given. That is the case where a plain Archive most likely means the encrypted one was swapped.
With no passphrase the behaviour is unchanged (restore with the WARNING), and `--require-encrypted`
keeps refusing a plain Archive outright. The library `restore()` takes the same rule through an
`allow_unencrypted` keyword. **Owner:** E008-S01-001.
