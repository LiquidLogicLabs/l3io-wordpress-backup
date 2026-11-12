# Backup exclusions (FR-5)

A files backup (`--mode files` or `--mode all`) can leave paths under the Site
Root out of the Archive. Exclusions only subtract: they are evaluated against
entries the traversal already found inside the Site Root, so no pattern can add
anything to an Archive. They apply to Site Root paths only — never to database
tables, and never to a `wp-config.php` kept outside the Site Root.

Every Archive's Manifest records the active set under `exclusions`: its
`source` (`default`, `custom` or `none`), its `patterns` in order, and each
excluded path with the pattern that excluded it. A directory that is excluded
is recorded once and not descended into; its contents are not listed.

## The default set

Applied whenever no exclusion is asked for: `wpbackup2 backup` with no
`--exclude`/`--no-default-excludes`, and the library `backup(exclusion=None)`,
both through `cli/backup.py::default_exclusion_policy()`
(`tests/contracts/test_default_exclusion_reach.py` fails if that stops being
this set). `--list-default-excludes` prints it. The Safety Snapshot a restore
takes is the exception: it excludes nothing. The source of truth is
`DEFAULT_EXCLUSIONS` in `src/l3io/wp/backup/core/exclusions.py`;
`tests/contracts/test_exclusions_documented.py` fails if this table and the
constant differ in content or order. It is deliberately conservative: nothing
under `wp-content/uploads/`, `wp-content/plugins/` or `wp-content/themes/` is
matched.

<!-- BEGIN CHECKED: default-exclusions (hand-written; test_exclusions_documented.py compares it with core/exclusions.py DEFAULT_EXCLUSIONS) -->
| Pattern | Why it is left out |
|---|---|
| `wp-content/cache/` | Object and page caches; rebuilt by the Site. |
| `wp-content/upgrade/` | Core's update scratch directory. |
| `wp-content/upgrade-temp-backup/` | Core's rollback copies from an update. |
| `wp-content/debug.log` | WordPress's own debug log. |
<!-- END CHECKED: default-exclusions -->

The set is enumerable at runtime (`enumerate_defaults()`) and replaceable or
extendable: an operator's patterns follow the defaults, or replace them when
the defaults are switched off.

**Always left out, whatever the flags say:** the Archive being written, earlier
Archives under the default name (`wpbak-*.tar.gz`, and the name before 1.1.0,
`wpbackup2-*.tar.gz`) and staged files a killed Run left
(`.wpbak-*.tar.gz.*.partial`, `.wpbackup2-*.tar.gz.*.partial`), anywhere under
the Site Root. A backup run from the docroot, where the default target lands, would
otherwise hold every earlier Archive and double in size each run
(BL-E002-100; `tests/unit/cli/test_archive_not_collected.py`). They are
recorded in the Manifest's excluded paths like any other exclusion.

## `wp-config.php` is never excluded

No pattern — default or custom, including `*`, `**`, `*.php` and
`/wp-config.php` — removes `wp-config.php` from an Archive: an Archive without
it cannot be restored to a working Site. When a pattern would have matched it,
the backup logs a WARNING `protected_path_kept` naming the pattern and keeps the
file. The protected paths are `PROTECTED_PATHS` in the same module; a directory
holding a protected path is never pruned either. The match is
case-insensitive (owner decision 2026-11-07, BL-E004-063): `WP-CONFIG.PHP` is
the file WordPress reads on a case-insensitive filesystem, so no spelling of it
is excluded.

## Pattern syntax

Patterns are [gitignore](https://git-scm.com/docs/gitignore) patterns, matched
with `pathspec`'s `GitIgnoreSpec`, against paths relative to the Site Root:

- a pattern containing a `/` other than a trailing one is anchored at the Site
  Root (`wp-content/debug.log`); a leading `/` anchors explicitly;
  a pattern with no `/` matches at any depth (`*.log`);
- a trailing `/` matches directories only (`wp-content/cache/`);
- `*` does not cross `/`; `**` does (`**/node_modules/`);
- `!pattern` re-includes what an earlier pattern excluded — later patterns win,
  so an operator's `!wp-content/debug.log` re-includes that default. As in git,
  nothing inside an excluded directory can be re-included;
- matching is case-sensitive and `/`-separated, and separators are never
  translated. A backslash escapes the next character, as in gitignore, so a
  Windows-style `wp-content\cache` means `wp-contentcache` and does not match
  `wp-content/cache`; `\#private/` matches a directory named `#private`;
- a symlink is matched by its own path, never its target.

An empty or whitespace-only pattern, one spanning lines, one gitignore
rejects (such as a lone `!`), or one that would match nothing (a `#` comment,
a lone `/`, an unclosed `[`) fails the backup before anything is read, with
exit code `USAGE`.
