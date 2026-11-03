# Arch gate review — E004 "Archive lifecycle — exclusions, legacy reads, encryption"

- **Date:** 2026-10-28
- **Work type:** CODE
- **Reviewers run:** `l3io-arch-review` (Mode B) first, alone; it reported 2 BLOCKER and 8
  MAJOR, so per step-04 §4 the gate widened to `bmad-agent-architect`, run on the same inputs
  without sight of the first report.
- **Scope:** 3 story files, `epic-004/sprint-01` (E004-S01-001, -002, -003) — derived count.
- **Spec alignment:** ON; 23 pointed-to ranges. The spec index is 27,215 bytes, above the
  16,384 guideline (spec §8), after today's ADR backlinks.
- **Raw reports:** `reviewer-l3io-arch-review-2026-10-28.md` (2 BLOCKER, 8 MAJOR, 15 MINOR),
  `reviewer-bmad-agent-architect-2026-10-28.md` (1 BLOCKER, 7 MAJOR, 17 MINOR).
- **Verdict:** **BLOCKED** — 2 BLOCKER, 10 MAJOR after merge (6 confirmed by both, 4
  single-source). MINORs deferred to the issues file.
- **Owner decisions of 2026-10-27/28 for E004:** all five are applied in the stories (both
  reviewers). Two carry defects, listed as AG-3 and a MINOR (E005-S01-002's superseded
  `required_tools` route).

## Findings

