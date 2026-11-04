"""AD-22 — ``python -m wpbackup2`` stays an invocation of the CLI.

The frozen v0.2.21 surface includes running the tool as a module. This package
exists only to carry that name; :mod:`wpbackup2.__main__` hands straight to the
console entry, ``l3io.wp.backup.cli:run_from_cli``. It is a regular package
outside the ``l3io`` namespace, so AD-27's marker rule does not apply to it.
"""
