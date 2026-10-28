---
reviewer: l3io-arch-review
mode: B — architectural review of an existing design
epic: E004
stories: [E004-S01-001, E004-S01-002, E004-S01-003]
date: 2026-10-28
standards: references/standards-core.md §1–10, references/standards-python.md
---

# E004 arch gate — l3io-arch-review (Mode B)

## Executive summary

The three stories are carefully built against E002's contracts. Exclusions (S01-001) and the v0 fixtures (S01-002) are close to buildable. Encryption (S01-003) is sound inside its own module, but its edges have problems. The count is 2 BLOCKER, 8 MAJOR and 15 MINOR.

The most important action is to give one story the job of decrypting payload members on restore. Today nothing decrypts `site-root` or `config` members. E002's `extract_archive` takes no cipher, E003 decrypts only the Dump, and E004-S01-003 hands restore to a story that does not do it. The second BLOCKER is that E004-S01-002 still expects `extract_archive` to refuse an outward symlink. The ADR-0017 amendment of 2026-10-28 made that a skip.

Two MAJOR findings were confirmed by experiment rather than reading:
- The binder wheel source, as S01-003 words it, does not lock under uv 0.11.6.
- The CI image's `apt` cannot provide `age` ≥ 1.3.

## Findings

BLOCKER: [Contract coherence E002/E003/E004 · Core §1, NFR-1, FR-4 "restore from one by supplying the same"] No story decrypts encrypted `site-root` or `config` members on restore, so an encrypted `files`/`all` Archive restores ciphertext into the Site Root.
- E004-S01-003 §Non-goals (L46) says restore is "Epic 3 story 3.1, which calls this story's decryption adapter". E003-S01-001 contains no cipher at all.
- The one extraction function, E002-S01-003 L65 `extract_archive(source, destination, view, *, roles, exclude)`, takes no cipher. A `tarfile` `filter` can only rewrite a `TarInfo`, so it cannot decrypt.
- E003-S02-005 L70 decrypts only the Dump.
- E003-S02-006 L68 reads the `config` member raw through `ArchiveProbe.read_member` and parses the bytes as `wp-config.php`. Those bytes are ciphertext whenever S01-003 L38/L88 has encrypted them.
- `ViewMember(path, role, kind, checksum, link_target)` (E002-S01-003 L58) has no `encrypted` field, so a restore cannot even tell from the view which members need decrypting.
- Remediation: assign the seam explicitly before build. Either E002-S01-003's `extract_archive` gains `cipher: MemberCipher | None` and streams `encrypted` file members via `extractfile` → `cipher.decrypt` → a contained destination file, with the containment rules unchanged, or E004-S01-003 owns a decrypting extraction step in `adapters/crypto` that the restore calls. Add `ViewMember.encrypted`. Have E003-S02-006's config-member read go through `cipher.decrypt`. Add an E004 end-to-end test: encrypted `all` backup → restore → tree equals source.

BLOCKER: [AD-11 / ADR-0017 amendment 2026-10-28 · spec-conflict] E004-S01-002 still asserts that `extract_archive` **refuses** `malformed/symlink-escape.tar.gz` (`wp-root/link -> ../../outside`): §Outcome L33, §Non-goals L46 ("asserts only that extraction refuses one"), §Interface contracts L57 ("must not extract"), §Error handling L77, CLI L80.
- The amendment, applied to E002-S01-003 L65/L102/L124, makes `extract_archive` skip an outward or absolute link, record it in `ExtractResult.skipped_links`, log `symlink_skipped`, and continue. The restore then exits `OK`.
- §Non-goals L46 also still calls the question "ADR-0017's Concern, owned by Epic 3", which the amendment resolved.
- As written, the test fails against E002's code, or invites a dev to "fix" E002's extraction back to refusal.
- Remediation: move `symlink-escape` out of `malformed/` into the passing set, or keep it there under an extraction-only row. Assert:
  - the link is absent from the destination;
  - `wp-root/link` is in `skipped_links` with its reason;
  - exactly one `symlink_skipped` WARNING;
  - every other member is extracted and nothing is written outside `tmp_path`;
  - `verify` passes `STRUCTURAL_ONLY`.

  Also assert `ViewMember.link_target is None` for every member of every v0 fixture (E002-S01-003 L58), which is the v0 half of the amendment's Dry Run note.

