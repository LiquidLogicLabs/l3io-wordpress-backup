"""Owner decision 84 — tiered retention: which Archives each rule keeps (pure).

``core/retention.apply_retention`` over ``(created_at, name)`` pairs. Each tier
walks the Archives newest first, buckets them by UTC day, ISO week, month or
year, and keeps the newest Archive of each of the N most recent buckets that
hold one; ``--keep N`` keeps the newest N. An Archive kept by any rule
survives, with every rule that keeps it as a reason.

The expected sets below are written out by hand from the calendar, not
computed by the function under test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from l3io.wp.backup.core.retention import (
    RetentionPolicy,
    RetentionVerdict,
    apply_retention,
)
from l3io.wp.backup.domain.errors import UsageError


def _at(text: str) -> datetime:
    """``2026-12-31`` (03:00 UTC) or ``2026-12-31T23:59:59``, as aware UTC."""
    when = datetime.fromisoformat(text if "T" in text else f"{text}T03:00:00")
    return when.replace(tzinfo=UTC)


def _name(when: datetime) -> str:
    return f"wpbak-wpdb-{when:%Y%m%dT%H%M%SZ}.tar.gz"


def _days(first: str, last: str) -> list[datetime]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return [
        _at((start + timedelta(days=n)).isoformat())
        for n in range((end - start).days + 1)
    ]


def _history() -> list[tuple[datetime, str]]:
    """400 days, 2025-12-02 to 2027-01-05, nightly at 03:00 UTC, with:

    - gaps: no Archive on Sat 2027-01-02 and Sun 2027-01-03; none in ISO week
      2026-W51 (Mon 12-14 to Sun 12-20); none in September 2026;
    - two on one day: 2027-01-05 also at 15:00, 2026-12-31 also at 23:59:59;
    - the ISO-year boundary inside one week: Thu 2026-12-31 and Fri 2027-01-01
      are both 2026-W53.
    """
    times = [
        t
        for t in _days("2025-12-02", "2027-01-05")
        if t.date() not in (date(2027, 1, 2), date(2027, 1, 3))
        and not date(2026, 12, 14) <= t.date() <= date(2026, 12, 20)
        and not (t.year == 2026 and t.month == 9)
    ]
    times += [_at("2027-01-05T15:00:00"), _at("2026-12-31T23:59:59")]
    return [(t, _name(t)) for t in times]


def _kept(verdicts: tuple[RetentionVerdict, ...]) -> dict[str, tuple[str, ...]]:
    """Kept Archives as ``{created: reasons}``, created as ``_at`` spells it."""
    return {
        f"{v.created:%Y-%m-%dT%H:%M:%S}".removesuffix("T03:00:00"): v.kept_by
        for v in verdicts
        if v.kept_by
    }


D, W, M, Y = "daily", "weekly", "monthly", "yearly"

#: --keep-daily 7: the 7 newest days that hold an Archive (01-02, 01-03 hold
#: none), the newest of each (15:00 on 01-05, 23:59:59 on 12-31).
DAILY_7 = {
    "2027-01-05T15:00:00": (D,),
    "2027-01-04": (D,),
    "2027-01-01": (D,),
    "2026-12-31T23:59:59": (D,),
    "2026-12-30": (D,),
    "2026-12-29": (D,),
    "2026-12-28": (D,),
}
#: --keep-weekly 4 (ISO weeks): 2027-W01 (Tue 01-05), 2026-W53 (Mon 12-28 to
#: Sun 01-03: its newest is Fri 2027-01-01), 2026-W52 (Sun 12-27); 2026-W51
#: holds none, so the fourth is 2026-W50 (Sun 12-13).
WEEKLY_4 = {
    "2027-01-05T15:00:00": (W,),
    "2027-01-01": (W,),
    "2026-12-27": (W,),
    "2026-12-13": (W,),
}
#: --keep-monthly 6: Jan 2027, Dec, Nov, Oct 2026; September holds none; Aug, Jul.
MONTHLY_6 = {
    "2027-01-05T15:00:00": (M,),
    "2026-12-31T23:59:59": (M,),
    "2026-11-30": (M,),
    "2026-10-31": (M,),
    "2026-08-31": (M,),
    "2026-07-31": (M,),
}
#: --keep-yearly 2: the newest of 2027 and of 2026.
YEARLY_2 = {"2027-01-05T15:00:00": (Y,), "2026-12-31T23:59:59": (Y,)}


def _union(*tiers: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for tier in tiers:
        for created, reasons in tier.items():
            out[created] = out.get(created, ()) + reasons
    return out


def test_the_history_is_400_days_long() -> None:
    times = sorted(t for t, _ in _history())
    assert (times[-1].date() - times[0].date()).days + 1 == 400


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (RetentionPolicy(keep_daily=7), DAILY_7),
        (RetentionPolicy(keep_weekly=4), WEEKLY_4),
        (RetentionPolicy(keep_monthly=6), MONTHLY_6),
        (RetentionPolicy(keep_yearly=2), YEARLY_2),
    ],
    ids=["daily-7", "weekly-4", "monthly-6", "yearly-2"],
)
def test_each_tier_keeps_exactly_the_newest_of_its_last_n_buckets(
    policy: RetentionPolicy, expected: dict[str, tuple[str, ...]]
) -> None:
    assert _kept(apply_retention(_history(), policy)) == expected


def test_seven_four_six_keeps_the_union_with_every_reason() -> None:
    verdicts = apply_retention(
        _history(), RetentionPolicy(keep_daily=7, keep_weekly=4, keep_monthly=6)
    )
    kept = _kept(verdicts)
    assert kept == _union(DAILY_7, WEEKLY_4, MONTHLY_6)
    assert len(kept) == 13
    assert kept["2027-01-05T15:00:00"] == (D, W, M)
    assert kept["2027-01-01"] == (D, W)
    assert kept["2026-12-31T23:59:59"] == (D, M)
    assert len(verdicts) == len(_history())
    # Newest first, as the CLI lists them.
    assert [v.created for v in verdicts] == sorted(
        (v.created for v in verdicts), reverse=True
    )


def test_seven_four_six_and_two_yearly() -> None:
    policy = RetentionPolicy(keep_daily=7, keep_weekly=4, keep_monthly=6, keep_yearly=2)
    kept = _kept(apply_retention(_history(), policy))
    assert kept == _union(DAILY_7, WEEKLY_4, MONTHLY_6, YEARLY_2)
    assert kept["2026-12-31T23:59:59"] == (D, M, Y)


def test_keep_n_combines_with_the_tiers() -> None:
    policy = RetentionPolicy(keep=3, keep_daily=7, keep_weekly=4, keep_monthly=6)
    kept = _kept(apply_retention(_history(), policy))
    # The newest 3: both of 01-05 and 01-04; 03:00 on 01-05 is kept by --keep only.
    assert kept == _union(
        {
            "2027-01-05T15:00:00": ("newest",),
            "2027-01-05": ("newest",),
            "2027-01-04": ("newest",),
        },
        DAILY_7,
        WEEKLY_4,
        MONTHLY_6,
    )
    assert kept["2027-01-05"] == ("newest",)
    assert kept["2027-01-05T15:00:00"] == ("newest", D, W, M)


def test_keep_alone_is_the_newest_n() -> None:
    kept = _kept(apply_retention(_history(), RetentionPolicy(keep=2)))
    assert kept == {"2027-01-05T15:00:00": ("newest",), "2027-01-05": ("newest",)}


def test_the_iso_week_spans_the_year_boundary() -> None:
    """Thu 2026-12-31 and Fri 2027-01-01 are one week (2026-W53): one is kept."""
    pairs = [(t, _name(t)) for t in (_at("2026-12-31"), _at("2027-01-01"))]
    kept = _kept(apply_retention(pairs, RetentionPolicy(keep_weekly=2)))
    assert kept == {"2027-01-01": (W,)}


def test_month_ends_and_the_leap_day() -> None:
    """28 Feb, 29 Feb 2028, 1 Mar: the leap day is February's newest."""
    pairs = [(t, _name(t)) for t in _days("2028-02-27", "2028-03-01")]
    verdicts = apply_retention(pairs, RetentionPolicy(keep_monthly=2, keep_daily=1))
    assert _kept(verdicts) == {"2028-03-01": (D, M), "2028-02-29": (M,)}


