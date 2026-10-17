"""Pure selection of publishing.weekly (no network, ffmpeg or models)."""
import pytest

from pipeline.publishing.weekly import (
    description_head,
    is_last_day,
    label_for,
    select,
    theme_filter,
    title_for,
    to_clips,
    week_dates,
)

WEEK = week_dates("2026-10-17")
LEN = lambda r: (r.get("filter") or {}).get("duration", 30)      # noqa: E731


def row(cid, who, s, date="2026-10-14", vod=None, off=None, dur=30, text="", ent=5, pq=5,
        vid="yt1"):
    return {"clip_id": cid, "date": date, "broadcaster": who, "twitch_title": text,
            "clip_url": f"https://clips.twitch.tv/{cid}", "video_youtube_id": vid,
            "filter": {"id": cid, "api_rank_score": s, "api_entertainment": ent,
                       "api_play_quality": pq, "duration": dur, "video_id": vod,
                       "vod_offset": off, "broadcaster_name": who, "title": text,
                       "local_path": "C:/x.mp4", "countdown_rank": 3}}


def test_week_is_the_seven_dates_ending_the_last_day():
    assert WEEK == ["2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15",
                    "2026-10-16", "2026-10-17"]


def test_the_stage_fires_only_the_night_before_the_weekday():
    assert is_last_day("2026-10-17", "sunday")          # Saturday's daily -> Sunday upload
    assert not is_last_day("2026-10-18", "sunday")
    assert is_last_day("2026-10-18", "Monday")
    assert not is_last_day("weekly_2026-10-17", "sunday")


def test_best_first_inside_the_week_and_countdown_order():
    rows = [row("a", "one", 9.0), row("b", "two", 7.0), row("c", "three", 8.0),
            row("old", "four", 10.0, date="2026-10-10"), row("low", "five", 5.0)]
    got = select(rows, WEEK, set(), length=LEN, target_s=1000, max_s=2000)
    assert [r["clip_id"] for r in got] == ["b", "c", "a"]     # weakest first, #1 last


def test_tie_break_is_entertainment_plus_play_quality():
    rows = [row("a", "one", 7.0, ent=5, pq=5), row("b", "two", 7.0, ent=8, pq=7)]
    got = select(rows, WEEK, set(), length=LEN, target_s=1000, max_s=2000)
    assert [r["clip_id"] for r in got] == ["a", "b"]


def test_used_ids_moments_and_repeats_are_skipped():
    rows = [row("a", "one", 9.0, vod="v1", off=1000),
            row("b", "two", 8.5, vod="v1", off=1050),            # same moment as a
            row("a", "one", 9.0, date="2026-10-15", vod="v1", off=1000),   # a, two dailies
            row("c", "three", 8.0, vod="v1", off=2000),
            row("d", "four", 7.5)]
    got = select(rows, WEEK, {"d"}, length=LEN, target_s=1000, max_s=2000)
    assert sorted(r["clip_id"] for r in got) == ["a", "c"]


def test_per_streamer_cap():
    rows = [row(c, "baus", 9.0 - i * 0.1) for i, c in enumerate("abcd")] + [row("e", "x", 6)]
    got = select(rows, WEEK, set(), length=LEN, max_per_streamer=2, target_s=1000,
                 max_s=2000)
    assert sorted(r["clip_id"] for r in got) == ["a", "b", "e"]


def test_fills_to_target_and_never_past_max():
    rows = [row("a", "1", 9, dur=200), row("b", "2", 8.9, dur=200), row("long", "3", 8.8,
            dur=300), row("c", "4", 8.7, dur=100), row("d", "5", 8.6, dur=100)]
    got = select(rows, WEEK, set(), length=LEN, target_s=500, max_s=550)
    # a+b = 400; long would make 700 > 550 -> passed over; c reaches 500 = target, stop
    assert sorted(r["clip_id"] for r in got) == ["a", "b", "c"]


def test_theme_categories_streamers_and_champions():
    cats = [{"key": "pentakills", "label": "Pentakills", "pattern": "penta"}]
    assert theme_filter("best", cats) is None
    label, pred = theme_filter("pentakills", cats)
    assert label == "Pentakills"
    assert pred(row("a", "x", 9, text="PENTAKILL")) and not pred(row("b", "x", 9))
    label, pred = theme_filter("streamer:TheBausffs", cats)
    assert pred(row("a", "thebausffs", 9)) and not pred(row("b", "x", 9))
    label, pred = theme_filter("champion:yasuo", cats)
    assert label == "Yasuo Plays" and pred(row("a", "x", 9, text="insane Yasuo 1v3"))
    with pytest.raises(ValueError):
        theme_filter("nope", cats)


def test_to_clips_ranks_n_to_1_and_drops_machine_paths():
    rows = [row("b", "two", 7.0, date="2026-10-12"), row("a", "one", 9.0, vid="yt9")]
    clips = to_clips(rows)
    assert [c["countdown_rank"] for c in clips] == [2, 1]
    assert clips[1]["weekly_source_video"] == "yt9"
    assert clips[0]["weekly_source_date"] == "2026-10-12"
    assert "local_path" not in clips[0] and clips[0]["url"]


def test_title_label_and_description():
    assert label_for("2026-10-17") == "weekly_2026-10-17"
    assert title_for("Best League Plays of the Week #{episode}", 3, 12) == \
        "Best League Plays of the Week #3"
    assert title_for("Best {Label} of the Week #{episode}", 1, 8, "pentakills") == \
        "Best Pentakills of the Week #1"
    head = description_head(12, "", ["abc"])
    assert "Top 12" in head[0] and "https://youtu.be/abc" in head
