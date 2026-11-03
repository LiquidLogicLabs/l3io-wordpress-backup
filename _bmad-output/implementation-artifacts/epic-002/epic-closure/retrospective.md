# Epic E002 retrospective

Twelve stories over two sprints delivered backup composition with Dry Run by adapter selection, the Site Lock, Archive format v0/v1 detection, verify (consistency, and `--authenticate`), the restore vocabulary, the frozen CLI surface, the restore ports with no-op twins and the real database, site-files and config-writer adapters. The code diff is about 10.7k lines of source and 18.5k of tests; the final CI replay passed 1357 tests.

What recurred: cross-story seams were where defects hid (the `.partial` self-archive regression at sprint 2 closure; the phase-name spelling and the `view.payload is None` format proxy at epic closure). Per-story gates stayed green over all of them. Sprint closures deferred most findings to the backlog, so the epic ends with owned preconditions for Epic 3 rather than open code defects.

Learnings carried forward:
- A story that reorders steps needs a test of the end result across stories, not only per-step tests.
- Settle the spelling of cross-flow identifiers (the log `phase` field) before the second producer ships; after Epic 3 it is a log-schema break (BL-E002-109).
- The AD-4 grep gate is spelling-sensitive and the cli/api boundary is convention only (AD-2, AD-3 in the epic review).
- Restore security preconditions (Archive-named DB credentials, extraction limits, swap window, passphrase in the environment) must be owned by Epic 3 and Epic 7 stories (RT-1, RT-3, RT-4, RT-8).
- `python -m wpbackup2` still does not reach the new parser (BL-E002-086), and the `adapters/crypto` hand-off is open (BL-E004-042).
