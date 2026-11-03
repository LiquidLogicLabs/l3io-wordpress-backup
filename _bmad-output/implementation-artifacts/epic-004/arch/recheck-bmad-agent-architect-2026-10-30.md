---
reviewer: bmad-agent-architect (Winston)
gate: E004 architecture gate, re-check after resolution (/l3io-execute step-04 §6)
date: 2026-10-30
diff: 7379075..e44ac5b
---

# E004 arch gate: architect re-check

ADR-0024 is applied consistently in E002, E003, E004, E006 and E008. Every port, signature and call site I traced uses the payload model: `PayloadCipher`, `build_payload_cipher`, `decrypt_payload`, and `payload=` on `extract_archive`, `read_member` and `SiteFilesCommand.extract`. I found no per-member or recipient text outside AC blocks and dated notes. The exceptions are `epics.md` Stories 3.3 and 4.3, listed below.

## Findings re-checked

| ID | Sev (orig) | Verdict | Evidence |
|---|---|---|---|
| AG-1 | BLOCKER | resolved | ADR-0024 L109–117 makes the payload the unit of encryption and extracts the decrypted payload with `tarfile` `r:gz` and `filter='data'`. E002-S01-003 L65–66: `extract_archive(..., payload: Path \| None = None)` and `decrypt_payload(source, view, *, cipher, destination) -> Path`, with `ValueError` for an encrypted view that has no payload. E002-S02-012 L56: `SiteFilesCommand.extract(..., payload=)` delegates unchanged. E003-S01-001 L70–71: `VERIFY` decrypts into `inputs.payload`, and every extract and `read_member` passes `payload=inputs.payload`. E003-S02-005 L70: the Dump is extracted from the decrypted payload. E004-S01-003 L48 now names its consumers (E003-S01-001, E003-S02-006, E003-S02-003) instead of "Epic 3 story 3.1". |
| AG-2 | MAJOR | resolved | E004-S01-002 L33 (Outcome), L46 (Non-goals), L57 (`symlink-escape` moved out of `malformed/` into the passing set), L58 (`malformed/` is now only "must not pass verification") and L78 (verification passes `STRUCTURAL_ONLY`; the link is skipped, appears in `skipped_links` with `outside_destination`, one `symlink_skipped` WARNING, nothing outside `tmp_path`). L72 asserts `link_target is None` for v0. The CLI line L83 no longer exempts `symlink-escape`. |
| AG-4 | MAJOR | resolved | E004-S01-003 L97: a pinned `age` v1.3.2 download, a hard-coded `AGE_SHA256` checked with `sha256sum -c`, and `install -m 0755` of `age`, `age-plugin-batchpass` and `age-inspect`; no apt. L178: the interop test fails, not skips, under `CI` when a binary is missing or `age` < 1.3. L81 declares `age` ≥ 1.3 as a `ToolRequirement`. I downloaded the tarball: it contains `age/age`, `age/age-plugin-batchpass` and `age/age-inspect`, so the step can be built as written. |
| AG-5 | MAJOR | resolved (dissolved by ADR-0024) | ADR-0024 L72–80 makes the passphrase the only v1 key and defers recipients and identities. E004-S01-003 L50 (Non-goals) and L132; E003-S02-003 L31, L48 and L64 (the snapshot is encrypted iff a passphrase resolved, otherwise the AD-26 WARNING); E002-S02-011 L65 registers no recipient or identity flag. An identity-only cipher can no longer be constructed. |
| AG-6 | MAJOR | resolved (dissolved by ADR-0024) | One key type: `build_payload_cipher(*, passphrase, required, secrets)` (E004-S01-003 L74–78), with no identity input. The passphrase-restore route exists: E003-S02-006 L57 resolves the passphrase on every restore with `required=False`, as do E008-S01-001 L54 and E006-S01-001 L111/L134. The spine's AD-26 L304 carries the dated ADR-0024 note. |
| AG-7 | MAJOR | resolved | Plan-time refusal: E003-S02-006 L64 step 6 raises `PassphraseRequiredError(operation="restore")` when `view.encrypted` and the cipher is `None`, before the payload is read; a Dry Run refuses the same way. Key check before destruction: E003-S01-001 L70 decrypts the whole payload in `VERIFY`, before `SNAPSHOT`; the no-config path decrypts even earlier (E003-S02-006 L68). E004-S01-003 L122–123 give the raise sites. E003-S01-001 L119 and L166 test it with a raising `PayloadCipher` double, so E003 needs nothing from E004. |
| AG-8 | MAJOR | resolved (E004 side), but see the new MAJOR below | `passphrase_backend_requirements` is now in `adapters/crypto` (E004-S01-003 L79–83), and its tests are there too (L176). E005-S01-002 L64 and L101 cite it, and E005 no longer defines it. E004 imports nothing from Epic 5. The reverse coupling, E005 reading `adapters/crypto`, is a new defect. |
| AG-9 | MAJOR | resolved | E004-S01-003 L74–78: the factory takes `required: bool`, and `required=True` with no passphrase raises `PassphraseRequiredError(operation="backup")`. L124 is the cron case. Wiring: E008-S01-001 L54 passes `required=args.encrypt_with_passphrase`; E006-S01-001 L134 passes `encrypt=True` → `required=True`. Tests: E004-S01-003 L175 and L185 (factory); E008-S01-001 L97 (CLI, no TTY). |
| AG-10 | MAJOR | resolved | E004-S01-001 L121: the end-to-end test now runs through `drive_backup(BackupRequest(..., exclusion=LoggedExclusionPolicy(...)))`. E004-S01-003 L166 bans CLI flags, `api/` and `drive_restore` from its tests; the inspector test (L179–184) uses `drive_backup`; restore-side tests (L185) use `decrypt_payload` and `extract_archive`. The `api/` and CLI halves moved to E006-S01-001 L248–249 and E008-S01-001 L92 and L97; the single-chain contract (L189) asserts only the one-module rule. I walked the `depends_on` edges. E004-S01-001 and E004-S01-003 need `drive_backup`/`BackupRequest` (E002-S01-004), `decrypt_payload`/`extract_archive`/`skipped_links` (E002-S01-003), `resolve_passphrase`/`SecretRegistry`/`credential_sentinel` (E002-S01-006), `PassphraseRequiredError` (E002-S01-001), `ToolRequirement` (E002-S02-007) and the RSS harness (E002-S01-010). E004-S01-002 needs E002-S01-003's `extract_archive`, `skipped_links` and `symlink_skipped`. All are E002, and E004's epic edge `depends_on: [E002]` orders every one before E004. Nothing comes from E003, E006 or E008. Residuals are MINOR: E002-S01-004 is missing from the story-level `depends_on`, and L187 has the wording problem, both below. |

