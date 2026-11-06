# E004 pre-build re-check against the built code — l3io-arch-review (Mode B)

- **Date:** 2026-11-06
- **Scope:** `epic-004/sprint-01/stories/E004-S01-00{1,2,3}.md`, checked line by line against
  `HEAD` (`655dcf8`, after E002 and E003 closed), with `git log a42ec0d..HEAD` as the change set.
- **Inputs:** `epic-004/arch/arch-gate-review.md` (resolution sections), `docs/closeout-gate.yaml`,
  `state/issues.yaml` (BL-E004-*), ADR-0016/0017/0022/0024 and their amendments, the installed
  `l3io-wordpress-database` 0.3.2 and `pathspec` 1.1.1.
- **Method:** every interface contract, edge case, test spec and file the stories name was
  looked up in `src/` and `tests/`. Claims about behaviour were checked by running the built
  code on throwaway Archives (Python `-I`, temp directory; nothing in the repository changed):
  - a v0 Archive with `/etc/x`, `wp-root//etc/x`, `wp-root/../escape`, `wp-root/link -> ../../outside`
    and a hard link;
  - `verify_archive`, then `extract_archive` on the symlink case.
- **Final decisions honoured, not reopened:** ADR-0024; the ADR-0017 amendments; the 2026-11-06
  amendments to ADR-0016 and ADR-0022; the 2026-11-03 decisions; TestPyPI with database
  `>=0.3.2,<0.4`.

## Executive summary

E004-S01-001 (exclusions) matches the built seams almost exactly. The E002 consumers it relies
on are all present: `collect_site_root`, `ExclusionPolicy`, `ExclusionDecision`, `ExcludedEntry`,
`ManifestExclusions`, `default_exclusion_policy`, the Dry Run exclusion line, and the
`NOT_YET_IMPLEMENTED` flags. Only MINOR wording remains.

E004-S01-002 (legacy reads) agrees with the narrowed `archive_format` and the v0 mapping, except
for one malformed fixture's expected result. It would also re-build v0 tests that E002 and E003
already built, and add a grep a built AST test already does.

E004-S01-003 (encryption) is the problem. It was written before E002's `PassphraseCipher`,
`verify --authenticate` and the 2026-11-03 decisions existed:
- It names errors, exit codes and a database pin that the built code and final decisions
  replace.
- Its two close-out items cannot be built inside its stated scope.
- BL-E002-115's binding, as worded, is circular.

**Counts:** 2 BLOCKER, 9 MAJOR, 11 MINOR; 2 need the owner. **Most important action:** the
owner amends ADR-0024 to say what BL-E002-115 binds and where (F-1). The story's file grant is
then widened to the E002/E003 files its close-out items must touch (F-2).

## Findings table

Settles: **code** = the built code settles the fix; **decision** = a final decision settles it;
**owner** = needs the owner.

