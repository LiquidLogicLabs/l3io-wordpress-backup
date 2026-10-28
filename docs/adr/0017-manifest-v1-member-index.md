# ADR-0017: The v1 Manifest member index, and where the supported format versions live

- **Status:** Accepted
- **Date:** 2026-10-27
- **Deciders:** Shawn Anderson
- **Epic:** E002 (stories E002-S01-001 writer, E002-S01-003 verification); E003 (extraction) and E004 (v0 fixtures) consume it
- **Resolves:** arch gate findings AG-39 and AG-38 (2026-10-27)
- **Departs from spec:** _bmad-output/planning-artifacts/architecture/architecture-py-wordpress-backup-2026-09-15/ARCHITECTURE-SPINE.md#ad-6--the-archive-format-is-one-owned-contract
- **Principle(s) in tension:** AD-6 (one owned format contract; supported range derived from fixtures), AD-11 (an Archive is untrusted input), AD-23 (format detection), Core §3 contract completeness

## Context

AD-6 makes the Manifest's member index the basis of Verification: every member is listed
with its checksum over bytes as stored. E002-S01-001 defines
`ManifestMember(path, role, size, sha256, encrypted)` and stores each Site Root file as
its own tar member, so per-member encryption and checksums work. It also says symlinks
are stored as symlink members and never followed, and that special files are skipped and
logged.

The index does not say what happens to anything other than a regular file:

- whether directory entries are listed, so either every tar directory entry fails
  E002-S01-003's `UNDECLARED_MEMBER` check, or directories are dropped and empty ones
  (`wp-content/uploads/` on a fresh site) do not survive a round trip;
- what `size` and `sha256` mean for a symlink;
- what happens to hard links, whose default `tarfile` representation (`LNKTYPE`) carries
  no data;
- that `manifest.json` is itself a tar member but cannot list its own checksum.

`FORMAT_VERSION = 1` is append-only from the first release: an Archive written by 1.0
must stay readable. So these have to be fixed before the first Archive is written.

Second (AG-38): E002-S01-003 defines the supported format set as "the fixture Archives
committed under `tests/fixtures/archives/`". The classifier ships in the wheel and cannot
read `tests/` at runtime, so the runtime code had nothing to compare a `format_version`
against.

## Options considered

**Directories**

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. Implied: no directory entries; directories are created as parents of files | Smallest Manifest | Empty directories are lost; tar directory entries need an exemption from `UNDECLARED_MEMBER`, which is an unchecked hole an attacker can put entries through | Weakens AD-11's "every member is accounted for" |
| B. List only empty directories | Empty directories survive; smaller than C | The writer must decide emptiness after exclusions; non-empty directory entries still need an exemption | Two rules for one kind |
| C. **List every directory as its own entry** | One rule: tar entries (except `manifest.json`) and index entries correspond one-to-one; empty directories and directory modes survive | Larger Manifest, by roughly the number of directories | Exact check, no exemption list |

**Symlinks**

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. Follow and store the target's content | Restores without links | Reads outside the Site Root; can loop; contradicts E002-S01-001 | Breaks S01-001 and AD-11's spirit |
| B. **Store as a symlink member, target recorded verbatim in the Manifest, never followed** | Matches S01-001; the target is checked against the tar header | Targets outside the Site Root are carried but cannot be extracted under AD-11 (see Concern) | Consistent with S01-001; extraction stays AD-11's job |
| C. Skip symlinks | Nothing to contain | Silent loss of site structure | Data loss |

**Hard links**

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. Store as tar hard-link members (`LNKTYPE`) | Preserves the link | A data-less member that points at another member; a fourth kind for Verification and extraction | More surface for AD-11 |
| B. **Do not store the link: each path is a regular file with its full content; the link relationship is dropped and logged** | Only three kinds; every file member carries its own bytes and checksum | Content shared by hard links is stored once per path; the link is not restored | Simplest correct rule |
| C. Refuse the backup | Nothing ambiguous | A site with one hard link cannot be backed up | Disproportionate |

**Special files** (sockets, FIFOs, devices): **skipped and logged**, as E002-S01-001
already says. Refusing the backup over a stray socket was rejected for the same reason as
hard-link option C. The `tarfile` `data` filter refuses them at extraction anyway.

