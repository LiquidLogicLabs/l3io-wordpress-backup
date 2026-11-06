# Spec proposal AD-1

Target: `_bmad-output/planning-artifacts/epics.md#epic-8-wire-the-parallel-epics-into-the-commands`

Proposed change: Epic 8 states that `ExclusionSet` and `LoggedExclusionPolicy` get their first production caller there, that `default_exclusion_policy()` stops returning `INCLUDE_EVERYTHING`, and that a contract test fails while either is true.

Why: E004 built the exclusion engine (FR-5) and proved it through an injected policy, but a shipped backup still excludes nothing and the `--exclude` family is "not available in this build". The wiring is E008-S01-001's; E008-S01-001 now carries the guard as an acceptance criterion.

Finding: AD-1, `_bmad-output/implementation-artifacts/epic-004/epic-closure/arch-drift-review.md`.
