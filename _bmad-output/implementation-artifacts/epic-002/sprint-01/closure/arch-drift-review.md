---
reviewer: l3io-arch-review (Mode B — architectural drift)
scope: E002 sprint 01 closure — stories E002-S01-001..006, -010; `git diff 3e9f80a..d3e0b2b`
date: 2026-11-03
spec_alignment: on (spec-align `sections`, index fresh)
---

# E002 sprint 01 — architectural drift review

## Executive summary

The sprint's code holds the spine's load-bearing boundaries. The three AD-1 import-linter
contracts in `pyproject.toml:314-357` cover `domain/` and `core/`, and the walk of their
imports found no I/O module. Dry Run is named only in the roots (AD-4). The workspace is
`mkdtemp`, owner-only and removed in a `finally` (AD-12). Publication is `mkstemp` + fsync +
`os.replace` (AD-20). Extraction has one function, with `data_filter` and an explicit
hard-link refusal (AD-11; the 2026-11-03 pre-build MAJOR is closed at `adapters/tar.py:905`).
`publish() -> bool` is now consistent (`ports/archive.py:34`). The Run Context is opened once,
and its `run_id` is a UUIDv4 bound first through `merge_contextvars` (AD-10/AD-16).

There are two drifts at **MAJOR**:
- The record that tells an operator *why* a Run failed is emitted after the Run's `run_id`
  is unbound, so a per-Run `jq` query does not return it (AD-10).
- The v0/v1 decision is made outside `domain/archive_format` by testing `payload is None`,
  which the AD-23 guard cannot see (AD-23).

**Count:** Blocker 0, Major 2, Minor 4.

**Most important action:** emit the failure record inside the Run (SD-01-1).

## Findings

