# ADR-0025: `api/` composes through the `cli/` drivers

- **Status:** Proposed — awaiting the owner (it departs from the spine's AD-1). The built code already composes this way (E006-S01-001); the E006 closure recommends ratifying it.
- **Date:** 2026-11-07
- **Deciders:** E006 closure orchestrator
- **Epic:** E006 (stories E006-S01-001, E006-S01-003)
- **Resolves:** E006 arch-drift finding AD-1 (`epic-006/epic-closure/arch-drift-review.md`)
- **Departs from spec:** _bmad-output/planning-artifacts/architecture/architecture-py-wordpress-backup-2026-09-15/ARCHITECTURE-SPINE.md#ad-1 (the structural seed draws `cli/` and `api/` as peer composition roots)
- **Principle(s) in tension:** Core §1 separation of concerns, AD-16 one sequence, FR-24 one public surface

## Context

The seed treats `cli/` and `api/` as two peer composition roots. E006-S01-001 built `api/` so that each
entry point makes one call to the driver the CLI calls (`drive_backup`, `drive_verify`, `drive_restore`),
which live in `cli/`, and applies the CLI root's own exit rule to the result. Public types
(`BackupResult`, `RestoreOutcome`, `RelocationFlags`, `RunContext`, `configure_logging`, four
exceptions) are defined in `cli/` and re-exported. The `layers` contract lets siblings import each
other, and a second contract bars `cli/` from importing `api/`, so lint passes and no document recorded
the choice.

## Decision

**Accept it.** The drivers are the one sequence (AD-16): the Site Lock, the Descriptor, adapter binding,
the Dry Run set (AD-4) and every refusal (FR-7, `--require-encrypted`, `--archive-database`,
`--reset-existing-db-user`) run inside them. A second copy in `api/` would let a library caller reach
a different sequence from the CLI's, which is the bypass the epic exists to prevent; the parity
contract (`tests/contracts/test_api_parity.py`, `test_api_one_sequence.py`) holds the two together.
The cost is accepted: a change to a driver, or to a type re-exported from `cli/`, is a change to the
public surface, and `docs/api.md` states that the defining module is not part of the contract.

## Consequences

- Positive: one sequence, no drift between the roots, no refactor of integration-tested code.
- Negative: argparse-facing and library-facing code share modules; `drive_restore` prints three stderr
  notices that a library host receives (documented in `docs/api.md`).
- **Revisit when:** the drivers, request/result types and passphrase chain are extracted to a neutral
  module both roots import (BL-E006-021), **or** a second front end (a service, a
  plugin) needs `api/` without `cli/` being importable, **or** a `cli/` change breaks a library caller.
