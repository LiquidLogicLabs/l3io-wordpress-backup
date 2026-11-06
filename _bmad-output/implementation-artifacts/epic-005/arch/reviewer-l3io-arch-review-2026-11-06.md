# Arch gate review — E005 "Credential providers and environment readiness"

- **Reviewer:** l3io-arch-review, Mode B (gate review)
- **Date:** 2026-11-06
- **Scope:** `epic-005/sprint-01/stories/E005-S01-001.md`, `E005-S01-002.md`
- **Checked against:** `ARCHITECTURE-SPINE.md` (AD-8, AD-9, AD-13, AD-14, AD-22), PRD FR-14/FR-27/§13,
  ADR-0015 (with its 2026-10-27 amendment), ADR-0016/ADR-0022 (with the 2026-11-06 amendments), ADR-0024,
  `docs/closeout-gate.yaml`, `epics.md`, `plan-2026-10-27-v2.yaml`, the built code at `HEAD` (`655dcf8`),
  the E004 stories that run in parallel, E006-S01-001, E007-S01-004, E008-S01-001, and the owner's global rules.
- **Review only:** no story, source or test was edited.

## Executive summary

Both stories were elaborated on 2026-10-27, before E002 and E003 were built. They still describe the
interfaces as planned then, not as built. E005-S01-001 gives the library database's `CredentialSource`.
That contradicts the ADR-0015 amendment, the built `cli/credentials.py` seam and E006's API, which all
pass the backup-owned `CredentialSpec`. E005-S01-002's allow-list ignores the `database-dump`
requirement that every built database plan carries, so binding `BinaryPreflight` would crash every
database Run. Several acceptance criteria and both integration tests can only pass once E008 has wired
the providers in, so they cannot be met inside E005's own loop.

The E004 gate's Re-check 2 edits are present and correct (see the conformance section). Neither closeout-gate item
is owned by E005.

Totals: **2 BLOCKER, 12 MAJOR, 11 MINOR; 3 owner decisions.**

**Most important action:** rewrite E005-S01-001's interface contracts against the built
`CredentialSpec` seam, and have the owner decide which epic may edit `adapters/database.py` (F5).

## Conformance with the E004 gate's Re-check 2 (2026-11-03)

| Edit required | Where it is in the story | Status |
|---|---|---|
| The tool-docs tripwire no longer reads `adapters/crypto` | E005-S01-002:122 ("This story does not import `adapters/crypto`"), :201, :212 | PASS |
| `core/restore` dropped from the trigger set | E005-S01-002:212 | PASS |
| The `RestoreMode` enumeration moved to E008-S01-001 | E005-S01-002:121, :122; E008-S01-001:93 | PASS |
| The extension point named `adapters/preflight.py::DOCUMENTED_REQUIREMENT_SOURCES` | E005-S01-002:122 | PASS |
| BL-E004-007 (L139/L141 described the superseded route) | E005-S01-002:136 now says `cipher.required_tools()` via `extra_tools` | Resolved. :139 is stale on a different point (G1, G6) |

## Findings table

Settled by: **built** = the built code settles the fix; **decision** = a final decision already
settles it; **owner** = Shawn must decide.

### E005-S01-001 — credential providers