**Supported format versions** (AG-38)

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. Read the fixtures at runtime | Fixtures are literally the source | `tests/` is not in the wheel | Does not work |
| B. Ship fixtures as package data | Runtime can read them | Ships test Archives to every install for a lookup | Wrong home |
| C. **A constant in `domain/archive_format.py`, tested equal to the fixture-derived set** | Runtime reads a constant; the fixtures remain the evidence; drift fails CI | The constant is written by hand | Mechanically enforced (global rule 3); scope derived, not listed (rule 4) |

## Decision

### The member index, v1

Every tar member except `manifest.json` has exactly one index entry, and every index entry
has exactly one tar member. `ManifestMember` gains two fields that are part of v1 from the
first release:

`ManifestMember(path, role, kind, size, sha256, encrypted, link_target)`, with
`kind: MemberKind`, `class MemberKind(StrEnum): FILE = "file"; DIR = "dir"; SYMLINK = "symlink"`.

| Kind | What is stored | `size` | `sha256` | `encrypted` | `link_target` | Verification checks |
|---|---|---|---|---|---|---|
| `file` (regular files, the Dump, the `config` member) | The member's bytes, `age`-encrypted when encryption is on | Bytes as stored | SHA-256 of bytes **as stored** (ciphertext when encrypted, AD-6) | As written | `null` | Tar member is a regular file; size and checksum match |
| `dir` (the Site Root itself and every directory under it, empty or not) | A tar directory entry | `0` | `null` | `false` | `null` | Tar member is a directory |
| `symlink` | A tar symlink entry; never followed at backup | `0` | `null` | `false` | The link target, verbatim, as read by `os.readlink` | Tar member is a symlink and its link name equals `link_target` exactly |

- **Paths** are the tar member names, role-prefixed by `member_path` (AD-6), without a
  trailing slash for directories. Directory entries are written only under the
  `site-root` role (the Site Root and every directory below it). The `dump` and `config`
  prefixes have no directory entry of their own. The writer's sorted order puts a
  directory before its contents.
- **Mode, mtime, uid and gid** are carried by the tar header only. They are not in the
  Manifest and not checksummed. Restore applies them subject to AD-11's `data` filter,
  which strips set-id and group/other-write bits and ignores ownership.
