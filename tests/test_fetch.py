"""Fetch de-duplication: Helix pages can repeat a clip, which used to shrink the day."""
from pipeline.ingestion.fetch import dedup_by_views


def test_dedup_keeps_one_copy_most_viewed_first():
    clips = [{"id": "a", "view_count": 5}, {"id": "b", "view_count": 9},
             {"id": "a", "view_count": 5}, {"id": "c", "view_count": 7}]
    assert [c["id"] for c in dedup_by_views(clips)] == ["b", "c", "a"]


def test_slack_then_trim_gives_exactly_the_requested_count():
    # 225 fetched with 3 repeats -> 222 unique; trimming to fetch_count keeps the top 222
    clips = [{"id": str(i), "view_count": 1000 - i} for i in range(222)]
    clips += [dict(clips[0]), dict(clips[5]), dict(clips[9])]
    out = dedup_by_views(clips)[:222]
    assert len(out) == 222 and out[0]["id"] == "0"
