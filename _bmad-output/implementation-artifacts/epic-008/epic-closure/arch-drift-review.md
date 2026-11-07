# E008 architectural review (Mode B): built vs planned

Scope: commit 14029e8 (E008-S01-001, 78 files), ADRs 0016, 0018, 0019, 0024, 0025, spine AD-1/3/4/8/14/26, standards-core 1-10 and standards-python. Reviewer: l3io-arch-review. Prior code review (`sprint-01/closure/review-E008-S01-001.md`) not duplicated. Owner decisions in force and not reported as drift: ADR amendments of 2026-11-06/07, BL-E008-004 (a), E006 library decisions, ADR-0025 option a.

## Executive summary

The build matches the plan. All four import-linter contracts are kept (134 files, 890 dependencies), `check_purity` (25 pure modules) and `check_mode_defaults` (81 modules) pass, both doc generators pass `--check`, and `NOT_YET_IMPLEMENTED` holds exactly `restore --only-appointed-in-asg`. Every `*Error` class under the package is exported from `api`, and the new ones carry typed fields. Findings: 0 blockers, 3 majors, 5 minors. The most important action is AD-1: the `Secret` type, an owner decision that changed the public surface, is recorded in no ADR, and ADR-0018 still says it is a backlog item.

## Findings

| # | Severity | Principle | Location | Finding | Remediation |
|---|----------|-----------|----------|---------|-------------|
| AD-1 | MAJOR | Core Decision hook / §10 (docs contradict code) | `docs/adr/0018-redaction-at-the-boundary.md:146-147`; `src/l3io/wp/backup/domain/secret.py`; spine AD-26 | The `Secret` type (opaque, `.reveal()` allowed in four modules, no `==` against `str`, no pickling, public on `ExplicitCredentialSpec`/`RelocationFlags`/entry-point params) is a load-bearing public-surface and redaction decision. It is recorded only in the module docstring, `docs/api.md` and `tests/contracts/test_secret_reveal.py`. No ADR or spine amendment mentions it. ADR-0018 still reads "a wrapper type for secret arguments is BL-E006-009", which the code contradicts. | Add an ADR-0018 amendment (or a new ADR) recording the type, the four-module reveal allowlist, the rejected alternatives and the revisit condition; delete the BL-E006-009 sentence; add one line to AD-26 pointing at it. |
| AD-2 | MAJOR | Spine AD-4 (a Dry Run that says "would back up" where the real Run refuses is a defect); ADR-0019 | `src/l3io/wp/backup/adapters/tar.py:610-687` (`_refuse_oversized_manifest`, called from `TarArchiveWriter.write`) | The owner decision (E004 D2 c) and the story say the Manifest cap is refused "at plan time ... with nothing written". The check sits in the archive-writer command adapter, which the Dry Run binds as a no-op. A backup Dry Run on a site whose Manifest exceeds 256 MiB therefore reports success and the real Run refuses. ADR-0019 option C permits this only for refusals "only a command can detect"; this one needs only the member list. The tests (`test_manifest_cap.py`) cover the adapter, not the Dry Run. | Either move the size computation to where the plan's member list is available (a query-side `ManifestSizeCheck`, so the real query set runs it in a Dry Run), or amend ADR-0019 to list this refusal as Dry-Run-invisible and state it in `docs/api.md`. Add a Dry Run contract test either way. |
| AD-3 | MAJOR | Core §10 (docs contradict the code); §3 (guard reach) | `docs/api.md:94` ("A call that joined an enclosing Run through `context=`"), `docs/api.md:560` ("passes it on as `context=` (see *Joining a Run* above)"); guard `tests/contracts/test_api_docs.py` | The 2026-11-07 decision removed `context=` and the "Joining a Run" section, but two hand-written passages still describe joining a Run, and one links to a section that no longer exists. `test_api_docs.py` and `gen_*_docs --check` passed over them, so the guard's reach is signatures and headings, not prose naming a removed parameter. | Rewrite both passages. Extend the docs contract to scan `docs/*.md` for backticked identifiers that match no live parameter, option or heading, derived from `inspect.signature` of the `api` entry points rather than listed by hand. Plant `context=` in a doc to prove it. |
| AD-4 | MINOR | Core §1 (public surface); Spine FR-24 | `src/l3io/wp/backup/api/__init__.py:48` (`RunContext` re-export), `docs/api.md:539-560` | `RunContext` is still public, but after `context=` was removed no entry point accepts or returns one. It documents a type no caller can obtain, and it exposes internals (`workspace`, `site_lock`, `tracer`). | Remove it from `api.__all__` and `docs/api.md` (it is unreachable, so the break is nil), or keep it with a stated reason tied to BL-E006-024. |
| AD-5 | MINOR | Core §10; ADR currency | `docs/adr/0025-api-composes-through-the-cli-drivers.md` (Consequences, Negative) | It says `drive_restore` "prints three stderr notices that a library host receives". E008 made the notices go to `on_warning` as `RunWarning` when it is given (`api/operations.py` docstring, `docs/api.md`), so the sentence is stale. The Context's list of re-exported types predates `RunWarning`, `Secret`, `Diagnosis` and `doctor`; `Diagnosis` is defined in `adapters/preflight.py`, which `api` re-exports. | Amend the ADR-0025 consequence and the type list; state in `docs/api.md` that `Diagnosis` (adapters) and `RunWarning` (cli) are also non-contract defining modules. |
| AD-6 | MINOR | Core Decision hook | `domain/archive_format.py:123` (`MANIFEST_MAX_BYTES` 16 to 256 MiB); ADR-0017, ADR-0024 | The format-level limit changed (an older reader with the 16 MiB cap cannot read the new Archives). The change appears only in the story, the commit message and `docs/api.md`; ADR-0017/0024 do not record it. Pre-release, so low impact. | Add a short ADR-0017 amendment: value, owner decision D2 (c), the streaming-parse backlog item as revisit condition. |
| AD-7 | MINOR | Core §6/§10 (internal consistency) | `docs/adr/0024-one-encrypted-payload-passphrase-only.md:231-255` | The first amendment paragraph lists "the Safety Snapshot's encryption" as bound by the 12-character floor. The Deviation and the BL-E008-004 addendum below it say the opposite. A reader who stops at the first paragraph gets the superseded rule. | Edit the first paragraph to drop the Safety Snapshot, or mark it "(narrowed below)". |
| AD-8 | MINOR | Spine AD-14 ("an exception from the stdlib is re-raised as a package-owned type") | `src/l3io/wp/backup/api/errors.py` (`public_boundary`, `signature.bind`); `docs/api.md:64` | A wrong call (a missing or unknown keyword) now raises a bare `TypeError`, outside the `BackupError` family. This is deliberate and in the docstring, but `docs/api.md`'s error section says any other exception becomes `UnexpectedError`. `api/operations.py` module docstring also has a typo ("open or open the Run"). | State the `TypeError` exception once in the Errors section of `docs/api.md` and in AD-14's amendment note; fix the typo. |

