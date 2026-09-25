"""Pure selection of publishing.ranking_shorts (no network, ffmpeg or models)."""
from pipeline.publishing.ranking_shorts import (
    description_for,
    eligible,
    pick_category,
    title_for,
    window,
)

PENTA = {"key": "pentakills", "label": "Pentakills", "pattern": "penta"}
YASUO = {"key": "yasuo", "label": "Yasuo Plays", "pattern": r"\byasuo\b"}


def row(cid, who, text, s):
    return {"clip_id": cid, "broadcaster": who, "twitch_title": text,
            "clip_url": f"https://clips.twitch.tv/{cid}", "broadcaster_url": f"https://twitch.tv/{who}",
            "filter": {"api_rank_score": s, "api_what_happens": ""}}


ROWS = [row("a", "one", "PENTA!!", 9.0), row("b", "one", "penta again", 8.5),
        row("c", "one", "third penta", 8.0), row("d", "two", "Yasuo outplay", 7.5),
        row("e", "three", "almost a pentakill", 5.0), row("f", "four", "penta", 7.0)]


def test_matches_text_best_first_and_caps_per_streamer():
    got = [r["clip_id"] for r in eligible(ROWS, PENTA, set(), 6.0, max_per_streamer=2)]
    assert got == ["a", "b", "f"]                    # c: 3rd clip of "one"; e: score 5


def test_used_clips_are_never_ranked_twice():
    got = [r["clip_id"] for r in eligible(ROWS, PENTA, {"a"}, 6.0)]
    assert "a" not in got


def test_judge_text_counts_not_only_the_title():
    r = row("g", "five", "gg", 8.0)
    r["filter"]["api_what_happens"] = "The player on Yasuo dodges everything"
    assert [x["clip_id"] for x in eligible([r], YASUO, set())] == ["g"]


def test_category_of_the_day_rotates_and_needs_enough_clips():
    cats = [PENTA, YASUO]
    assert pick_category(cats, ROWS, set(), [], n=3, min_score=6.0)["key"] == "pentakills"
    hist = [{"category": "pentakills"}]
    # yasuo was never used but has only 1 clip -> pentakills again
    assert pick_category(cats, ROWS, set(), hist, n=3, min_score=6.0)["key"] == "pentakills"
    assert pick_category(cats, ROWS, set(), hist, n=1, min_score=6.0)["key"] == "yasuo"
    assert pick_category(cats, ROWS, {"a", "b", "f"}, [], n=3, min_score=6.0) is None


def test_window_leads_into_the_best_moment_and_stays_inside_the_clip():
    assert window(30, 18, 10, 6) == (12, 10)
    assert window(30, 2, 10, 6) == (0, 10)
    assert window(30, 29, 10, 6) == (20, 10)
    assert window(8, None, 10, 6) == (0, 8)


def test_title_and_credits():
    assert title_for(PENTA, 5, "TOP {n} {LABEL} | League of Legends #Shorts") == \
        "TOP 5 PENTAKILLS | League of Legends #Shorts"
    d = description_for(PENTA, [ROWS[1], ROWS[0]], 2)
    assert d.index("#2 one") < d.index("#1 one") and "twitch.tv/one" in d
