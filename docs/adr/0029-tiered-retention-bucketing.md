# ADR-0029: Tiered retention bucketing is a pure function in `core/`, not a library

- **Status:** Accepted (Shawn Anderson, 2026-11-12; owner decision 85)
- **Date:** 2026-11-11
- **Deciders:** Shawn Anderson
- **Epic:** E008 (close-out, 1.1.0)
- **Resolves:** owner decision 84, "Implementation: the bucketing uses a maintained library if one
  fits (rule 1); otherwise an ADR records the evaluation"
- **Principle(s) in tension:** global rule 1 (never hand-roll what a maintained library does),
  AD-1 (`core/` is pure)

## Context

Owner decision 84 adds `--keep-daily`, `--keep-weekly`, `--keep-monthly` and `--keep-yearly` to
`wpbackup2 prune`, beside `--keep N`. The semantics are restic's `forget`:

- each tier walks this site's Archives newest first by Manifest `created_at` (UTC), groups them by
  UTC day, **ISO week** (`isocalendar()`), month or year, and keeps the newest Archive of each of
  the **N most recent groups that hold an Archive** (a group with no Archive does not count);
- the tiers are **independent**: an Archive kept by any rule survives and carries every rule that
  keeps it as a reason (`["daily", "monthly"]`);
- ties on `created_at` are broken by the later file name, as `--archive latest` breaks them.

The input is already in hand: `core/archive_choice.site_archives` yields this site's Archives with
their parsed `created_at`. What is needed is a function from `(created_at, name)` pairs and five
counts to a keep/delete verdict per Archive, with the reasons. It must pass `mypy --strict` and
`check_purity` (no clock read: the newest Archive, not "now", anchors every window).

## Libraries evaluated

Searched PyPI and the web on 2026-11-11 for backup rotation, grandfather-father-son and retention
packages. Each candidate was read at the version below (source downloaded from PyPI). The four
that implement this kind of bucketing:

| Package | Version (last release) | Licence | Why rejected (checkable) |
|---|---|---|---|
| `rotate-backups` | 8.1 (2020-05-17) | MIT | No release in 6.5 years. Built around a `Location` it lists and deletes from itself (local or SSH), not around caller-supplied objects. Pulls 10 dependencies (`executor`, `humanfriendly`, `property-manager`, `natsort`, `simpleeval`, `six`, `update-dotdee`, `verboselogs`, `python-dateutil`, …). Keeps the **oldest** backup per period unless `prefer_recent=True`. Its weekly key is `(calendar year, ISO week)` (`group_backups`), which **splits ISO week 2026-W53**: Thu 2026-12-31 is `(2026, 53)` and Fri 2027-01-01 is `(2027, 53)`. Its `strict` mode (the default) also drops backups older than `newest − N × period`, which is not the "N groups that have one" rule. No type hints. |
| `GrandFatherSon` (`grandfatherson`) | 1.3 (2016-09-16) | BSD | No release in 10 years. Windows are calendar ranges counted back from `now` (`Filter.start`: `mask(now) − (N−1) units`), so a gap (a week with no backup) shrinks what is kept instead of reaching further back. It also keeps the **first** datetime per unit, starts weeks on `firstweekday` (Saturday by default) rather than ISO weeks, takes no names (only datetimes: no tie-break, no reasons), and has no type hints. |
| `timegaps` | 0.1.1 (2014-05-19) | not declared in metadata | No release in 12 years; classifiers stop at Python 3.4. Rules are **age** categories against a reference time (`days7` means items 1–7 days old), not calendar buckets. |
| `borgbackup` | 1.4.5 (2026-09-27) | BSD-3-Clause | Maintained, and `borg.helpers.misc.prune_split` uses ISO weeks (`%G-%V`). But it is an internal helper of a backup application, not a public API, and `borgbackup` needs compiled extensions and system libraries (OpenSSL, libacl, lz4, zstd, xxhash) to install: a heavy dependency for one helper. Its semantics also differ: **rules are not independent** (an archive kept by an earlier rule does not count towards a later rule's N, `if a.id not in kept_because`), and it keeps the oldest archive when a rule's count is not reached (`[oldest]`). It ships no `py.typed`. |

restic's `forget` has the semantics wanted but is a Go program, not a library. No other package
found applies retention rules to caller-supplied timestamps.

## Decision

`core/retention.py` holds the bucketing: `RetentionPolicy` (the five counts, each at least 1 when
given, at least one given; otherwise `UsageError`) and `apply_retention(pairs, policy)`, which
returns each Archive's verdict, newest first: the rules that keep it, or one reason per rule why
not. It is about 60 lines of logic. The grouping keys are standard-library calls
(`date()`, `isocalendar()[:2]`, `(year, month)`, `year`). The module is in the pure layer, so
`check_purity` and import-linter's AD-1 contracts cover it. `core/archive_choice.choose_prune`
applies it to `site_archives`, so the safety rules (another site's Archive, a Safety Snapshot, an
unreadable Manifest are never candidates) are unchanged.

## Cost accepted

- **Correctness risk, owned by this repository.** Calendar bucketing has known traps: the ISO
  year boundary, month ends, leap days, offsets other than UTC, ties, gaps. Each has a test in
  `tests/unit/core/test_retention.py`, whose expected sets are written out by hand from the
  calendar rather than computed by the function: a 400-day history with gaps and two Archives on
  one day, Thu 2026-12-31 and Fri 2027-01-01 in one week, 29 Feb 2028, and the promotion of Sun
  31 Aug 2025 from daily to weekly to monthly to pruned.
- **Maintenance:** about 60 lines of logic plus about 300 lines of tests, maintained with the
  prune command (`cli/prune.py`) by whoever maintains this package.
- **Divergence from restic:** none intended. Any difference found is a defect here.

## Revisit when

- a maintained (released within the last 2 years), typed or type-stub-carrying library appears
  that applies independent restic-style day/ISO-week/month/year rules to caller-supplied
  timestamps; or
- the rules grow beyond these five (hourly, `within` durations, tags), at which point a library's
  breadth is worth its weight; or
- a second family project needs the same retention rules (for example database snapshots or
  another backup tool): then extract `core/retention.py` and its tests into a family library
  (owner decision 85).
