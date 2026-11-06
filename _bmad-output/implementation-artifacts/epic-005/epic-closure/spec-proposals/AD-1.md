# Spec proposal AD-1

Target: `_bmad-output/planning-artifacts/epics.md#epic-8-wire-the-parallel-epics-into-the-commands`

Proposed change: Epic 8 states that `BinaryPreflight`, `diagnose` and `unsupported_platform` get their first production caller there, and that a contract test fails while any of them has none.

Why: E005 built the preflight adapter, but every composition root still binds `DumpReadinessPreflight`. The wiring is E008-S01-001's by an orchestrator decision; E008-S01-001 now carries the guard as an acceptance criterion.

Finding: AD-1, `_bmad-output/implementation-artifacts/epic-005/epic-closure/arch-drift-review.md`.
