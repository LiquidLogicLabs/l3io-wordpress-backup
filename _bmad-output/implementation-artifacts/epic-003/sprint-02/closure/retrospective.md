# Sprint 02 retrospective (E003)

Four stories (S02-003 to -006) plus BL-E003-053 and the RT-002 owner decision made `wpbackup2 restore` real: Safety Snapshot, database restore report, root-level restore with run journal and staging cutover, TLS to the administrator.

Closure review found one CRITICAL (the mysql client ran client commands from an untrusted Dump), two HIGH on the administrator-password channel and journal/space guidance, and a MAJOR drift (adapter-module introspection standing in for a dry-run flag). All were fixed in closure with regression tests that fail without the fix. Remaining MEDIUM findings need a design decision and are in the backlog and the closure report.

Lessons: a guard that checks help prose (RT-S02-002) is not a guard on behaviour; untrusted Archive content needs a threat pass at the story that first feeds it to an external process, not at sprint closure.
