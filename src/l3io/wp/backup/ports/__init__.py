"""Protocol definitions — the interfaces through which effects are reached.

Every port splits into queries and commands (AD-4, AG-12): each capability is
two ``typing.Protocol`` classes named ``<Capability>Query`` and
``<Capability>Command``. A query never changes the Site, the database or a
Storage Target; a command may. A Dry Run binds the real queries and the no-op
commands of ``adapters/noop.py`` rather than branching on a flag.

Single-role allowance — the Protocols that are neither, each with its reason:

- ``ArchiveProbe``: a query; it reads an Archive and writes nothing.
- ``Clock``: a query; it tells the time.
- ``Preflight``: a query; it looks for tools and changes nothing (AD-13).
- ``PayloadCipher``: a transform from a reader to a writer (ADR-0024).
- ``ExclusionPolicy``: a pure query over paths it is handed.
- ``Tracer``: observation; it records spans and changes nothing (AD-24).
- ``RunSpan``: observation; the root span a ``Tracer`` opens.
- ``PhaseSpan``: observation; a phase span a ``Tracer`` opens.
- ``Redactor``: observation; what a span needs from the Run's registry.
- ``StagedArchive``: what ``ArchiveStoreCommand.stage`` returns; reached
  only through that command.
- ``SiteLock``: command-only Run machinery (AD-19); a Dry Run takes the lock
  for real, so it has no no-op.
- ``LockHandle``: what ``SiteLock.acquire`` returns; reached only through it.

``tests/contracts/test_port_split.py`` reads this list: every Protocol under
``ports/`` is a ``*Query``, a ``*Command`` or named here, and every name here
is a Protocol that exists.
"""

from __future__ import annotations

__all__: list[str] = []
