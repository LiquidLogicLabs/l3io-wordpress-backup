# Spec proposal AD-1

Target: _bmad-output/planning-artifacts/architecture/architecture-py-wordpress-backup-2026-09-15/ARCHITECTURE-SPINE.md#ad-17--restore-phase-order-is-data-not-a-diagram

Finding: AD-1 (epic arch drift review)

Proposed change: Specify one spelling for the log 'phase' field across backup steps and restore phases (spine writes upper case, code emits lower-case kebab for restore), and update docs/logging.md accordingly before Epic 3 emits it.

Why: A log-schema break after Epic 3 if two spellings ship in one field; renaming now is cheap.