MAJOR: [Python overlay — locked, reproducible install · FR-25 · Core §7] E004-S01-003 §Interface contracts L84–85 says to add "a `[tool.uv.sources]` entry for the binder's release wheels". Under the pinned uv 0.11.6 that does not work, checked in a scratch project with database at `48a7da9`:
- (a) A root `tool.uv.sources` entry for the transitive `l3io-age-passphrase` is ignored. The lock resolved the binder from `git+…l3io-age-passphrase.git?tag=v0.1.0`, which is database's own source, so it is a Rust sdist build.
- (b) Declaring the binder directly as well fails `uv lock` with "Requirements contain conflicting URLs for package `l3io-age-passphrase` in all marker environments".
- ADR-0014's amendment premise (L103–105, mirrored in `pyproject.toml` L55–59), "uv ignores a dependency's own `[tool.uv.sources]`", is false for a git-sourced dependency.
- What does lock reproducibly: `[tool.uv] override-dependencies` with one marker-split direct URL per published wheel, for example `l3io-age-passphrase @ …/l3io_age_passphrase-0.1.0-cp311-abi3-manylinux_2_17_x86_64….whl ; sys_platform == 'linux' and platform_machine == 'x86_64'`, with linux aarch64 and darwin arm64/x86_64 alongside. `uv.lock` then records each wheel's sha256. The wheels are `abi3-py311`, so one per platform covers 3.11–3.14.
- Remediation: rewrite L84–85 to name this mechanism and the four supported platform markers (spine §Stack: Linux and macOS). State what happens on an unlisted platform: musl, where `platform_machine` cannot tell musl from glibc, and others. Correct ADR-0014's amendment and the `pyproject.toml` comment. Add a CI assertion that `uv.lock` holds no `git` source for `l3io-age-passphrase`.

MAJOR: [Core §4 testability · CI provisioning] E004-S01-003 L86 and L167 require the interop tests to cover both passphrase backends and to fail rather than skip when `CI` is set. L167 provisions `age` with "an apt step". Distribution `age` is below the 1.3 floor that database's `cli` backend needs: 1.2.1 on Ubuntu 26.04, checked with `apt-cache policy age`, and older on 24.04. `age-plugin-batchpass` is not packaged at all.
- The CLI-backend case is therefore red on every CI run, or a dev quietly drops it.
- Remediation: install pinned `age` ≥ 1.3 and `age-plugin-batchpass` release binaries in the test job, verified against published checksums (GitHub Actions/Docker overlay: pinned and verified). Alternatively, scope the hard-fail rule to the `binder` backend and stock `age -d`/`-r` interop (which 1.2.x serves), and record the `cli` backend as covered by database's own suite.

MAJOR: [Contract coherence E003/E004 · AD-12, AD-26 · spec-gap] The cipher's mode matrix leaves the most common encrypted restore undefined.
- E003-S02-003 L62 encrypts the Safety Snapshot "iff `request.cipher is not None`" through that same cipher.
- A restore of a recipient-encrypted Archive supplies `--identity-file` (E002-S02-011), so `build_member_cipher` returns `AgeMemberCipher(identities=…)` with no recipients.
- E004-S01-003 L68–76 never says what `encrypt()` does in identity-only mode. Database v0.2.0 exports no identity→recipient derivation (`encryption.py` has `generate_identity`, `read_identity`, `check_*`, `encrypt`, `decrypt` only), and this package may not import `pyrage` (ADR-0014).
- Result: SNAPSHOT fails on every identity-only encrypted restore, or a dev improvises.
- Remediation (owner decision): add `AgeMemberCipher.can_encrypt: bool`. Either require `--recipient` alongside `--identity-file` for an encrypted Snapshot, or treat identity-only as "no Snapshot cipher" with AD-26's explicit WARNING. Change E003-S02-003's rule to "iff `cipher is not None and cipher.can_encrypt`". Add a test for each mode.

