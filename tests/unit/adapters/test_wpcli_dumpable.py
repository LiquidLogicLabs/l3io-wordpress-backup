"""``PR_SET_DUMPABLE`` is process-wide, so overlapping ``wp`` children share one toggle.

Two library calls in two threads each run a ``wp`` child. The flag drops when
the first starts and is restored only when the last is done (E006 closure
AR-3/RT-5); without the count the first to finish re-enabled ``/proc`` access
while the other's child still ran.
"""

# ruff: noqa: SLF001 - the unit under test is the module-private toggle
from __future__ import annotations

import threading

import pytest

from l3io.wp.backup.adapters import wpcli


class _Flag:
    """A stand-in for the kernel's flag: records each ``_set_dumpable`` call."""

    def __init__(self) -> None:
        self.value = 1
        self.calls: list[int] = []

    def set(self, value: int) -> int:
        previous = self.value
        self.calls.append(value)
        self.value = value
        return previous


@pytest.fixture
def flag(monkeypatch: pytest.MonkeyPatch) -> _Flag:
    state = _Flag()
    monkeypatch.setattr(wpcli, "_set_dumpable", state.set)
    monkeypatch.setattr(wpcli, "_DUMPABLE", wpcli._DumpableGuard())
    return state


def test_the_flag_is_restored_only_when_the_last_overlapping_child_is_done(
    flag: _Flag,
) -> None:
    first_in, second_in = threading.Event(), threading.Event()
    first_out = threading.Event()
    seen_while_second_runs: list[int] = []

    def first() -> None:
        with wpcli._unreadable_by_the_child():
            first_in.set()
            second_in.wait(5)

    def second() -> None:
        with wpcli._unreadable_by_the_child():
            second_in.set()
            first_out.wait(5)
            seen_while_second_runs.append(flag.value)

    t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
    t1.start()
    first_in.wait(5)
    t2.start()
    t1.join(5)
    first_out.set()
    t2.join(5)
    assert seen_while_second_runs == [0]
    assert flag.value == 1
    assert flag.calls == [0, 1]


def test_a_process_that_was_not_dumpable_stays_that_way(flag: _Flag) -> None:
    flag.value = 0
    with wpcli._unreadable_by_the_child():
        pass
    assert flag.value == 0
    assert flag.calls == [0]


def test_the_flag_is_restored_when_the_child_raises(flag: _Flag) -> None:
    with pytest.raises(RuntimeError), wpcli._unreadable_by_the_child():
        raise RuntimeError
    assert flag.value == 1
