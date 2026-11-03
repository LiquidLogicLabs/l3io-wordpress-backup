"""FR-2 — the Backup Scope's values are the Glossary's, verbatim."""

from __future__ import annotations

from l3io.wp.backup.domain.backup_scope import BackupScope


def test_the_values_are_exactly_the_glossary_set() -> None:
    assert {scope.value for scope in BackupScope} == {"files", "database", "all"}


def test_each_member_is_its_value_as_a_string() -> None:
    assert [str(scope) for scope in BackupScope] == [s.value for s in BackupScope]