MAJOR: [AD-26 L299–304 · spec-conflict] AD-26 says "the same chain unlocks a passphrase-protected identity file". E004-S01-003's factory (L76) reads identity files with `read_identity` and passes no passphrase. It also treats `passphrase` only as passphrase-mode encryption, and its mutual exclusion (L73) covers only recipients + passphrase.
- `build_member_cipher(identity_files=[f], passphrase=p)` is therefore ambiguous: is it passphrase-mode decryption, or `read_identity(f, p)`?
- A passphrase-protected identity file cannot be used at all.
- Remediation: state that with `identity_files` present, `passphrase` is passed to `read_identity` and the cipher is in identity mode. Decide whether one resolved passphrase may serve both purposes in one Run. Test a passphrase-protected identity (database `read_identity(path, passphrase)` exists at v0.2.0).

MAJOR: [NFR-1 · AD-14 · Core §3] E004-S01-003 §Error handling L112 raises `ArchiveDecryptionError(reason="no_key")` "when the first encrypted member is about to be decrypted". With no key, `request.cipher is None` (S01-003 L99), so no `AgeMemberCipher` exists to raise it.
- No E003 story refuses an encrypted Archive with no cipher at plan time; a search for `no_key`/`ArchiveDecryptionError` in E002, E003 and E008 finds nothing.
- The failure therefore surfaces late, after SNAPSHOT and possibly after destructive phases, or not at all.
- Remediation: state the precondition at the plan. Restore planning refuses with `ArchiveDecryptionError(reason="no_key")`, before any effect, when any view member is `encrypted` and `request.cipher is None` (this needs `ViewMember.encrypted`, as in BLOCKER 1). Give the raise site an owner (E003's `build_restore_plan` or the restore root), and add an E004 test that drives it.

MAJOR: [Core §9 logging · single ownership] E004-S01-001 §Observability L95 specifies three events, emitted by "the consumer": `exclusions_applied` (INFO), `path_excluded` (DEBUG) and `protected_path_kept` (WARNING). The §Error handling L79 WARNING depends on the last of these.
- `core/exclusions` is pure and cannot log them.
- No consumer story emits them: searching `epic-002` and `epic-008` for all three names finds nothing, and E002-S01-010 L61's `collect_site_root` lists none of them.
- The `decide()` contract (L55, L79) also never says that a `protected=True` decision carries the pattern that would have matched, which the WARNING has to name.
- Remediation: give the three events to E002-S01-010's `collect_site_root`/`TarArchiveWriter`, or to E008-S01-001. State `ExclusionDecision(excluded=False, pattern=<matching pattern>, protected=True)` for a protected path. Add a captured-log test in the owning story.

MAJOR: [Plan ordering · single ownership] E004-S01-003 L100 makes `AgeMemberCipher.required_tools()` call E005-S01-002's `passphrase_backend_requirements` (`adapters/preflight.py`, E005-S01-002 L74/L101).
- E004 and E005 are parallel peers in phase 2, and `plan-2026-10-27-v2.yaml` gives E004 a dependency on E002 only.
- E004-S01-003 cannot import or test a function that may not exist yet.
- Remediation: declare E004-S01-003 → E005-S01-002, ordered within phase 2, or keep `required_tools()` behind an injected callable until E008-S01-001 binds it. Record either in the plan.

MAJOR: [Contract coherence E003/E004 · AD-6/AD-23 single owner of member names] Two E003 behaviours collide with E004's v0 work:
- (a) E003-S02-006 L68 reads a v0 Archive's config at the literal `wp-root/wp-config.php` in `cli/restore.py`. E004-S01-002's `test_no_v0_writer.py` (a), at L107, fails on any `"wp-root"` literal outside `domain/archive_format.py`. Two phase-2 epics will turn each other's CI red.
- (b) E003-S01-001 L100 (ADR-0023) refuses `--overwrite-wp-config` whenever `view.config_path is None`, which is always for v0 (S01-002 L45). A v0 Archive does carry `wp-root/wp-config.php`, so every v0 overwrite is refused. 0.2.21 restored it.
- Remediation: have `domain/archive_format` expose the config member path for every format, for example `ArchiveView.config_member: str | None`, distinct from FR-29's `config_path` origin metadata. E003 should use it in both places, and E004-S01-002's fixtures should assert it equals `wp-root/wp-config.php` for the v0 shapes that carry one. Ask the owner whether `--overwrite-wp-config` applies to v0.

MINOR: [Single ownership · docs] `epics.md` §Epic 4 header (L513) says Epic 4 owns "`domain/archive_format` (v0 classifier only)". The Epic List (L131) and the ownership note (L143) give the classifier to Epic 2, with Epic 4 owning only the v0 fixtures and tests, and this gate's brief repeats the header. The stories follow the ownership note, which is correct. Remediation: fix the header line.

MINOR: [ADR-0017 member table] E004-S01-003 says encryption sets `encrypted: true` for "every payload member" (L88), and the inspector test says "every non-link member is `encrypted: true` and starts with `age-encryption.org/v1`" (L171). ADR-0017 `dir` entries, including the Site Root, carry no content and `encrypted: false`. Remediation: say "every `kind == file` member" in both places.

MINOR: [Core rule 4 — guard scope] E004-S01-003's committed-key guard (L147, L179) has three problems:
- Its wording at L147 contradicts itself: the test identity is inside `tests/fixtures/`.
- Scanning for the prefix `AGE-SECRET-KEY-` also matches this story's own prose and the guard's source.
- Allowing all of `tests/fixtures/` lets a real key hide there.

Remediation: match the full key form (`AGE-SECRET-KEY-1[0-9A-Z]{58}`), require exactly one hit repository-wide, and require it at `tests/fixtures/archives/v1/encrypted-recipient.identity.txt`.

MINOR: [Core rule 4 — guard scope] E004-S01-001 L107/L119 bans a hand-listed set of `pathspec` I/O helpers (`match_tree*`, `check_tree_files`). It misses others, such as the `pathspec.util.iter_tree_*` walkers. Remediation: invert it into an allow-list of the names `core/exclusions.py` may reference (`GitIgnoreSpec.from_lines` and the `match_file`/`check_file` query), so a new helper fails closed.

MINOR: [Core §3 contracts · typing] Two small gaps in E004-S01-001:
- `ExclusionSource` is a `StrEnum` (L54), while E002-S01-001 L70 types `ManifestExclusions.source` as `Literal["default","custom","none"]` and the port as `str`. Under `mypy --strict` a `StrEnum` member is not that `Literal`.
- `from_patterns((), include_defaults=False)` has no stated `source`: is it `CUSTOM` with no patterns, or `NONE`?

Remediation: state the conversion at the Manifest builder, and state that an empty, defaults-free `from_patterns` returns `none()`.

MINOR: [Ownership · format stability] E004-S01-001 L87 says a backup whose Manifest exceeds the cap "fails before publishing". No E002 writer story enforces a write-side cap; the only cap is the 16 MiB read cap at E002-S01-003 L113. The excluded-path list is also not what drives Manifest size: ADR-0017's per-member index is.
- At roughly 190 bytes per `ManifestMember` this is an estimate, not a measurement. It puts the cap near 90k members, which an `uploads/` tree with thumbnails reaches.
- Remediation: give the write-side check to E002-S01-010. Carry the member-index sizing to the E002 owner, where it is likely more than MINOR.

MINOR: [Core §4 · test stability] E004-S01-002's reproducibility test (L106) requires generated `.tar.gz` files to be byte-identical to the committed ones on every CI Python and OS. Deflate output depends on the zlib build (zlib vs zlib-ng). Remediation: compare the decompressed tar stream, or the members and headers, rather than the gzip bytes.

MINOR: [Single ownership] E004-S01-002 L58 edits E002-S01-003's `tests/contracts/test_supported_formats.py` "unless the orchestrator rules the file Epic 2's". Remediation: decide now. The ownership note gives Epic 4 the v0 tests, so record the two-line edit as granted.

MINOR: [FR-10 fidelity · fixtures] The v0 fixture set (L56–57) lacks two shapes the legacy `tarfile.add(site_root, arcname="wp-root")` actually writes: a hard-link member, which it emits for a multiply-linked inode, and a FIFO or other special file. E002's extraction refuses hard links that escape the destination, and the ADR-0017 amendment says it refuses special files. Remediation: add `synthetic-all-hardlink` (and a FIFO case), with their asserted outcome, so FR-10's "restores by default" is pinned for them.

MINOR: [Contract accuracy] E004-S01-002 L107(b) builds a v1 Archive "for every `BackupScope` member" with `write_archive(out, manifest_builder, members, *, cipher)`, which takes no scope. Remediation: drive E002-S02-007's `TarArchiveWriter.write(..., site_root=, dump=, config=, exclusion=, cipher=)` per scope.

MINOR: [Cross-epic consistency] E005-S01-002 L139 and L141 still say the roots, or `RootPreflight`, append `passphrase_backend_requirements` when the cipher is in passphrase mode. That was superseded on 2026-10-28 by `cipher.required_tools()` → `extra_tools`, now in E004-S01-003 L100, E002-S02-007 L60, E003-S02-006 L71 and E008-S01-001 L57. Remediation: update E005-S01-002 so `RootPreflight` does not also infer the mode.

MINOR: [spec-conflict — PRD FR-4] PRD §5.1 FR-4, consequence 2 (prd.md L130), records "algorithm, KDF parameters, salt" in the Manifest. E004-S01-003 L88 records none, correctly per ADR-0014 and AD-6. ADR-0014 says it changes FR-4's "mechanism (not its requirement)", but this consequence is a requirement, and the PRD carries no backlink the way FR-19 does for ADR-0019. Remediation: add an ADR-0014 amendment note under FR-4.

MINOR: [FR-26 · Core §7] Two gaps in the dependency inventory:
- E004-S01-001 L58 says "the dependency inventory records its licence", but no inventory file exists in the repository.
- E004-S01-003 adds `pyrage` and the pre-1.0 `l3io-age-passphrase` (MIT) with no inventory entry and no reference to the exit plan, as FR-26 requires (ADR-0014 amendment L85–88 is the exit plan).

The published `[database]` extra is still not resolvable from PyPI (existing revert condition, `pyproject.toml` L49–73), and S01-003 should say the `[encryption]` extra inherits that. Remediation: name the inventory file and add the three entries.

MINOR: [Fixture layout] Two fixture details need stating:
- E004-S01-003 L89 puts `encrypted-recipient.identity.txt` inside `tests/fixtures/archives/v1/`. E002-S01-003 L126's `v*/` walk reads "every fixture Archive" there, so it must filter by Archive suffix. State the filter, or keep the identity outside `v1/`.
- E004-S01-002's `escape-*` fixtures (L57) must also carry the v0 shape. Otherwise `verify` reports `UNRECOGNISED_FORMAT` rather than the asserted `UNSAFE_MEMBER_PATH`.

MINOR: [Parallel-phase file contention] E004-S01-001 and S01-003 edit `pyproject.toml`/`uv.lock`, and S01-003 edits `.github/workflows/ci.yml`. Phase-2 peers may touch the same files: E005-S01-002 L83 needs `packaging` at runtime. Remediation: record the shared-file edits in the plan, so the sprint agents serialise lock regeneration.

## Adoption check

| Recorded change | Present / absent | Evidence |
|---|---|---|
| S01-001: Non-goals name E002-S01-001/-010, E002-S02-011, E002-S01-004 and E008-S01-001 as owners | present | E004-S01-001 L43 |
| S01-001: `InvalidExclusionPatternError` subclasses `UsageError`, exits `USAGE` through the class attribute | present | L54, L82 |
| S01-001: `ExclusionDecision`/`ExcludedEntry` imported from `domain/exclusion.py`; `ExclusionSet` satisfies `ExclusionPolicy` structurally | present | L55–56 |
| S01-001: policy passed per call (`BackupRequest.exclusion` → `backup` → `ArchiveWriterCommand.write`), never an `AdapterSet` field | present | L63 |
| S01-001: flags in `NOT_YET_IMPLEMENTED` naming E008-S01-001 | present | L65 |
| Decision 1: library default = CLI's `default_exclusion_policy()`; include-everything explicit | present | L64, L67; E002-S01-004 L65 matches |
| Decision 2: Dry Run plan lists the active exclusion source and patterns | present | L69, L96; E002-S02-007 L61, L105 |
| S01-001: empty-site case is E002-S01-010's; E2E gated on E008-S01-001; `manifest_to_json` cited | present | L86, L120, L121 |
| S01-002: `synthetic-files-only` passing; `missing-database-sql` dropped; `missing-wp-root` = `database.sql` alone | present | L56–57, L74 |
| S01-002: supported-set paragraph rewritten to ADR-0017 (`frozenset({1})`, v0 by shape), with a two-line edit to `test_supported_formats.py` | present | L58 |
| S01-002: consumed signatures re-cited from E002-S01-003 | present | L59 |
| S01-002: `symlink-escape` checked at extraction; outward-symlink question "ADR-0017's Concern, owned by Epic 3" | present but **stale**: the 2026-10-28 amendment resolved the Concern as skip-and-warn | L33, L46, L77 (BLOCKER 2) |
| E003 gate: `ExtractResult.skipped_links`; `ViewMember.link_target` `None` for v0 | **absent** from E004-S01-002 | no mention in E004-S01-002 (BLOCKER 2) |
| S01-003: AC note — v0.2.0 baseline, no passphrase refusal | present | L32 |
| S01-003: signatures re-verified at v0.2.0 (`48a7da9`); `read_identity` from `l3io.wp.database.encryption` | present, and verified | L60–66; database `48a7da9` `__init__.py` L25–30, `encryption.py` L34–154 |
| S01-003: `EncryptedMemberCorruptError` declares `VERIFICATION_FAILED`; `EncryptionConfigError(UsageError)` | present | L79–80 |
| S01-003: `[encryption]` extra on `>=0.2.0,<0.3` plus binder wheel source | present but **mechanism unworkable as written** | L83–85 (MAJOR, binder wheel source) |
| Decision c: cipher factory `adapters/crypto.build_member_cipher` | present | E004-S01-003 L76; E003-S02-006 L57 cites it |
| 2026-10-28: `AgeMemberCipher.required_tools()` via `extra_tools` | present | E004-S01-003 L100; E002-S02-007 L60; E003-S02-006 L71; E008-S01-001 L57. E005-S01-002 L139/L141 stale (MINOR) |
| S01-003: cipher per call (`BackupRequest`/`VerifyRequest`/`RestoreRequest.cipher`) | present | L99 |
| S01-003: Dry Run lists `ENCRYPT`, encrypts nothing; `check()` before preflight in both Run kinds | present | L95, L135 |
| S01-003: `test_passphrase_round_trip` on every install; `credential_sentinel` passphrase parameter; RSS test parametrised; single-chain test at `cli/passphrase.py` | present | L166, L175, L176, L177 |
| E003 gate: Safety Snapshot encrypted iff `request.cipher is not None` through `build_member_cipher` | present in E003; **incoherent** with an identity-only cipher | E003-S02-003 L62 vs E004-S01-003 L68–76 (MAJOR, cipher mode matrix) |
| E003 gate: `--overwrite-wp-config` reads the Archive's config member | present in E003; **not decryptable** (encrypted Archive) and **always refused** (v0) | E003-S01-001 L100, E003-S02-006 L68 (BLOCKER 1, MAJOR on the v0 config member) |
| Epic List: E004 owns only the v0 fixtures and tests, not the classifier | present in stories; `epics.md` §Epic 4 header contradicts it | E004-S01-002 L41; epics.md L131/L143 vs L513 |

## Per-principle walkthrough

- Core §1 Separation of concerns: `core/exclusions` is pure, and `adapters/crypto` is a thin adapter. BLOCKER 1 is an unassigned seam rather than leakage.
- Core §2 Reuse: PASS. There is one cipher factory and one passphrase chain, and `pathspec` and `l3io-wp-database` are reused rather than rewritten.
- Core §3 Design by contract: MAJORs on the cipher mode matrix, passphrase + identity file, and `no_key`; MINOR on `ExclusionSource` typing.
- Core §4 Testability: MAJOR on CI provisioning of `age`; MINORs on reproducibility, `write_archive`, and fixtures.
- Core §5 Brevity: PASS.
- Core §6 Comments: the `pyproject.toml` comment at L55–59 states a false premise (MAJOR, binder wheel source).
- Core §7 Dependencies: `pathspec` 1.1.1 is MPL-2.0, pure and already locked, so PASS. MINOR on the missing inventory.
- Core §8 GA: the pre-1.0 binder is recorded in the ADR-0014 amendment. MINOR asks for the inventory entry.
- Core §9 Logging: MAJOR, the exclusion events have no owner.
- Core §10 Docs: `docs/exclusions.md` is generated and checked (S01-001 L59, L118), so PASS.
- Python overlay: MAJOR, uv lock reproducibility of the binder.

## Gate

The two BLOCKERs and eight MAJORs must be resolved or ADR-justified before sign-off. The MINORs defer to the backlog.

## Recommended ADRs

- An amendment to ADR-0014 recording the binder source mechanism (`override-dependencies`, per-platform wheels, sha256 in the lock) and correcting the "uv ignores a dependency's sources" premise.
- An amendment to ADR-0014 or AD-26 recording the cipher mode matrix: identity-only and the Snapshot; a passphrase with identity files.
- A record of which module decrypts payload members on restore (BLOCKER 1), as an amendment to ADR-0014 or to AD-11.

Assumption: the brief says E004 owns "`domain/archive_format` (the v0 classifier only)". I followed the Epic List ownership note (epics.md L131/L143), which the stories also follow: Epic 2 owns the classifier, and Epic 4 owns the v0 fixtures and tests.

Sections read:
- `_bmad-output/implementation-artifacts/epic-004/sprint-01/stories/E004-S01-001.md` L1–130, `E004-S01-002.md` L1–121, `E004-S01-003.md` L1–190
- `ARCHITECTURE-SPINE.md` L44–51, L83–89, L97–112, L113–138, L139–144, L151–156, L157–163, L164–170, L177–182, L246–257, L268–277, L299–304, L342–385
- `prd.md` L99–153, L154–222 (FR-10 lines only, via grep), L338–400, L437–473, L474–484, L494–501
- `epics.md` L124–203, L509–572
- `docs/adr/0014-age-encryption-via-database.md` L1–111; `docs/adr/0017-manifest-v1-member-index.md` L84–95, L160–199 (plus grep for cap and symlink); `docs/adr/0018-redaction-at-the-boundary.md` L1–105
- `gate-reconciliation-2026-10-27.md` L116–180
- `epic-003/arch/arch-gate-review.md` L67–115
- `pyproject.toml` L1–355; `uv.lock` (`pathspec` entry, grep); `tools/check_purity.py` (grep of the header and import checks)
- E002 contracts, single lines by grep: E002-S01-001 L47, L70–71, L75, L78–79, L124, L132; E002-S01-003 L43, L58, L65, L69, L87, L101–102, L113, L121, L124, L126; E002-S01-004 L65, L117; E002-S01-010 L49, L61; E002-S02-007 L55, L60–61, L105
- Cross-epic single lines by grep: E003-S01-001 L96, L100; E003-S02-003 L46, L62, L102, L142; E003-S02-005 L64, L70; E003-S02-006 L57, L64, L68, L71, L105, L136; E005-S01-002 L74, L83, L95–103, L136–152; E008-S01-001 L52, L57
- `_bmad-output/planning-artifacts/plan-2026-10-27-v2.yaml` L1–125
- External checks: `l3io-wordpress-database` at `48a7da9`, `src/l3io/wp/database/__init__.py` and `encryption.py` (grep) and `pyproject.toml` (grep); `l3io-age-passphrase` `Cargo.toml`/`pyproject.toml` (grep), `.github/workflows/publish.yml` (grep) and the v0.1.0 release asset list; legacy `wpbackup2/classes/wp_internal_backup.py` at tag `baseline-pre-rewrite` L78–98, L150–160; uv 0.11.6 lock experiments in a deleted scratch directory; `apt-cache policy age`.
