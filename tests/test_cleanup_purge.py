"""2026-10-28: purge_after_upload keeps metadata only. Pure date selection."""
from pipeline.publishing.cleanup import purge_dates


def test_only_confirmed_uploads_and_old_unpublished_weeklies():
    up = {"2026-10-26": "a", "2026-10-27": "b", "weekly_2026-10-24": None}.get
    dates = ["2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28",
             "weekly_2026-10-17", "weekly_2026-10-24", "_stream", "__captest__"]
    got = purge_dates(dates, "2026-10-27", up)
    assert got == ["2026-10-26", "2026-10-27", "weekly_2026-10-17"]
    # 10-25 never uploaded -> kept; 10-28 is after tonight -> kept; recent weekly -> kept
