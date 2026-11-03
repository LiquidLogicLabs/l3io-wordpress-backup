# Sprint E002-S02 retrospective

Five stories delivered (S02-007, -008, -009, -011, -012): backup composition root with Dry Run by adapter selection, the Site Lock, the restore vocabulary and frozen CLI surface, the restore ports with no-op twins, and the real restore adapters (database, site files, config writer).

Closure found one real regression across stories: with the Archive target inside the Site Root, the staged `.partial` file was collected into its own Archive (S02-007 moved collection after staging). Fixed in `cli/backup.py` (`_SkipStaged`), with `StagedArchive.staged_path` added to the port and a regression test. Everything else was deferred to the backlog (BL-E002-092..105); two HIGH red-team items (RT-1 `%` account host, RT-2 extraction race) and two HIGH UX items (UX-1, UX-2) await owning stories before Epic 3 wires restore.

Lesson: a story that reorders steps needs a cross-story test of the end result (an Archive written inside its Site Root), not only per-step tests.