- **Hard links** are never written as `LNKTYPE` members. A path whose inode has more than
  one link is stored as an ordinary `file` with its full content (the writer builds a
  regular `TarInfo` rather than relying on `tarfile.gettarinfo`'s inode tracking). The
  dropped relationship is logged once per inode with its paths. An Archive whose tar holds
  a hard-link member fails Verification.
- **Special files** (sockets, FIFOs, block and character devices) are skipped at backup and
  logged, and never appear in the index. An Archive whose tar holds one fails Verification.
- **Symlinks** are recorded with their target whatever it points at, including absolute
  targets and targets outside the Site Root, as E002-S01-001 states. The backup logs a
  WARNING naming each link whose target is absolute or resolves outside the Site Root.
  Whether such a link can be restored is AD-11's call at extraction, not the Manifest's
  (see Concern).
- **`manifest.json` is not listed in its own index.** It cannot contain its own checksum.
  The exemption is stated in `domain/archive_format.py` and is the only one: the name is
  `MANIFEST_MEMBER`, it must be the first tar member, and it must occur exactly once. A
  second `manifest.json` anywhere in the tar is `UNDECLARED_MEMBER`. The Manifest is
  protected by being parsed and schema-checked before anything else is trusted, and by
  the checksums it carries for everything else. It is never encrypted (AD-6).
- A tar member whose type does not match its index entry's `kind` fails Verification with
  a reason E002-S01-003 adds to `VerificationReason` (for example `KIND_MISMATCH`).
- **v1 is frozen at the first release.** After it, v1's Manifest may gain only optional
  keys that older readers ignore (`manifest_from_json` already ignores unknown keys). Any
  change to the meaning of `kind`, `size`, `sha256` or `link_target`, or a new kind, is
  format v2.

### The supported format versions

- `domain/archive_format.py` declares
  `SUPPORTED_FORMAT_VERSIONS: Final[frozenset[int]] = frozenset({1})`. The classifier
  compares a Manifest's `format_version` against it at runtime. A version not in it is
  `UnsupportedFormatVersionError` / `UNSUPPORTED_VERSION`.
- `tests/contracts/test_supported_formats.py` asserts that the constant **equals** the set
  of `format_version` values read from the Manifest of every committed fixture Archive
  under `tests/fixtures/archives/`. The fixture list comes from a glob, and the test
  asserts that list is non-empty. A version supported without a fixture fails, and so
  does a fixture whose version is unsupported. The fixtures are the evidence; the constant
  is what the runtime reads.
- v0 has no Manifest and no `format_version`. AD-23 detects it by shape, not through this
  constant. The same test asserts that every fixture under `v0/` classifies as v0. Until
  Epic 4 commits those fixtures, the test reports v0 as accepted-without-fixture in its
  output, as E002-S01-003 already states.

## Consequences

- Positive: Verification becomes an exact one-to-one comparison between the tar and the
  index, with one stated exemption (`manifest.json`). An empty `uploads/` survives a round
  trip, and a crafted extra entry of any kind is caught before extraction.
- Positive: AD-6's "derived from committed fixtures" now holds mechanically. The runtime
  reads a constant, and CI fails if the constant and the fixtures disagree.
- Negative / trade-offs accepted: the Manifest grows by one entry per directory. Hard-linked
  content is stored once per path, and the link is not restored. Symlink targets, like
  member names (ADR-0014), are visible to anyone holding the Archive.
- Follow-ups: E002-S01-001 adds `MemberKind`, `kind` and `link_target` to
  `ManifestMember` and the writer rules above. E002-S01-003 adds the kind check, the
  hard-link and special-file failures, the `manifest.json` exemption, and
  `SUPPORTED_FORMAT_VERSIONS` with its contract test. The v1 fixture includes an empty
  directory, a relative symlink inside the Site Root and a hard-linked pair.
- **Revisit when:** a restore needs the hard-link relationship or file ownership, or
  outward symlinks have to be restorable. Either is a format v2 decision.

## Concern

**Outward and absolute symlinks pass backup and Verification but are refused at
extraction.** E002-S01-001 stores a symlink "pointing outside the Site Root" as a link,
and this ADR records it. AD-11 requires `tarfile`'s `data` filter. That filter raises
`AbsoluteLinkError` for any absolute link target, even one pointing inside the Site Root,
and `LinkOutsideDestinationError` for a target that resolves outside the extraction
destination (checked against CPython's `tarfile._get_filtered_attrs`). Under the default
`errorlevel` both abort extraction. So a site with, for example,
`wp-content/uploads -> /mnt/media/uploads` produces an Archive that verifies, but whose
restore stops at extraction. The Manifest records enough to support either remedy:
refusing such Archives at backup time, or a restore-side filter that wraps `data_filter`
and skips and reports those links rather than aborting. Choosing between them is
restore's (Epic 3) and AD-11's, and is not made here.

## Amendment — 2026-10-28: the Concern is resolved — skip and warn

Decided by Shawn, 2026-10-28, from the E003 arch gate (finding AG-2).

- **Symlinks whose target is absolute or outside the Site Root are not supported for
  restore**, and the documentation says so. Backup still records them with the WARNING
  above; the format is unchanged.
- **A restore does not fail on them.** `extract_archive` (E002-S01-003) wraps
  `tarfile.data_filter`. When a symlink member makes it raise `AbsoluteLinkError` or
  `LinkOutsideDestinationError`, the filter returns `None` (`tarfile`'s documented skip),
  records the link in `ExtractResult.skipped_links`, logs `symlink_skipped`, and extraction
  continues. The restore exits `OK`, and prints one WARNING line per skipped link
  (E003-S01-001, E003-S01-006).
- **Every other filter refusal still aborts**: unsafe member names, members outside the
  destination, hard links anywhere, special files. Containment stays `tarfile`'s; the only
  decision added is which of its exceptions, on which member type, becomes a skip.
- An absolute link that points *inside* the Site Root is skipped as well. Rewriting it to
  a relative link would be a new transformation of operator data, and was not chosen.
- A Dry Run names the links it would skip from `ViewMember.link_target`, which
  E002-S01-003 adds to the view. For a v0 Archive, which has no Manifest, it says they are
  reported at extraction.
- Since the restore no longer aborts mid-EXTRACT, the `*-clean` failure mode and the
  unrestorable Safety Snapshot that the gate described do not occur. The site comes back
  without those links, and the operator is told which ones.