| # | Sev | Principle | Story:line | Code:line | Finding | Settles |
|---|-----|-----------|------------|-----------|---------|---------|
| F-1 | BLOCKER | §3 Design by Contract | S01-003:153 | `adapters/tar.py:437-445, 494-509`; `domain/archive_format.py:14-16, 691-706`; `adapters/tar.py:999-1003`; `cli/verify.py:95-119`; `adapters/restore_runner.py:572-592` | BL-E002-115 says to put "the Manifest's sha256 and the Site identity as the first member inside the encrypted payload". That cannot be built. See [F-1](#f-1). | **owner** |
| F-2 | BLOCKER | §2 Reuse; §3 | S01-003:63, 67, 73-74, 152, 177 | `adapters/crypto.py:35-88`; `cli/restore.py:71, 1670`; `cli/verify.py:41, 113`; `tests/contracts/test_credential_hygiene.py:521`; `tests/conftest.py:289-334`; `tests/unit/cli/test_verify_authenticate.py` | BL-E004-042 cannot be closed within the story's scope, and its own AST test would fail. See [F-2](#f-2). | **code** |
| F-3 | MAJOR | §3; contradicts a final decision | S01-003:40, 84-87, 122, 125, 144, 172-173 | `domain/errors.py:466-494`; `adapters/crypto.py:85-88`; `cli/verify.py:24-26`; E003-S01-001 L72/L121 | The story defines `ArchiveDecryptionError(CredentialsError)` (exit 4) and `EncryptedMemberCorruptError` (exit 8) in `adapters/crypto.py`, and says the two "map to different exit codes". See [F-3](#f-3). | **decision** (Shawn 2026-11-03) + code |
| F-4 | MAJOR | §3; duplicates built code | S01-003:76, 124, 177, 187 | `cli/passphrase.py:106-125`; `domain/errors.py:122-135, 251-256`; E002-S01-006 L76; BL-E002-069 (open) | For "encryption requested, no passphrase", the story has `build_payload_cipher` raise `PassphraseRequiredError(operation="backup")` → `REFUSED` (3). See [F-4](#f-4). | **owner** |
| F-5 | MAJOR | §7 Dependencies; contradicts a final decision | S01-003:63-66, 91-93, 201 | `pyproject.toml:81-84, 109-113`; database 0.3.2 `METADATA` | See [F-5](#f-5). | **decision** |
| F-6 | MAJOR | §3; contradicts a final decision | S01-003:53, 109 | `cli/verify.py:18-28, 95-119, 151-160` | The Non-goal says verification "stays keyless" and rules out a keyed check outside a restore. Seam 7 says "A verify takes none, because verification is keyless". See [F-6](#f-6). | **decision** (2026-11-03) |
| F-7 | MAJOR | §4 Testability (guard scope) | S01-003:191 | `cli/parser.py:265, 480`; `cli/passphrase.py:43` | `test_single_passphrase_chain` requires `L3IO_WP_BACKUP_PASSPHRASE` in exactly one module. `cli/parser.py` spells it twice, in help text, so the test fails on arrival. See [F-7](#f-7). | **code** |
| F-8 | MAJOR | §4 Testability (guard scope) | S01-002:56, 59, 106; S01-003:99 | `tests/contracts/test_supported_formats.py:33-34, 56-67` | `_archives()` treats every file whose suffix is not `.py` as an Archive. See [F-8](#f-8). | **code** |
| F-9 | MAJOR | §3; the expected result contradicts the code | S01-002:58, 77 | `domain/archive_format.py:709-715, 769-774`; `core/verify.py:190-194` | `escape-absolute.tar.gz` holds the member `/etc/x` and is expected to report `UNSAFE_MEMBER_PATH`, then be refused by `extract_archive`. See [F-9](#f-9). | **code** |
| F-10 | MAJOR | §2 Reuse | S01-002:108 | `tests/contracts/test_archive_format_owner.py:1-26` | `test_no_v0_writer` (a) adds a grep "no E002 story defines; this is the only one". See [F-10](#f-10). | **code** |
| F-11 | MAJOR | §2 Reuse | S01-002:41-42, 56, 75-81, 109 | `tests/adapters/test_tar_probe.py:104, 157, 402-433, 570`; `tests/integration/test_verify_cli.py:44-54`; `tests/restore_harness.py:186-200`; `tests/integration/test_restore_cli.py:247-270` | E002 and E003 already built in-test v0 Archive builders and most of the story's v0 cases. See [F-11](#f-11). | **code** |
| F-12 | MINOR | §10 Docs / process | all three stories | — | No story has a `## Files in scope` section, which E002 and E003 stories carry from sprint preparation. A derived list is [below](#derived-files-in-scope); it includes the files F-1, F-2, F-7 and F-8 add. | **code** |
| F-13 | MINOR | §7 | S01-001:59 | `pyproject.toml:36-45` | "The dependency inventory records its licence": no inventory file exists. The built convention is a comment beside the dependency in `pyproject.toml` (see `platformdirs`). Say that. BL-E004-024 remains for the rest. | **code** |
| F-14 | MINOR | §4 | S01-001:121 | `cli/backup.py:183-208, 443-447` | The end-to-end test asserts `exclusions.excluded` lists "exactly the pruned paths". `_SkipStaged` adds one more entry when the target is inside the Site Root (pattern `"the Archive being written"`). The test must put the target outside the Site Root or expect that entry. | **code** |
| F-15 | MINOR | wording | S01-002:78 | `adapters/tar.py:974-977, 1024`; `domain/archive_format.py:656-661` | The story expects "`wp-root/link` is in `ExtractResult.skipped_links`". `SkippedLink.path` is role-relative, so it is `link`. This was confirmed by running it: `SkippedLink(path='link', link_target='../../outside', reason='outside_destination')`. | **code** |
| F-16 | MINOR | §3 | S01-002:33, 72 | `core/verify.py:54, 142`; ADR-0017 amendment 2026-11-03 L233-234 | A v0 hard link fails Verification (`UNSUPPORTED_MEMBER_TYPE`, confirmed). The 0.2.21 writer's `stream.add` emits `LNKTYPE` for any multiply-linked file (`baseline-pre-rewrite:wpbackup2/classes/wp_internal_backup.py` L97). So some real operator Archives will not verify or restore, and the Outcome's "existing backups still restore" does not say so. Add `malformed/hardlink.tar.gz` and a line in the operator docs (BL-E004-022). | **decision** |
| F-17 | MINOR | §4 | S01-002:97 | `.github/workflows/ci.yml:243-244` | The `escape-*` fixtures are to run "inside the existing AD-11 extraction containment step". That step runs only `tests/contracts/test_extraction.py`, and the story puts the cases in `tests/adapters/test_v0_fixtures.py`. Put them in `test_extraction.py`, or widen the step. | **code** |
| F-18 | MINOR | §3 / AD-14 | S01-003:88, 128, 179 | `domain/errors.py:230-240`; `adapters/crypto.py:46-56`; database 0.3.2 `_passphrase.resolve_backend` | See [F-18](#f-18). | **code** |
| F-19 | MINOR | §3 | S01-003:111 | `ports/crypto.py:104-117`; `cli/restore.py:1340` | `required_tools()` is not on the `PayloadCipher` port, so a root holding `PayloadCipher \| None` cannot call it. State that the root reads it from the factory's concrete return type, or add it to the port (an E002 file, which needs a grant). | **code** |
| F-20 | MINOR | §9 Logging | S01-003:143 | `adapters/tar.py:448-455, 565-569` | The `WRITE_ARCHIVE` record "gains `encrypted` and `bytes`". It already carries the stored payload size, and `ENCRYPT` has its own record with `bytes`. Adding `encrypted` edits `adapters/tar.py` (E002's, outside decision 6). Grant the edit or drop the line. | **code** |
| F-21 | MINOR | §3 | S01-003:153 | `cli/verify.py:84-92` | "Until this lands, `verify --authenticate` states rollback and swaps are not detected." The built output states nothing of the sort. Even with the binding, a whole older authentic Archive (rollback) stays undetectable, so the rollback statement is permanent, not interim. | **code** |
| F-22 | MINOR | §2 | S01-003:48, 132, 168, 187 | `cli/restore.py:942-945, 1420-1431, 1645-1670`; `adapters/restore_runner.py:411-414, 585-592` | E003 built every restore-side seam this story's Non-goals hand to it. See [F-22](#f-22). | **code** |

### F-1

BL-E002-115 asks for "the Manifest's sha256 and the Site identity as the first member inside
the encrypted payload". That cannot be built, for these reasons:
- **The Manifest's hash comes too late.** The Manifest carries `payload.size` and
  `payload.sha256` over the encrypted bytes, and `write_archive` sets them only after encrypting.
  So no hash of the Manifest exists while the payload is being written.
- **The member index comes too late for a first member.** `_write_payload` builds the member
  index as it writes, so a first member cannot carry a digest of it either.
- **An unindexed member breaks extraction.** `extract_archive` refuses any inner member not in
  the index ("not among the members the archive lists"). An unindexed binding member would
  therefore fail every encrypted restore.
- **Indexing it changes the format contract.** It needs a new role or a reserved name in
  `archive_format`, the AD-6 contract E003 just narrowed.
- **"First" conflicts with ADR-0024.** ADR-0024/AD-6 put the Dump first.
- **The comparison sites are out of scope.** They are `cli/verify.py` and
  `restore_runner._verify`, which decision 6 says the story does not edit.

The ADR-0024 amendment must choose three things:
- what is bound, for example a digest of the canonical Manifest with `payload` omitted, plus
  `source`;
- where it goes, for example the last inner member, indexed under a reserved name;
- where the check lives, for example one function beside `decrypt_payload` that both callers
  use.

### F-2

BL-E004-042 cannot be closed within the story's scope. The story's own AST test would fail.
- **The file already exists.** `adapters/crypto.py` exists as `PassphraseCipher`, yet L63 still
  says "(new)" and L67 gives a keyword-only `__init__`.
- **The roots construct the cipher directly.** Both built roots do:
  `PassphraseCipher(resolved.value)` in `cli/restore.py` and `cli/verify.py`.
- **The rename reaches out of scope.** Renaming the class to `AgePayloadCipher` breaks those
  imports and the hygiene test.
- **Keeping direct construction fails the AST test.** The story requires that "neither root
  constructs directly", and the L177 AST scan of `cli/` enforces it.
- **Decision 6 blocks the fix.** It forbids editing E002/E003 modules and gives the roots to
  E008.
- **The factory has no verify case.** It can raise only `PassphraseRequiredError("backup")`,
  while verify raises `("verify")` itself.

Fix:
- Take the BL's own option: keep `PassphraseCipher` and have `build_payload_cipher` return it.
- Route `cli/restore.py` and `cli/verify.py` through the factory, adding a verify `operation`.
- Grant edits to those two files, `test_credential_hygiene.py`, `test_verify_authenticate.py`
  and `conftest.py` (FakeAge removal).

### F-3

The story's error classes do not match what was built:
- **Story:** `ArchiveDecryptionError(CredentialsError)` (exit 4) and
  `EncryptedMemberCorruptError` (exit 8), defined in `adapters/crypto.py`, mapping to "different
  exit codes".
- **Built:** `PassphraseRejectedError` and `PayloadDamagedError`, both `VERIFICATION_FAILED`
  (8) by Shawn's decision of 2026-11-03.
  - They live in `domain/errors.py`, so Epic 3 can name them.
  - `cli/verify.py` and E003's runner and tests already depend on them.

Use the built classes. In the Outcome, replace "different exit codes" with "distinct errors,
both exit 8".

### F-4

There are two refusals for the same condition, "encryption requested, no passphrase":
- **Story:** `build_payload_cipher` raises `PassphraseRequiredError(operation="backup")` →
  `REFUSED` (3).
- **Built:** E002-S01-006's `require_passphrase` raises `PassphraseUnavailableError`
  (`UsageError`, 2) for exactly this case. Its docstring says "a backup root calls this before it
  reads anything from the Site".

The built code therefore has two refusals with different exit codes, and BL-E002-069 (exit 2
vs 4 for a missing passphrase) is still open. The owner must pick one error and one exit code.
The factory then either raises it, or is never handed `None` with `required=True`.

### F-5

The story's database pin is stale:
- **Story:** the `database` extra is `>=0.2.0,<0.3` (rev `48a7da9`), and it is changed to
  `l3io-wordpress-database[encryption]>=0.2.0,<0.3`.
- **Built:** `l3io-wordpress-database>=0.3.2,<0.4` from TestPyPI.

Taken literally, the story would drop below the 0.3.2 floor that the RT-S02-003 fix (`bc38d95`)
requires.

Write it as `l3io-wordpress-database[encryption]>=0.3.2,<0.4`. The binder override must be
`l3io-age-passphrase>=0.1.1,<0.2`, because database 0.3.2's metadata needs `>=0.1.1`.

Re-cite "v0.2.0 (rev 48a7da9)" as 0.3.2 throughout (L63-66, L93, L201). The `encrypt`,
`decrypt` and `check_passphrase` signatures are unchanged in 0.3.2 (checked).

### F-6

Both lines contradict the decision of 2026-11-03: keyless verify reports consistency, and
`verify --authenticate` authenticates. They also contradict this story's own L153.

Rewrite both lines:
- Verification without a key is the default.
- `verify --authenticate` decrypts with the passphrase, and it is a factory consumer (F-2).

### F-7

Fix it one of two ways:
- the help text formats `PASSPHRASE_ENV` (a `parser.py` edit grant, then regenerate the AG-25
  docs); or
- the test counts only uses outside string constants.

Name the choice in the story.

### F-8

`_archives()` treats every file whose suffix is not `.py` as an Archive. Two sidecar files are
therefore read as Archives:
- **v0:** E004-S01-002's `released-0.2.21-all.provenance.json` is classified as an Archive and
  fails "classifies as v0".
- **v1:** E004-S01-003's `encrypted-passphrase.passphrase.txt` is opened as a tar and fails
  `test_supported_versions_equal_the_fixtures`.

The stories are incomplete about this file:
- S01-002 names only two edits to the file. The `malformed/` exclusion is already implicit
  through `is_file()`.
- S01-003 does not list the file at all.

Use one shared Archive-selection helper, by content or by an explicit sidecar suffix rule.
Use it in this file and in `test_v0_fixtures.py`.

### F-9

The test as written cannot pass:
- **Verification.** `_v0_role("/etc/x")` is `None`, so `classify` raises `UnrecognisedArchiveError`.
  `verify_probe` then reports `UNRECOGNISED_FORMAT` naming the first member (`database.sql`).
  Confirmed by running it.
- **Extraction.** With no view, `extract_archive` cannot be driven at all.

Fix it one of two ways:
- expect `UNRECOGNISED_FORMAT` for `/etc/x` and drop its extraction half; or
- spell the member inside the v0 prefix (`wp-root//etc/x` → `UNSAFE_MEMBER_PATH`, confirmed)
  and keep both halves.

### F-10

`test_archive_format_owner.py` already AST-scans all of `src/` for member-name literals, with a
planted-tree scope test. Add the v0 spellings to its tables: `V0_DUMP_MEMBER` exactly, and the
`V0_SITE_ROOT` prefix. That also settles BL-E004-013.

Part (b) still drives `write_archive` with a scope it does not take (BL-E004-023). Drive
`TarArchiveWriter.write` or `drive_backup` per `BackupScope`.

### F-11

Already built:
- **Crafted cases:** v0 hard link, FIFO, `..` and a stray manifest; a dump-only Archive
  (unrecognised); a v0 STRUCTURAL_ONLY pass and v0 extraction.
- **CLI:** the v0 `STRUCTURAL_ONLY` check.
- **Restore:** files-only v0 with `--mode db`, and `--overwrite-wp-config` from a v0 config.

The story's malformed set, its CLI test and the new generator repeat these. The generator would
be a fourth v0 builder.

Keep what is new:
- the committed fixtures, the released one above all;
- the reproducibility guard.

Then remove the repetition:
- parametrise the existing tests over the committed fixtures, rather than adding parallel cases;
- have `restore_harness.write_v0` and `test_tar_probe._v0` call the generator's builder.

L42 (Epic 3's phase-order test "carries a v0 fixture from this story") needs an edit grant on an
E003 test, or should be dropped. E003 is closed.

### F-18

Two message and mapping gaps:
- **The install hint.** `MissingExtraError` has a fixed message, "pip install
  'l3io-wordpress-backup[database]'". Once the `database` extra takes `[encryption]`, that is
  the right advice, so drop "message naming `l3io-wordpress-database[encryption]`".
- **The `cli` backend.** Database 0.3.2 raises `InvalidArgumentsError`, not
  `MissingDependencyError`, under `L3IO_AGE_PASSPHRASE_BACKEND=cli` when `age` is absent.
  - Built `check()` does not map it, so a raw database exception crosses the boundary today.
  - The story's `EncryptionConfigError` mapping fixes that.
  - L179's "no usable backend → `MissingExtraError`" then holds only for `auto` and `binder`.
    Say so.

### F-22

E003 built every restore-side seam this story's Non-goals hand to it:
- the plan-time refusal;
- the decrypt in `VERIFY`;
- `check()` in `PREFLIGHT`;
- the Safety Snapshot encryption and its stderr warning;
- the restore root's passphrase resolution.

Nothing in the story re-builds them. But L168's reason for avoiding `drive_restore` ("E003 runs
beside E004 … not ordered before it") is stale: `drive_restore` exists. The restore cases with
the real cipher can now run through it, rather than through a new harness.

## Built by E003 (or E002) that an E004 story would build again

- **The cipher.** `adapters/crypto.py::PassphraseCipher` is the full `PayloadCipher` the story
  calls `AgePayloadCipher` (F-2).
- **The cipher's errors.** `PassphraseRejectedError` and `PayloadDamagedError` in
  `domain/errors.py` (F-3).
- **The backup-side refusal.** `cli/passphrase.py::require_passphrase` (F-4).
- **The v0 literal guard.** `tests/contracts/test_archive_format_owner.py` (F-10).
- **v0 tests and builders.** Crafted-v0 verification and extraction tests, the CLI v0 test, and
  the v0 restore tests and builders (F-11).
- **The restore-side seams** (decrypt in VERIFY, plan-time refusal, snapshot encryption). The
  story correctly does not build them; only its test rationale is stale (F-22).

## Assumed by an E004 story but absent

- A dependency inventory file (F-13).
- A WRITE_ARCHIVE `encrypted` field (F-20).
- `required_tools()` on the `PayloadCipher` port (F-19).
- An interim rollback statement in the verify output (F-21).
- A role or reserved name, and an extraction allowance, for a binding member (F-1).
- A `cli/` free of direct cipher construction (F-2).
- A single module spelling `L3IO_WP_BACKUP_PASSPHRASE` (F-7).
- `extract_archive` reachable for an unclassifiable v0 Archive (F-9).

## Checked and consistent (no finding)

- **E004-S01-001:**
  - **Consumed seams.** All exist with the stated shapes:
    - `ExclusionPolicy` (`source`, `patterns`, `decide`);
    - `ExclusionDecision`, `ExcludedEntry`, `INCLUDE_EVERYTHING`;
    - `ManifestExclusions` with the `default`/`custom`/`none` sources, written for every scope,
      so a `--mode db` Manifest records the set with an empty `excluded` list;
    - `collect_site_root(site_root, policy)`, which decides once per entry before descending,
      records the top-most pruned path, and keeps `wp-config.php` with DEBUG
      `wp_config_exclusion_ignored`;
    - `BackupRequest.exclusion` → `default_exclusion_policy()` → `INCLUDE_EVERYTHING`;
    - the Safety Snapshot passing `INCLUDE_EVERYTHING` unwrapped;
    - `build_backup_plan(..., exclusion_source, exclusion_patterns)` and the Dry Run collect
      line.
  - **Flags.** The three flags are in `NOT_YET_IMPLEMENTED`, bound by E008.
  - **Dependency and guards.** `pathspec` 1.1.1 is in `uv.lock` with `GitIgnoreSpec.from_lines`
    and the named tree helpers. import-linter's `forbidden` contract (external packages as
    nodes) and `tools/check_purity.py` allow `core/` to import it.
  - **Errors.** Error classes outside `domain/errors.py` are an established pattern
    (`core/restore.py`, `adapters/restore_runner.py`).
- **E004-S01-002:**
  - **Classification.** `classify` recognises exactly the three shapes.
  - **v0 view.** v0 roles and kinds are synthesized; `link_target=None`; `config_path` is
    `"wp-root/wp-config.php"` exactly when present.
  - **E003's narrowing.** `member_relative` maps v0 `database.sql` to the `dump` role with
    relative `database.sql` (the v1 name), and `wp-root/…` to `site-root`. So E003's narrowing
    (`dump/database.sql` and `config/wp-config.php` only) is consistent with every v0 fixture.
  - **Escapes.** `wp-root/../escape` gives `UNSAFE_MEMBER_PATH` in verification and "escapes its
    role prefix" in extraction.
  - **Outward symlinks.** `symlink-escape` passes `STRUCTURAL_ONLY`; the link is skipped with one
    `symlink_skipped` WARNING.
  - **Other malformed cases.** `stray-manifest-late` and `missing-wp-root` give
    `UNRECOGNISED_FORMAT`, and `truncated` gives `TRUNCATED`.
  - **The VERIFY record.** It carries `format_version="v0"`, `integrity`, `passed` and repr'd
    member names.
  - **CLI.** The output names `STRUCTURAL_ONLY` and why; a failure exits 8.
  - **Test infrastructure.** The `baseline-pre-rewrite` tag, the legacy writer lines,
    `RSS_BOUND_BYTES` and `credential_sentinel` all exist.
- **E004-S01-003:**
  - **Payload layout.** `write_archive(..., cipher=)` names `payload.tar.gz.age`, sets
    `encrypted`, and hashes the stored bytes.
  - **Plan and preflight.** `build_backup_plan(..., encrypted=)` puts `ENCRYPT` in the plan.
    `execute_backup_plan` calls `cipher.check()` before any step. `RestoreRequest.extra_tools`
    and `ToolRequirement` exist.
  - **Decryption.** `decrypt_payload` and `extract_archive(..., payload=)` exist.
  - **Passphrase.** `PassphraseRequiredError` takes `"backup" | "restore" | "verify"`.
    `--encrypt-with-passphrase` is registered and not yet implemented.
  - **CI.** The verify-install extras check uses anonymous TestPyPI.
  - **Close-out tracking.** `test_closeout_gate.py` needs BL-E002-115 and BL-E004-042 named in
    a story that is not done, and this one names both.

## Per-principle walkthrough

- §1 Separation of concerns: PASS. The `core/exclusions` → `adapters/exclusions` split and the
  cipher behind the port are right. F-1 is where the check must live.
- §2 Reuse: F-2, F-10, F-11, F-22.
- §3 Design by contract: F-1, F-3, F-4, F-6, F-9, F-16, F-18, F-19, F-21.
- §4 Testability: F-7, F-8, F-14, F-17.
- §5 Brevity: PASS.
- §6 Comments as current state: F-21. Also the stale "v0.2.0 (rev 48a7da9)" text under F-5.
- §7 Dependencies: F-5, F-13. `pathspec` (MPL-2.0, maintained) passes.
- §8 GA over beta: PASS. TestPyPI is an owner decision, recorded as a stepping stone.
- §9 Logging: F-20. S01-001's events now have an owner (`LoggedExclusionPolicy`); BL-E004-039
  stays open.
- §10 Documentation: F-12, F-16 (an operator-docs line).
- Python overlay (uv, extras, `py.typed`, mypy-strict typing): F-5. BL-E004-016 (the
  `ExclusionSource` versus `str` typing) stays open.

## Gate

- **BLOCKER/MAJOR:** fix in the stories before build. F-1 and F-4 need the owner; the rest are
  settled by the built code or a final decision.
- **MINOR:** defer to the backlog, or fold into the same story edit.
- **Still-open MINORs that bear on this re-check:**
  - BL-E004-013, -022, -023 and -026 are absorbed by F-10, F-16, F-10 and S01-002 L60 respectively.
  - BL-E004-025, -027, -034 and -039 are unchanged.

## Recommended ADRs

- **ADR-0024 amendment (owner):** the inner binding member for BL-E002-115 — content, position,
  role or name, and where it is checked (F-1).
- **Exit-code record (owner):** "encryption requested, no passphrase" — `USAGE` versus `REFUSED`
  versus `CREDENTIALS` (F-4, BL-E002-069).

## Derived files in scope

- **E004-S01-001**
  - New:
    - `src/l3io/wp/backup/core/exclusions.py`
    - `src/l3io/wp/backup/adapters/exclusions.py`
    - `docs/exclusions.md`
    - `tests/unit/core/test_exclusions.py`
    - `tests/unit/adapters/test_exclusions_logging.py`
    - `tests/contracts/test_wp_config_never_excluded.py`
    - `tests/contracts/test_exclusions_documented.py`
    - `tests/contracts/test_exclusions_pure.py`
    - `tests/integration/test_backup_exclusions.py`
  - Edited: `pyproject.toml`, `uv.lock` (pathspec into `dependencies`).
- **E004-S01-002**
  - New:
    - `tests/fixtures/archives/v0/` (the fixtures, `make_v0_fixtures.py`, the provenance file
      and `malformed/`)
    - `tests/adapters/test_v0_fixtures.py`
    - `tests/contracts/test_v0_fixtures_reproducible.py`
    - `tests/integration/test_verify_cli_v0.py`
  - Edited:
    - `tests/contracts/test_supported_formats.py` (F-8)
    - `tests/contracts/test_archive_format_owner.py` (F-10, in place of a new `test_no_v0_writer.py`)
    - `tests/contracts/test_extraction.py` (F-17)
  - Optionally edited: `tests/restore_harness.py`, `tests/adapters/test_tar_probe.py` (F-11).
- **E004-S01-003**
  - Edited:
    - `src/l3io/wp/backup/adapters/crypto.py` (factory, `passphrase_backend_requirements`,
      `required_tools`, the `InvalidArgumentsError` mapping)
    - `src/l3io/wp/backup/domain/errors.py` (only if F-4 or F-18 add or change a class)
    - `src/l3io/wp/backup/cli/restore.py` and `src/l3io/wp/backup/cli/verify.py` (F-2, F-1)
    - `src/l3io/wp/backup/cli/parser.py` (F-7)
    - `src/l3io/wp/backup/adapters/tar.py`, `src/l3io/wp/backup/adapters/restore_runner.py` and
      `src/l3io/wp/backup/domain/archive_format.py` (F-1, once the ADR decides)
    - `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml`
    - `docs/adr/0024-one-encrypted-payload-passphrase-only.md` (amendment)
    - `tests/conftest.py` (FakeAge), `tests/contracts/test_credential_hygiene.py`,
      `tests/unit/cli/test_verify_authenticate.py`
    - `tests/contracts/test_supported_formats.py` (F-8)
  - New:
    - `tests/fixtures/archives/v1/encrypted-passphrase.*` plus its generator
    - `tests/adapters/test_crypto.py`
    - `tests/adapters/test_age_interop.py`
    - `tests/integration/test_encrypted_backup.py`
    - `tests/unit/domain/test_manifest_no_key_material.py`
    - `tests/contracts/test_single_passphrase_chain.py`
    - `tests/contracts/test_no_direct_crypto.py`
    - the committed-key guard

## Summary

E004-S01-001 can be built as written. Its E002 seams exist exactly as stated, and only test
wording needs care. E004-S01-002 agrees with E003's narrowed `archive_format` and the v0 mapping
in `member_relative`/`_classify_v0`. But one fixture's expected result is wrong:
`/etc/x` gives `UNRECOGNISED_FORMAT`, not `UNSAFE_MEMBER_PATH`. Its fixture glob also breaks the
built supported-formats test, and it would re-add a literal guard and v0 cases E002 and E003
already built. E004-S01-003 needs the most rework:
- it names a second cipher and two error classes the built code replaced, with exit codes the
  owner overruled on 2026-11-03;
- it pins database below the final `>=0.3.2,<0.4`;
- its non-goal contradicts the keyed `verify --authenticate` decision;
- its single-chain test fails on `cli/parser.py`'s help text;
- its two close-out items cannot be built inside the scope it grants itself. BL-E004-042 needs
  edits to `cli/restore.py`, `cli/verify.py` and three test files. BL-E002-115's "Manifest
  sha256 as the first payload member" is circular, because the Manifest holds the payload's
  hash, and an unindexed inner member would make every encrypted restore refuse.

Two items need the owner: the ADR-0024 amendment defining the binding, and the single exit code
for "encryption requested, no passphrase", where the built `require_passphrase` (USAGE) and the
story's factory (REFUSED) disagree. Everything else is settled by the built code or a final
decision.

DONE — BLOCKER 2, MAJOR 9, MINOR 11, owner decisions 2

## Re-check — 2026-11-06

- **Scope:** the uncommitted `git diff` of `epic-004/sprint-01/stories/E004-S01-00{1,2,3}.md`,
  `E006-S01-001.md`, `E007-S01-004.md`, `E008-S01-001.md`, `state/planned/epic-004/sprint-01/E004-S01-003.yaml`
  and `docs/adr/0024-one-encrypted-payload-passphrase-only.md` (the 2026-11-06 amendment, option A).
- **Checked against:** the built code at `HEAD` (`655dcf8`); `docs/adr/`; the installed
  `l3io-wordpress-database` 0.3.2. Read-only: nothing but this section was written.

### BLOCKER and MAJOR findings

| # | Status | Evidence |
|---|---|---|
| F-1 | **Partly resolved** | The ADR-0024 amendment chooses what is bound (the Manifest less `payload.size`/`payload.sha256`), where (last inner member, reserved name in `domain/archive_format.py`, unindexed, skipped by extraction) and which callers check it. It is buildable: `write_archive` (`adapters/tar.py:494-510`) already knows `encrypted` and the payload name before encrypting, and `_write_payload` (`:437-445`) can append one member after `_add` finishes the index; `_ExtractFilter.__call__` (`:1000-1004`) is where the reserved name is skipped instead of refused. Not resolved: the story's grant for these files contradicts itself (N-1), the check's input and its single home are unspecified (N-3), and the amendment's "`verify --help` says so" has no owner (N-4). |
| F-2 | Resolved | E004-S01-003 L107-122, L226-229: `PassphraseCipher` (`adapters/crypto.py:35-88`) kept; `build_payload_cipher(*, passphrase, secrets)` returns it; `cli/restore.py:1670` and `cli/verify.py:113-114` granted and routed; `conftest.py` `FakeAge`, `test_credential_hygiene.py`, `test_verify_authenticate.py` granted. `SecretRegistry.register` is set-based (`adapters/redaction.py:96-104`), so the factory's re-registration is idempotent as stated. |
| F-3 | Resolved | L38, L141-144, L189-191, L259: `PassphraseRejectedError`/`PayloadDamagedError` (`domain/errors.py:466, 481`), both exit 8; no new classes. |
| F-4 | Resolved | L40, L112-116, L151, L273: the factory raises no refusal; the backup refusal is the built `require_passphrase` → `PassphraseUnavailableError(UsageError)` (`cli/passphrase.py:106`; `domain/errors.py:122`). One refusal, one exit code. BL-E002-069 (the owner's exit-code record) stays open, as the story says; it no longer blocks the build. |
| F-5 | Resolved | L37, L110, L158-163: `l3io-wordpress-database[encryption]>=0.3.2,<0.4`; override `l3io-age-passphrase>=0.1.1,<0.2`. Matches installed 0.3.2 metadata (`l3io-age-passphrase<0.2,>=0.1.1; extra == 'encryption'`) and `pyproject.toml`'s `[tool.uv]` (only `required-version` today). |
| F-6 | Resolved | L41, L85, L176: keyless verify reports consistency; `verify --authenticate` authenticates through the factory. Matches `cli/verify.py:10-28`. |
| F-7 | Resolved | L283: counts only an `ast.Constant` equal to the name. The only such constant in `src/` is `cli/passphrase.py:43`; `cli/parser.py:265, 480` are prose. Planted cases both ways. |
| F-8 | Resolved | E004-S01-002 L36, L80 add `tests/archive_fixtures.py::archive_files` with `SIDECAR_SUFFIXES`; E004-S01-003 L66, L166 consume it; `depends_on: E004-S01-002` added to the state file. |
| F-9 | Resolved | E004-S01-002 L79, L99: `wp-root//etc/x`, consistent with `_v0_role` (`domain/archive_format.py:709-715`) and `member_relative`. |
| F-10 | Resolved | E004-S01-002 L42, L133-135: extends `tests/contracts/test_archive_format_owner.py` (whole-constant `V0_DUMP_MEMBER`, exact and prefix `V0_SITE_ROOT`, defined at `domain/archive_format.py:122-123`; no other `src/` module spells either); part (b) drives `TarArchiveWriter.write` (`adapters/tar.py:535-570`). |
| F-11 | Resolved | E004-S01-002 L39-46, L63, L129, L136-137: parametrises `test_extract_reads_a_v0_archive_itself` (`tests/adapters/test_tar_probe.py:570`) and `test_a_v0_archive_passes_structural_only` (`tests/integration/test_verify_cli.py:44`); no fourth builder; the E003 phase-order edit dropped. |

**E004: 10 of 11 resolved, 1 partly resolved (F-1), 0 not resolved.**

### New findings

| # | Sev | Where | Finding |
|---|---|---|---|
| N-1 | MAJOR | E004-S01-003 L46, L48-66, L168, L218, L231; ADR-0024 amendment last line | **The F-1 file grant contradicts itself.** L46 and L231 grant `domain/archive_format.py`, `adapters/tar.py`, `adapters/restore_runner.py` and their tests. But the file list (L48-64) omits all three. L66 lists `adapters/tar.py` as "Consumed, not edited". L168 heads the seams "which this story does not edit (decision 6)". L218 drops a log field because `adapters/tar.py` "is not granted". The ADR amendment's own grant names only `cli/verify.py` and `adapters/restore_runner.py`. A builder reading the list or L66/L218 cannot write the inner member. Fix: put the three source files and their tests in the list, delete `adapters/tar.py` from L66, scope L168/L218 to "except the BL-E002-115 edits", and make the ADR grant name all four files. |
| N-2 | MAJOR | E004-S01-003 L46 (test grant); `tests/adapters/test_tar_writer.py:289-315`; `tests/adapters/conftest.py:83-98` | **A built E002 test fails once the writer appends the inner Manifest, and its file is not granted.** `test_the_encrypted_payload_reverses_to_the_plain_members` asserts `[i.name for i in read.inner] == [i.name for i in plain.inner]` and `read.contents == plain.contents` (L310-311). The encrypted payload gains the reserved member and the plain one does not, so both assertions fail. The grant names `tests/unit/adapters/test_tar.py`, which tests extraction sizes, not the writer. Grant `tests/adapters/test_tar_writer.py` (and `tests/adapters/conftest.py` if `ReadArchive` should filter the reserved name). |
| N-3 | MAJOR | ADR-0024 amendment "The check"; E004-S01-003 L231; `domain/archive_format.py:746-765`; `adapters/tar.py:854-884`; `cli/verify.py:95-119`; `adapters/restore_runner.py:572-588` | **The check's input does not reach either check site, and the check has no single home.** The amendment compares the inner Manifest field by field with the outer one, including `source`. But `_classify_v1` parses the whole Manifest and keeps only `ArchiveView` fields (no `source`, `created_at`, `origin`, `exclusions`). `verify_archive_view` returns `(view, result)` and drops `scan.manifest_text`. `_authenticate` and `_verify` hold only the view. Also, `manifest_to_json` (`:273`) and `_payload` (`:433`) require `size` and `sha256`, so the inner document needs its own serialiser and parser. All of this fits inside the granted files, but neither the ADR nor the story names it. F-1 asked for "one function beside `decrypt_payload` that both callers use"; without one, two security comparisons get written separately in `cli/verify.py` and `restore_runner.py`. Name one function, for example in `adapters/tar.py`, that takes the decrypted payload path and the outer Manifest. Name how the outer Manifest is returned, for example `verify_archive_view` re-parsing `scan.manifest_text`. Name the inner-Manifest (de)serialiser in `domain/archive_format.py`. |
| N-4 | MAJOR | ADR-0024 amendment ("`docs/restore.md` and `verify --help` say so"); E004-S01-003 L66, L232 | **The story contradicts the amendment on where the rollback limit is stated.** The ADR requires `verify --help` and `docs/restore.md` to state it. The story states it only in `_describe`'s pass line, lists `cli/parser.py` as "Consumed, not edited", and lists neither `docs/restore.md` nor `README.md`. Changing `--help` means a `cli/parser.py` edit and regenerating README's cli-usage block, which `tools/gen_cli_docs.py --check` enforces (`ci.yml:122`). `README.md` is also edited in parallel by both E005 stories (BL-E004-006). Grant `cli/parser.py`, `docs/restore.md` and `README.md` (naming BL-E004-006), or amend the ADR to the `_describe` line plus `docs/restore.md`. |
| N-5 | MAJOR | E004-S01-003 L49, L178; E008-S01-001 ("Restore and verify are already done", `RootPreflight` "(Decided 2026-10-28)", `test_root_preflight.py`); `cli/restore.py:1652-1679` | **Nobody passes the restore root's `extra_tools=cipher.required_tools()`.** The built `run_restore` passes no `extra_tools` (`RestoreRequest.extra_tools` defaults to `()`, `cli/restore.py:787`). E004-S01-003 L178 says "a root reads it", but L49 grants only the factory call in `cli/restore.py`. E008-S01-001 says "this story adds no cipher code to `cli/restore.py`", yet in the same file it still says every root, Epic 3's `cli/restore.py` included, passes `extra_tools=cipher.required_tools()`, and its `test_root_preflight.py` relies on that. As written, an encrypted restore under `L3IO_AGE_PASSPHRASE_BACKEND=cli` with `age` below 1.3 passes preflight and fails later at decrypt. Give the line to E004-S01-003 (same edit as the factory call) and have E008 cite it. |
| N-6 | MINOR | E004-S01-003 L266 | The "Inspector" test still calls `build_payload_cipher(..., required=True, ...)`. The L258 and L272 supersession notes do not cover this bullet, and the parameter no longer exists (L111). |
| N-7 | MINOR | E004-S01-003 L111, L118; `cli/restore.py:1645-1681`; `adapters/restore_runner.py:411-414` | Routing the restore root through the factory moves `check()` before `drive_restore`. It then runs before the Run opens and for every restore with a resolved passphrase, unencrypted Archives included (for snapshot encryption). A backend failure is therefore no longer recorded in the Run or journal, and it precedes E003's `PREFLIGHT`, which calls `check()` again. Probably acceptable; state it. |
| N-8 | MINOR | ADR-0024 amendment; `tests/contracts/test_archive_format_owner.py:24-26` | The reserved inner-Manifest name is "declared in `domain/archive_format.py`", but the story does not add it to the owner guard's `NAMES` (global rule 3), and does not have that file in its grant; E004-S01-002 edits it. If the name is `manifest.json` it is already covered. Name the spelling. |
| N-9 | MINOR | E004-S01-001 L36-38 | E004-S01-001 edits `pyproject.toml`/`uv.lock` (pathspec) in parallel with E004-S01-003, E005-S01-001 and E005-S01-002, but does not name BL-E004-006 as the other three do. |
| N-10 | MINOR | E004-S01-003 L57 | The encrypted fixture's "generator" is not named. The built `tests/fixtures/archives/v1/generate.py` writes with `cipher=None` (L81), and it is not in the grant. Say whether that file is extended or a new one is added. |

### Totals

- E004: resolved 10/11; partly resolved 1 (F-1); not resolved 0.
- New: BLOCKER 0, MAJOR 5 (N-1 … N-5), MINOR 5 (N-6 … N-10).
- **The ADR-0024 mechanism is buildable** against `adapters/tar.py` and `domain/archive_format.py`. N-1 and N-3 say what must be written down first.
