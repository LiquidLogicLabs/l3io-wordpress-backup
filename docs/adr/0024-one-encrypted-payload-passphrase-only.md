# ADR-0024: One encrypted payload per Archive, passphrase only for v1

- **Status:** Accepted (Shawn, 2026-10-29)
- **Date:** 2026-10-29
- **Deciders:** Shawn Anderson
- **Epic:** E004 (story E004-S01-003); E002 (E002-S01-001 writer, E002-S01-003 verify and
  extract) and E003 (restore) consume it
- **Resolves:** E004 arch gate findings AG-1, AG-5, AG-6 and AG-7 (2026-10-28)
- **Supersedes:** AD-6 rule 3 (each member encrypted independently); ADR-0017's per-member
  encryption and per-member checksums; ADR-0014's dump encryption through
  `dump(passphrase=)` and its recipient/identity mode for v1
- **Departs from spec:** _bmad-output/planning-artifacts/architecture/architecture-py-wordpress-backup-2026-09-15/ARCHITECTURE-SPINE.md#ad-6--the-archive-format-is-one-owned-contract
- **Principle(s) in tension:** rule 1, never hand-roll what a library does; AD-11 (one
  extraction function, `filter='data'`); AD-21 (stream everything); NFR-1, NFR-7

## Context

The v1 format encrypted each archive member on its own, inside a plain tar. So
`tarfile` could not extract an encrypted Archive: this package would have to decrypt
member by member while extracting (E004 gate AG-1), and track `encrypted` and a ciphertext
checksum per file. On top of that, recipient and identity encryption brought four more
findings (AG-5, AG-6, AG-7, AG-3's binder packaging).

No Archive code exists yet (`src/` is 200 lines with no `tarfile`), so the format can still
change at the cost of story text only.

Libraries were checked before choosing (2026-10-28/29):
- **zip.** The stdlib `zipfile` offers only ZipCrypto. AES needs `pyzipper`. Reading needs
  a seek to the central directory. There is no equivalent of `tarfile`'s `data` filter.
- **7z** (`py7zr` 1.1.4). Passphrase only. Needs seeking. OSV lists a directory traversal
  (CVE-2022-44900), an arbitrary file write (CVE-2026-23879) and two 2026 DoS advisories.
- **tar + age.** Stdlib `tarfile` with `filter='data'`, and age through
  `l3io-wordpress-database`'s released `encrypt`/`decrypt(passphrase=)` (v0.2.0). Both
  stream, and `age -d` recovers an Archive without this package.

## Options considered

| Option | Pros | Cons | Standards fit |
|---|---|---|---|
| A. Per-member age encryption (current) | Per-file integrity without a key | We write decrypt-while-extract code; per-file encryption state; four open findings | Weak on rule 1 |
| B. **One encrypted payload, passphrase only** | `tarfile` and database's stream functions do all the work; keys later are additive | Without a key, a damaged payload is located only to the payload, not to a file | Rule 1, AD-11, AD-21 |
| C. 7z with a passphrase | Well known; `7z x` everywhere | Seeking; py7zr's extraction advisories; a second format beside database's age dumps | Fails NFR-5's spirit and AD-21 |
| D. GnuPG symmetric | No binder; keys later in the same tool | A new external binary, a per-Run `GNUPGHOME` and agent; splits the family's formats | Adds an FR-27 dependency |

## Decision

**Option B.**

**Layout.** The Archive is an uncompressed outer tar with exactly two members, in this
order:

1. `manifest.json`: first and never encrypted, as AD-6 requires.
2. The payload: `payload.tar.gz` when not encrypted, `payload.tar.gz.age` when encrypted.
   It is a gzip-compressed inner tar whose members keep AD-6's role-prefixed v1 names
   (`dump/…`, `config/…`, `site-root/…`; E002-S01-001 owns them), the Dump first (when the
   scope includes the database), then the config, then the site root. Putting the Dump
   first lets a database-only restore stop reading early. *(Corrected 2026-10-30: the
   accepted text named the inner members `database.sql` and `wp-root/…`, which is v0's
   layout; this ADR never meant to change AD-6's role prefixes.)*

**Writing.** The inner tar is gzip-compressed and then encrypted as **one stream** by
`l3io-wordpress-database`'s `encrypt(reader, writer, passphrase=)` (v0.2.0, push-style:
it reads one file object and writes another). It is written into the Run's workspace
first, because the outer tar must know the payload's size before writing its header and
the Manifest must carry its `sha256`. The payload is then copied into the outer tar,
behind the Manifest, and the Archive is published by AD-20's temp-name-and-rename. This package imports no
`pyrage` and no `cryptography` cipher (ADR-0014 holds). The Dump is produced **plain** by
`dump()` with no `passphrase=`. It goes into the Run's private, owner-only workspace
(AD-12), which is removed in a `finally`. It has to land on disk anyway, because a tar
member's size must be known before it is written.

**Passphrase only, for v1.** The passphrase comes from AD-26's one resolution chain,
unchanged.
- A backup with encryption requested and no passphrase resolved is **refused**. No silent
  plaintext (AD-26; E004 gate AG-9).
- The Safety Snapshot is encrypted when a passphrase resolves. Otherwise it proceeds,
  saying so on stderr and in a WARNING record (AD-26, decided 2026-10-20).
- Recipient and identity encryption are deferred. Adding them later changes only age's
  header stanzas, not this layout. That will be a new flag set and an ADR, not a format
  version.

**Manifest.** It carries:
- `format_version`, unchanged at 1 (v1 is still unreleased);
- `encrypted: bool`;
- the payload's `name`, `size` and `sha256` over the bytes as stored;
- the member list of the inner tar: `path`, `role`, `kind`, `size`, `link_target`. This
  serves planning, the Dry Run, FR-7 and the config path.

There are **no per-file checksums**. ADR-0017's member kinds, its symlink rules (and
2026-10-28 amendment) and its `SUPPORTED_FORMAT_VERSIONS` stand. Its per-member `sha256`
and `encrypted` fields are withdrawn.

