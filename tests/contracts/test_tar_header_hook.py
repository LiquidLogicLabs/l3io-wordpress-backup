"""RT-2 contract — ``_BoundedTarInfo``'s refusal is reached on this interpreter.

The bound on a tar extended-header body (``adapters/tar.py``) lives in a
``tarfile.TarInfo`` hook, and which hooks ``tarfile`` calls is a CPython
implementation detail: 3.13+ stopped calling ``frombuf`` (the first home of the
check), and nothing failed on 3.11. So this test reads, through every mode the
readers use, an Archive tar written by ``tarfile`` itself whose extended header
is one byte over the bound — at the top level and as the header that follows
another GNU header — and requires the hook's own refusal. A planted bypass
(``_proc_member`` routed around the check) must fail the same assertion, and so
must the pre-fix ``frombuf`` hook wherever this interpreter no longer calls
``frombuf``.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable

import pytest

from l3io.wp.backup.adapters import tar as tar_module

BoundedTarInfo = tar_module._BoundedTarInfo  # noqa: SLF001
OVER = tar_module._MAX_EXTENDED_HEADER + 1  # noqa: SLF001
REFUSAL = f"a tar extended header declares over {OVER - 1} bytes"
#: The modes the Archive readers open a tar with (the gzip layer is irrelevant
#: to which TarInfo hook runs, so plain tar keeps the bodies cheap).
MODES = ("r|", "r:")


def _write(member: tarfile.TarInfo, tar_format: int, **kwargs: object) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tar_format, **kwargs) as tar:  # type: ignore[call-overload]
        tar.addfile(member)
    return out.getvalue()


def _gnu_long_name() -> bytes:  # an ``L`` header
    return _write(tarfile.TarInfo("n" * OVER), tarfile.GNU_FORMAT)


def _gnu_long_link() -> bytes:  # a ``K`` header
    info = tarfile.TarInfo("link")
    info.type = tarfile.SYMTYPE
    info.linkname = "t" * OVER
    return _write(info, tarfile.GNU_FORMAT)


def _follow_up_long_name() -> bytes:  # a small ``K``, then the oversized ``L``
    info = tarfile.TarInfo("n" * OVER)
    info.type = tarfile.SYMTYPE
    info.linkname = "t" * 200
    return _write(info, tarfile.GNU_FORMAT)


def _pax_extended() -> bytes:  # an ``x`` header
    info = tarfile.TarInfo("member")
    info.pax_headers = {"comment": "c" * OVER}
    return _write(info, tarfile.PAX_FORMAT)


def _pax_global() -> bytes:  # a ``g`` header
    return _write(
        tarfile.TarInfo("member"),
        tarfile.PAX_FORMAT,
        pax_headers={"comment": "c" * OVER},
    )


ARCHIVES: dict[str, Callable[[], bytes]] = {
    "gnu-L": _gnu_long_name,
    "gnu-K": _gnu_long_link,
    "gnu-K-then-L": _follow_up_long_name,
    "pax-x": _pax_extended,
    "pax-g": _pax_global,
}


def _refusal(tarinfo: type[tarfile.TarInfo], data: bytes, mode: str) -> str | None:
    """The ``ReadError`` reading ``data`` raised, or ``None`` if it read whole."""
    try:
        with tarfile.open(
            fileobj=io.BytesIO(data),
            mode=mode,  # type: ignore[call-overload]
            tarinfo=tarinfo,
        ) as tar:
            tar.getmembers()
    except tarfile.ReadError as exc:
        return str(exc)
    return None


def assert_hook_reached(tarinfo: type[tarfile.TarInfo]) -> None:
    """Every oversized extended header, in every mode, gets the hook's refusal."""
    for name, build in ARCHIVES.items():
        data = build()
        for mode in MODES:
            got = _refusal(tarinfo, data, mode)
            assert got == REFUSAL, f"{name} via {mode!r}: {got!r}"


def test_the_bound_is_reached_on_this_interpreter() -> None:
    assert_hook_reached(BoundedTarInfo)


def test_the_archives_are_readable_without_the_bound() -> None:
    """The headers are well formed: plain ``TarInfo`` reads every one whole."""
    for build in ARCHIVES.values():
        for mode in MODES:
            assert _refusal(tarfile.TarInfo, build(), mode) is None


class _Bypassed(BoundedTarInfo):
    """Planted: ``tarfile``'s dispatch reaches the body without the check."""

    def _proc_member(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        member: tarfile.TarInfo = tarfile.TarInfo._proc_member(self, archive)  # type: ignore[attr-defined]  # noqa: SLF001
        return member


class _FrombufOnly(tarfile.TarInfo):
    """The pre-fix hook (b424f7c): the check in ``frombuf``."""

    @classmethod
    def frombuf(
        cls, buf: bytes | bytearray, encoding: str, errors: str
    ) -> _FrombufOnly:
        info = super().frombuf(buf, encoding, errors)
        if info.type in tar_module._EXTENDED_HEADER_TYPES and info.size > OVER - 1:  # noqa: SLF001
            raise tarfile.ReadError(REFUSAL)
        return info


def _frombuf_is_called() -> bool:
    """Whether this interpreter's ``tarfile`` still parses headers via ``frombuf``."""
    seen: list[bool] = []

    class _Spy(tarfile.TarInfo):
        @classmethod
        def frombuf(cls, buf: bytes | bytearray, encoding: str, errors: str) -> _Spy:
            seen.append(True)
            return super().frombuf(buf, encoding, errors)

    _refusal(_Spy, _write(tarfile.TarInfo("member"), tarfile.USTAR_FORMAT), "r|")
    return bool(seen)


def test_a_planted_bypass_fails_the_contract() -> None:
    with pytest.raises(AssertionError, match="gnu-L"):
        assert_hook_reached(_Bypassed)


def test_the_pre_fix_hook_fails_the_contract_where_frombuf_is_not_called() -> None:
    if _frombuf_is_called():
        assert_hook_reached(_FrombufOnly)
    else:
        with pytest.raises(AssertionError, match="gnu-L"):
            assert_hook_reached(_FrombufOnly)