| # | Severity | Principle | Location | Finding | Remediation |
|---|----------|-----------|----------|---------|-------------|
| SD-01-1 | MAJOR | Spine AD-10 ("the Run ID … appears on every record"); Core §9 (a Run you cannot trace end to end by one ID is MAJOR); E002-S01-004 AC (`jq 'select(.run_id==…)'` returns one Run "start to finish") | `src/l3io/wp/backup/cli/__init__.py:53-54`, `cli/exit.py:116-134`, `cli/context.py:159-161,203-219` | **What happens.** `open_run` binds `run_id` through an `ExitStack` and resets it when the stack exits. The exception then propagates to `main`, and only there does `report_failure` emit `command_failed`. That record holds the operator message, `exit_code` and `exit_reason`. Because the stack has already exited, the record carries no `run_id`.<br>**What the Run's own records hold.** `run_finished` has the `run_id` but only `error=<class name>`. So the per-Run query the story promises returns a failed Run without the reason it failed. In a week-long JSON log of many Runs, the two records can be matched only by timestamp.<br>**Status.** `docs/logging.md:86-88` discloses the gap, and `closure/review-E002-S01-005.md:51` observed it. Neither is an owner decision that departs from AD-10. | Emit the failure record while the Run is still bound. One way: in `open_run`'s `finally`, before `run_finished`, log `command_failed` with `render_failure(..., with_traceback=False)`. Then keep `main`'s `report_failure` for failures before a Run opens: print to stderr and log only when no Run was open. Add a case to `tests/integration/test_run_correlation.py` where a failing Run must put `run_id` on every record, `command_failed` included. Update `docs/logging.md:86-88`. |
| SD-01-2 | MAJOR | Spine AD-23 ("No module outside `domain/archive_format` branches on version"; "everything downstream consumes a normalized `ArchiveView`, never a format version"); global rule §4 (a guard proves a rule, not its reach) | `src/l3io/wp/backup/core/verify.py:118-157`; `adapters/tar.py:837,1004,1036`; guard `tests/contracts/test_version_branching.py:5-24` | **What happens.** `core/verify.verify` selects a whole v1 or v0 rule set on `view.payload is None`. `payload` is `None` exactly when the Archive is v0, so this is a version branch under another name. The v0 rule set (`_v0`) then re-inspects the raw `MemberIndex` (`MANIFEST_MEMBER in index.names`). `adapters/tar` picks the extraction source and the kind checks the same way.<br>**Why the guard misses it.** The contract test matches only the token `format_version`. Its own "not covered" note names copied values, not proxies like this one. So the gate stays green over the branch AD-23 exists to prevent: two consumers, here `core/verify` and `adapters/tar`, each re-deriving the format.<br>**Status.** E002-S01-003:61 and :65 specify this placement, so the story text diverges from the spine. This is reported, not reopened. | Make `domain/archive_format` own the discrimination. For example, `classify` returns a tagged view (`V1View \| V0View`, or an `ArchiveView.shape` enum owned there), and `archive_format` exports the per-shape verification-rule selector and the extraction-source choice. Consumers then call those and do not test `payload`. Then widen `test_version_branching.py` to flag a branch on `.payload is None`/`is not None` outside the owner, and plant a proxy branch to show the guard fires. If the owner instead regards payload presence as a normalized fact, the same guard extension applies once that is recorded as an AD-23 amendment. |
| SD-01-3 | MINOR | Core §6 (comments state current state, not history; a stale comment is worse than none) | `src/l3io/wp/backup/cli/parser.py:1-12,36` | **What the docstring says.** The module docstring describes the parser as "deliberately incomplete at this stage: the packaging skeleton and its contract gates land first". It also presents `python -m wpbackup2` as a working invocation.<br>**What is true.** The parser now carries `backup` and `verify` in full. The wheel ships only `src/l3io` (`pyproject.toml:193-194`), so module invocation does not work. E002-S02-011:111 tracks that as an owner-decided strict-xfail.<br>**History comment.** `:36` is a changelog note ("Triage E-14: …"). | Rewrite the docstring to today's surface. State that `python -m wpbackup2` is the open AD-22 legacy-tree item owned by E002-S02-011. Reduce `:36` to the reason for `INFO`. |
| SD-01-4 | MINOR | Core §10 (docs that contradict the code); ADR-0009 (floor 3.11); ADR-0012 amendment (distribution `l3io-wordpress-backup`); AD-22 | `README.md:28,36,146` | **What contradicts the code.** The README, edited in this diff, still says `pip install wpbackup2` (`:28`), but the distribution is `l3io-wordpress-backup` (`pyproject.toml:2`). Its usage example is `python3 -m wpbackup2 …` (`:36`), which is not shipped (SD-01-3). It says "requires Python 3.6 or newer" (`:146`), but `requires-python` is `>=3.11`.<br>**Why CI passes.** The generated blocks are current. `tools/gen_cli_docs.py:19` honestly states that prose outside the markers is not covered.<br>**Cross-reference.** `closure/ux-review.md` UX-01-1, UX-01-2 and UX-01-9 rate the operator impact. | Correct the three lines: install `l3io-wordpress-backup`, show the `wpbackup2` console script, and give the floor as 3.11. Optionally, have `gen_cli_docs.py --check` also fail on a `python3 -m wpbackup2` or `pip install wpbackup2` string while AD-22's module item is open. |
| SD-01-5 | MINOR | Core §4 (hard-wired clock); Spine Consistency Conventions (one Clock; UTC timestamps) | `src/l3io/wp/backup/adapters/tar.py:460-466` | `_write_outer` stamps both outer members with `time.time()`. The Run carries a `Clock` port (`ports/clock.py`), and `execute_backup_plan` already receives `now=ctx.clock.now()` (`cli/backup.py:355`). So the outer tar's mtimes can differ from the Manifest's own timestamp, and byte-identical Archives are not reproducible under a fake clock. | Pass the plan's `now` into `write_archive`/`_write_outer`, and use `now.timestamp()` for the header mtime. |
| SD-01-6 | MINOR | Global rule §3 (prose must not overstate its coverage); Core §10 | `docs/logging.md:63-71`; `src/l3io/wp/backup/cli/__init__.py:35-41` | **What the doc claims.** "A failed command always leaves one `command_failed` record", and under `--log-format json` "stderr stays one JSON object per line".<br>**What `main` does.** It calls `parser.parse_args` and `parser.error` (the latter for unimplemented actions) *before* `configure_logging`. So every argparse usage error, exit 2, leaves no record and prints plain text to stderr, whatever `--log-format` says.<br>**Cross-reference.** `closure/ux-review.md` UX-01-6. | Either state the exception in `docs/logging.md` ("argparse usage errors, exit 2, are plain text and leave no record"), or override `ArgumentParser.error` so it raises `argparse.ArgumentError`. `exit_code_for` already maps that to `USAGE`, so the error would be routed through `report_failure` once logging is configured. Add a test for whichever is chosen. |

## Per-principle walkthrough

**Core §1–10:**
- **§1 Separation of concerns:** PASS. The `domain/`/`core/` imports are stdlib value types only (`types`, `pathlib.Pure*`, `datetime`). The import-linter forbidden and layers contracts cover the I/O modules and outward layers. `cli : api` is a non-independent layer: `:` was checked against the installed import-linter 2.15 `layers.py:20-21`, so the E006 plan for `api/` to call the `cli/` drivers is legal.
- **§2 Reuse:** PASS. One exit mapping: `cli/exit.exit_code_for` delegates to `domain/errors.exit_code_of`. One redaction implementation (`adapters/redaction.py`). No member-name literal outside `domain/archive_format`.
- **§3 Design by contract:** PASS. Frozen request and value types, keyword-required `backup(...)`, and no `BackupScope` defaults. Dependency exceptions are translated at `adapters/database.py:219-486` (AD-14).
- **§4 Testability:** SD-01-5.
- **§5 Brevity:** PASS. No findings. `adapters/tar.py`, at 1,062 lines, is large but is one responsibility.
- **§6 Comments:** SD-01-3.
- **§7 Dependency selection:** PASS. The diff adds no runtime dependency.
- **§8 GA over preview:** PASS. Nothing new. `opentelemetry` is not imported in E002.
- **§9 Logging and correlation:** SD-01-1. The remaining items PASS: structlog configured once (`cli/logging.py:105`), `merge_contextvars` first, UTC ISO timestamps, phase fields `phase`/`duration_ms`/`bytes`/`exit_code` (`adapters/phase.py`), and no stray `print` outside `cli/output.py`.
- **§10 Documentation:** SD-01-4, SD-01-6.

