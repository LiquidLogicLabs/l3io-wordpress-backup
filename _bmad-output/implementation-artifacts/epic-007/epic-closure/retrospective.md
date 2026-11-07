# E007 retrospective (combined sprint 01 and epic closure)

Evidence: commits 91f8222, 2b3de85, c4059b6, f1cf076; per-story reviews in `sprint-01/closure/review-E007-S01-00*.md`; the three epic-wide reports in this folder.

## Summary

Four stories shipped a restore that can move a Site: relocation flags and the config rewrite (S01-001), WP-CLI `search-replace` over serialized data with a pinned, explicit child environment (S01-003), a config that lives outside the Site Root (S01-002), and a WP-CLI preflight that fails before anything changes (S01-004). Each story passed its gates on 3.11 and 3.14, and `make ci` at f1cf076 was green with the real WP-CLI 2.12 against WordPress 6.8.3. Per-story fix iterations: 1, 2, 1, 1.

The epic-wide pass found no CRITICAL. It found one HIGH from each of two lenses, both about the same seam: the stories each hardened one half of the `wp` surface and each assumed the other half was covered. Four MEDIUM findings were reachable on the live restore path and were fixed in the closure pass; the rest is carried to E008-S01-001, which owns the preflight binding.

## What went well

- Never hand-rolling serialized-data handling held: every replacement goes through `wp search-replace --precise`, and no Python code edits a serialized value.
- Credential handling held under three independent lenses: nothing in `wp`'s argv or environment, redaction registered before use, no value in any log field.
- The "explicit `env=`" rule became a mechanical gate (`test_child_env_explicit.py`, scanning for credential variables in source) rather than prose.

## Pain points and learnings

1. **A property claimed by two stories is checked by neither.** S01-003 hardened the `wp` child (no `wp-cli.yml`, minimal env) and S01-004 added a second `wp` call (the version probe) to the restore path. The probe has none of the child's protections, and the docs said it did. A story that adds a call site to an existing trust boundary must list the boundary's guards and show each one applies to it. (AR-1)
2. **"Minimal environment" is not "isolated from secrets".** Same-user code can read `/proc/<ppid>/environ`. The owner decision (O3 a) stood; the prose around it overstated what it buys. Docs for security controls now say what the control does not cover. (RT-1)
3. **Checkable preconditions were left to the last phase.** The `wp-cli.yml` ancestor refusal and the state-directory setup run at SEARCH_REPLACE, after the site is wiped. "Fail before touching anything" (S01-004's title) was true only for the tool probe. (AR-2)
4. **Data from the Archive is untrusted at every hop, including the database it carries.** Operator input had string rules; the `siteurl`/`home` read back from the restored database did not, and became an argv element. Rules are now applied where untrusted text enters a command, not only where the operator types. (RT-5)
5. **Interim documentation drifted from shipped behaviour.** The relocation flags were removed from `NOT_YET_IMPLEMENTED` while the preflight binding that makes them usable belongs to E008. The deferral was recorded in the story and a code comment, not in `docs/restore.md`. A flag is documented as working only when an end-to-end path through the CLI exists. (ED-1)
6. **No process had a time limit.** `run_child` had no timeout while the preflight probe did; the same author wrote both in one epic. A restored site's PHP is hostile input, so every child gets a ceiling. (RT-2)

## Process notes

- The stories were committed interleaved with E006 merges; per-story review files and gate records made the interleave auditable.
- Closure ran as one combined pass (owner-approved): three reviewers in parallel, each over one set of inputs, instead of two passes per lens.
- Test limitation: this host has no `wp`/`php`, so reviewers marked WP-CLI-behaviour claims as plausible. The integration tests that exercise real WP-CLI run in CI only.

## Learnings to carry forward

- A new call site on an existing trust boundary inherits the boundary's guard list, with a test per guard.
- State what a security control does not cover in the same paragraph that states what it does.
- Apply string rules to untrusted text at the point it is placed into argv, whatever its origin.
- Give every child process a time limit and an output bound at the point the runner is written.
- Documentation for a deferred binding says "not yet" in the user-facing text.

## Acceptance decision

Accepted, with the closure fixes below applied and the carry-over scheduled to E008-S01-001 (BL-E007-007, -008, -009, -016).
