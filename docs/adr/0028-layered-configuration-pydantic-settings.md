# ADR-0028: Layered configuration (flag, variable, TOML file) through pydantic-settings

- **Status:** Proposed
- **Date:** 2026-11-08
- **Deciders:** Shawn Anderson (review pending)
- **Epic:** close-out (automation-first change set, step 1)
- **Resolves:** owner decision 60, "One setting, three spellings" and "Config file"
- **Principle(s) in tension:** global rule 1 (never hand-roll layered configuration or schema
  validation), AD-22 (the CLI surface is frozen: argparse stays the parser), AD-8 (a library
  caller loads nothing it does not use)

## Context

Owner decision 60: every setting a Run needs has a CLI flag, an `L3IO_WP_BACKUP_*`
environment variable and a config-file key, with precedence **command line, then environment,
then config file, then defaults**. A config file holding a secret must not be group- or
world-readable. The format and the library are chosen here, and a contract test derives the
flag, variable and key mapping from one source.

Before this, a handful of settings had a variable (`PASSPHRASE`, `DB_USER`/`DB_PASSWORD`,
`ADMIN_USER`/`ADMIN_PASSWORD`, `NEW_DB_PASSWORD`), read where each was used, and no setting
could come from a file. The CLI is argparse with four subcommands; AD-22 freezes its spellings
and `tests/contracts/test_cli_surface.py` checks them against the legacy parser.

Constraints the choice must meet, each checkable:

1. Keep argparse as the parser (AD-22's frozen surface, `CliParser`'s exit-2 messages, the
   restore flag groups AD-25's table is derived from).
2. Settings live on subcommands (`--wp-dir` on `backup`, `restore`, `doctor`); one config file
   serves all of them.
3. A value read from a file or a variable must never be echoed in an error, because some are
   secrets.
4. `mypy --strict` passes over `src/` with no new `ignore_missing_imports`.

## Options considered

| Option | Version evaluated | Pros | Cons |
|---|---|---|---|
| **A. pydantic-settings** | 2.16.2 (2026-10-28), pydantic 2.14.3 | Typed (`py.typed`), a source order we set (`settings_customise_sources`), an environment source with a prefix, a TOML source on stdlib `tomllib`, `SecretStr`, `extra="forbid"` for unknown keys, errors that can be rendered without their input | Brings pydantic and pydantic-core (a compiled extension, wheels for CPython 3.11 to 3.14 on the platforms we support), typing-inspection, annotated-types and python-dotenv (required by pydantic-settings, unused here); it does not parse the command line our way, so argparse stays and supplies the top layer |
| B. ConfigArgParse | 1.9.0 (2026-10-27) | Built for exactly this: a drop-in argparse subclass with `env_var=`/`auto_env_var_prefix=` and config files, the same precedence; pure Python, no dependencies | Read in 1.9.0's `parse_known_args`: a config file's keys are matched against **the parsing parser's own actions** (`known_config_keys` from `self._actions`), so a shared file holding `wp_dir` is an unknown key to the top-level parser, and a top-level `--config` does not reach the subparsers (fails constraint 2). Values from a file or a variable are spliced into the argument list as `--key value`; an unknown key becomes `--key value` and argparse's "unrecognized arguments" error prints it, value included (fails constraint 3 for a mistyped secret key). No `py.typed` (fails constraint 4 without an override) |
| C. Dynaconf | 3.2.x | Layered settings, many formats | No argparse integration: the command-line layer would be ours to merge, and it is a general settings framework far larger than the problem; untyped access (`settings.WP_DIR`) |
| D. argparse + `tomllib` + `os.environ`, merged by hand | stdlib | No dependency | Hand-rolled layering, coercion (booleans and lists from strings) and validation: the domains global rule 1 forbids. Rejected without an ADR exception |

## Decision (proposed)

**A, pydantic-settings, with argparse as the top layer.**

- `cli/settings.py::Settings` (a `BaseSettings`) is the **one source** of what the settings
  are. A field's name is its argparse `dest` and its config-file key; its variable is
  `L3IO_WP_BACKUP_` + the upper-cased name; its flag is `--` + the name with dashes. Every
  field is `None` unless a variable or a file sets it: the defaults stay the parser's, in one
  place.
- `parse_layered` parses the command line with argparse, every setting's default suppressed so
  only what was typed is present; pydantic-settings layers the environment over the config
  file(s); the parser's default fills the rest. A value from a variable or a file goes through
  the action's own `type` and `choices`, so each setting is validated once, the same way
  whichever spelling gave it. An option declared `required=True` is required after layering,
  with argparse's own message.
- **Files.** `--config PATH` or `L3IO_WP_BACKUP_CONFIG` names one file, which must exist.
  Otherwise `/etc/l3io-wp-backup/config.toml` and then
  `~/.config/l3io-wp-backup/config.toml` (platformdirs' per-user config directory, the same
  `l3io-wp-backup` name as the state directory) are read when present, the user's keys over the
  system's. Format: TOML, flat keys (`wp_dir = "/var/www/html"`).
- **Refusals, each before any Run opens and each naming the file or the variable, never a
  value:** a file holding a secret key (`passphrase`, `db_password`, `admin_password`,
  `new_db_password`, the `SecretStr` fields) that its group or others can read; an unknown key;
  a value that does not validate. An `L3IO_WP_BACKUP_*` variable no setting has is logged as
  WARNING `unknown_setting_variable`.
- **Secrets** are read as `SecretStr`, handed on as the package's `Secret`, and registered with
  the Run's `SecretRegistry` before anything else runs.
- **Scope.** Only the CLI root imports `cli/settings.py`; `import l3io.wp.backup.api` loads no
  pydantic.

`tests/contracts/test_settings_spellings.py` derives the mapping from `Settings` and the parser:
every field has a flag on some command and is read from its variable and its key; every parser
option is a field or is listed, with a reason, in `NOT_SETTINGS`; every `L3IO_WP_BACKUP_*` name
in the source is a field's variable or `L3IO_WP_BACKUP_CONFIG`. Planted violations attack each
scope.

## Consequences

- Four new runtime dependencies (pydantic-settings, pydantic, pydantic-core, typing-inspection,
  plus python-dotenv and annotated-types). pydantic-core is compiled; a platform without a wheel
  cannot install the package from source without a Rust toolchain. Owned by this package's
  maintainers; `uv.lock` pins them.
- pydantic's environment source matches variable names case-insensitively, so
  `l3io_wp_backup_wp_dir` is read too. Harmless, and documented.
- A list setting (`exclude`) is a JSON list in a variable (`["a","b"]`) and a TOML array in a
  file.
- The variables the credential chains already read (`DB_USER`/`DB_PASSWORD`, `ADMIN_USER`/
  `ADMIN_PASSWORD`, `NEW_DB_PASSWORD`, `PASSPHRASE`) keep their meaning: a value from its own
  variable is left to the chain, which reads it there and labels it `environment`; the same
  setting typed or from the file becomes the chain's explicit step.

## Revisit when

- pydantic-settings drops the TOML source or `settings_customise_sources`, or pydantic-core stops
  shipping a wheel for a supported CPython; or
- ConfigArgParse matches config keys against subparsers' actions and stops splicing values into
  the argument list, at which point it is the lighter fit; or
- the CLI moves off argparse.