def test_the_promotion_of_sunday_31_august() -> None:
    """A nightly series: Sun 2025-08-31 is kept as a daily, then as the newest
    of its ISO week (2025-W35) and of August, then as August's only."""
    policy = RetentionPolicy(keep_daily=7, keep_weekly=4, keep_monthly=6)
    sunday = _at("2025-08-31")

    def reasons_on(last: str) -> tuple[str, ...]:
        pairs = [(t, _name(t)) for t in _days("2025-08-01", last)]
        (verdict,) = [v for v in apply_retention(pairs, policy) if v.created == sunday]
        return verdict.kept_by

    assert reasons_on("2025-09-06") == (D, W, M)  # a daily of the last 7 days
    assert reasons_on("2025-09-07") == (W, M)  # 8 days on: weekly and monthly
    assert reasons_on("2025-09-21") == (W, M)  # W35 is still 1 of the last 4
    assert reasons_on("2025-09-22") == (M,)  # W39 begins: kept as monthly only
    assert reasons_on("2026-01-31") == (M,)  # Aug is the 6th month with one
    assert reasons_on("2026-02-01") == ()  # Feb is the 7th: pruned


def test_a_pruned_archive_says_why_for_each_rule() -> None:
    verdicts = apply_retention(
        _history(), RetentionPolicy(keep=1, keep_daily=7, keep_monthly=6)
    )
    by_created = {v.created: v for v in verdicts}
    early = by_created[_at("2026-12-31")]  # 03:00, beaten by 23:59:59 that day
    assert early.kept_by == ()
    assert early.pruned_because == (
        "newest: not among the newest 1",
        "daily: not the newest of its day",
        "monthly: not the newest of its month",
    )
    old = by_created[_at("2026-06-30")]
    assert old.pruned_because == (
        "newest: not among the newest 1",
        "daily: older than the newest 7 days that have an archive",
        "monthly: older than the newest 6 months that have an archive",
    )
    assert by_created[_at("2026-12-30")].pruned_because == ()


