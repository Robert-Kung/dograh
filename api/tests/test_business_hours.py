"""Business-hours gate tests (S-L3-PRESS0 §2.3).

Covers open/closed, cross-midnight intervals, unset schedule (fail-open, C4),
timezone boundaries, and malformed input.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from api.services.pipecat.business_hours import is_open

TPE = ZoneInfo("Asia/Taipei")
WEEKDAYS = {"tz": "Asia/Taipei", "mon": [["09:00", "18:00"]]}


def test_open_within_interval():
    # Monday 10:00 Taipei
    assert is_open(WEEKDAYS, datetime(2026, 6, 29, 10, 0, tzinfo=TPE)) is True


def test_closed_before_open():
    assert is_open(WEEKDAYS, datetime(2026, 6, 29, 8, 59, tzinfo=TPE)) is False


def test_closed_after_close_is_exclusive_end():
    # 18:00 is the exclusive end → closed exactly at 18:00
    assert is_open(WEEKDAYS, datetime(2026, 6, 29, 18, 0, tzinfo=TPE)) is False
    assert is_open(WEEKDAYS, datetime(2026, 6, 29, 17, 59, tzinfo=TPE)) is True


def test_open_at_inclusive_start():
    assert is_open(WEEKDAYS, datetime(2026, 6, 29, 9, 0, tzinfo=TPE)) is True


def test_closed_on_unscheduled_day():
    # Sunday has no entry → closed
    assert is_open(WEEKDAYS, datetime(2026, 6, 28, 12, 0, tzinfo=TPE)) is False


def test_multiple_intervals_lunch_break():
    sched = {"tz": "Asia/Taipei", "tue": [["09:00", "12:00"], ["13:00", "18:00"]]}
    tue = lambda h, m: datetime(2026, 6, 30, h, m, tzinfo=TPE)  # noqa: E731
    assert is_open(sched, tue(11, 0)) is True
    assert is_open(sched, tue(12, 30)) is False  # lunch
    assert is_open(sched, tue(14, 0)) is True


def test_cross_midnight_before_midnight():
    sched = {"tz": "Asia/Taipei", "fri": [["22:00", "02:00"]]}
    # Friday 23:00 → open (pre-midnight portion)
    assert is_open(sched, datetime(2026, 7, 3, 23, 0, tzinfo=TPE)) is True


def test_cross_midnight_spills_into_next_day():
    sched = {"tz": "Asia/Taipei", "fri": [["22:00", "02:00"]]}
    # Saturday 01:00 → open via Friday's wrapping interval
    assert is_open(sched, datetime(2026, 7, 4, 1, 0, tzinfo=TPE)) is True
    # Saturday 02:00 → closed (exclusive end)
    assert is_open(sched, datetime(2026, 7, 4, 2, 0, tzinfo=TPE)) is False
    # Saturday 03:00 → closed
    assert is_open(sched, datetime(2026, 7, 4, 3, 0, tzinfo=TPE)) is False


def test_unset_schedule_fails_open():
    now = datetime(2026, 6, 29, 3, 0, tzinfo=TPE)
    assert is_open(None, now) is True
    assert is_open({}, now) is True


def test_timezone_conversion_from_utc():
    # 02:00 UTC Monday == 10:00 Taipei Monday → open
    assert is_open(WEEKDAYS, datetime(2026, 6, 29, 2, 0, tzinfo=timezone.utc)) is True
    # 23:00 UTC Sunday == 07:00 Taipei Monday → before open
    assert is_open(WEEKDAYS, datetime(2026, 6, 28, 23, 0, tzinfo=timezone.utc)) is False


def test_naive_now_interpreted_in_schedule_tz():
    # Naive datetime treated as Taipei wall-clock
    assert is_open(WEEKDAYS, datetime(2026, 6, 29, 10, 0)) is True
    assert is_open(WEEKDAYS, datetime(2026, 6, 29, 8, 0)) is False


def test_malformed_schedule_fails_open():
    bad = {"tz": "Asia/Taipei", "mon": [["nine", "eighteen"]]}
    assert is_open(bad, datetime(2026, 6, 29, 10, 0, tzinfo=TPE)) is True


def test_bad_timezone_fails_open():
    bad = {"tz": "Not/AZone", "mon": [["09:00", "18:00"]]}
    assert is_open(bad, datetime(2026, 6, 29, 10, 0, tzinfo=TPE)) is True
