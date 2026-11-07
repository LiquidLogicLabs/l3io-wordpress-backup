# Spec proposal for AD-2: where the Manifest cap fires (owner decision)

**Target:** story E008-S01-001 acceptance ("the plan computes the serialized Manifest size ... refused at plan time with nothing written"), owner decision E004 D2 (c), ADR-0019 (option C: a Dry Run binds no-op commands; the plan reads nothing), spine AD-4.

**Finding:** the refusal is in `TarArchiveWriter.write` (`adapters/tar.py`), after `DUMP`. A backup Dry Run binds `NoopArchiveWriter`, which collects nothing, so it reports success where the real Run refuses (a defect by AD-4).

**Options**
- (a) Amend the acceptance to "refused at `WRITE_ARCHIVE`, before the Archive is written; a Dry Run does not measure it", and add the Manifest cap to ADR-0019 as a refusal "only a command can detect". Cost: the dump is taken and discarded on a refusal; a Dry Run can say "would back up" for a Site that will be refused. No code.
- (b) Add a read-only query port (for example `ManifestSizeCheck`) bound for real and Dry Runs, run before `DUMP`, that walks the Site Root with the Run's exclusion policy and measures the Manifest with the same builder and serialiser. Cost: a second Site Root walk per backup (real Run), a Dry Run that reads the Site tree (an ADR-0019 amendment: "the plan reads nothing" gains this exception), a new port, two adapters and tests. Benefit: the owner's wording is honoured exactly, and AD-4 holds.

**Recommendation:** (b) as a post-v1 story if the cap is expected to be reached (a Manifest of 256 MiB is on the order of a million members); (a) for v1, because the cap is far above any real Site and the worst case is a wasted dump, not a wrong Archive.

Backlog: BL-E008-003.