Resolved: 9/9 (AG-1, AG-2, AG-4, AG-5, AG-6, AG-7, AG-8, AG-9, AG-10).

## New findings

MAJOR: E005-S01-002 now depends on E004-S01-003 in the same phase, and the plan has no edge for it, which reverses AG-8's coupling. Three places (`gen_tool_docs.py` input, L122; contract test, L209; tripwire, L212) read E004's `adapters/crypto.passphrase_backend_requirements`, "enumerated once `adapters/crypto` exists". The tripwire also fails "once `adapters/crypto` exists … without their enumeration being added". `state/planned/epic-005/epic.yaml` and `E005-S01-002.yaml` depend only on E002. Whichever epic merges second sees `test_tool_requirements_documented.py` / `gen_tool_docs.py --check` fail. No story owns the fix: E004-S01-003 says nothing about the README table or `KNOWN_TOOLS`. Two options: (a) give the enumeration to E008-S01-001 (phase 4, which already depends on E004 and E005) and drop the "once it exists" clauses; or (b) record E005-S01-002 → E004-S01-003 in the state graph. Option (a) keeps phase 2 parallel.
MINOR: E004-S01-003 L185 asserts the restored tree has "`database.sql` intact". `database.sql` is v0's layout. A v1 inner Dump is the `dump`-role member (E002-S01-001; ADR-0024 L54–59, as corrected on 2026-10-30). This is the same slip that ADR-0024's own correction fixed. Reword to "the `dump`-role member intact".
MINOR: E004-S01-003 L187 applies the sentinel check to "a failed decrypt, both through the drivers". No driver reaches a decrypt in E004's dev loop, because `drive_restore` is E003's. Say "through `decrypt_payload`", matching L185, so the loop does not read it as needing `drive_restore`.
MINOR: ADR-0024 contradicts itself on AG-7. The Consequences (L125) say "AG-7 reduces to one header check", but the Decision (L102–107) decrypts the whole payload in `VERIFY`, and every story follows the Decision. Correct the Consequences line.
MINOR: The superseding note in the E004-S01-003 AC (L34) does not cover L22's Given clause ("encryption configured with `age` recipients, or with a passphrase"). Also, `epics.md` Story 4.3 (L567–575) and Story 3.3 (L449) still have the per-member and recipient AC text with no dated ADR-0024 note, although the spine (L105, L304) and the PRD (L138) have one. A planner reading `epics.md` sees the old model.
MINOR: E004-S01-001 and E004-S01-003 call `drive_backup`/`BackupRequest` (E002-S01-004), but neither lists E002-S01-004 in its story-level `depends_on` (`state/planned/epic-004/sprint-01/E004-S01-00{1,3}.yaml`). The epic edge E004 → E002 covers the order, so this is bookkeeping only.
MINOR: E004-S01-002 L60, "Consumed interfaces (… unchanged)", still gives `extract_archive(source, destination, view, *, roles, exclude=frozenset())` without the new `payload: Path | None = None` (E002-S01-003 L65). The default makes it harmless for v0, but the signature is stale.
MINOR: E004-S01-003 L97 calls `AGE_SHA256` "the release's published SHA-256" but does not record the value. Write the digest into the story, so the dev loop does not fetch it from the same channel as the file. For reference, I downloaded the file from the GitHub release and computed `cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10`. That digest came from the same channel, so treat it as a cross-check, not provenance.
MINOR: Resolving the passphrase "on every restore" puts an interactive prompt on every TTY restore, including one from a plaintext Archive. That follows from E003-S02-006 L57, E008-S01-001 L54 and E002-S01-006 L58: the prompt is bound when `stdin` is a TTY, before the view is known. Under ADR-0024 this is intended, because the passphrase also encrypts the Safety Snapshot. No story states the operator-visible prompt or documents it (`docs/restore.md`, E003-S01-001), so state it.
MINOR: E004-S01-003's estimate is from 2026-10-27 (`E004-S01-003.yaml`: 7.5 h, `updated_at` 2026-10-27), but the story was largely rewritten. It lost the recipient and identity modes and gained `passphrase_backend_requirements`, the family-index lock, the CI `age` step, a two-backend `pty` interop test and the restore-side `decrypt_payload` cases. It still reads as one dev loop: one thin adapter module, configuration and tests, no new seams. Re-estimate it so the plan totals are honest.
Note (no severity): the gate record `arch-gate-review.md` still reads "Resolution: Not started" at `e44ac5b`. The orchestrator should fill it from this re-check and its sibling.