**Python overlay:** PASS. uv with a committed `uv.lock`, `src/` layout, `requires-python >=3.11`, `mypy --strict`, ruff, pytest.

**GitHub Actions overlay:** PASS on the 5-line `ci.yml` change. The new step is `uv run python tools/gen_cli_docs.py --check`, a repo script, and adds no third-party action.

**Spine ADs in the pointed ranges:**

| Spine AD | Result | Basis |
|---|---|---|
| AD-1 | PASS | — |
| AD-2 | PASS | `l3io.wp.database` imported only in `adapters/`/`cli/` |
| AD-3 | PASS | `wp-config.php` read only from `cli/backup.drive_backup` via `locate_wp_config` |
| AD-5 | PASS | — |
| AD-6 | PASS | — |
| AD-7 | PASS | No MySQL client invoked; hygiene contract tests in the diff |
| AD-8 | PASS | `l3io.wp.database` imported inside functions |
| AD-9 | PASS | Explicit `resolve()` |
| AD-10 | SD-01-1 | — |
| AD-11 | PASS | — |
| AD-12 | PASS | — |
| AD-14 | PASS | — |
| AD-16 | PASS | `open_run` refuses nesting; `join_or_open` |
| AD-20 | PASS | — |
| AD-21 | PASS | Only the Manifest goes through `BytesIO`; `test_streaming.py` |
| AD-22 | Known open item | `python -m` is owner-decided strict-xfail in S02-011; SD-01-3/-4 cover the stale prose |
| AD-23 | SD-01-2 | — |
| AD-24 | PASS | Tracer ports only, no-op bound by the CLI |
| AD-26 | PASS for this sprint | No passphrase flag until E002-S02-011; the env-var note is UX-01-8 |

## Gate

- SD-01-1 and SD-01-2 (MAJOR) must be resolved or ADR-justified before sign-off.
- SD-01-3 to SD-01-6 (MINOR) defer to the backlog.

## Recommended ADRs

- Only if the owner chooses the second remediation for SD-01-2: an AD-23 amendment stating that payload presence is a normalized fact consumers may branch on, with the guard widened to match.

Sections read:
- **Spine:** `ARCHITECTURE-SPINE.md` L44–305 (AD-1 to AD-26), L343–472 (Consistency Conventions, Stack, Structural Seed, Capability Map, Deferred).
- **PRD:** `prd.md` L338–400 (§5.6, grep for exit-code lines).
- **Skill:** `references/standards-core.md`, `standards-python.md`, `review-report.md`, and the headings of `standards-github-actions.md`.
- **Gate records:** `epic-002/arch/reviewer-l3io-arch-review-2026-11-03.md` (summary and findings); `epic-002/arch/arch-gate-review.md` (grep hits for AD-23/AG-7/AG-10/AG-17 only).
- **Other planning and closure files, by grep only:**
  - `E006-S01-001.md:66`
  - `E002-S02-011.md:111`
  - `docs/adr/0012-package-naming-l3io-wp-namespace.md` (amendment lines)
  - `closure/review-E002-S01-004.md`, `review-E002-S01-005.md` (`command_failed` hits)
- **Stories:** `E002-S01-003.md` and `E002-S01-004.md` (targeted lines 27–138 and 35–152); the headers of the other sprint stories.
- **Code:**
  - `core/verify.py` (full)
  - `cli/__init__.py` L25–61
  - `cli/exit.py` L43–134
  - `cli/context.py` L1–30 and L140–240
  - `adapters/tar.py` L205–222 and L440–466
  - `adapters/database.py` L325–345
  - `adapters/noop.py` L110–125
  - `docs/logging.md` L60–90
  - `pyproject.toml`: import-linter, the hatch build and scripts blocks
  - the `ci.yml` diff
  - targeted greps across `src/l3io/wp/backup/`
- **Not opened:** `spec-index.md` and the ADR bodies. ADR content was taken from the spine's amendment notes.

Blocker: 0, Major: 2, Minor: 4