**Verification, with no key.**
- The Manifest parses.
- The version is supported.
- The payload is present, and its size and `sha256` match.
- The inner member list, read from the Manifest, is well formed.

This catches corruption, truncation and a payload swapped in from another Archive before
anything is touched.

**Restore VERIFY, with the passphrase.** For an encrypted Archive, VERIFY also
decrypts the whole payload with `decrypt(reader, writer, passphrase=)` into the Run's
workspace (owner-only). age authenticates every chunk, so a wrong passphrase, a damaged
chunk or a truncated stream all fail **here, with a named error, before SNAPSHOT and
before anything destructive** (E004 gate AG-7; FR-4; NFR-1). If no passphrase is supplied
for an encrypted Archive, the restore is refused when the plan is built.

**Extraction.** AD-11's one function opens the plain payload with
`tarfile.open(..., mode="r:gz")` and extracts with `filter='data'`. The plain payload is
the decrypted file in the workspace for an encrypted Archive, or the outer tar's payload
member for a plain one. No threads and no pipes are needed.

The existing filter wrapper still applies: the skip for unsupported symlinks
(ADR-0017 amendment) and the `wp-config.php` exclusion (ADR-0023). Reading a single inner
member streams to it, for example the config (≤ 1 MiB) or the Dump into the
workspace. This package adds no decryption or file-writing code.

**v0.** Unchanged. It is read-only, with no Manifest and one-way detection (AD-23).

## Consequences

- **Positive.**
  - The libraries do every byte of extraction and decryption.
  - E004 gate AG-1, AG-5 and AG-6 disappear, and AG-7 reduces to one header check.
  - E002-S01-003's per-member verification becomes one checksum.
  - The format no longer depends on the key type, so adding keys later is cheap.
- **Negative / trade-offs accepted.**
  - **Damage location.** Without a key, verification locates damage to the payload, not
    to a file. With a key, age authenticates every 64 KiB chunk while decrypting.
  - **Single-file reads.** Reading one inner file decrypts the stream up to it. Nothing in
    the PRD needs single-file restore.
  - **Plain data on disk.** The plain Dump at backup, and the decrypted payload at
    restore, exist on disk inside the owner-only workspace for the Run's length. NFR-3
    allows owner-only temporary files, and AD-12 removes them in a `finally`.
  - **Extra disk space and I/O.** The workspace needs room for one compressed payload,
    and the Run writes it twice: once to the workspace, then once into the Archive or
    the Site. This counts against NFR-7's 1.5× budget, but it is the compressed size,
    and the alternatives (patching tar headers, threads with pipes) are hand-written
    code.
- **Still open, unchanged by this ADR.**
  - The passphrase backends in database (binder or `age` CLI) remain. E004 gate AG-3 (the
    binder's wheels on the family index) and AG-4 (CI downloads a pinned `age` v1.3.2,
    which ships `age-plugin-batchpass`) still need doing.
  - AG-8 (who owns the passphrase-backend tool requirement) is separate.
- **Follow-ups.**
  - Amend AD-6 rule 3 and FR-4's consequence 2 (no KDF or salt in the Manifest) with
    backlinks here.
  - Amend ADR-0014 and ADR-0017 to point here.
  - Re-scope E002-S01-001 (writer), E002-S01-003 (verify, extract, `read_member`),
    E004-S01-003 (passphrase-only encryption) and the E003 stories that read members or
    decrypt the Dump (E003-S02-005's load reads the extracted plain Dump).
- **Revisit when:** a single-file restore or keyless per-file integrity becomes a product
  requirement, or key-based encryption is scheduled. Keys need only a new ADR; neither of
  the other two is possible within this layout.