@pytest.mark.parametrize(
    "policy",
    [
        RetentionPolicy(keep=1),
        RetentionPolicy(keep_daily=1),
        RetentionPolicy(keep_weekly=1),
        RetentionPolicy(keep_monthly=1),
        RetentionPolicy(keep_yearly=1),
    ],
)
def test_the_newest_is_always_kept(policy: RetentionPolicy) -> None:
    verdicts = apply_retention(_history(), policy)
    assert verdicts[0].created == _at("2027-01-05T15:00:00")
    assert verdicts[0].kept_by
    assert sum(1 for v in verdicts if v.kept_by) == 1


def test_ties_keep_the_later_file_name() -> None:
    same = _at("2026-11-02")
    verdicts = apply_retention(
        [(same, "a.tar.gz"), (same, "b.tar.gz")], RetentionPolicy(keep_daily=5)
    )
    assert [(v.name, v.kept_by) for v in verdicts] == [
        ("b.tar.gz", (D,)),
        ("a.tar.gz", ()),
    ]


def test_buckets_are_utc_whatever_the_offset_given() -> None:
    """23:30 at -05:00 on 1 Nov is 04:30 UTC on 2 Nov: the same UTC day."""
    est = timezone(timedelta(hours=-5))
    pairs = [
        (datetime(2026, 11, 1, 23, 30, tzinfo=est), "late.tar.gz"),
        (_at("2026-11-02T01:00:00"), "early.tar.gz"),
    ]
    verdicts = apply_retention(pairs, RetentionPolicy(keep_daily=5))
    assert [(v.name, v.kept_by) for v in verdicts] == [
        ("late.tar.gz", (D,)),
        ("early.tar.gz", ()),
    ]


def test_no_archives_is_no_error_and_a_naive_time_is_refused() -> None:
    assert apply_retention([], RetentionPolicy(keep_daily=1)) == ()
    with pytest.raises(ValueError, match="UTC offset"):
        apply_retention(
            [(datetime(2026, 1, 1), "naive.tar.gz")],
            RetentionPolicy(keep_daily=1),
        )


def test_at_least_one_rule_is_required() -> None:
    with pytest.raises(UsageError) as caught:
        RetentionPolicy()
    assert caught.value.arguments == (
        "keep",
        "keep_daily",
        "keep_weekly",
        "keep_monthly",
        "keep_yearly",
    )
    assert "--keep-daily" in str(caught.value)


@pytest.mark.parametrize(
    "field", ["keep", "keep_daily", "keep_weekly", "keep_monthly", "keep_yearly"]
)
@pytest.mark.parametrize("value", [0, -1, True])
def test_each_rule_is_at_least_one(field: str, value: int) -> None:
    with pytest.raises(UsageError) as caught:
        RetentionPolicy(**{field: value})  # type: ignore[arg-type]
    assert caught.value.arguments == (field,)


def test_a_policy_describes_its_rules_as_flags_and_settings() -> None:
    policy = RetentionPolicy(keep_daily=7, keep_weekly=4, keep_monthly=6)
    assert policy.describe() == "--keep-daily 7 --keep-weekly 4 --keep-monthly 6"
    assert policy.settings() == {"keep_daily": 7, "keep_weekly": 4, "keep_monthly": 6}
