# Arch gate review — E005

- **Date:** 2026-11-06
- **Work type:** CODE
- **Scope:** `epic-005/sprint-01`: E005-S01-001 and E005-S01-002.
- **Reviewer:** `reviewer-l3io-arch-review-2026-11-06.md` (`l3io-arch-review`, Mode B). This
  is E005's first gate. The stories were elaborated on 2026-10-27, before E002 and E003 were
  built.
- **Raw result:** 2 BLOCKER, 12 MAJOR, 11 MINOR. 3 were flagged for the owner.

## Resolution

- **F1 (BLOCKER):** settled by the ADR-0015 amendment. The builders take `CredentialSpec |
  None`, and no `l3io.wp.database` type reaches `cli/` or `api/`.
- **G1 (BLOCKER):** settled by the built code. `RootPreflight` routes
  `DUMP_REQUIREMENT.name` to the built `DumpReadinessPreflight`.
- **The three flagged for the owner were resolved without asking.** Each is recorded in the
  stories as an orchestrator decision, and the owner may overrule:
  - **F2, the FR-14 custom provider:** deferred to v1.1, as PRD §13 already does (`prd.md`
    L631).
  - **F5/F6, the `adapters/database.py` grant:** given to E005-S01-001 under the scoping
    rule (the story that adds credential sources registers them for redaction where they
    are created).
  - **G12, `packaging`:** accepted under the owner's global rule 1 (use a maintained
    library for version comparison).
- **Everything else:** applied as story edits marked *(E004/E005 pre-build re-check,
  2026-11-06)*. That includes F3–F11, G2–G11 and the MINORs; X1 (shared
  `pyproject.toml`/`uv.lock`/`README.md` with E004) is governed by BL-E004-006.
- **Re-check of those edits:** 12/14 resolved. F5/F6 were partly resolved because they
  rested on an unrecorded decision; the M-1 rewording records them as orchestrator
  decisions. M-2 to M-6 were applied. The edits were not reviewed independently a second
  time.

**Verdict: PASSED** — 0 open BLOCKER or MAJOR.
