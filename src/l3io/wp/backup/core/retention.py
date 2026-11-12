"""Owner decision 84, ADR-0029 — tiered retention: which Archives each rule keeps.

**The rules.** ``keep`` keeps the newest N. ``keep_daily``, ``keep_weekly``,
``keep_monthly`` and ``keep_yearly`` each walk the Archives newest first,
group them by UTC day, ISO week (``isocalendar()``: a week that spans 31
December is one week), month or year, and keep the newest Archive of each of
the N most recent groups that hold one; a group with no Archive does not
count. The rules are independent (restic's ``forget``, not borg's ``prune``,
where an Archive kept by one rule does not count towards the next): an
Archive kept by any rule survives, and carries every rule that keeps it, so a
daily that is also the last of its week and month is kept by all three and
stays when the daily window passes (the "promotion"; nothing is copied).

**The newest Archive is always kept**: every rule is at least 1, and the
newest is the newest of every rule's first group.

**Order.** Newest first by ``created_at`` (UTC); a tie by the later name, as
``core/archive_choice.site_archives`` orders them.

Pure, so ``check_purity`` covers it; ADR-0029 records why it is not a library.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final, Literal

from l3io.wp.backup.domain.errors import UsageError

__all__ = [
    "RULES",
    "RetentionPolicy",
    "RetentionVerdict",
    "Rule",
    "apply_retention",
]

#: A retention rule, as an Archive's reason names it.
Rule = Literal["newest", "daily", "weekly", "monthly", "yearly"]
#: The rules in the order reasons are listed.
RULES: Final[tuple[Rule, ...]] = ("newest", "daily", "weekly", "monthly", "yearly")
#: Each rule's setting (its flag is ``--`` + the name with dashes).
_SETTING: Final[Mapping[Rule, str]] = MappingProxyType(
    {
        "newest": "keep",
        "daily": "keep_daily",
        "weekly": "keep_weekly",
        "monthly": "keep_monthly",
        "yearly": "keep_yearly",
    }
)
#: What one group of a rule is called, in a reason.
_UNIT: Final[Mapping[Rule, str]] = MappingProxyType(
    {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}
)
#: The group an Archive falls in, from its UTC time and its place (newest 0).
_GROUP: Final[Mapping[Rule, Callable[[datetime, int], Hashable]]] = MappingProxyType(
    {
        "newest": lambda _when, place: place,
        "daily": lambda when, _place: when.date(),
        "weekly": lambda when, _place: when.isocalendar()[:2],
        "monthly": lambda when, _place: (when.year, when.month),
        "yearly": lambda when, _place: when.year,
    }
)


def _flag(setting: str) -> str:
    return "--" + setting.replace("_", "-")


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """The retention rules a prune applies; ``None`` is a rule not given.

    Each given rule is at least 1, and at least one is given; otherwise
    :class:`UsageError` naming the setting(s).
    """

    keep: int | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
    keep_yearly: int | None = None

    def __post_init__(self) -> None:
        """Refuse a rule below 1, and a policy with no rule."""
        for rule in RULES:
            setting = _SETTING[rule]
            count = getattr(self, setting)
            if count is None:
                continue
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                msg = (
                    f"{setting} must be a whole number of at least 1, not "
                    f"{count!r}: leave it unset to not use that rule"
                )
                raise UsageError(msg, arguments=(setting,))
        if not self.rules():
            flags = [_flag(_SETTING[rule]) for rule in RULES]
            msg = (
                "prune needs at least one retention rule: "
                f"{', '.join(flags[:-1])} or {flags[-1]} (or its "
                "L3IO_WP_BACKUP_* variable or config key)"
            )
            raise UsageError(msg, arguments=tuple(_SETTING[rule] for rule in RULES))

    def rules(self) -> tuple[tuple[Rule, int], ...]:
        """The rules given, with their counts, in :data:`RULES` order."""
        found: list[tuple[Rule, int]] = []
        for rule in RULES:
            count = getattr(self, _SETTING[rule])
            if count is not None:
                found.append((rule, count))
        return tuple(found)

    def settings(self) -> dict[str, int]:
        """The rules given, by setting name (``{"keep_daily": 7}``)."""
        return {_SETTING[rule]: count for rule, count in self.rules()}

    def describe(self) -> str:
        """The rules given, as flags: ``--keep-daily 7 --keep-weekly 4``."""
        return " ".join(f"{_flag(name)} {n}" for name, n in self.settings().items())


@dataclass(frozen=True, slots=True)
class RetentionVerdict:
    """One Archive's fate: the rules that keep it, or why each lets it go.

    ``kept_by`` is empty when no rule keeps it; ``pruned_because`` then holds
    one reason per rule given (``"daily: not the newest of its day"``), and is
    empty when it is kept. ``created`` is in UTC.
    """

    name: str
    created: datetime
    kept_by: tuple[Rule, ...]
    pruned_because: tuple[str, ...]


def _why_not(rule: Rule, count: int, *, superseded: bool) -> str:
    if rule == "newest":
        return f"newest: not among the newest {count}"
    unit = _UNIT[rule]
    if superseded:
        return f"{rule}: not the newest of its {unit}"
    return f"{rule}: older than the newest {count} {unit}s that have an archive"


def apply_retention(
    archives: Sequence[tuple[datetime, str]], policy: RetentionPolicy
) -> tuple[RetentionVerdict, ...]:
    """Each of ``archives`` (``(created_at, name)``) with its verdict, newest first.

    A ``created_at`` without a UTC offset is :class:`ValueError`.
    """
    for when, name in archives:
        if when.tzinfo is None or when.utcoffset() is None:
            msg = f"created_at of {name!r} has no UTC offset"
            raise ValueError(msg)
    ordered = sorted(
        ((when.astimezone(UTC), name) for when, name in archives),
        key=lambda item: (item[0], item[1]),
        reverse=True,
    )
    kept: list[list[Rule]] = [[] for _ in ordered]
    why: list[list[str]] = [[] for _ in ordered]
    for rule, count in policy.rules():
        group_of = _GROUP[rule]
        seen = 0
        last: Hashable = None
        for place, (when, _name) in enumerate(ordered):
            group = group_of(when, place)
            superseded = place > 0 and group == last
            if not superseded:
                seen += 1
                last = group
            if not superseded and seen <= count:
                kept[place].append(rule)
            else:
                why[place].append(_why_not(rule, count, superseded=seen <= count))
    return tuple(
        RetentionVerdict(
            name=name,
            created=when,
            kept_by=tuple(kept[place]),
            pruned_because=() if kept[place] else tuple(why[place]),
        )
        for place, (when, name) in enumerate(ordered)
    )
