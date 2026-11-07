"""An opaque secret value (owner decision 2026-11-07, E006 closure decision 4).

:class:`Secret` holds a password or passphrase so that ``repr``, ``str``,
logging and an accidental ``==`` against text never show it. ``reveal()``
returns the text; it is called only where the value is handed to
``l3io-wordpress-database`` or the ``PassphraseCipher`` (and where the
``SecretRegistry`` learns it), which ``tests/contracts/test_secret_reveal.py``
enforces. A ``Secret`` is not equal to, nor hashable as, its plain string; two
compare in constant time. Pickling one raises ``TypeError``: the value never
leaves the process that read it.
"""

from __future__ import annotations

import hmac
from typing import NoReturn

__all__ = ["Secret", "as_secret"]


class Secret:
    """A secret string that never shows itself; ``reveal()`` returns it."""

    __slots__ = ("_value",)
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, value: str | Secret) -> None:
        text: object = value.reveal() if isinstance(value, Secret) else value
        if not isinstance(text, str):
            msg = f"a Secret holds a str, not {type(text).__name__}"
            raise TypeError(msg)
        self._value = text

    def reveal(self) -> str:
        """The secret text. Call it only where the value leaves the package."""
        return self._value

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return hmac.compare_digest(
            self._value.encode("utf-8", "surrogatepass"),
            other._value.encode("utf-8", "surrogatepass"),
        )

    def __reduce__(self) -> NoReturn:
        msg = "a Secret is never pickled; pass the text and wrap it again"
        raise TypeError(msg)


def as_secret(value: str | Secret) -> Secret:
    """``value`` as a :class:`Secret`; a ``Secret`` is returned unchanged."""
    return value if isinstance(value, Secret) else Secret(value)
