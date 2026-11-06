# Retrospective: E005 (credential providers, binary preflight)

Single sprint, two stories, both merged (`33f739e`, `608ccc6`), built in parallel with E004.

## What went well
- Per-story code review caught the two defects that mattered (restore honouring ambient `L3IO_WP_BACKUP_DB_*`, S01-001 F-1; database's extra names leaking into the install hint, F-2) and both were fixed before merge.
- Delegating to `l3io-wp-database` (ADR-0015) and `packaging` kept the diff free of hand-rolled parsers or chains; the single-chain contract test attacks its scope with a planted nested module.
- Gates stayed green on 3.11 and 3.14.

## Pain points
- Deliverables with no production caller. `BinaryPreflight`, `diagnose` and `unsupported_platform` ship tested and unused until E008-S01-001. Two independent closure reviewers (arch AD-1, adversarial A-3) flagged it; the guard proves the seam, not the reach.
- Low findings from per-story reviews (F-3, F-5, L1) were left unfixed and resurfaced in closure as three more reports of the same defects.
- Probe hardening (relative `PATH` entries) was not considered in per-story review; fixed at closure.

## Learnings
- When a story builds a component whose caller is a later story, put a "has a caller" guard in the later story's ACs at planning time (done now for E008-S01-001).
- A subprocess probe must resolve its binary from absolute PATH entries only and report the absolute path it will run.
- Record per-story Low findings as backlog items at story merge, not at closure.
