"""Missed nights: which dates to render, and how uploads are spaced (pure)."""
from datetime import date, datetime, timedelta, timezone

from pipeline.catchup import missed_dates
from pipeline.publishing.upload import publish_slot, status_for

Y = date(2026, 9, 29)


def test_normal_night_renders_yesterday_only():
    assert missed_dates(Y, {"2026-09-28"}, 3) == ["2026-09-29"]


def test_missed_nights_are_caught_up_oldest_first():
    assert missed_dates(Y, {"2026-09-26"}, 3) == ["2026-09-27", "2026-09-28", "2026-09-29"]
    assert missed_dates(Y, {"2026-09-20"}, 3) == ["2026-09-27", "2026-09-28", "2026-09-29"]


def test_already_done_and_fresh_clone():
    assert missed_dates(Y, {"2026-09-29"}, 3) == []
    assert missed_dates(Y, set(), 3) == ["2026-09-29"]
    assert missed_dates(Y, {"2026-09-26"}, 1) == ["2026-09-29"]


def test_a_gap_before_the_newest_finished_date_is_not_backfilled():
    assert missed_dates(Y, {"2026-09-25", "2026-09-28"}, 5) == ["2026-09-29"]


NOW = datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc)


def test_publish_slot_spaces_videos():
    assert publish_slot(NOW, [], 3) is None
    assert publish_slot(NOW, [NOW - timedelta(hours=5)], 3) is None
    assert publish_slot(NOW, [NOW - timedelta(hours=1)], 3) == NOW + timedelta(hours=2)
    assert publish_slot(NOW, [NOW, NOW + timedelta(hours=3)], 3) == NOW + timedelta(hours=6)
    assert publish_slot(NOW, [NOW], 0) is None


def test_only_public_uploads_get_scheduled():
    st = status_for("public", NOW + timedelta(hours=2))
    assert st["privacyStatus"] == "private" and st["publishAt"] == "2026-09-30T05:00:00Z"
    assert status_for("unlisted", NOW) == {"privacyStatus": "unlisted",
                                           "selfDeclaredMadeForKids": False}
    assert "publishAt" not in status_for("public", None)
