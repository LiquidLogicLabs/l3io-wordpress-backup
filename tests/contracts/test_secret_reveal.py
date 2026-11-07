"""Owner decision 2026-11-07 (E006 closure decision 4) -- ``Secret`` stays opaque.

* ``.reveal()`` is called only in :data:`ALLOWED`: where the value is handed
  to ``l3io-wordpress-database`` (``adapters/credential_providers.py``), to the
  ``PassphraseCipher`` (``adapters/crypto.py``), to the ``SecretRegistry``
  (``adapters/redaction.py``), and ``domain/secret.py`` itself. The scope is
  every module under ``src/l3io/wp/backup/`` (``rglob``, scope asserted); a
  planted call elsewhere is reported.
* Every public credential field and parameter -- found from the type hints of
  ``api``'s entry points and of ``ExplicitCredentialSpec``/``RelocationFlags``,
  by name (``password``/``passphrase``), never listed by hand -- accepts
  ``Secret``.
* ``repr`` of a public value holding one shows no plaintext.

Not covered: a value revealed under another name (``getattr(s, "reveal")``).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pickle
import typing
import uuid
from pathlib import Path

import pytest

from l3io.wp.backup import api
from l3io.wp.backup.domain.secret import Secret, as_secret

PACKAGE = Path(api.__file__).resolve().parents[1]
ALLOWED = frozenset(
    {
        "adapters/credential_providers.py",
        "adapters/crypto.py",
        "adapters/redaction.py",
        "domain/secret.py",
    }
)
SECRET_WORDS = ("password", "passphrase")


def _reveals(root: Path) -> set[str]:
    found = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "reveal"
            ):
                found.add(path.relative_to(root).as_posix())
    return found


def test_the_scan_reaches_the_package() -> None:
    files = {p.relative_to(PACKAGE).as_posix() for p in PACKAGE.rglob("*.py")}
    assert files >= ALLOWED
    assert "cli/restore.py" in files
    assert _reveals(PACKAGE), "the scan found no reveal() at all"


def test_reveal_is_called_only_where_the_value_leaves() -> None:
    assert _reveals(PACKAGE) - ALLOWED == set()


def test_a_planted_reveal_is_reported(tmp_path: Path) -> None:
    (tmp_path / "cli").mkdir()
    (tmp_path / "cli" / "leak.py").write_text(
        "def f(s):\n    return s.reveal()\n", encoding="utf-8"
    )
    (tmp_path / "adapters").mkdir()
    (tmp_path / "adapters" / "crypto.py").write_text(
        "x = s.reveal()\n", encoding="utf-8"
    )
    assert _reveals(tmp_path) - ALLOWED == {"cli/leak.py"}


def _secret_slots() -> list[tuple[str, object]]:
    """``(owner.name, annotation)`` for each public password/passphrase slot."""
    slots: list[tuple[str, object]] = []
    for name in ("backup", "restore", "verify"):
        fn = getattr(api, name)
        hints = typing.get_type_hints(inspect.unwrap(fn))
        slots += [(f"{name}.{p}", t) for p, t in hints.items() if p in SECRET_WORDS]
    for cls in (api.ExplicitCredentialSpec, api.RelocationFlags):
        hints = typing.get_type_hints(cls)
        slots += [
            (f"{cls.__name__}.{f.name}", hints[f.name])
            for f in dataclasses.fields(cls)
            if any(word in f.name for word in SECRET_WORDS)
        ]
    return slots


def test_every_public_secret_slot_accepts_a_secret() -> None:
    slots = _secret_slots()
    assert len(slots) >= 5, slots  # three passphrases, two passwords
    refused = [n for n, t in slots if Secret not in typing.get_args(t)]
    assert refused == []


def test_a_str_is_wrapped_at_once_and_never_shown() -> None:
    text = f"sentinel-secret-{uuid.uuid4().hex}"
    spec = api.ExplicitCredentialSpec("u", text)
    flags = dataclasses.replace(api.RelocationFlags.none(), new_db_password=text)
    assert isinstance(spec.password, Secret)
    assert isinstance(flags.new_db_password, Secret)
    for value in (spec, flags, Secret(text), spec.password):
        assert text not in repr(value)
        assert text not in str(value)


def test_secret_semantics() -> None:
    text = f"sentinel-secret-{uuid.uuid4().hex}"
    secret = Secret(text)
    assert repr(secret) == str(secret) == "Secret('***')"
    assert secret.reveal() == text
    assert secret != text
    assert text != secret
    assert secret == Secret(text)
    assert as_secret(secret) is secret
    assert len(secret) == len(text)
    with pytest.raises(TypeError):
        hash(secret)
    with pytest.raises(TypeError, match="never pickled"):
        pickle.dumps(secret)
    with pytest.raises(TypeError):
        Secret(b"bytes")  # type: ignore[arg-type]