| # | Sev | Principle | Location | Finding | Remediation | Settled by |
|---|---|---|---|---|---|---|
| F1 | BLOCKER | Core §1 SoC; AD-14; ADR-0015 amendment | E005-S01-001:34, :50, :84, :91, :104 | The library-facing interface is database's `CredentialSource`. `wordpress_credentials(explicit: CredentialSource …)`, "a library caller can pass any object that satisfies `l3io-wordpress-database`'s `CredentialSource` Protocol", and "Epic 6 decides whether `api/` re-exports `CredentialSource` …". ADR-0015's amendment (`docs/adr/0015…md:60-69`) says the opposite: "`cli/` and `api/` pass backup-owned values, never database types", and "Database's `CredentialSource` never appears on this package's public API". The built seam takes `CredentialSpec \| None` (`cli/credentials.py:47-104`; `domain/credentials.py:160-179`), and E006-S01-001:116, :229 put `CredentialSpec` on the API. | The two builders take `spec: CredentialSpec \| None`, `wp_config: CredentialSource` (internal), `environ` and `secrets: SecretRegistry`. They turn an `ExplicitCredentialSpec` into `Explicit` and an `AwsSecretCredentialSpec` into `AwsSecretsManager`, then add the `Environment` step. No database type reaches `cli/` or `api/`. Strike :104's re-export list. | decision (ADR-0015 amendment) |
| F2 | MAJOR | FR-14 vs ADR-0015 amendment; PRD §13 | E005-S01-001:27 (AC "supply their own provider without subclassing"), :104, :186 | Under the amendment, a custom provider cannot be database's Protocol. The PRD defers "Pluggable Credential Provider contract … the documented extension point" to **v1.1** (`prd.md:631`). The ADR's revisit clause says "add the case to the backup-owned value". The story offers neither route. | Owner chooses one: **(a)** defer this AC to v1.1, as PRD §13 already does, and drop :104/:186's custom-provider test; or **(b)** add a backup-owned variant to `CredentialSpec` (for example a `domain/` Protocol whose `resolve()` returns a backup-owned frozen pair), adapted to database in `adapters/`. Record the choice as an ADR-0015 amendment. | **owner** |
| F3 | MAJOR | AD-9 ("resolution order is declared in `domain/`"); Core §2 reuse | E005-S01-001:102, :182 | The body still declares `CREDENTIAL_PRECEDENCE`/`ADMIN_CREDENTIAL_PRECEDENCE` in `adapters/` and parametrises the test from them. Both already exist as `WORDPRESS_CREDENTIAL_PRECEDENCE`/`ADMIN_CREDENTIAL_PRECEDENCE` in `domain/credentials.py:149-157`, tested in `tests/unit/domain/test_credential_precedence.py`. The OPEN at :119 notes the import, but the body was never updated. | Delete :102's declarations. Import the domain tuples and `CredentialOrigin`, and parametrise :182 from `WORDPRESS_CREDENTIAL_PRECEDENCE`. | built |
| F4 | MAJOR | Core §2 reuse; AD-22 exit codes | E005-S01-001:91-95, :106, :138-139 | `admin_credentials(username, password, aws_secret_id, aws_region, …)` re-specifies flag validation that the built `cli/credentials.admin_spec` already does (`cli/credentials.py:54-81`): halves, explicit-vs-AWS and region-without-id. The built code raises `UsageError`. The story's version differs: (i) a missing password becomes `CredentialsError` (exit 4) instead of `UsageError` (exit 2, `cli/credentials.py:71-74`); (ii) an empty variable stays "set" (:139), where the built code reads `""` as unset (`cli/credentials.py:70`); (iii) it adds `CredentialSourceConflictError`, which nothing needs. It also declares `ADMIN_PASSWORD_VAR` again, beside `cli/parser.py:71` (BL-E002-084). | Remove flag parsing from the providers; they take the spec (F1). Drop `CredentialSourceConflictError`. Keep the four variable-name constants here (E005 owns them, BL-E002-084) and state that E008 retires `cli/parser.py:71` in favour of the adapter constant. | built |
| F5 | MAJOR | Core §1 SoC; AD-7/ADR-0018 redaction | E005-S01-001:53, :113-117; E008-S01-001:50 | The environment and AWS steps are missing from the built `adapters/database.py`: `wordpress_source`/`admin_source` (:587-623) carry "Epic 5 binds the environment step" and raise `_AWS_NOT_BOUND` (:519-522). Only that module wraps sources in `_RegisteringSource` (:525-575), which registers each resolved password before anything can emit it. E008-S01-001:50 has `cli/credentials.py` delegate straight to `credential_providers`. That bypasses `_registering`, so a password from the environment or AWS would never be registered. `adapters/database.py` is Epic 2's (closed); E005 owns only `credential_providers`; E008 owns only the `cli/` hook bodies. No story may make the edit. | Owner picks the edit site: **(a)** grant E005-S01-001 the edit to `adapters/database.py::wordpress_source`/`admin_source` to delegate to `credential_providers` (registration kept there); or **(b)** make a registering wrapper public and require `credential_providers` to return registered sources, with E008's `cli/credentials` unchanged. Then update E008-S01-001:50. | **owner** |
| F6 | MAJOR | AD-8; AD-14 | E005-S01-001:135 | The expected AWS outcomes are unreachable through the built mapping. A missing `boto3` should give `MissingExtraError(extra="aws")`, and a missing region should give `CredentialsError` naming `--admin-credentials-aws-region`. But `adapters/database.py:110` fixes `_EXTRA = "database"`, and the dump/survey handlers (`:774-782`, `:1059-1066`) map every `MissingDependencyError` to that extra. `RegionNotKnownError` (a `CredentialsUnobtainableError`) becomes a bare `CredentialsError(source)`. | Read `MissingDependencyError.extra` in those handlers, and map `RegionNotKnownError` to a reason naming the region flag. The edit lands in `adapters/database.py`, so it follows the F5 decision. | owner (same decision as F5) |
| F7 | MAJOR | Core §7; Python overlay (lockfile) | E005-S01-001:108; :50, :209, :215, :217 | `aws = ["l3io-wordpress-database[aws]>=0.1.0,<0.2"]` cannot be satisfied alongside the built `database = ["l3io-wordpress-database>=0.3.2,<0.4", …]` (`pyproject.toml`), and CI installs `--all-extras` (`ci.yml:60, :87, :234`). The story was verified against rev `47384ba`; the lock has 0.3.2. Installed 0.3.2's metadata declares `Provides-Extra: aws` → `boto3<2,>=1.34`. E004-S01-003:91 rewrites the same `database` line (with its own stale `>=0.2.0,<0.3`). | `aws = ["l3io-wordpress-database[aws]>=0.3.2,<0.4"]`, resolved through the existing `testpypi` source. Re-verify against 0.3.2's `credentials.py`, not `47384ba`. Agree the final `database` extra line with E004-S01-003 (X1). | built |
| F8 | MAJOR | Core §4; buildable in its own loop | E005-S01-001:24 (AC "selectable from both the CLI and the library"), :53, :155, :199-202 | E005 cannot meet this AC or run its integration test. The AWS flags are still `NOT_YET_IMPLEMENTED` (`cli/parser.py:99-109`). The environment step is skipped (`adapters/database.py:597`) until wiring that `epics.md:798-812` gives to **Epic 8**; library selectability is E006 (phase 3). :53 still assigns the binding to "Epic 2 … and Epic 3", which is stale. | Scope E005's AC to the builders: each source is selectable through the builder's arguments, shown by unit tests. Move `tests/integration/test_credential_sources.py` and the `credential_source` log assertion (:155) to E008-S01-001's test list. Correct :53 to Epic 8. | decision (`epics.md` Epic 8) |
| F9 | MAJOR | Global rule 4 (a guard's reach); Core §4 | E005-S01-001:191 | The guard is red on arrival. `test_single_credential_chain.py` fails on "any class [that] defines a method named `resolve` returning `ResolvedCredentials`", and the built `adapters/database.py:552` (`_RegisteringSource.resolve -> ResolvedCredentials`) is such a class. | Add a named exemption that must still exist and still match, as in `tests/contracts/test_repr_clean.py`'s `NOT_SECRETS`. Or narrow the rule to a class that also iterates sources. Plant both a violation and a stale exemption. | built |
| F10 | MINOR | AD-8; global rule 3 | E005-S01-001:196 | "The CI base-install import check … imports `…credential_providers`". The `verify-install` job imports only `l3io.wp.backup` (`ci.yml:317-322`), so nothing imports this module. | Either add the module import to `verify-install` (a `ci.yml` edit, shared with E004; see X1), or add a pytest test that imports it with `l3io.wp.database` hidden from `sys.modules`. | built |
| F11 | MINOR | Core §4 | E005-S01-001:181, :187 | The story says "The `database` extra is in the dev group" and "`boto3`, in the dev group". Neither is in `[dependency-groups]`; CI gets them through `--all-extras`. `botocore.stub.Stubber` cannot attach to the client, because `AwsSecretsManager.resolve()` builds its own (`l3io/wp/database/credentials.py:143`). | Correct the wording. Patch `boto3.client` to return a stubbed client. | built |
| F12 | MINOR | Core §10; global rule 3 | E005-S01-001:63-67 vs :164; :189-193 | :164 requires a README credentials section, but the "Files added or changed" list omits `README.md`. The new guard does not say what it cannot see (for example `contextlib.suppress(CredentialsAbsentError)`, recursion, or a name bound at runtime), which `test_credential_sources_built_once.py:10-13` does state. | Add `README.md` to the file list. State the guard's blind spots in its docstring. | built |

### E005-S01-002 — binary preflight and the diagnostic

| # | Sev | Principle | Location | Finding | Remediation | Settled by |
|---|---|---|---|---|---|---|
| G1 | BLOCKER | Core §3 DbC; AD-13; ADR-0015 | E005-S01-002:96-98, :139, :155, :207-210; E008-S01-001:52 | Every built plan that dumps carries `DUMP_REQUIREMENT` (`name="database-dump"`, `core/backup.py:44-48, :97`; `core/restore.py:1486`), and `cli/preflight.py:68-95` routes it to `check_dump`. The story's `BinaryPreflight` raises `ValueError` for any name outside {`age`, `age-plugin-batchpass`, `wp`}. E008-S01-001:52 then binds it in place of `DumpReadinessPreflight`, checking "exactly the plan's `required_tools`". Every `database`/`all` backup and every restore that takes a snapshot would crash with a programming error. The story's own tripwire ("every name is in `KNOWN_TOOLS`") fails on the built code for `BackupScope.DATABASE`/`ALL`. :139 still reasons about a `mysqldump` literal; the built code resolved that with `database-dump`. | `BinaryPreflight` checks only binaries this package runs. `RootPreflight` (E008) sends `DUMP_REQUIREMENT.name`, imported rather than written as a literal, to the built `DumpReadinessPreflight`, and everything else to `BinaryPreflight`. The tripwire imports `DUMP_REQUIREMENT` and maps it to the fixed MySQL README row. The allow-list check exempts exactly that one name. Rewrite :139. Make the matching edit to E008-S01-001:52. | built |
| G2 | MAJOR | Core §3 DbC (one contract, not two) | E005-S01-002:87, :91, :140, :184 vs :63 | The OPEN added at :140, from E007-S01-004's decision, was never folded into the body, and the two conflict. :140: always probe `wp`, with `[path, "cli", "version"]`, `--allow-root` iff root, cwd = the Run workspace, and `ToolStatus.error` on failure. Body: probe "only when a minimum is set" (:91), argv fixed at `[path, "--version"]` (:91, :184), and a constructor with no cwd (:87). :63 also says the adapter has "no WP-CLI-specific logic". | Fold :140 into the body. `ToolInfo` gains `version_argv` (and `always_probe`). The constructor takes `cwd: Path`. Reword :63 to allow per-tool probe arguments held as data in `KNOWN_TOOLS`. | decision (E007-S01-004:79, resolved 2026-10-27) |
| G3 | MAJOR | FR-27 ("reports … their versions") | E005-S01-002:45-47, :91, :99, :105-110 | `diagnose` checks the `KNOWN_TOOLS` entries, which carry no minimum (:99), and the probe runs only when a minimum is set (:91). So every version in the diagnosis is `None`, and FR-27's third consequence (`prd.md:471-473`) fails. | Probe every present tool in `diagnose`. Either always probe present tools, or add a `probe_versions=True` mode used by `diagnose`. | decision (FR-27) |
| G4 | MAJOR | Core §4; buildable in its own loop | E005-S01-002:24-26 (ACs), :52-54, :137, :216-222 | `test_preflight_refusal.py` needs a real passphrase-encrypted backup. `backup --encrypt-with-passphrase` is `NOT_YET_IMPLEMENTED` (`cli/parser.py:104`), the cipher is E004-S01-003's, and the binding is E008's `RootPreflight`. `doctor` is also `NOT_YET_IMPLEMENTED` (`cli/parser.py:110`), and E008-S01-001:53 owns it. The ACs "checked before the first effect" and "a diagnostic command reports" are therefore E008's (`epics.md:812-813`). | E005 delivers `BinaryPreflight`, `unsupported_platform`, `diagnose` and the generator, with unit tests that use injected doubles and fake executables. Move `test_preflight_refusal.py` and the `test_preflight_order.py` reuse (:222) to E008-S01-001. Reword the ACs as the data and adapter E008 binds. | decision (`epics.md` Epic 8) |
| G5 | MAJOR | Global rule 4; plan ordering | E005-S01-002:121, :212 | The tripwire "fails once relocation emits requirements (Epic 7) without their enumeration being added", but no story owns that enumeration. E007 runs in phase 3, after E005 (`plan-2026-10-27-v2.yaml:46-54`). E008 does not depend on E007 (`epics.md:133`). No E007 story names `DOCUMENTED_REQUIREMENT_SOURCES`, `gen_tool_docs.py` or the tripwire (grep of `epic-007/` is empty). E007 would land red with no owner, the same failure Re-check 2 fixed for E003/E004. How the tripwire detects "relocation emits" is also unstated. | Apply Shawn's 2026-11-03 pattern (the later epic registers). Grant E007-S01-004 one entry in `DOCUMENTED_REQUIREMENT_SOURCES`, and add the tuple, README table and test to its file list. Define the trigger mechanically, for example: any `RelocationFlags` whose `required_tools()` is non-empty and whose requirement is not registered. | decision (2026-11-03 pattern) |
| G6 | MINOR | Core §6 (current state, not history) | E005-S01-002:121, :233, :235 | Stale text. :235 says this story "restates the documented `auto`/`binder`/`cli` rule in about 10 lines", and :233 lists binder detection; both moved to E004-S01-003 (AG-8). :121's reason, "Epic 3 runs beside this epic", no longer holds: E003 is closed (`655dcf8`). | Delete :233/:235's backend text. Keep `RestoreMode` in E008, but restate the reason: it is already granted there, and moving it back would churn. | built |
| G7 | MINOR | Core §2 reuse; DbC | E005-S01-002:90, :130, :140, :152-154 | The story drifts from the built `domain/preflight.py`. `ToolStatus` needs `name` (`domain/preflight.py:78`), which :90 omits. :130/:154 write `found="version unknown"`; the one decision is `preflight_failure` (`:117-145`) with `UNKNOWN_VERSION = "unusable (version unknown)"`. :152's "raised error names the first" describes `require_tools` (`cli/preflight.py:98-108`). :140's `install_hint` from `KNOWN_TOOLS` cannot reach the error, because `BinaryPreflight` returns only `ToolStatus`. | `BinaryPreflight` returns statuses only and builds no error text. The `found` wording and the raise come from the built functions. An `install_hint` default belongs to the requirement's author (E004/E007), or to `RootPreflight` through `dataclasses.replace`. | built |
| G8 | MINOR | ADR-0018 (one redaction) | E005-S01-002:154 | "Passed through E002-S01-006's `redact`". `redact` is a method of `SecretRegistry` (`adapters/redaction.py:108`), and `BinaryPreflight` is given no registry. The logging pipeline already redacts every record (`cli/logging.py:34, :72`, `redact_open_runs`). | State that the pipeline covers probe output; add no second redaction call. | built |
| G9 | MINOR | Consistency with built conventions | E005-S01-002:76, :126 | The markers `<!-- tools:begin -->`/`<!-- tools:end -->` do not match the built convention, `<!-- BEGIN GENERATED: cli-usage (tools/gen_cli_docs.py) -->` (`README.md:58`). The CI step (:126) edits `ci.yml`, which is not in the file list. | Use `<!-- BEGIN GENERATED: tool-requirements (tools/gen_tool_docs.py) -->`. Add `.github/workflows/ci.yml` to the file list (or rely on `test_tool_docs.py` alone and say so). | built |
| G10 | MINOR | Global rule 4 (scope from the source of truth) | E005-S01-002:116, :205 | Circular test: the assertion "`extras` … equals `Provides-Extra`" reads the same metadata `diagnose` reads, so it cannot catch a stale install. | Compare with `[project.optional-dependencies]` read from `pyproject.toml` with `tomllib`. | built |
| G11 | MINOR | Core §9 (one log schema) | E005-S01-002:168-172; E008-S01-001:78; `cli/preflight.py:109` | Three record shapes. This story: `preflight_checked` at DEBUG/ERROR with `tool`, `outcome`. E008-S01-001:78: INFO with `name`, `found`, `needed_for`. Built: one `preflight_passed` at INFO. | E005 owns the record and fixes its shape; E008 cites it. Say whether `preflight_passed` stays. | built |
| G12 | MINOR | Core §7 (dependency recorded) | E005-S01-002:77, :231 | `packaging` becomes a runtime dependency. It is vetted (26.3, Apache-2.0 OR BSD-2-Clause, already locked, no transitive dependencies). The precedent records the owner's acceptance in `pyproject.toml` (platformdirs: "Accepted by Shawn 2026-10-20"). | Owner acknowledges, recorded in the comment beside the dependency. | **owner** (acknowledgement) |

### Cross-epic

| # | Sev | Principle | Location | Finding | Remediation | Settled by |
|---|---|---|---|---|---|---|
| X1 | MINOR | Parallel-phase file ownership | E005-S01-001:65, :108; E005-S01-002:76-77, :126; E004-S01-001:59; E004-S01-003:91, :97 | E004 and E005 both edit `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml` and `README.md` in phase 2. BL-E004-006 ("record the shared edits and an order") is still open, and the E005 stories do not name it. | Name BL-E004-006 in both E005 stories. Record the rule: whichever lands second rebases and runs `uv lock`, and keeps the other's lines. | built |

## Per-principle walkthrough

| Principle | Result |
|---|---|
| Core §1 Separation of concerns | F1, F5 |
| Core §2 Reuse over copy-paste | F3, F4, G7 |
| Core §3 Design by contract | G1, G2 |
| Core §4 Testability | F8, F9, F11, G4, G5, G10 |
| Core §5 Brevity | PASS. The builders are a few dozen lines once F1/F4 remove the flag parsing |
| Core §6 Comments state the current state | G6 |
| Core §7 Dependency selection | F7, G12 |
| Core §8 GA over preview | PASS. `packaging` 26.3 and `boto3` are GA. The TestPyPI family index is an existing owner decision (2026-11-02) |
| Core §9 Unified logging | G8, G11. Run ID through `merge_contextvars`: PASS |
| Core §10 Documentation | F12, G9 |
| Python overlay: uv, lockfile, `pyproject.toml` as source of truth | F7 (the pin would break `uv lock`); otherwise PASS |
| Python overlay: mypy strict, ruff, pytest only | PASS. Every test named is pytest; no custom runner |
| Global rule 1: never hand-roll | PASS. `CredentialChain`/`Environment`/`AwsSecretsManager` are reused; `packaging.version`, `packaging.requirements`, `importlib.metadata` and `shutil.which` are used; no hand-written version comparison or requirement parser |
| Global rule 3: enforce mechanically | F9, F10, G5 |
| Global rule 4: a guard's scope from the source of truth | G1 (the tripwire is wrong about the built plans), G10. Otherwise PASS: walked package scope, `BackupScope` from the enum, planted violations |
| `docs/closeout-gate.yaml` | PASS. No item is owned by an E005 story |
| ADR-0016 / ADR-0022 amendments (2026-11-06) | PASS. Neither touches credential sourcing or preflight. The Dump running with the WordPress account's rights does not change F1–F6 |
| E004 Re-check 2 conformance | PASS (table above) |

## Gate

BLOCKERs F1 and G1, and every MAJOR, must be resolved or recorded in an ADR before E005 builds.
Ten of the fourteen are settled by the built code or by a final decision, and can be edited into the
stories without the owner. MINORs defer to the backlog.

## Decisions needed — Shawn's

1. **F2:** FR-14's custom-provider AC. Defer it to v1.1, as PRD §13 already does, or add a
   backup-owned `CredentialSpec` variant. Record either way as an ADR-0015 amendment.
2. **F5 (and F6):** who may edit the closed Epic 2 module `adapters/database.py` to bind the
   environment and AWS steps and correct the extra and region mapping: E005-S01-001, or E008-S01-001
   through a public registering wrapper.
3. **G12:** acknowledge `packaging` as a runtime dependency.

## Recommended ADRs

- An ADR-0015 amendment for F2: how a library caller supplies a custom provider, or that it waits
  for v1.1.
- An ADR-0015 amendment, or an epics.md ownership note, for F5: the edit site that keeps password
  registration on every credential source.

## Summary

- **E005-S01-001** was written before the ADR-0015 amendment's seam was built. The fix is to
  rebuild its interface around `CredentialSpec`, import the domain precedence, drop the duplicated
  flag validation, fix the `aws` extra's pin, exempt `_RegisteringSource` from the new guard, and
  hand the end-to-end evidence to E008. Two points need the owner (F2, F5).
- **E005-S01-002** must account for the built `database-dump` requirement before
  `BinaryPreflight` can be bound. Its `wp` probe text must be merged into one contract. The
  diagnostic must probe versions. The relocation tripwire needs an owner in E007, and the
  integration and `doctor` tests move to E008.
- The E004 gate's Re-check 2 edits are reflected correctly.

DONE — BLOCKER 2, MAJOR 12, MINOR 11, owner decisions 3

## Re-check — 2026-11-06

- **Scope:** the uncommitted `git diff` of `epic-005/sprint-01/stories/E005-S01-00{1,2}.md`,
  and the dependent edits in `E006-S01-001.md`, `E007-S01-004.md` and `E008-S01-001.md`.
- **Checked against:** the built code at `HEAD` (`655dcf8`), `docs/adr/`, the installed
  `l3io-wordpress-database` 0.3.2 (`credentials.py`, `errors.py`, `_passphrase.py`) and the PRD.
- **Read-only:** nothing but this section was written.

### BLOCKER and MAJOR findings

| # | Status | Evidence |
|---|---|---|
| F1 | Resolved | E005-S01-001 L36, L98-106: the builders take `CredentialSpec \| None` (`domain/credentials.py`), and no database type reaches `cli/`/`api/`; the custom-provider paragraph and the re-export list are struck (L164). |
| F2 | Resolved (record missing) | L35, L90, L201, L247: the custom-provider AC is deferred to v1.1, which PRD §13.2 (`prd.md:631`) already says. The review's "record as an ADR-0015 amendment" was not done (`docs/adr/0015…` is unchanged); see M-1. |
| F3 | Resolved | L107, L243: the domain tuples are imported, nothing is declared in `adapters/`. |
| F4 | Resolved | L108-111, L199-200, L245; E008-S01-001: no flag parsing, no `CredentialSourceConflictError`; the built `admin_spec` (`cli/credentials.py:54-81`, `""` read as unset at L70) is cited exactly; E008 retires `cli/parser.py::ADMIN_PASSWORD_VARIABLE`. |
| F5 | **Partly resolved** | L45-49, L84-86, L112-117; E008-S01-001's `cli/credentials.py` bullet: E005-S01-001 now edits `adapters/database.py::wordpress_source`/`admin_source` (`:586-623`) to call the builders inside `_registering` (`:568-573`), so `_RegisteringSource.resolve` (`:551-565`) registers every resolved password. E008 no longer bypasses it. The mechanism is right and buildable. Not resolved: this is the owner's grant to edit a closed Epic 2 module, which the review sent to Shawn. The stories cite it as "decision 9", but no ADR-0015 amendment, `epics.md` note or other repository record holds it (M-1). |
| F6 | **Partly resolved** | L48, L118-120, L196: `MissingExtraError(extra=exc.extra)` (`MissingDependencyError.extra` exists, database `errors.py:57`); `RegionNotKnownError` (`errors.py:92`, a `CredentialsUnobtainableError`) caught first. It rides on the same unrecorded grant as F5. |
| F7 | Resolved | L121, L168: `l3io-wordpress-database[aws]>=0.3.2,<0.4`; installed 0.3.2 declares `boto3<2,>=1.34; extra == 'aws'`. |
| F8 | Resolved | L31-34, L64, L216, L266: the AC is scoped to the builders and `drive_backup`; `credential_source_selected` is added in `_RegisteringSource.resolve`; the CLI selectability and AWS-flag cases move to E008-S01-001, whose diff carries them. The integration test reaches the env step through `cli/backup.py:609-614` → `cli/credentials.py:84-94` → `adapters/database.wordpress_source`, and `require_wp_config_credentials` (`adapters/database.py:473-498`) resolves the whole chain, so env-wins passes. |
| F9 | Resolved | L258-260: a named exemption for `_RegisteringSource`, checked for staleness, with planted cases and stated blind spots. |
| G1 | Resolved | E005-S01-002 L35, L104-108, L204, L277; E008-S01-001 `RootPreflight` bullets: `DUMP_REQUIREMENT` (`core/backup.py:44`) is routed to `DumpReadinessPreflight` (`cli/preflight.py:64-95`), everything else to `BinaryPreflight`. `DUMP_REQUIREMENT` is the only `ToolRequirement(` built in `src/`, so no other non-binary name reaches `BinaryPreflight`. The tripwire exempts exactly that name. |
| G2 | Resolved | L109-118, L205: one contract with `version_argv`, `root_argv`, `cwd`, argv-taking `probe`, and `ToolStatus.error`. |
| G3 | Resolved | L119: every present tool is probed in `check` and `diagnose`. |
| G4 | Resolved | L30-33, L81-83, L289-298: the ACs are delivered through the built `cli/backup.py::backup(..., extra_tools=, preflight=)` (`:516-528`) in an opened Run. The CLI Run and `doctor` move to E008-S01-001, whose diff takes `test_preflight_refusal.py`. |
| G5 | Resolved | L279-284; E007-S01-004 diff L60: E007 gets the `DOCUMENTED_REQUIREMENT_SOURCES` entry. The trigger is mechanical, over `cli/restore.py::RESTORE_EXTENSION` (`:724`) and `RelocationFlags` (`:653`), through `RestoreExtensionFactory(flags, *, secrets)` (`:715-721`). There is a planted case. Wording defects are in M-2. |

**E005: 12 of 14 resolved, 2 partly resolved (F5, F6), 0 not resolved.**

### New findings

| # | Sev | Where | Finding |
|---|---|---|---|
| M-1 | MAJOR | E005-S01-001 L42, L84, L112, L118; E005-S01-002 L35, L39, L133, L183, L307-311; E008-S01-001 | **The stories cite owner decisions that are recorded nowhere.** "Decision 9" grants the edit to the closed Epic 2 module `adapters/database.py`. Decisions 10-14 cover routing, `packaging`, E007's grant, files-in-scope and the moved ACs. A repository-wide grep finds "decision 9/10/14" only in these story files. No ADR changed except ADR-0024, and the review's two recommended ADR-0015 amendments (F2, F5) do not exist. E005-S01-002 L311 also has the builder write "Accepted by Shawn 2026-11-06 (E005 gate G12)" into `pyproject.toml`. That attribution has no source. Global rule 3: prose that cites a decision nobody can find reads as authoritative. Record decisions 9-14 where the stories can link them: an ADR-0015 amendment for F2/F5/F6, and the G12 acceptance with its source. If the owner has not made them, F5, F6 and G12 are still open. |
| M-2 | MINOR | E005-S01-002 L284 vs `git diff` of E007-S01-004.md L60; E005-S01-002 L273-286 | L284 says "E007-S01-004's own story text is not edited by this re-check", but it was (one line under **Files.**). That line names the tripwire as `tests/contracts/test_tool_docs.py`. E005-S01-002 puts the relocation tripwire in `tests/contracts/test_tool_requirements_documented.py`, and `test_tool_docs.py` only runs `--check` (L286). Correct both. |
| M-3 | MINOR | `cli/credentials.py:5-7`; `adapters/database.py:515` | Two built comments go stale and nobody is granted to fix them. `cli/credentials.py` says "Epic 8 (E008-S01-001) changes only the delegation here, to Epic 5's providers". E008 now changes nothing there, and E005 marks the file "unchanged". `adapters/database.py`'s banner "the only construction of database's credential classes" becomes false once `credential_providers.py` constructs them. That one is in E005's grant, but the story does not say to fix it. |
| M-4 | MINOR | E005-S01-001 L100; `adapters/database.py:577-583` | The builders register an explicit spec's password "as the built `_explicit` does". `_explicit` lives in `adapters/database.py`, which will import `credential_providers`, so the builders cannot call it without an import cycle. That leaves a second copy of the rule, and `_explicit` either dead or registering twice. Say that `_explicit` moves into `credential_providers.py` (or is deleted). |
| M-5 | MINOR | E005-S01-001 L233; E005-S01-002 L253; `tests/conftest.py:210-250`; E004-S01-003 L54 | Both E005 stories ask for `credential_sentinel` "variants". The fixture takes only `{"passphrase": True}`. If a variant is added as a fixture parameter, that is a `tests/conftest.py` edit outside both E005 grants. E004-S01-003 edits the same file at the same time (removing `FakeAge`), so the two would collide. State that the variants plant the variables in-test through `sentinel.monkeypatch.setenv`. |
| M-6 | MINOR | E005-S01-002 L114-115, L167 | `BinaryPreflight` now requires `cwd` (the Run workspace). `diagnose(*, preflight=...)`, used by `doctor`, has no Run workspace, and neither story says what `cwd` the diagnostic passes. |

**No E004/E005 file collision beyond the four shared files BL-E004-006 already governs** (`pyproject.toml`, `uv.lock`, `README.md`, plus M-5's `tests/conftest.py` if it is done as a fixture parameter). E005 no longer edits `.github/workflows/ci.yml`. E004-N-4 would add `README.md` to E004-S01-003 under the same rule.

### Totals

- E005: resolved 12/14; partly resolved 2 (F5, F6); not resolved 0.
- New: BLOCKER 0, MAJOR 1 (M-1), MINOR 5 (M-2 … M-6).