| ID | Sev | Source | Finding |
|---|---|---|---|
| AG-1 | BLOCKER | both | Encrypted site-file and config members are never decrypted on restore. E004-S01-003 L46 hands restore to Epic 3, but Epic 3 extracts only through E002-S01-003's `extract_archive` (AD-11's single extraction function), which takes no cipher; only the Dump has a decrypt path (E003-S02-005); E003-S02-006 L68 reads the config member raw; `ViewMember` has no `encrypted` field. An encrypted `files`/`all` restore writes ciphertext over the Site. The fix changes E002-S01-003's gated contract |
| AG-2 | BLOCKER | l3io-arch-review (BLOCKER); architect (MAJOR) | E004-S01-002 still expects `extract_archive` to refuse `symlink-escape` (L33, L46, L57, L77, L80), against today's ADR-0017 amendment (skip, `skipped_links`, continue); it never asserts `ViewMember.link_target is None` for v0 |
| AG-3 | MAJOR single | l3io-arch-review | The binder wheel source (E004-S01-003 L84–85) does not lock as written. Confirmed independently 2026-10-28 (uv 0.11.6, scratch project): through the git-pinned database, `uv lock` resolves `l3io-age-passphrase` from database's own git source (tag v0.1.0), i.e. a Rust source build. ADR-0014's amendment and the `pyproject.toml` comment give the reason backwards ("uv ignores a dependency's own sources"). The reviewer reports `[tool.uv] override-dependencies` with per-platform wheel URLs locks with sha256s — not yet verified here |
| AG-4 | MAJOR confirmed | both | CI cannot install `age` ≥ 1.3 or `age-plugin-batchpass` from apt (Ubuntu 26.04: 1.2.1; 24.04: 1.1.x; batchpass unpackaged), so the CLI-backend interop test cannot "skip neither silently in CI" as specified |
| AG-5 | MAJOR confirmed | both | An identity-only cipher (restore given `--identity-file`) has no recipient, but E003 encrypts the Safety Snapshot whenever a cipher is present; database offers no identity→recipient derivation. **Needs Shawn's decision** |
| AG-6 | MAJOR confirmed | both | Restore-side key setup: a passphrase given with identity files is ambiguous (AD-26: it unlocks a protected identity; read literally the cipher gets both and breaks database's "exactly one" rule); and (architect) no route resolves a passphrase to restore a passphrase-encrypted Archive |
| AG-7 | MAJOR confirmed | both | A missing or wrong key is found only at the first decrypt — in a restore after the Safety Snapshot and, in clean modes, after CLEAN/drop. Nothing raises `no_key` when the cipher is `None`; no plan-time refusal of an encrypted Archive without a key |
| AG-8 | MAJOR confirmed | both | E004-S01-003 calls E005-S01-002's `passphrase_backend_requirements`, but E004 and E005 run in parallel in phase 2 and the plan records no edge |
| AG-9 | MAJOR single | architect | `--encrypt-with-passphrase` with no resolvable passphrase (e.g. under cron): `resolve_passphrase` → `None` → `build_member_cipher` → `None` → a plaintext Archive. Violates AD-26 "no silent plaintext" |
| AG-10 | MAJOR single | architect | Acceptance tests that cannot pass in E004's own loop: they go through CLI flags or `api/`, which exist only after E006/E008 — E004-S01-001's end-to-end test, E004-S01-003's inspector test, `test_single_passphrase_chain`'s CLI/`api/` checks, the L165 AST scan. Driving them through `drive_backup(BackupRequest(...))` fixes the first two |
| AG-11 | MAJOR single | l3io-arch-review | E003/E004 collide on v0 config: E003-S02-006 uses the literal `wp-root/wp-config.php`, which E004's `test_no_v0_writer` grep rejects; `--overwrite-wp-config` is refused for every v0 Archive because v0 has no `config_path` |
| AG-12 | MAJOR single | l3io-arch-review | The exclusion log events (`exclusions_applied`, `path_excluded`, `protected_path_kept`) have no owner in any E002/E008 story; `decide()` never says a protected decision carries the matching pattern |

MINORs (32 raw, deduplicated to 24) are filed in `state/issues.yaml` (§5).

## Decisions needed — Shawn's

- **AG-5:** what a restore given only an identity encrypts the Safety Snapshot with.
- **AG-1:** decrypting on restore changes E002-S01-003's gated `extract_archive` contract.
- **AG-4:** how CI gets `age` ≥ 1.3 and `age-plugin-batchpass`, or whether the CLI backend's
  CI coverage is dropped.
- **AG-8:** order E005 before E004, or move the passphrase-backend function.

## Resolution — 2026-10-29/30

Shawn's decisions:
- **ADR-0024** (accepted 2026-10-29): one encrypted payload, passphrase only for v1. This
  dissolves AG-1, AG-5 and AG-6, and AG-7 moves into VERIFY.
- **AG-3 (a):** the binder's wheels are on the family index (database, 2026-10-30); CI
  installs every extra from that index with `--no-build` (`f17170c`).
- **AG-4 (a):** a pinned `age` v1.3.2 download in CI.
- **AG-8 (a):** `passphrase_backend_requirements` moves to `adapters/crypto`.

Applied in `e44ac5b`, with in-place edits to 21 stories.

## Re-check — 2026-10-30

- `recheck-bmad-agent-architect-2026-10-30.md`: **9/9 resolved**; 1 new MAJOR, 9 MINOR.
- `recheck-l3io-arch-review-2026-10-30.md`: **9/10 resolved**; AG-3 is partly resolved
  (no fallback stated for a platform without a wheel); 1 new MAJOR, 11 MINOR.
- **New MAJOR (architect): E005 → E004 is unrecorded.** E005-S01-002's tool-docs check
  fails once `adapters/crypto` exists without its enumeration. E004 and E005 run in
  parallel, so whichever lands second breaks CI, and no story owns the fix.
- **New MAJOR (l3io-arch-review): the family index needs credentials.**
  - Confirmed 2026-10-30: anonymous `/simple/` returns 401, the repository is public, and
    `pyproject.toml` already keeps database off that index for this reason.
  - AG-3's dev-lock mechanism would make every `uv sync --locked` (ci.yml L60, L87, L228)
    need credentials.
  - The `f17170c` step is red on a pull request from a fork, where `secrets.GITEA_TOKEN`
    is empty.
  - **Needs Shawn's decision.**

**Verdict after one resolution pass: BLOCKED** — 2 blocking findings.

## The credentials MAJOR — resolved 2026-11-03 (Shawn's decision of 2026-11-02)

- **Decision:** the family resolves anonymously from **TestPyPI** for development and CI,
  through an explicit uv index. TestPyPI is a stepping stone, not a release index: the
  family moves to PyPI before backup's first real release (Shawn's call when it comes).
- **What exists now:** database 0.2.1, config 0.1.3 and binder 0.1.1 are on TestPyPI.
  - database 0.2.1 and config 0.1.3 are source-identical to 0.2.0 and 0.1.2 (`git diff`
    over `src/` is empty).
  - database 0.2.1's published metadata names config and the binder by version range.
- **Changes:**
  - `pyproject.toml`: database and config come from the `testpypi` explicit index; there
    are no git sources left in `uv.lock`.
  - The CI extras check is anonymous: an explicit TestPyPI index, with its package set
    derived from `uv.lock`, and it runs on pull requests.
  - The contract test is proven by four plants. A guard keeps any future family-index
    step off pull requests, gated on the event.
- **E004-S01-003:** takes the binder from the same index when it adds the `encryption`
  extra, via `override-dependencies`, because uv applies sources only to direct
  dependencies.
- **Still open for E004:** the architect's MAJOR. E005-S01-002's tool-docs check fails
  once `adapters/crypto` exists, and nothing records the order.