## Per-principle walkthrough

| Principle | Result |
|-----------|--------|
| Core §1 Separation of concerns | PASS on layering: contracts "layers run inward only", "pure layers never face outward" and "cli/ never imports api/" are KEPT. `api` composing through `cli` drivers is ADR-0025 option a (owner). `RootPreflight`, `doctor` and `credentials` sit in `cli/` as composition-root code. AD-4 (public surface). |
| Core §2 Reuse | PASS. One `root_preflight` factory is the only construction (AST guard `test_preflight_construction.py`); `api.doctor` and the CLI share `drive_doctor`. |
| Core §3 Design by contract | PASS with AD-8. Errors carry typed fields (`minimum`, `members/size/limit`, `prompts`) and are exported; `test_api_exception_exports.py` and my full-package scan found no unexported `*Error`. |
| Core §4 Testability | PASS. `RootPreflight` takes `platform=` and routes injected ports; the reveal guard is AST-derived over `rglob`. |
| Core §5 Brevity | PASS (no finding). |
| Core §6 Comments | AD-7 (stale ADR text); code comments describe current state. |
| Core §7/§8 Dependencies, GA | PASS. No new dependencies in the diff; `pathspec`, `packaging` and `platformdirs` are justified in `pyproject.toml`. |
| Core §9 Logging | PASS. Doctor runs inside a Run; its output is passed through the Run's registry before printing; `RunWarning` carries no secret. |
| Core §10 Documentation | AD-1, AD-3, AD-5. README/CLI/tool tables are generated and `--check` passes; `docs/restore.md` known-gap note removed. |
| Python overlay | PASS per commit message (ruff, mypy strict, uv lock check). Not re-run beyond `lint-imports`, `check_purity`, `check_mode_defaults` and the two doc generators. |