Sections read:
- `_bmad-output/implementation-artifacts/epic-004/arch/reviewer-bmad-agent-architect-2026-10-28.md` (all); `arch-gate-review.md` (all)
- `docs/adr/0024-one-encrypted-payload-passphrase-only.md` (all)
- `git diff 7379075..e44ac5b --stat`; full diffs of E004-S01-001, E004-S01-002, E005-S01-002, E006-S01-001, E007-S01-002, E008-S01-001, `state/`, the planning artifacts (spine, `epics.md`, PRD), ADR-0014, ADR-0017, `ci.yml`, `pyproject.toml`; added lines of every E002 and E003 story in the diff
- `epic-004/sprint-01/stories/E004-S01-003.md` (all, at HEAD); E004-S01-001 and E004-S01-002 by grep at HEAD
- `state/planned/epic-00{2,3,4,5,6,8}/epic.yaml`; `state/planned/epic-004/sprint-01/*.yaml`; `state/planned/epic-005/sprint-01/E005-S01-002.yaml`
- E005-S01-002 L112–125 and L196–215; E006-S01-001 L66–112 and passphrase grep; E008-S01-001 L54; E002-S01-004 L65 (`drive_backup` signature); E002-S02-007 L60 (`ToolRequirement`); E002-S01-003 L103 (`symlink_skipped`); E002-S01-010 L61 and L118 (`encrypted`, `ENCRYPT`); E003-S01-001 L166 (encryption test case); spine L300–306; `epics.md` L170–182 and L562–580
- Repository grep for leftover `recipient` / `identity` / `MemberCipher` / `build_member_cipher` / `per-member` / `VerifyRequest(` / `no_key` across E002–E008 stories, `epics.md`, the spine, ADR-0014 and ADR-0017
- Host check: downloaded and listed `age-v1.3.2-linux-amd64.tar.gz` from the official GitHub release (contents and SHA-256)

