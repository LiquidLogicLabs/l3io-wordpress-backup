# E006 retrospective (combined sprint and epic closure, 2026-11-07)

Epic goal: embed the package in Python automation: `api.backup/verify/restore`, typed errors with structured fields, OpenTelemetry spans for a host that traces, a declared public surface with `docs/api.md`. One sprint, four stories, all merged (S01-001 `b321422`, S01-004 `22ecf94`, S01-002 `b615590`, S01-003 `83d73a7`). `make ci` at `83d73a7`: 4552 passed per Python 3.11-3.14, 4679 against MySQL.

## Velocity
4 of 4 stories done, no carry-over. Story actuals sum to 2.57 h elapsed, 59 counterfactual man-hours, 111.1M tokens (cost 88.96). No story went through a fix-loop iteration it recorded as blocked. Closure then found 2 HIGH and 4 MEDIUM defects that all four per-story reviews had missed; the fixes were small (about 120 changed source lines).

## What went well
- The guard-parity claim held under three independent lenses: every CLI refusal and flag (FR-7 guard, Dry Run, `--archive-database`, `--require-encrypted`, `--reset-existing-db-user`, Safety Snapshot, Site Lock) lives inside `drive_*`, which the API calls. No bypass found.
- Contract tests derived from the tree (`tests/exception_scope.py`, `test_public_surface.py`, `test_api_docs.py`) made scope drift fail loudly; `lint-imports` kept all four contracts.
- Redaction at the span and registry level worked for registered secrets, and the CLI's own failure rule (`cli.exit.render_failure`) was reusable at the library boundary with no new code.

## What did not
- **Consumer-shaped failure paths were untested.** O-1(b) turned most exception constructors into field-taking signatures and nothing round-tripped them through `pickle`, so a `ProcessPoolExecutor` host would have lost every error (AR-1/RT-4). A process pool, a task queue and an error tracker are the first things a library caller uses.
- **"It never prints" and "nothing here redacts" were stated, not tested.** Three claims (stdout/stderr, `os.environ` reads, span text) were wrong in the code a reviewer reads first. `test_api_docs.py` checks headings, not prose.
- **A process-wide toggle (`PR_SET_DUMPABLE`) designed for a one-shot CLI shipped into a library unchanged.** The same shape (global state written by an adapter) is the thing to grep for when a CLI-era module becomes callable from a thread.
- **The redaction registry's lifetime is the Run's.** Anything logged after the Run (the boundary's `unexpected_exception`) is outside redaction. Per-story reviews looked at each record's content, not at when it is written.

## Learnings to carry forward
1. When a change alters constructor signatures across a hierarchy, add a round-trip test through `pickle`/`copy` and a real `ProcessPoolExecutor` in the same story.
2. Any record written after a Run closes must not carry a dependency's text; reuse `render_failure`, never `exc_info`, at a boundary outside the registry's lifetime.
3. A docstring that claims an absence ("never prints", "reads no environment") needs a test that observes the absence (`capsys`, an environ spy), or must not be written.
4. Grep CLI-era adapters for process-global writes (`prctl`, `signal`, `os.environ`, `chdir`, structlog contextvars) before exposing them through an API, and either lock them or document them in the API page.
5. Owner decisions on API shape (a secret wrapper type, a warning sink, a required `target`, whether `context=` is public) are cheaper before v1.0 than after; they are batched in the closure report.

## Acceptance decision
Accept E006, with the fixes in this closure, BL-E006-008 to BL-E006-023 in the backlog and five owner decisions open (closure report). None blocks archive.