## Whole-surface checks requested

- Import-linter layering: 4 contracts kept. The `layers` contract puts `cli : api` as siblings, so `api` to `cli` is legal and `cli` to `api` is barred by the FR-24 contract, matching ADR-0025 and the amended AD-1.
- Secret placement: `domain/secret.py` (imports only `hmac`, so `check_purity` passes); `.reveal()` is called only in `adapters/credential_providers.py`, `adapters/crypto.py` and `adapters/redaction.py`, as the guard allows. Placement is sound; the record is missing (AD-1).
- Purity: `check_purity` passed over 25 modules, `check_mode_defaults` over 81. The new `Secret.__len__` exposes length; this is accepted by the registry's floor and not a finding.
- Optional extras (AD-8): importing `cli.parser`, `cli` and `api` in a base environment loads no `boto3`, `pymysql` or `l3io.wp.database`.
- DONE-vs-planned: `NOT_YET_IMPLEMENTED` = `{"restore --only-appointed-in-asg": removed}` only, as the story's AC says. README line 232 still shows "(not available in this build)" for that flag, which is correct for a permanently removed option. All 11 mapped entries are bound, `doctor` and `api.doctor` exist and have a parity test.

## Orchestrator decisions in the story that may be questioned

1. The `RootPreflight` routing (`database-dump` to `DumpReadinessPreflight`, everything else to `BinaryPreflight`) is by requirement name inside one wrapper. It is sound and now guarded, but it makes the dump check a special case in a generic port. The alternative is a requirement kind that the port dispatches on. Not a defect; revisit if a second special requirement appears.
2. `RootPreflight` was placed in `cli/preflight.py` rather than `cli/context.py`, and `RestoreRequest.credentials` was added to a file the original non-goals protected. Both were widened by the 2026-11-06 arch gate (B1, M1) and are recorded in the story.
3. The 11-entry "no flag still refused" AC was narrowed by the story to exclude the permanently removed `--only-appointed-in-asg`. Correct, but the AC text itself ("no flag that now works is still refused") was never edited, only footnoted.

## Gate

Blocker and Major findings (AD-1, AD-2, AD-3) must be resolved or ADR-justified before sign-off. Minors (AD-4 to AD-8) defer to the backlog.

## Recommended ADRs

- ADR-0018 amendment or new ADR: the opaque `Secret` type and the reveal allowlist (AD-1).
- ADR-0019 amendment (or a design change): where the Manifest-cap refusal fires relative to a Dry Run (AD-2).
- ADR-0017 amendment: Manifest cap 256 MiB (AD-6).

Blocker: 0, Major: 3, Minor: 5

Sections read: standards-core.md 1-169; standards-python.md 1-43; review-report.md 1-31; ADR-0025 1-39; ADR-0018 105-147; ADR-0016 168-177; ADR-0024 231-255; ADR-0019 43-62; story E008-S01-001 1-140 and line 194; spine AD-1 (header), AD-3, AD-4, AD-8, AD-14 rule, AD-26 (grep); pyproject.toml 40-110 and 344-406; cli/doctor.py 1-200; cli/preflight.py 127-197; domain/secret.py 1-58; domain/errors.py diff; api/__init__.py and api/errors.py diffs; api/operations.py 1-60 and 340-420; adapters/tar.py 605-660; cli/parser.py 75-125; docs/api.md 52-98, 539-562, 1215-1225; commit message of 14029e8 and its stat; tool runs: lint-imports, check_purity, check_mode_defaults, gen_cli_docs --check, gen_tool_docs --check, an error-export scan and a base-install import scan.