DONE — resolved 9/9, new: MAJOR 1, MINOR 9

## Re-check 2

Date: 2026-11-02. Input: the uncommitted working-tree diff of E005-S01-002 and E008-S01-001. The state graph is unchanged.

**Is the MAJOR resolved? Yes.**
- E005-S01-002 no longer reads `adapters/crypto`:
  - L122: the README table input is now "an extension point … which E008-S01-001 fills"; the text says this story does not import `adapters/crypto`.
  - L209: the contract test collects only what has been registered.
  - L212: the tripwire's trigger set drops `adapters/crypto`, with the reason stated.
- E008-S01-001 L92 takes over all three obligations: it registers E004's requirements, regenerates the README table and extends the tripwire.
- E008-S01-001 depends on both stories involved (`state/planned/epic-008/sprint-01/E008-S01-001.yaml`: E004-S01-003, E005-S01-002), and E008 is phase 4. So the second story to merge in phase 2 no longer turns CI red.

**Does E004 or E005 still need the other in phase 2? No.**
- E004 imports nothing from Epic 5 (AG-8, unchanged).
- E005-S01-002 still mentions E004 at L64, L101, L111, L136, L158 and L201. Each is a citation of ownership or behaviour, not an import, a test input or a trigger:
  - L111's `tools` arrive through the plan's `extra_tools`.
  - L158 describes database's `MissingDependencyError` mapping. E005 does not assert it.
- E005's `depends_on` (E002 only) is now accurate.

**Anything new?**
- MAJOR (pre-existing, same class, exposed by this check; not introduced by the edit): the tripwire still fails "once `core/restore` exists (Epic 3)" (E005-S01-002 L212). E003 runs in the same parallel phase as E005 (`plan-2026-10-27-v2.yaml` phase 2: E003, E004, E005). No E003 story mentions `test_tool_requirements_documented.py` or `gen_tool_docs.py` (repository grep: only E005, E008 and this file). The edit's own reason, "whichever landed second would turn the other's CI red with no owner", applies word for word to Epic 3. The same fix works: drop `core/restore` from E005's trigger set and give the `RestoreMode` enumeration to E008-S01-001, which depends on E003. This is outside E004's gate, so it belongs to whichever gate owns E005 or E003. It should be fixed before phase 2 starts.
- MINOR: the extension point has no contract. E005-S01-002 L122 names "an extension point for further requirement sources" but gives no name, signature, location or registration mechanism. Is it a registry function, a module-level tuple or a `gen_tool_docs.py` argument, and who imports it? E008-S01-001 L92 registers "with E005-S01-002's requirement-source extension point". Every other cross-story seam in these stories has one stated signature. State one here, for example `tools/gen_tool_docs.py::requirement_sources: Sequence[Callable[[], Iterable[ToolRequirement]]]`, consumed by both the generator and the contract test.
- MINOR: E008-S01-001 states its new work only under §Testability (L92). That work is: register the source, regenerate the README table, and edit E005's `tests/contracts/test_tool_requirements_documented.py`. Nothing under Interface contracts or the files list records it, and nothing records a grant to edit an Epic 5 file. The phases are sequential, so the edits cannot conflict. Still, list the files E008 changes and record the grant, as AG-8's resolution did in `epics.md` L173–175.
- MINOR: the new annotations are dated "2026-11-03" (E005-S01-002 L122, L212; E008-S01-001 L92), a day after this re-check. Correct the date so the record's chronology holds.
- Note (no severity): between E005 landing and E008 landing, the README tool table omits `age` ≥ 1.3 and `age-plugin-batchpass`. Preflight is unaffected, because the requirements still arrive through `cipher.required_tools()`. This is an accepted documentation gap for phases 2–3, not a defect.

Sections read (re-check 2): `git diff` of E005-S01-002 and E008-S01-001 (working tree); E005-S01-002 grep for `E004|adapters/crypto|passphrase_backend|extension point|Epic 4`; `state/planned/epic-008/sprint-01/E008-S01-001.yaml` (`depends_on`); `planning-artifacts/plan-2026-10-27-v2.yaml` L25–46 (phase 2); repository grep for `test_tool_requirements_documented|gen_tool_docs`.

DONE — MAJOR resolved: yes, new: MAJOR 1 (pre-existing, E003↔E005), MINOR 3
